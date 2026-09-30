import os
import re
import sys
import json
import urllib3
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup, Tag
from urllib.parse import urljoin
from dotenv import load_dotenv

load_dotenv()

BASE_URL    = os.getenv("BASE_URL", "").rstrip("/")
ADMIN_PATH  = os.getenv("ADMIN_PATH", "/admin")
LOGIN_PATH  = os.getenv("LOGIN_PATH", "/admin/main/login")
LOGOUT_PATH = os.getenv("LOGOUT_PATH", "/admin/logout")
ORG_PATH    = os.getenv("ORG_PATH", "/admin/organization")
USERNAME    = os.getenv("SITE_USER", "")
PASSWORD    = os.getenv("SITE_PASS", "")
TIMEOUT     = int(os.getenv("TIMEOUT", "15"))
VERIFY_TLS  = os.getenv("VERIFY_TLS", "True").lower() in ("1", "true", "yes", "y")
MAX_PAGES   = int(os.getenv("MAX_PAGES", "30"))

if not BASE_URL or not USERNAME or not PASSWORD:
    print(json.dumps({"error": "Проверь .env: BASE_URL, SITE_USER, SITE_PASS"},
                     ensure_ascii=False, indent=2))
    sys.exit(1)

if not VERIFY_TLS:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

admin_url  = urljoin(BASE_URL + "/", ADMIN_PATH.lstrip("/"))
login_url  = urljoin(BASE_URL + "/", LOGIN_PATH.lstrip("/"))
logout_url = urljoin(BASE_URL + "/", LOGOUT_PATH.lstrip("/"))
org_url    = urljoin(BASE_URL + "/", ORG_PATH.lstrip("/"))

report: dict = {
    "target": BASE_URL,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "login": None,
    "organizations": {"count": 0, "items": []},
    "logout": None,
    "finished_at": None,
}

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
})


# ---------- утилиты ----------
def attr_str(tag: Tag | None, name: str) -> str:
    if not isinstance(tag, Tag):
        return ""
    v = tag.get(name)
    if isinstance(v, list):
        v = v[0] if v else ""
    return str(v) if v is not None else ""


def page_title(html: str) -> str:
    tag = BeautifulSoup(html, "html.parser").title
    return tag.get_text(strip=True) if tag is not None else ""


def extract_csrf(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    inp = soup.find("input", {"name": "_csrf"})
    if isinstance(inp, Tag):
        v = attr_str(inp, "value")
        if v:
            return v
    meta = soup.find("meta", {"name": "csrf-token"})
    if isinstance(meta, Tag):
        v = attr_str(meta, "content")
        if v:
            return v
    return None


# ---------- логин ----------
def do_login() -> tuple[bool, str]:
    try:
        r = session.get(admin_url, timeout=TIMEOUT,
                        verify=VERIFY_TLS, allow_redirects=True)
    except requests.RequestException as e:
        return False, f"GET admin: {e}"

    csrf = extract_csrf(r.text)
    if not csrf:
        return False, "CSRF не найден на странице логина"

    try:
        resp = session.post(login_url, data={
            "_csrf": csrf,
            "LoginForm[username]": USERNAME,
            "LoginForm[password]": PASSWORD,
            "LoginForm[rememberMe]": "0",
            "login-button": "",
        }, timeout=TIMEOUT, verify=VERIFY_TLS, allow_redirects=True)
    except requests.RequestException as e:
        return False, f"POST login: {e}"

    login_form_still = ("login-form" in resp.text) or ("loginform-password" in resp.text)
    ok = (not login_form_still) and resp.url.rstrip("/") != login_url.rstrip("/")
    if ok:
        soup = BeautifulSoup(resp.text, "html.parser")
        btn = soup.find("button", {"id": "user-nav-btn"})
        name = btn.get_text(strip=True).replace("\xa0", " ").strip() \
               if isinstance(btn, Tag) else ""
        report["login"] = {"status": "PASS", "username": name,
                           "title": page_title(resp.text), "final_url": resp.url}
        return True, name
    report["login"] = {"status": "FAIL", "title": page_title(resp.text),
                       "final_url": resp.url}
    return False, "вход не выполнен"


# ---------- логаут ----------
def do_logout() -> bool:
    try:
        r = session.get(admin_url, timeout=TIMEOUT,
                        verify=VERIFY_TLS, allow_redirects=True)
    except requests.RequestException as e:
        report["logout"] = {"status": "FAIL", "error": str(e)}
        return False

    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form", attrs={"action": re.compile("logout", re.I)})
    if isinstance(form, Tag):
        action = attr_str(form, "action") or LOGOUT_PATH
        tok = form.find("input", {"name": "_csrf"})
        data = {"_csrf": attr_str(tok, "value")} if isinstance(tok, Tag) else {}
        url = urljoin(BASE_URL, action)
        try:
            rr = session.post(url, data=data, timeout=TIMEOUT,
                              verify=VERIFY_TLS, allow_redirects=True)
            report["logout"] = {"status": "PASS",
                                "detail": f"POST {url} → {rr.status_code} {rr.url}"}
            return True
        except requests.RequestException as e:
            report["logout"] = {"status": "FAIL", "error": str(e)}
            return False
    report["logout"] = {"status": "FAIL", "error": "форма логаута не найдена"}
    return False


# ---------- парсинг одной организации ----------
def parse_org(item: Tag) -> dict:
    out: dict = {}

    # id из ссылки update
    upd = item.find("a", href=re.compile(r"/organization/update\?id=\d+"))
    if isinstance(upd, Tag):
        m = re.search(r"[?&]id=(\d+)", attr_str(upd, "href"))
        if m:
            out["id"] = int(m.group(1))

    group = item.find("div", class_="btn-group")
    if not isinstance(group, Tag):
        return out

    for child in group.find_all(recursive=False):
        if not isinstance(child, Tag) or child.name != "div":
            continue
        classes = child.get("class") or []
        if "col" not in classes:
            continue
        text = child.get_text(" ", strip=True)
        low = text.lower()
        is_center = "text-center" in classes
        strong = child.find("strong")
        small = child.find("small")

        # Название: div.col.align-self-center с <strong>, без <small>
        if not is_center and isinstance(strong, Tag) and not isinstance(small, Tag):
            if "name" not in out:
                out["name"] = strong.get_text(strip=True)
            continue

        # Район: div.col.align-self-center с <small>, текст начинается с "Район"
        if not is_center and isinstance(small, Tag) and low.startswith("район"):
            if isinstance(strong, Tag):
                out["territory"] = strong.get_text(strip=True)
            continue

        # id (пропускаем, уже взяли из ссылки)
        if is_center and isinstance(small, Tag) and low.startswith("id"):
            continue

        # Просмотры
        if is_center and "просмотры" in low:
            m = re.search(r"(\d+)", text)
            if m:
                out["views"] = int(m.group(1))
            continue

        # Создан
        if is_center and isinstance(small, Tag) and low.startswith("создан"):
            if isinstance(strong, Tag):
                out["created_at"] = strong.get_text(strip=True)
            continue

    return out


def parse_orgs(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    for item in soup.find_all("div", class_="item-organization"):
        if isinstance(item, Tag):
            parsed = parse_org(item)
            if parsed:
                items.append(parsed)
    return items


def max_page_from_pagination(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    mx = 1
    for a in soup.find_all("a", class_="page-link"):
        m = re.search(r"[?&]page=(\d+)", attr_str(a, "href"))
        if m:
            mx = max(mx, int(m.group(1)))
    return mx


# ---------- сбор всех организаций ----------
def collect_organizations() -> list[dict]:
    # получаем страницу, чтобы взять свежий CSRF
    r0 = session.get(org_url, timeout=TIMEOUT, verify=VERIFY_TLS,
                     allow_redirects=True)
    csrf = extract_csrf(r0.text)

    # просим все сразу
    if csrf:
        session.post(org_url, data={"_csrf": csrf, "pageSize": "all"},
                     timeout=TIMEOUT, verify=VERIFY_TLS, allow_redirects=True)

    # финальный GET страницы
    r = session.get(org_url, timeout=TIMEOUT, verify=VERIFY_TLS,
                    allow_redirects=True)
    items = parse_orgs(r.text)
    pages = max_page_from_pagination(r.text)

    # если pageSize=all не сработал — идём по страницам
    if pages > 1 and len(items) <= 20:
        seen_ids = {it.get("id") for it in items if it.get("id")}
        for p in range(2, min(pages, MAX_PAGES) + 1):
            rp = session.get(f"{org_url}?page={p}", timeout=TIMEOUT,
                             verify=VERIFY_TLS, allow_redirects=True)
            for it in parse_orgs(rp.text):
                if it.get("id") not in seen_ids:
                    items.append(it)
                    if it.get("id"):
                        seen_ids.add(it["id"])

    return items


# ---------- main ----------
ok, who = do_login()
if not ok:
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(2)

try:
    orgs = collect_organizations()
    report["organizations"] = {"count": len(orgs), "items": orgs}
except requests.RequestException as e:
    report["organizations"] = {"count": 0, "items": [], "error": str(e)}

do_logout()
session.cookies.clear()
report["finished_at"] = datetime.now(timezone.utc).isoformat()

OUT_FILE = os.getenv("OUT_FILE", "organizations.json")

with open(OUT_FILE, "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)

print(f"Записано: {OUT_FILE} ({report['organizations']['count']} организаций)")