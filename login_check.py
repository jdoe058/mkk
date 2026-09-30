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

# ---------- конфиг ----------
BASE_URL   = os.getenv("BASE_URL", "").rstrip("/")
ADMIN_PATH = os.getenv("ADMIN_PATH", "/admin")
LOGIN_PATH = os.getenv("LOGIN_PATH", "/admin/main/login")
LOGOUT_PATH = os.getenv("LOGOUT_PATH", "/admin/logout")
USERNAME   = os.getenv("SITE_USER", "")
PASSWORD   = os.getenv("SITE_PASS", "")
TIMEOUT    = int(os.getenv("TIMEOUT", "15"))
VERIFY_TLS = os.getenv("VERIFY_TLS", "True").lower() in ("1", "true", "yes", "y")

if not BASE_URL or not USERNAME or not PASSWORD:
    print(json.dumps(
        {"error": "Проверь .env: BASE_URL, SITE_USER, SITE_PASS обязательны"},
        ensure_ascii=False, indent=2))
    sys.exit(1)

if not VERIFY_TLS:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

admin_url  = urljoin(BASE_URL + "/", ADMIN_PATH.lstrip("/"))
login_url  = urljoin(BASE_URL + "/", LOGIN_PATH.lstrip("/"))
logout_url = urljoin(BASE_URL + "/", LOGOUT_PATH.lstrip("/"))

report: dict = {
    "target": BASE_URL,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "steps": {},
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
def step(name: str, status: str, **extra) -> None:
    report["steps"][name] = {"status": status, **extra}


def page_title(html: str) -> str:
    tag = BeautifulSoup(html, "html.parser").title
    return tag.get_text(strip=True) if tag is not None else ""


def attr_str(tag: Tag | None, name: str) -> str:
    if not isinstance(tag, Tag):
        return ""
    v = tag.get(name)
    if isinstance(v, list):
        v = v[0] if v else ""
    return str(v) if v is not None else ""


def collect_set_cookies(resp: requests.Response) -> list[str]:
    """Все Set-Cookie из цепочки редиректов + финального ответа."""
    out: list[str] = []
    for r in [*resp.history, resp]:
        try:
            raw = r.raw.headers.getlist("Set-Cookie") if r.raw is not None else []
        except AttributeError:
            raw = []
        if not raw:
            sc = r.headers.get("Set-Cookie")
            raw = [sc] if sc else []
        out.extend(raw)
    return out


def cookie_flags(set_cookies: list[str], name: str) -> dict:
    """Флаги для конкретной cookie по имени."""
    prefix = name.lower() + "="
    for c in set_cookies:
        cl = c.lower()
        if not cl.startswith(prefix):
            continue

        m_ss = re.search(r"samesite=(\w+)", cl)
        same_site = m_ss.group(1).capitalize() if m_ss else None

        m_ma = re.search(r"max-age=(\d+)", cl)
        max_age = int(m_ma.group(1)) if m_ma else None

        m_path = re.search(r"path=([^;]+)", cl)
        path = m_path.group(1) if m_path else None

        return {
            "Secure":   "secure" in cl.split(";")[0] or "; secure" in cl,
            "HttpOnly": "httponly" in cl,
            "SameSite": same_site,
            "MaxAge":   max_age,
            "Path":     path,
        }
    return {}


def username_from_header(html: str) -> str | None:
    """Логин пользователя из шапки админки (#user-nav-btn)."""
    soup = BeautifulSoup(html, "html.parser")
    btn = soup.find("button", {"id": "user-nav-btn"})
    if isinstance(btn, Tag):
        return btn.get_text(strip=True).replace("\xa0", " ").strip()
    return None


def find_logout_form(html: str) -> Tag | None:
    soup = BeautifulSoup(html, "html.parser")
    # 1) точный action
    form = soup.find("form", attrs={"action": re.compile(re.escape(LOGOUT_PATH) + r"$", re.I)})
    if isinstance(form, Tag):
        return form
    # 2) любой action со словом logout
    form = soup.find("form", attrs={"action": re.compile("logout", re.I)})
    return form if isinstance(form, Tag) else None


def do_logout(sess: requests.Session) -> tuple[bool, str]:
    try:
        r = sess.get(admin_url, timeout=TIMEOUT,
                     verify=VERIFY_TLS, allow_redirects=True)
    except requests.RequestException as e:
        return False, f"GET admin: {e}"

    form = find_logout_form(r.text)
    if form is not None:
        action = attr_str(form, "action") or LOGOUT_PATH
        url = urljoin(BASE_URL, action)
        tok = form.find("input", {"name": "_csrf"})
        data = {"_csrf": attr_str(tok, "value")} if isinstance(tok, Tag) else {}
        try:
            rr = sess.post(url, data=data, timeout=TIMEOUT,
                           verify=VERIFY_TLS, allow_redirects=True)
            return True, f"POST {url} → {rr.status_code} {rr.url}"
        except requests.RequestException as e:
            return False, f"POST logout: {e}"

    # fallback: ссылка GET
    soup = BeautifulSoup(r.text, "html.parser")
    link = soup.find("a", attrs={"href": re.compile("logout", re.I)})
    if isinstance(link, Tag):
        url = urljoin(BASE_URL, attr_str(link, "href"))
        try:
            rr = sess.get(url, timeout=TIMEOUT,
                          verify=VERIFY_TLS, allow_redirects=True)
            return True, f"GET {url} → {rr.status_code} {rr.url}"
        except requests.RequestException as e:
            return False, f"GET logout: {e}"

    return False, "форма/ссылка логаута не найдена"


# ---------- 1. Получаем страницу логина и CSRF ----------
try:
    r = session.get(admin_url, timeout=TIMEOUT,
                    verify=VERIFY_TLS, allow_redirects=True)
    r.raise_for_status()
except requests.RequestException as e:
    step("fetch_admin", "FAIL", error=str(e))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(1)

soup = BeautifulSoup(r.text, "html.parser")
csrf_in = soup.find("input", {"name": "_csrf"})
csrf_token = attr_str(csrf_in, "value") or None
if not csrf_token:
    meta = soup.find("meta", {"name": "csrf-token"})
    csrf_token = attr_str(meta, "content") or None

step("fetch_admin", "OK" if csrf_token else "FAIL",
     csrf_found=bool(csrf_token),
     cookies_before=session.cookies.get_dict())

if not csrf_token:
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(1)

# ---------- 2. Логин (rememberMe=0 для короткой сессии) ----------
login_data = {
    "_csrf": csrf_token,
    "LoginForm[username]": USERNAME,
    "LoginForm[password]": PASSWORD,
    "LoginForm[rememberMe]": "0",
    "login-button": "",
}

try:
    resp = session.post(login_url, data=login_data, timeout=TIMEOUT,
                        verify=VERIFY_TLS, allow_redirects=True)
except requests.RequestException as e:
    step("login", "FAIL", error=str(e))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(1)

cookies_after = session.cookies.get_dict()
set_cookies_all = collect_set_cookies(resp)

flags_phpsessid = cookie_flags(set_cookies_all, "PHPSESSID")
flags_csrf      = cookie_flags(set_cookies_all, "_csrf")
flags_identity  = cookie_flags(set_cookies_all, "_identity")

login_form_still = ("login-form" in resp.text) or ("loginform-password" in resp.text)
login_url_norm = login_url.rstrip("/")
final_url_norm = resp.url.rstrip("/")
has_auth_content = any(m in resp.text for m in ("Аналитика", "logout", "Выйти", "Выход"))

auth_ok = (not login_form_still) and (final_url_norm != login_url_norm) and has_auth_content

step("login",
     "PASS" if auth_ok else "FAIL",
     post_status=resp.status_code,
     final_url=resp.url,
     title=page_title(resp.text),
     username=username_from_header(resp.text),
     cookies_after=cookies_after,
     set_cookie_raw=set_cookies_all,
     flags_phpsessid=flags_phpsessid,
     flags_csrf=flags_csrf,
     flags_identity=flags_identity,
     login_form_present=login_form_still,
     identity_cookie_present=("_identity" in cookies_after),
     session_based=("_identity" not in cookies_after))

# ---------- 3. Заголовки безопасности ----------
sec_headers = {}
for h in ["Server", "X-Powered-By", "Strict-Transport-Security",
          "Content-Security-Policy", "X-Frame-Options",
          "X-Content-Type-Options", "Referrer-Policy", "Permissions-Policy"]:
    sec_headers[h] = resp.headers.get(h)

present = [h for h, v in sec_headers.items() if v]
missing = [h for h, v in sec_headers.items()
           if v is None and h not in ("Server", "X-Powered-By")]

step("security_headers",
     "PASS" if not missing else "WARN",
     present=present,
     missing=missing,
     raw=sec_headers)

# ---------- 4. Логаут ----------
logout_ok, logout_detail = do_logout(session)
step("logout", "PASS" if logout_ok else "FAIL", detail=logout_detail)

# ---------- 5. Проверка инвалидации сессии ----------
try:
    chk = session.get(admin_url, timeout=TIMEOUT,
                      verify=VERIFY_TLS, allow_redirects=True)
    still_auth = ("login-form" not in chk.text) and (
        any(m in chk.text for m in ("Аналитика", "logout", "Выйти")))
    step("session_invalidated",
         "FAIL" if still_auth else "PASS",
         final_url=chk.url,
         title=page_title(chk.text),
         cookies=session.cookies.get_dict())
except requests.RequestException as e:
    step("session_invalidated", "WARN", error=str(e))

# ---------- 6. Чистим локальные cookies ----------
session.cookies.clear()
report["finished_at"] = datetime.now(timezone.utc).isoformat()
report["summary"] = {
    "login":  report["steps"].get("login", {}).get("status"),
    "logout": report["steps"].get("logout", {}).get("status"),
    "session_invalidated": report["steps"].get("session_invalidated", {}).get("status"),
}

# ---------- 7. Печать читаемого JSON ----------
print(json.dumps(report, ensure_ascii=False, indent=2))