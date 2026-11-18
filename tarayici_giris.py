# -*- coding: utf-8 -*-
"""
Tarayici ile yari otomatik yeniden giris.

sinav_takip.py calistigi surece 127.0.0.1:47615'te kucuk bir yerel sunucu acik tutar.
Tampermonkey script'i (aksis_otomatik_giris.user.js) bu sunucudan kullanici adi/sifreyi alir,
girisi doldurur, Cloudflare dogrulamasini SENIN onaylamani bekler ve giris bitince cookie'leri
buraya gonderir. Python cookie'leri .env'deki IDS_COOKIE satirina yazar.

Sunucu .env'deki YEREL_ANAHTAR ile korunur; bu anahtari bir kez Tampermonkey paneline yapistirirsin.
Robot dogrulamasini script atlatmaz; tiki her zaman sen isaretlersin.
"""
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("sinav_takip")

PORT = 47615
START_URL = "https://aksis.iuc.edu.tr/giris/main"
REQUIRED_COOKIE = ".OGRISFormAuth"

CHROME_CANDIDATES = [
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
]


def _find_chrome(custom=""):
    for path in [custom] + CHROME_CANDIDATES:
        if path and Path(path).exists():
            return path
    return shutil.which("chrome") or shutil.which("google-chrome")


def _open_browser(url, chrome_path=""):
    chrome = _find_chrome(chrome_path)
    if chrome:
        subprocess.Popen([chrome, "--new-window", url])
    else:
        log.warning("Chrome bulunamadi, varsayilan tarayici aciliyor (Tampermonkey orada kurulu olmali).")
        webbrowser.open(url)


WINDOW_TITLE_HINTS = ("AKS", "İstanbul Üniversitesi", "ÖĞRENCİ İŞLERİ", "Cerrahpa")


def _chrome_windows():
    """Gorunur Chrome pencereleri: {hwnd: baslik}. Windows disinda bos doner."""
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value == "Chrome_WidgetWin_1":
                length = user32.GetWindowTextLengthW(hwnd)
                title = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title, length + 1)
                if title.value:
                    found[hwnd] = title.value
        return True

    user32.EnumWindows(callback, 0)
    return found


def _close_new_windows(before):
    """Sadece biz actiktan sonra olusan Aksis/OBS penceresini kapatir; senin pencerelerine dokunmaz."""
    if os.name != "nt":
        return
    import ctypes
    WM_CLOSE = 0x0010
    for hwnd, title in _chrome_windows().items():
        if hwnd not in before and any(h.lower() in title.lower() for h in WINDOW_TITLE_HINTS):
            ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            log.info("Giris penceresi kapatildi: %s", title)


def _set_env_line(env_path, key, value):
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"(?m)^{re.escape(key)}=", text):
        text = re.sub(rf"(?m)^{re.escape(key)}=.*$", lambda m: line, text)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n" if text else line + "\n"
    env_path.write_text(text, encoding="utf-8")


def update_env_cookie(env_path, cookie_string):
    _set_env_line(env_path, "IDS_COOKIE", cookie_string)


def ensure_local_key(env_path, current=""):
    """YEREL_ANAHTAR yoksa uretir ve .env'e yazar."""
    if current:
        return current
    key = secrets.token_urlsafe(18)
    _set_env_line(env_path, "YEREL_ANAHTAR", key)
    return key


class CookieServer:
    """Tampermonkey script'inin konustugu yerel sunucu. Script calistigi surece acik kalir."""

    def __init__(self, env_path, username, password, key):
        self.env_path = env_path
        self.username = username
        self.password = password
        self.key = key
        self.received = threading.Event()
        self.last_cookies = None
        self.httpd = None

    def start(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self):
                return secrets.compare_digest(self.headers.get("X-Sinavtakip-Key", ""), server.key)

            def do_GET(self):
                if not self._authorized():
                    return self._reply(403, {"error": "anahtar yanlis"})
                if self.path == "/durum":
                    return self._reply(200, {"ok": True, "bilgiler": bool(server.username and server.password)})
                if self.path == "/bilgiler":
                    if not (server.username and server.password):
                        return self._reply(404, {"error": ".env'de AKSIS_USERNAME / AKSIS_PASSWORD yok"})
                    return self._reply(200, {"username": server.username, "password": server.password})
                self._reply(404, {"error": "yok"})

            def do_POST(self):
                if self.path != "/cookie" or not self._authorized():
                    return self._reply(403, {"error": "yetkisiz"})
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    data = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._reply(400, {"error": "gecersiz json"})
                cookies = {c["name"]: c["value"] for c in data.get("cookies", [])
                           if c.get("name") and not c["name"].startswith("_ga")}
                if REQUIRED_COOKIE not in cookies:
                    return self._reply(422, {"error": f"{REQUIRED_COOKIE} yok", "names": sorted(cookies)})
                update_env_cookie(server.env_path, "; ".join(f"{k}={v}" for k, v in cookies.items()))
                server.last_cookies = cookies
                log.info("Tarayicidan yeni oturum cookie'leri alindi ve .env'e yazildi (%s).",
                         ", ".join(sorted(cookies)))
                self._reply(200, {"ok": True})
                server.received.set()

        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        except OSError as exc:
            log.error("Yerel sunucu acilamadi (port %d kullanimda mi? baska bir kopya calisiyor olabilir): %s",
                      PORT, exc)
            return False
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        log.info("Tampermonkey icin yerel baglanti hazir (127.0.0.1:%d).", PORT)
        return True

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    def refresh_via_browser(self, timeout=10 * 60, chrome_path=""):
        """Tarayicida girisi baslatir, cookie'ler gelene kadar bekler. Basariliysa True."""
        if not self.httpd:
            return False
        if not (self.username and self.password):
            log.error("Tarayici ile giris icin .env'de AKSIS_USERNAME ve AKSIS_PASSWORD gerekli.")
            return False
        self.received.clear()
        log.warning("Oturum dustu: tarayici aciliyor. Cloudflare dogrulamasini onaylaman bekleniyor "
                    "(en fazla %d dk).", timeout // 60)
        before = _chrome_windows()
        _open_browser(f"{START_URL}#sinavtakip={self.key}", chrome_path)
        if not self.received.wait(timeout):
            log.error("Tarayicidan cookie gelmedi (zaman asimi).")
            return False
        time.sleep(1.5)  # userscript kendi kapatmayi denesin, olmazsa biz kapatalim
        _close_new_windows(before)
        return True
