import os
import sys
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from dotenv import load_dotenv

# загружаем .env из папки скрипта
load_dotenv()

BASE_URL    = os.getenv("BASE_URL", "").rstrip("/")
ADMIN_PATH  = os.getenv("ADMIN_PATH", "/admin")
LOGIN_PATH  = os.getenv("LOGIN_PATH", "/admin/main/login")
USERNAME    = os.getenv("SITE_USER", "")
PASSWORD    = os.getenv("SITE_PASS", "")
TIMEOUT     = int(os.getenv("TIMEOUT", "15"))
VERIFY_TLS  = os.getenv("VERIFY_TLS", "True").lower() in ("1", "true", "yes", "y")

if not BASE_URL or not USERNAME or not PASSWORD:
    print("[!] Проверь .env: BASE_URL, SITE_USER, SITE_PASS обязательны.")
    sys.exit(1)

if not VERIFY_TLS:
    requests.packages.urllib3.disable_warnings()  # noqa

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
})

admin_url = urljoin(BASE_URL + "/", ADMIN_PATH.lstrip("/"))
login_url = urljoin(BASE_URL + "/", LOGIN_PATH.lstrip("/"))

# 1. Получаем страницу логина (и cookies, и CSRF)
try:
    r = session.get(admin_url, timeout=TIMEOUT, allow_redirects=True, verify=VERIFY_TLS)
    r.raise_for_status()
except requests.RequestException as e:
    print(f"[!] Не удалось открыть {admin_url}: {e}")
    sys.exit(1)

soup = BeautifulSoup(r.text, "html.parser")

csrf_input = soup.find("input", {"name": "_csrf"})
csrf_token = csrf_input.get("value") if csrf_input else None

if not csrf_token:
    meta = soup.find("meta", {"name": "csrf-token"})
    csrf_token = meta.get("content") if meta else None

if not csrf_token:
    print("[!] CSRF-токен не найден. Изменилась разметка или защита.")
    sys.exit(1)

print(f"[+] CSRF: {csrf_token[:24]}...")
print(f"[+] Cookies до входа: {session.cookies.get_dict()}")

# 2. Одна попытка входа
data = {
    "_csrf": csrf_token,
    "LoginForm[username]": USERNAME,
    "LoginForm[password]": PASSWORD,
    "LoginForm[rememberMe]": "1",
    "login-button": "",
}

try:
    resp = session.post(
        login_url,
        data=data,
        timeout=TIMEOUT,
        allow_redirects=True,
        verify=VERIFY_TLS,
    )
except requests.RequestException as e:
    print(f"[!] Ошибка POST: {e}")
    sys.exit(1)

print(f"\n[+] POST статус : {resp.status_code}")
print(f"[+] Финальный URL: {resp.url}")
print(f"[+] Cookies после: {session.cookies.get_dict()}")

text = resp.text
page = BeautifulSoup(text, "html.parser")
title = page.title.string.strip() if page.title and page.title.string else ""

print(f"[+] Title: {title}")

login_form_present = ("login-form" in text) or ("Авторизация" in text and "loginform-password" in text)

if login_form_present:
    print("[-] Вход НЕ выполнен: снова форма авторизации.")
elif resp.url.rstrip("/") != login_url.rstrip("/"):
    print("[+] Возможно, вход выполнен: редирект на другую страницу.")
else:
    print("[?] Неоднозначный результат — проверь ответ вручную.")

print("\n[+] Заголовки:")
for h in ["Server", "X-Powered-By", "Content-Security-Policy",
          "X-Frame-Options", "X-Content-Type-Options", "Set-Cookie"]:
    if h in resp.headers:
        print(f"    {h}: {resp.headers[h]}")