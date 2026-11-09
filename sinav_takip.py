# -*- coding: utf-8 -*-
"""
IUC OBS Sinav Takip

OBS'deki sinav sonuclarini ve hocalardan gelen mesajlari (Yazismalarim)
periyodik olarak kontrol eder; yeni bir sinav aciklandiginda, bir not
degistiginde veya yeni mesaj geldiginde e-posta gonderir.

Kullanim:
    python sinav_takip.py                 # surekli takip
    python sinav_takip.py --once          # bir kez sorgula, notlari yazdir, cik
    python sinav_takip.py --test-mail     # sadece test maili gonder
    python sinav_takip.py --yil 2025 --donem 2 --once   # baska donemi goruntule

Ayarlar .env dosyasindan okunur (bkz. .env.example).
"""
import argparse
import json
import logging
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from email.mime.text import MIMEText
from pathlib import Path

import requests
import urllib3

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
STATE_PATH = BASE_DIR / "sinav_durumu.json"

OBS_URL = "https://obs.iuc.edu.tr"
LOGIN_URL = "https://aksis.iuc.edu.tr/Account/LogOn?returnUrl=%2FAccount%2FLogOff"
EXAM_URL = f"{OBS_URL}/OgrenimBilgileri/SinavSonuclariVeNotlar/GetOgrenciSinavSonuc"
DISTRIBUTION_URL = f"{OBS_URL}/OgrenimBilgileri/SinavSonuclariVeNotlar/HarfNotDagilimGoster?SinavID="
CHAT_PAGE_URL = f"{OBS_URL}/Genel/Chat2"
MESSAGES_API_URL = f"{OBS_URL}/api/Yazisma/GetirYazismalar"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")
EXAM_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": OBS_URL,
    "Referer": f"{OBS_URL}/OgrenimBilgileri/SinavSonuclariVeNotlar/Index",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
}

RELOGIN_EVERY_SECONDS = 20 * 60 * 60  # 20 saatte bir yeniden giris
MAX_BACKOFF_SECONDS = 10 * 60
LOGIN_RETRY_SECONDS = 5 * 60

log = logging.getLogger("sinav_takip")


# ----------------------------------------------------------------- ayarlar
@dataclass
class Config:
    username: str
    password: str
    kauth_cookie: str
    email_address: str
    email_password: str
    email_to: list
    yil: str
    donem: str
    interval: int
    verify_ssl: bool
    check_messages: bool
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587

    @property
    def exam_data(self):
        return {"sort": "", "group": "DersAdi-asc", "filter": "", "yil": self.yil, "donem": self.donem}


def load_env(path=ENV_PATH):
    if not path.exists():
        sys.exit(".env dosyasi bulunamadi! .env.example'i .env olarak kopyalayip doldur.")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_exam_data(raw):
    """Eski format EXAM_DATA (sort=...&yil=2026&donem=1) icin geriye uyumluluk."""
    yil = re.search(r"yil=(\d+)", raw or "")
    donem = re.search(r"donem=(\d+)", raw or "")
    return (yil.group(1) if yil else None), (donem.group(1) if donem else None)


def load_config(args):
    load_env()
    env = os.environ.get
    old_yil, old_donem = parse_exam_data(env("EXAM_DATA", ""))
    cfg = Config(
        username=env("AKSIS_USERNAME", ""),
        password=env("AKSIS_PASSWORD", ""),
        kauth_cookie=env("KAUTH_COOKIE", ""),
        email_address=env("EMAIL_ADDRESS", ""),
        email_password=env("EMAIL_PASSWORD", "").replace(" ", ""),
        email_to=[e.strip() for e in env("EMAIL_TO_LIST", "").split(",") if e.strip()],
        yil=args.yil or env("YIL") or old_yil or "2026",
        donem=args.donem or env("DONEM") or old_donem or "1",
        interval=int(env("CHECK_INTERVAL", "30")),
        verify_ssl=env("VERIFY_SSL", "false").lower() in ("1", "true", "yes"),
        check_messages=env("CHECK_MESSAGES", "true").lower() in ("1", "true", "yes"),
    )
    missing = [name for name, value in [
        ("AKSIS_USERNAME", cfg.username), ("AKSIS_PASSWORD", cfg.password),
        ("EMAIL_ADDRESS", cfg.email_address), ("EMAIL_PASSWORD", cfg.email_password),
        ("EMAIL_TO_LIST", cfg.email_to),
    ] if not value]
    if missing:
        sys.exit("Eksik .env alanlari: " + ", ".join(missing))
    return cfg


# ------------------------------------------------------------------- OBS
class SessionExpired(Exception):
    pass


class LoginFailed(Exception):
    pass


class ObsClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.session.verify = cfg.verify_ssl
        self.last_login = 0.0
        self.chat_token = None

    def _adopt_cookies(self, resp):
        # aksis ve obs farkli alt alan adlari; cookie'leri ikisinde de gecerli olacak sekilde kopyala
        for cookie in resp.cookies:
            self.session.cookies.set(cookie.name, cookie.value)

    def refresh_obs_session(self):
        resp = self.session.get(OBS_URL + "/", timeout=30)
        resp.raise_for_status()
        self._adopt_cookies(resp)
        log.debug("OBS oturumu yenilendi.")

    def login(self):
        self.session.cookies.clear()  # eski giris cookie'si basari kontrolunu yaniltmasin
        resp = self.session.get(LOGIN_URL, timeout=30)
        resp.raise_for_status()
        self._adopt_cookies(resp)
        match = re.search(r'name="__RequestVerificationToken" type="hidden" value="([^"]+)"', resp.text)
        if not match:
            raise LoginFailed("Giris sayfasinda dogrulama token'i bulunamadi (site degismis olabilir).")

        if self.cfg.kauth_cookie:
            self.session.cookies.set("AKSISAutKH", self.cfg.kauth_cookie)
        form = {
            "__RequestVerificationToken": match.group(1),
            "UserName": self.cfg.username,
            "Password": self.cfg.password,
            "IpAddr": "",
            "SmsDogrulaModal": "0",
            "SmsDogrulaSecond": "0",
            "PhoneNumber": "",
        }
        resp = self.session.post(
            LOGIN_URL, data=form, allow_redirects=False, timeout=30,
            headers={"Origin": "https://aksis.iuc.edu.tr", "Referer": LOGIN_URL},
        )
        resp.raise_for_status()
        self._adopt_cookies(resp)
        if ".OGRISFormAuth" not in self.session.cookies:
            log.debug("Giris cevabi: HTTP %s, Location=%s", resp.status_code, resp.headers.get("Location"))
            raise LoginFailed("Giris basarisiz: kullanici adi/sifre yanlis olabilir veya "
                              "KAUTH_COOKIE suresi dolmus (SMS dogrulamasi istendi).")
        self.refresh_obs_session()
        self.chat_token = None
        self.last_login = time.time()
        log.info("Aksis girisi basarili.")

    def fetch_exams(self):
        """Sinav listesini dondurur: {SinavID: item}. Oturum dustuyse bir kez yeniler."""
        for attempt in range(2):
            resp = self.session.post(EXAM_URL, data=self.cfg.exam_data, headers=EXAM_HEADERS, timeout=30)
            resp.raise_for_status()
            try:
                payload = resp.json()
            except ValueError:
                raise SessionExpired("OBS JSON yerine HTML dondu (giris oturumu dusmus).")
            data = payload.get("Data") if isinstance(payload, dict) else None
            if isinstance(data, list):
                return {exam_key(item): item for course in data for item in course.get("Items", [])}
            errors = str(payload.get("Errors", "")) if isinstance(payload, dict) else ""
            if attempt == 0 and "tirilemedi" in errors:
                log.debug("OBS oturumu dusmus, yenileniyor...")
                self.refresh_obs_session()
                continue
            raise SessionExpired(f"Beklenmeyen OBS cevabi: {errors or payload!r:.200}")
        raise SessionExpired("OBS oturumu yenilenemedi.")

    def _load_chat_token(self):
        # Yazismalarim sayfasi API icin gereken token'i sayfa icinde "var token = ..." olarak veriyor
        resp = self.session.get(CHAT_PAGE_URL, timeout=30)
        resp.raise_for_status()
        match = re.search(r'var\s+token\s*=\s*["\']([^"\']+)["\']', resp.text)
        if not match:
            raise SessionExpired("Yazismalarim sayfasinda token bulunamadi (oturum dusmus olabilir).")
        self.chat_token = match.group(1)

    def fetch_messages(self, count=20):
        """Son yazismalari (her hoca icin bir konusma) dondurur."""
        for attempt in range(2):
            if not self.chat_token:
                self._load_chat_token()
            resp = self.session.get(MESSAGES_API_URL, timeout=30, headers={"Accept": "application/json"},
                                    params={"gosterilecekYazismaSayisi": count, "token": self.chat_token})
            try:
                payload = resp.json()
            except ValueError:
                payload = None
            if resp.ok and isinstance(payload, dict) and payload.get("IsSuccess"):
                return payload.get("Object") or []
            self.chat_token = None  # token eskimis olabilir, bir kez yenile
        raise SessionExpired("Mesajlar alinamadi (oturum dusmus olabilir).")


# ---------------------------------------------------------- karsilastirma
def exam_key(item):
    # OBS SinavID'yi 0 donduruyor; sinavi ders grubu + ad + tur ile ayirt ediyoruz
    parts = [item.get(k) for k in ("FKDersGrupID", "DersAdi", "SinavAdi", "SinavTuru", "EnumNotTuru")]
    return "|".join(str(p).strip() for p in parts)


def describe(item):
    return f"{item.get('DersAdi', '').strip()} - {item.get('SinavAdi', '').strip()}"


def find_changes(old, new):
    """Yeni aciklanan sinavlar ve notu degisenler."""
    changes = []
    for sinav_id, item in new.items():
        before = old.get(sinav_id)
        if before is None:
            changes.append(("Yeni", item, None))
        elif str(before.get("Notu", "")) != str(item.get("Notu", "")):
            changes.append(("Not degisti", item, before.get("Notu", "")))
    return changes


def message_fingerprint(c):
    return f"{c.get('SGZaman', '')}|{c.get('SonMetin', '')}"


def message_state(conversations):
    return {str(c.get("ID")): message_fingerprint(c) for c in conversations}


def find_new_messages(old_state, conversations):
    """Son mesaji karsi taraf (hoca) yazmis ve daha once gormedigimiz konusmalar."""
    new = []
    for c in conversations:
        from_other = c.get("FKSonGuncelleyenKisiID") not in (None, c.get("FKKisiID"))
        if from_other and old_state.get(str(c.get("ID"))) != message_fingerprint(c):
            new.append(c)
    return new


def sender_name(c):
    name = c.get("MuhatapKisiAdSoyad") or f"{c.get('MuhatapKisiAd', '')} {c.get('MuhatapKisiSoyad', '')}"
    return name.strip()


def print_messages(conversations):
    unread = [c for c in conversations if c.get("OkunduMu") is False]
    log.info("Yazismalar: %d konusma, %d okunmamis.", len(conversations), len(unread))
    for c in unread:
        print(f"   [okunmamis] {sender_name(c)} ({c.get('SGZamanString', '')})")


def print_exams(exams):
    if not exams:
        log.info("Bu donem icin henuz aciklanmis sinav yok.")
        return
    current = None
    for item in sorted(exams.values(), key=lambda i: i.get("DersAdi", "")):
        course = item.get("DersAdi", "").strip()
        if course != current:
            print(f"\n{course}")
            current = course
        print(f"   {item.get('SinavAdi', '').strip():<40} {item.get('Notu', '')}")
    print()


# ---------------------------------------------------------------- durum
def load_state(cfg):
    """(sinavlar, mesajlar) dondurur; ilk calistirmada None olurlar."""
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None, None
    exams = state.get("exams")
    if state.get("yil") != cfg.yil or state.get("donem") != cfg.donem:
        exams = None  # donem degismis, sinav listesi sifirdan
    return exams, state.get("messages")


def save_state(cfg, exams, messages):
    STATE_PATH.write_text(
        json.dumps({"yil": cfg.yil, "donem": cfg.donem, "exams": exams, "messages": messages},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ----------------------------------------------------------------- mail
def send_mail(cfg, subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = cfg.email_address
    msg["To"] = ", ".join(cfg.email_to)
    msg["Subject"] = subject
    with smtplib.SMTP(cfg.smtp_server, cfg.smtp_port, timeout=30) as server:
        server.starttls()
        server.login(cfg.email_address, cfg.email_password)
        server.sendmail(cfg.email_address, cfg.email_to, msg.as_string())


def notify_changes(cfg, changes):
    if len(changes) == 1:
        kind, item, _ = changes[0]
        subject = f"{kind} sinav sonucu: {describe(item)}"
    else:
        subject = f"{len(changes)} yeni sinav sonucu aciklandi"

    blocks = []
    for kind, item, old_grade in changes:
        lines = [
            f"[{kind}] {describe(item)}",
            f"  Not        : {item.get('Notu', '')}" + (f"  (onceki: {old_grade})" if old_grade not in (None, "") else ""),
            f"  Tur        : {item.get('SinavTuru', '')}",
            f"  Tarih      : {item.get('SinavTarihiString', '')}",
            f"  Etki orani : {item.get('EtkiOrani', '')}",
            f"  Girdi mi   : {item.get('SinavaGirdiMi', '')}",
        ]
        if item.get("SinavIDEncrypted"):
            lines.append(f"  Can egrisi : {DISTRIBUTION_URL}{item['SinavIDEncrypted']}")
        blocks.append("\n".join(lines))
    body = "\n\n".join(blocks)

    try:
        send_mail(cfg, subject, body)
        log.info("Mail gonderildi: %s", subject)
    except Exception as exc:
        log.error("Mail gonderilemedi: %s", exc)


def notify_messages(cfg, conversations):
    if len(conversations) == 1:
        subject = f"OBS'de yeni mesaj: {sender_name(conversations[0])}"
    else:
        subject = f"OBS'de {len(conversations)} yeni mesaj"
    blocks = [
        f"Gonderen : {sender_name(c)}\n"
        f"Zaman    : {c.get('SGZamanString', '')}\n"
        f"Mesaj    : {c.get('SonMetin', '')}"
        for c in conversations
    ]
    body = "\n\n".join(blocks) + f"\n\nTum yazismalar: {CHAT_PAGE_URL}"
    try:
        send_mail(cfg, subject, body)
        log.info("Mail gonderildi: %s", subject)
    except Exception as exc:
        log.error("Mail gonderilemedi: %s", exc)


def check_messages(cfg, client, known, once):
    conversations = client.fetch_messages()
    if once:
        print_messages(conversations)
    elif known is None:
        log.info("Ilk mesaj sorgusu tamam, %d konusma kayitli.", len(conversations))
    else:
        new = find_new_messages(known, conversations)
        for c in new:
            log.info("Yeni mesaj: %s", sender_name(c))
        if new:
            notify_messages(cfg, new)
    return message_state(conversations)


# ----------------------------------------------------------------- ana akis
def run(cfg, once=False):
    client = ObsClient(cfg)
    while True:
        try:
            client.login()
            break
        except (LoginFailed, requests.RequestException) as exc:
            if once:
                raise
            # Aksis kisa surede tekrarlanan girisleri reddedebiliyor; biraz bekleyip tekrar dene
            log.error("%s %d dk sonra tekrar denenecek.", exc, LOGIN_RETRY_SECONDS // 60)
            time.sleep(LOGIN_RETRY_SECONDS)

    known, known_messages = load_state(cfg)
    backoff = cfg.interval
    log.info("Takip basladi: %s-%s %s donemi%s, %d sn aralikla.",
             cfg.yil, int(cfg.yil) + 1, "Guz" if cfg.donem == "1" else "Bahar",
             " + mesajlar" if cfg.check_messages else "", cfg.interval)

    while True:
        try:
            if time.time() - client.last_login > RELOGIN_EVERY_SECONDS:
                log.info("Periyodik yeniden giris yapiliyor...")
                client.login()

            exams = client.fetch_exams()
            if cfg.check_messages:
                known_messages = check_messages(cfg, client, known_messages, once)

            if once:
                print_exams(exams)
                return
            if known is None:
                print_exams(exams)
                log.info("Ilk sorgu tamam, %d sinav kayitli. Degisiklikler izleniyor.", len(exams))
            else:
                changes = find_changes(known, exams)
                if changes:
                    for kind, item, _ in changes:
                        log.info("%s: %s -> %s", kind, describe(item), item.get("Notu", ""))
                    notify_changes(cfg, changes)
                else:
                    log.debug("Degisiklik yok (%d sinav).", len(exams))

            known = exams
            save_state(cfg, exams, known_messages)
            backoff = cfg.interval

        except (SessionExpired, LoginFailed) as exc:
            log.warning("%s Yeniden giris deneniyor...", exc)
            try:
                client.login()
                continue
            except Exception as login_exc:
                log.error("Yeniden giris basarisiz: %s", login_exc)
                backoff = max(LOGIN_RETRY_SECONDS, min(backoff * 2, MAX_BACKOFF_SECONDS))
        except requests.RequestException as exc:
            log.warning("Baglanti hatasi: %s", exc)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        time.sleep(backoff)


def main():
    parser = argparse.ArgumentParser(description="IUC OBS sinav sonucu ve mesaj takibi, mail bildirimi")
    parser.add_argument("--once", action="store_true", help="bir kez sorgula, notlari ve okunmamis mesajlari yazdir, cik")
    parser.add_argument("--test-mail", action="store_true", help="alicilara test maili gonder ve cik")
    parser.add_argument("--yil", help="akademik yil baslangici (orn. 2026 = 2026-2027)")
    parser.add_argument("--donem", choices=["1", "2"], help="1 = Guz, 2 = Bahar")
    parser.add_argument("-v", "--verbose", action="store_true", help="ayrintili log")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    cfg = load_config(args)
    if not cfg.verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if args.test_mail:
        send_mail(cfg, "Sinav takip - test maili",
                  "Bu bir test mailidir. Bunu goruyorsan mail ayarlarin dogru.")
        log.info("Test maili gonderildi: %s", ", ".join(cfg.email_to))
        return

    try:
        run(cfg, once=args.once)
    except KeyboardInterrupt:
        log.info("Durduruldu.")
    except LoginFailed as exc:
        sys.exit(f"HATA: {exc}")


if __name__ == "__main__":
    main()
