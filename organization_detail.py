import argparse
import json
import os
import re
import sys
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

if not BASE_URL or not USERNAME or not PASSWORD:
    print("ERROR: .env must define BASE_URL, SITE_USER, SITE_PASS", file=sys.stderr)
    sys.exit(1)

if not VERIFY_TLS:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

admin_url  = urljoin(BASE_URL + "/", ADMIN_PATH.lstrip("/"))
login_url  = urljoin(BASE_URL + "/", LOGIN_PATH.lstrip("/"))
logout_url = urljoin(BASE_URL + "/", LOGOUT_PATH.lstrip("/"))
org_url    = urljoin(BASE_URL + "/", ORG_PATH.lstrip("/"))


# ---------- helpers ----------
def attr_str(tag: Tag | None, name: str) -> str:
    if not isinstance(tag, Tag):
        return ""
    v = tag.get(name)
    if isinstance(v, list):
        v = v[0] if v else ""
    return str(v) if v is not None else ""


def get_classes(tag: Tag) -> list[str]:
    c = tag.get("class") or []
    return c if isinstance(c, list) else [c]


def set_nested(d: dict, name: str, value) -> None:
    """Organization[map][latitude]=X -> d["Organization"]["map"]["latitude"]=X"""
    m = re.match(r"^([^\[]+)((?:\[[^\]]+\])*)$", name)
    if not m:
        d[name] = value
        return
    keys = [m.group(1)] + re.findall(r"\[([^\]]+)\]", m.group(2))
    cur = d
    for k in keys[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    cur[keys[-1]] = value


def extract_csrf(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    inp = soup.find("input", {"name": "_csrf"})
    if isinstance(inp, Tag):
        v = attr_str(inp, "value")
        if v:
            return v
    meta = soup.find("meta", {"name": "csrf-token"})
    if isinstance(meta, Tag):
        return attr_str(meta, "content") or None
    return None


def page_title(html: str) -> str:
    tag = BeautifulSoup(html, "html.parser").title
    return tag.get_text(strip=True) if tag is not None else ""


# ---------- login / logout ----------
def do_login(session: requests.Session) -> tuple[bool, str]:
    try:
        r = session.get(admin_url, timeout=TIMEOUT, verify=VERIFY_TLS, allow_redirects=True)
    except requests.RequestException as e:
        return False, f"GET admin failed: {e}"

    csrf = extract_csrf(r.text)
    if not csrf:
        return False, "CSRF not found"

    try:
        resp = session.post(login_url, data={
            "_csrf": csrf,
            "LoginForm[username]": USERNAME,
            "LoginForm[password]": PASSWORD,
            "LoginForm[rememberMe]": "0",
            "login-button": "",
        }, timeout=TIMEOUT, verify=VERIFY_TLS, allow_redirects=True)
    except requests.RequestException as e:
        return False, f"POST login failed: {e}"

    if "login-form" in resp.text:
        return False, "login form still present"

    soup = BeautifulSoup(resp.text, "html.parser")
    btn = soup.find("button", {"id": "user-nav-btn"})
    name = btn.get_text(strip=True).replace("\xa0", " ").strip() if isinstance(btn, Tag) else ""
    return True, name


def do_logout(session: requests.Session) -> bool:
    try:
        r = session.get(admin_url, timeout=TIMEOUT, verify=VERIFY_TLS, allow_redirects=True)
    except requests.RequestException:
        return False
    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form", attrs={"action": re.compile("logout", re.I)})
    if not isinstance(form, Tag):
        return False
    tok = form.find("input", {"name": "_csrf"})
    data = {"_csrf": attr_str(tok, "value")} if isinstance(tok, Tag) else {}
    try:
        session.post(logout_url, data=data, timeout=TIMEOUT, verify=VERIFY_TLS, allow_redirects=True)
        return True
    except requests.RequestException:
        return False


# ---------- parsers ----------
def parse_form(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", {"id": "organization-form"})
    if not isinstance(form, Tag):
        return {}

    out: dict = {}

    # --- inputs ---
    for inp in form.find_all("input"):
        if not isinstance(inp, Tag):
            continue
        name = attr_str(inp, "name")
        if not name.startswith("Organization["):
            continue
        if name.endswith("[]"):
            continue  # file-array sentinels
        itype = (attr_str(inp, "type") or "text").lower()
        if itype == "file":
            continue

        if itype == "radio":
            if inp.get("checked") is not None:
                value = attr_str(inp, "value")
                label = ""
                nxt = inp.find_next_sibling("label")
                if isinstance(nxt, Tag):
                    label = nxt.get_text(strip=True)
                set_nested(out, name, {"value": value, "label": label} if label else value)
            continue

        if itype == "checkbox":
            if inp.get("checked") is not None:
                set_nested(out, name, attr_str(inp, "value"))
            continue

        # text / hidden / email / number / url ...
        set_nested(out, name, attr_str(inp, "value"))

    # --- selects ---
    for sel in form.find_all("select"):
        if not isinstance(sel, Tag):
            continue
        name = attr_str(sel, "name")
        if not name.startswith("Organization["):
            continue
        for opt in sel.find_all("option"):
            if not isinstance(opt, Tag):
                continue
            if opt.get("selected") is not None:
                set_nested(out, name, {
                    "value": attr_str(opt, "value"),
                    "label": opt.get_text(strip=True),
                })
                break

    # --- textareas ---
    for ta in form.find_all("textarea"):
        if not isinstance(ta, Tag):
            continue
        name = attr_str(ta, "name")
        if not name.startswith("Organization["):
            continue
        set_nested(out, name, ta.get_text())

    return out


def parse_meta(soup: BeautifulSoup) -> dict:
    meta: dict = {}
    for ul in soup.find_all("ul", class_="list-group"):
        if not isinstance(ul, Tag):
            continue
        parent = ul.parent
        if isinstance(parent, Tag) and "dropdown-menu" in get_classes(parent):
            continue
        for li in ul.find_all("li", class_="list-group-item"):
            if not isinstance(li, Tag):
                continue
            txt = li.get_text(" ", strip=True)
            if ":" not in txt:
                continue
            k, _, v = txt.partition(":")
            k = k.strip().lower()
            v = v.strip()
            if k == "создатель":
                meta["creator"] = v
            elif k == "дата создания":
                meta["created_at"] = v
            elif k == "дата обновления":
                meta["updated_at"] = v
            elif k == "район":
                meta["territory"] = v
    return meta


def parse_files(soup: BeautifulSoup) -> list[dict]:
    files: list[dict] = []
    for wrapper in soup.find_all("div", id=re.compile(r"^fileblock-wrapper-gallery-\d+$")):
        if not isinstance(wrapper, Tag):
            continue
        for card in wrapper.find_all("div", class_="card", recursive=False):
            if not isinstance(card, Tag):
                continue

            info: dict = {}
            m = re.match(r"file-(\d+)-(\d+)$", attr_str(card, "id"))
            if m:
                info["id"] = int(m.group(1))

            img = card.find("img")
            if isinstance(img, Tag):
                info["url"] = attr_str(img, "src")

            for li in card.find_all("li", class_="list-group-item"):
                if not isinstance(li, Tag):
                    continue
                txt = li.get_text(" ", strip=True)
                if ":" in txt:
                    k, _, v = txt.partition(":")
                    k, v = k.strip().lower(), v.strip()
                    if k == "имя на сервере":
                        info["server_name"] = v
                    elif k == "размер":
                        info["size"] = v
                    elif k == "дата загрузки":
                        info["uploaded_at"] = v
                    elif k == "путь":
                        info["path"] = v
                else:
                    strong = li.find("strong")
                    if isinstance(strong, Tag):
                        info["name"] = strong.get_text(strip=True)

            files.append(info)
    return files


# ---------- main ----------
def main() -> int:
    ap = argparse.ArgumentParser(description="Выгрузка карточки организации из админки Yii2")
    ap.add_argument("org_id", type=int, help="ID организации")
    ap.add_argument("-o", "--output", default=None,
                    help="Файл JSON (по умолчанию org_<id>.json)")
    args = ap.parse_args()

    out_file = args.output or f"org_{args.org_id}.json"

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    })

    report: dict = {
        "target": BASE_URL,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "org_id": args.org_id,
        "login": None,
        "organization": None,
        "logout": None,
        "finished_at": None,
    }

    ok, who = do_login(session)
    report["login"] = {"status": "PASS" if ok else "FAIL", "username": who}
    if not ok:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"ERROR: login failed -> {out_file}")
        return 2

    detail_url = f"{org_url}/update?id={args.org_id}&_return=1"
    try:
        r = session.get(detail_url, timeout=TIMEOUT, verify=VERIFY_TLS, allow_redirects=True)
    except requests.RequestException as e:
        report["organization"] = {"error": str(e)}
    else:
        soup = BeautifulSoup(r.text, "html.parser")
        report["organization"] = {
            "id": args.org_id,
            "url": detail_url,
            "http_status": r.status_code,
            "title": page_title(r.text),
            "form": parse_form(r.text),
            "meta": parse_meta(soup),
            "files": parse_files(soup),
        }

    report["logout"] = {"status": "PASS" if do_logout(session) else "FAIL"}
    session.cookies.clear()
    report["finished_at"] = datetime.now(timezone.utc).isoformat()

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"OK: {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())