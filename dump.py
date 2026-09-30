"""
Сводная выгрузка административной панели МИАЦ:
  1. Категории вакансий
  2. Организации
  3. Вакансии

Результат — единый dump.json (UTF-8), сохраняется инкрементально.
"""

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

# ======================== КОНФИГ ========================
BASE_URL    = os.getenv("BASE_URL", "").rstrip("/")
ADMIN_PATH  = os.getenv("ADMIN_PATH", "/admin")
LOGIN_PATH  = os.getenv("LOGIN_PATH", "/admin/main/login")
LOGOUT_PATH = os.getenv("LOGOUT_PATH", "/admin/logout")

ORG_PATH    = os.getenv("ORG_PATH", "/admin/organization")
VAC_PATH    = os.getenv("VAC_PATH", "/admin/vacancy")
CAT_PATH    = os.getenv("CAT_PATH", "/admin/vacancy/category/index")

SITE_USER   = os.getenv("SITE_USER", "")
SITE_PASS   = os.getenv("SITE_PASS", "")

TIMEOUT     = int(os.getenv("TIMEOUT", "15"))
VERIFY_TLS  = os.getenv("VERIFY_TLS", "True").lower() in ("1", "true", "yes", "y")

MAX_ORG_PAGES = int(os.getenv("ORG_MAX_PAGES", "50"))
MAX_VAC_PAGES = int(os.getenv("VAC_MAX_PAGES", "300"))
MAX_CAT_PAGES = int(os.getenv("CAT_MAX_PAGES", "50"))

OUT_FILE    = os.getenv("OUT_FILE", "dump.json")

# ========================================================

if not BASE_URL or not SITE_USER or not SITE_PASS:
    print("ERROR: .env must define BASE_URL, SITE_USER, SITE_PASS", file=sys.stderr)
    sys.exit(1)

if not VERIFY_TLS:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

admin_url  = urljoin(BASE_URL + "/", ADMIN_PATH.lstrip("/"))
login_url  = urljoin(BASE_URL + "/", LOGIN_PATH.lstrip("/"))
logout_url = urljoin(BASE_URL + "/", LOGOUT_PATH.lstrip("/"))
org_url    = urljoin(BASE_URL + "/", ORG_PATH.lstrip("/"))
vac_url    = urljoin(BASE_URL + "/", VAC_PATH.lstrip("/"))
cat_url    = urljoin(BASE_URL + "/", CAT_PATH.lstrip("/"))


# ==================== ОБЩИЕ ХЕЛПЕРЫ ====================
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
    m = re.match(r"^([^\[]+)((?:\[[^\]]+\])*)$", name)
    if m is None:
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
    t = BeautifulSoup(html, "html.parser").title
    return t.get_text(strip=True) if t is not None else ""


def max_page_from_pagination(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    mx = 1
    for a in soup.find_all("a", class_="page-link"):
        m = re.search(r"[?&]page=(\d+)", attr_str(a, "href"))
        if m is not None:
            mx = max(mx, int(m.group(1)))
    return mx


def save_report(report: dict, path: str = OUT_FILE) -> None:
    """Атомарная запись UTF-8 JSON через temp + rename."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


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
            k, v = k.strip().lower(), v.strip()
            if k == "создатель":
                meta["creator"] = v
            elif k == "дата создания":
                meta["created_at"] = v
            elif k == "дата обновления":
                meta["updated_at"] = v
            elif k == "район":
                meta["territory"] = v
    return meta


def parse_form(html: str, form_id: str, prefix: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", {"id": form_id})
    if not isinstance(form, Tag):
        return {}

    out: dict = {}

    for inp in form.find_all("input"):
        if not isinstance(inp, Tag):
            continue
        name = attr_str(inp, "name")
        if not name.startswith(prefix) or name.endswith("[]"):
            continue
        itype = (attr_str(inp, "type") or "text").lower()
        if itype == "file":
            continue
        if itype == "radio":
            if inp.get("checked") is not None:
                value = attr_str(inp, "value")
                nxt = inp.find_next_sibling("label")
                label = nxt.get_text(strip=True) if isinstance(nxt, Tag) else ""
                set_nested(out, name, {"value": value, "label": label} if label else value)
            continue
        if itype == "checkbox":
            if inp.get("checked") is not None:
                set_nested(out, name, attr_str(inp, "value"))
            continue
        set_nested(out, name, attr_str(inp, "value"))

    for sel in form.find_all("select"):
        if not isinstance(sel, Tag):
            continue
        name = attr_str(sel, "name")
        if not name.startswith(prefix):
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

    for ta in form.find_all("textarea"):
        if not isinstance(ta, Tag):
            continue
        name = attr_str(ta, "name")
        if not name.startswith(prefix):
            continue
        set_nested(out, name, ta.get_text())

    return out


def fetch_list(
    session: requests.Session,
    url: str,
    item_class: str,
    row_parser,
    max_pages: int,
) -> list[dict]:
    """Универсальный обход списочной страницы."""
    r0 = session.get(url, timeout=TIMEOUT, verify=VERIFY_TLS, allow_redirects=True)
    csrf = extract_csrf(r0.text)
    if csrf:
        try:
            session.post(url, data={"_csrf": csrf, "pageSize": "all"},
                         timeout=TIMEOUT, verify=VERIFY_TLS, allow_redirects=True)
        except requests.RequestException:
            pass

    r = session.get(url, timeout=TIMEOUT, verify=VERIFY_TLS, allow_redirects=True)
    soup = BeautifulSoup(r.text, "html.parser")
    rows: list[dict] = []
    for item in soup.find_all("div", class_=item_class):
        if isinstance(item, Tag):
            parsed = row_parser(item)
            if parsed:
                rows.append(parsed)

    pages = max_page_from_pagination(r.text)
    if pages > 1 and len(rows) <= 20:
        seen = {x.get("id") for x in rows if x.get("id")}
        for p in range(2, min(pages, max_pages) + 1):
            try:
                rp = session.get(f"{url}?page={p}", timeout=TIMEOUT,
                                 verify=VERIFY_TLS, allow_redirects=True)
            except requests.RequestException:
                continue
            sp = BeautifulSoup(rp.text, "html.parser")
            for item in sp.find_all("div", class_=item_class):
                if isinstance(item, Tag):
                    parsed = row_parser(item)
                    if parsed and parsed.get("id") not in seen:
                        rows.append(parsed)
                        if parsed.get("id"):
                            seen.add(parsed["id"])

    return rows


# ==================== AUTH ====================
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
            "LoginForm[username]": SITE_USER,
            "LoginForm[password]": SITE_PASS,
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
        session.post(logout_url, data=data, timeout=TIMEOUT,
                     verify=VERIFY_TLS, allow_redirects=True)
        return True
    except requests.RequestException:
        return False


# ==================== КАТЕГОРИИ ВАКАНСИЙ ====================
def parse_category_row(item: Tag) -> dict:
    out: dict = {}

    upd = item.find("a", href=re.compile(r"/vacancy/category/update\?id=\d+"))
    if isinstance(upd, Tag):
        m = re.search(r"[?&]id=(\d+)", attr_str(upd, "href"))
        if m is not None:
            out["id"] = int(m.group(1))

    card = item.find("div", class_="card")
    if not isinstance(card, Tag):
        return out

    for child in card.find_all(recursive=False):
        if not isinstance(child, Tag) or child.name != "div":
            continue
        classes = get_classes(child)
        if "col" not in classes:
            continue
        text = child.get_text(" ", strip=True)
        low = text.lower()
        is_center = "text-center" in classes
        strong = child.find("strong")
        small = child.find("small")

        if not is_center and isinstance(strong, Tag) and not isinstance(small, Tag):
            if "name" not in out:
                out["name"] = strong.get_text(strip=True)
            continue

        if is_center and isinstance(small, Tag) and low.startswith("id"):
            continue

        if is_center and isinstance(small, Tag) and low.startswith("дата создания"):
            if isinstance(strong, Tag):
                out["created_at"] = strong.get_text(strip=True)
            continue

    return out


def fetch_category_detail(session: requests.Session, cat_id: int) -> dict:
    url = f"{cat_url.replace('/index', '')}/update?id={cat_id}&_return=1"
    try:
        r = session.get(url, timeout=TIMEOUT, verify=VERIFY_TLS, allow_redirects=True)
    except requests.RequestException as e:
        return {"error": str(e)}

    soup = BeautifulSoup(r.text, "html.parser")
    return {
        "url": url,
        "http_status": r.status_code,
        "title": page_title(r.text),
        "form": parse_form(r.text, "vacancy-form", "VacancyCategory["),
        "meta": parse_meta(soup),
    }


def dump_categories(
    session: requests.Session,
    report: dict,
    save_cb,
    save_every: int = 10,
) -> list[dict]:
    print("[*] Категории вакансий...", file=sys.stderr)
    rows = fetch_list(session, cat_url, "item-user", parse_category_row, MAX_CAT_PAGES)
    total = len(rows)
    print(f"    найдено: {total}, тянем детали...", file=sys.stderr)

    report["categories"] = {"count": total, "items": rows}
    save_cb()

    for i, row in enumerate(rows, 1):
        cat_id = row.get("id")
        if isinstance(cat_id, int):
            row["detail"] = fetch_category_detail(session, cat_id)
        if i % save_every == 0 or i == total:
            print(f"    [{i}/{total}]", file=sys.stderr)
            save_cb()

    return rows


# ==================== ОРГАНИЗАЦИИ ====================
def parse_org_row(item: Tag) -> dict:
    out: dict = {}

    upd = item.find("a", href=re.compile(r"/organization/update\?id=\d+"))
    if isinstance(upd, Tag):
        m = re.search(r"[?&]id=(\d+)", attr_str(upd, "href"))
        if m is not None:
            out["id"] = int(m.group(1))

    group = item.find("div", class_="btn-group")
    if not isinstance(group, Tag):
        return out

    for child in group.find_all(recursive=False):
        if not isinstance(child, Tag) or child.name != "div":
            continue
        classes = get_classes(child)
        text = child.get_text(" ", strip=True)
        low = text.lower()
        is_center = "text-center" in classes
        strong = child.find("strong")
        small = child.find("small")

        if not is_center and isinstance(strong, Tag) and not isinstance(small, Tag):
            if "name" not in out:
                out["name"] = strong.get_text(strip=True)
            continue

        if not is_center and isinstance(small, Tag) and low.startswith("район"):
            if isinstance(strong, Tag):
                out["territory"] = strong.get_text(strip=True)
            continue

        if is_center and isinstance(small, Tag) and low.startswith("id"):
            continue

        if is_center and "просмотры" in low:
            m = re.search(r"(\d+)", text)
            if m is not None:
                out["views"] = int(m.group(1))
            continue

        if is_center and isinstance(small, Tag) and low.startswith("создан"):
            if isinstance(strong, Tag):
                out["created_at"] = strong.get_text(strip=True)
            continue

    return out


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
            if m is not None:
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


def fetch_org_detail(session: requests.Session, org_id: int) -> dict:
    url = f"{org_url}/update?id={org_id}&_return=1"
    try:
        r = session.get(url, timeout=TIMEOUT, verify=VERIFY_TLS, allow_redirects=True)
    except requests.RequestException as e:
        return {"error": str(e)}

    soup = BeautifulSoup(r.text, "html.parser")
    return {
        "url": url,
        "http_status": r.status_code,
        "title": page_title(r.text),
        "form": parse_form(r.text, "organization-form", "Organization["),
        "meta": parse_meta(soup),
        "files": parse_files(soup),
    }


def dump_organizations(
    session: requests.Session,
    report: dict,
    save_cb,
    save_every: int = 25,
) -> list[dict]:
    print("[*] Организации...", file=sys.stderr)
    rows = fetch_list(session, org_url, "item-organization", parse_org_row, MAX_ORG_PAGES)
    total = len(rows)
    print(f"    найдено: {total}, тянем детали...", file=sys.stderr)

    report["organizations"] = {"count": total, "items": rows}
    save_cb()

    for i, row in enumerate(rows, 1):
        org_id = row.get("id")
        if isinstance(org_id, int):
            row["detail"] = fetch_org_detail(session, org_id)
        if i % save_every == 0 or i == total:
            print(f"    [{i}/{total}]", file=sys.stderr)
            save_cb()

    return rows


# ==================== ВАКАНСИИ ====================
def parse_vacancy_row(item: Tag) -> dict:
    out: dict = {}

    upd = item.find("a", href=re.compile(r"/vacancy/update\?id=\d+"))
    if isinstance(upd, Tag):
        m = re.search(r"[?&]id=(\d+)", attr_str(upd, "href"))
        if m is not None:
            out["id"] = int(m.group(1))

    card = item.find("div", class_="card")
    if not isinstance(card, Tag):
        return out

    for child in card.find_all(recursive=False):
        if not isinstance(child, Tag) or child.name != "div":
            continue
        classes = get_classes(child)
        if "col" not in classes:
            continue

        text = child.get_text(" ", strip=True)
        low = text.lower()

        if "col-auto" not in classes and "col-2" not in classes:
            strong = child.find("strong")
            if isinstance(strong, Tag) and "name" not in out:
                out["name"] = strong.get_text(strip=True)
            continue

        if "col-auto" in classes and "ставка:" in low and "кол-во:" in low:
            m1 = re.search(r"Ставка:\s*([\d.,]+)", text)
            m2 = re.search(r"Кол-во:\s*(\d+)", text)
            if m1 is not None:
                out["vacancy_part"] = m1.group(1)
            if m2 is not None:
                out["qty"] = int(m2.group(1))
            continue

        if "col-auto" in classes and low.startswith("id"):
            continue

        if "col-2" in classes and "учреждение" in low:
            strong = child.find("strong")
            if isinstance(strong, Tag):
                out["organization"] = strong.get_text(strip=True)
            continue

        if "col-auto" in classes and "дата обновления" in low:
            strong = child.find("strong")
            if isinstance(strong, Tag):
                out["updated_at"] = strong.get_text(strip=True)
            continue

        if "col-auto" in classes and "просмотры" in low and child.find("small") is None:
            m = re.search(r"(\d+)", text)
            if m is not None:
                out["views"] = int(m.group(1))
            continue

    return out


def fetch_vacancy_detail(session: requests.Session, vac_id: int) -> dict:
    url = f"{vac_url}/update?id={vac_id}&_return=1"
    try:
        r = session.get(url, timeout=TIMEOUT, verify=VERIFY_TLS, allow_redirects=True)
    except requests.RequestException as e:
        return {"error": str(e)}

    soup = BeautifulSoup(r.text, "html.parser")
    return {
        "url": url,
        "http_status": r.status_code,
        "title": page_title(r.text),
        "form": parse_form(r.text, "vacancy-form", "Vacancy["),
        "meta": parse_meta(soup),
    }


def dump_vacancies(
    session: requests.Session,
    report: dict,
    save_cb,
    save_every: int = 50,
) -> list[dict]:
    print("[*] Вакансии...", file=sys.stderr)
    rows = fetch_list(session, vac_url, "item-user", parse_vacancy_row, MAX_VAC_PAGES)
    total = len(rows)
    print(f"    найдено: {total}, тянем детали...", file=sys.stderr)

    report["vacancies"] = {"count": total, "items": rows}
    save_cb()

    for i, row in enumerate(rows, 1):
        vac_id = row.get("id")
        if isinstance(vac_id, int):
            row["detail"] = fetch_vacancy_detail(session, vac_id)
        if i % save_every == 0 or i == total:
            print(f"    [{i}/{total}]", file=sys.stderr)
            save_cb()

    return rows


# ==================== MAIN ====================
def main() -> int:
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
        "login": None,
        "categories": {"count": 0, "items": []},
        "organizations": {"count": 0, "items": []},
        "vacancies": {"count": 0, "items": []},
        "logout": None,
        "finished_at": None,
    }

    def save_cb() -> None:
        save_report(report, OUT_FILE)

    exit_code = 0
    try:
        # ---- логин ----
        ok, who = do_login(session)
        report["login"] = {"status": "PASS" if ok else "FAIL", "username": who}
        save_cb()
        if not ok:
            print(f"ERROR: login failed -> {OUT_FILE}", file=sys.stderr)
            return 2

        # ---- 1. категории ----
        try:
            dump_categories(session, report, save_cb)
        except requests.RequestException as e:
            report["categories"]["error"] = str(e)
            save_cb()

        # ---- 2. организации ----
        try:
            dump_organizations(session, report, save_cb)
        except requests.RequestException as e:
            report["organizations"]["error"] = str(e)
            save_cb()

        # ---- 3. вакансии ----
        try:
            dump_vacancies(session, report, save_cb)
        except requests.RequestException as e:
            report["vacancies"]["error"] = str(e)
            save_cb()

    except KeyboardInterrupt:
        print("\n[!] Прервано пользователем, сохраняю текущий прогресс...",
              file=sys.stderr)
        exit_code = 130
    finally:
        try:
            report["logout"] = {"status": "PASS" if do_logout(session) else "FAIL"}
        except Exception:
            report["logout"] = {"status": "FAIL", "error": "logout crashed"}
        session.cookies.clear()
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save_cb()

    print(
        f"OK: {OUT_FILE} "
        f"(categories={report['categories']['count']}, "
        f"organizations={report['organizations']['count']}, "
        f"vacancies={report['vacancies']['count']})"
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())