# -*- coding: utf-8 -*-
"""
IUC OBS Sinav Takip

OBS'deki sinav sonuclarini, hocalardan gelen mesajlari (Yazismalarim) ve
Dokuman Paylasimi'ndaki dosyalari periyodik olarak kontrol eder; yeni bir
sinav aciklandiginda, bir not degistiginde, yeni mesaj geldiginde veya
yeni dokuman paylasildiginda e-posta gonderir.

Kullanim:
    python sinav_takip.py                 # surekli takip
    python sinav_takip.py --once          # bir kez sorgula, notlari yazdir, cik
    python sinav_takip.py --test-mail     # sadece test maili gonder
    python sinav_takip.py --yil 2025 --donem 2 --once   # baska donemi goruntule

Ayarlar .env dosyasindan okunur (bkz. .env.example).

Giris: Aksis artik teknokta.iuc.edu.tr kimlik sunucusu (OpenID Connect) uzerinden
giris yapiyor ve giris formunda robot dogrulamasi var. Script sifreyle giris
YAPMAZ; tarayicida bir kez kendin giris yaptiktan sonra teknokta'nin oturum
cookie'sini .env'e (IDS_COOKIE) koyarsin, script Aksis/OBS oturumunu bununla
acar. Oturum biterse mail atar; .env'deki cookie'yi guncellemen yeterli,
script'i yeniden baslatman gerekmez.
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
IDS_STATE_PATH = BASE_DIR / ".oturum.json"  # yenilenen kimlik cookie'leri (gitignore'da)

OBS_URL = "https://obs.iuc.edu.tr"
IDS_HOST = "teknokta.iuc.edu.tr"

AKSIS_LOGIN_URL = "https://aksis.iuc.edu.tr/giris/idslogin"
EXAM_URL = f"{OBS_URL}/OgrenimBilgileri/SinavSonuclariVeNotlar/GetOgrenciSinavSonuc"
DISTRIBUTION_URL = f"{OBS_URL}/OgrenimBilgileri/SinavSonuclariVeNotlar/HarfNotDagilimGoster?SinavID="
CHAT_PAGE_URL = f"{OBS_URL}/Genel/Chat2"
MESSAGES_API_URL = f"{OBS_URL}/api/Yazisma/GetirYazismalar"
DOCS_PAGE_URL = f"{OBS_URL}/Dokuman/Dokuman/Index"
DOCS_COURSES_URL = f"{OBS_URL}/Dokuman/Dokuman/DersListesi"
DOCS_LIST_URL = f"{OBS_URL}/Dokuman/Dokuman/DersIzlence"
DOCS_CHECK_EVERY_SECONDS = 5 * 60  # dokumanlar icin daha seyrek kontrol yeterli

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

MAX_BACKOFF_SECONDS = 10 * 60
LOGIN_RETRY_SECONDS = 5 * 60

log = logging.getLogger("sinav_takip")


# ----------------------------------------------------------------- ayarlar
@dataclass
class Config:
    email_address: str
    email_password: str
    email_to: list
    yil: str
    donem: str
    interval: int
    verify_ssl: bool
    check_messages: bool
    check_documents: bool
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


def read_env_value(key, path=ENV_PATH):
    """.env'i her seferinde yeniden okur; boylece calisirken guncellenen cookie hemen kullanilir."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    for line in lines:
        line = line.strip()
        if line.startswith(key) and "=" in line and line.split("=", 1)[0].strip() == key:
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def parse_cookie_string(raw):
    """'idsrv=...; idsrv.session=...' ya da sadece idsrv degeri kabul eder."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if "=" not in raw.split(";")[0]:
        return {"idsrv": raw}
    cookies = {}
    for part in raw.split(";"):
        if "=" in part:
            name, value = part.split("=", 1)
            cookies[name.strip()] = value.strip()
    return cookies


def load_ids_cookies():
    """.env'deki cookie degismediyse, sunucunun en son yeniledigi halini kullan."""
    env_raw = read_env_value("IDS_COOKIE")
    try:
        saved = json.loads(IDS_STATE_PATH.read_text(encoding="utf-8"))
        if saved.get("env") == env_raw and saved.get("cookies"):
            return env_raw, saved["cookies"]
    except (FileNotFoundError, ValueError):
        pass
    return env_raw, parse_cookie_string(env_raw)


def save_ids_cookies(env_raw, cookies):
    IDS_STATE_PATH.write_text(json.dumps({"env": env_raw, "cookies": cookies}, indent=2), encoding="utf-8")


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
        email_address=env("EMAIL_ADDRESS", ""),
        email_password=env("EMAIL_PASSWORD", "").replace(" ", ""),
        email_to=[e.strip() for e in env("EMAIL_TO_LIST", "").split(",") if e.strip()],
        yil=args.yil or env("YIL") or old_yil or "2026",
        donem=args.donem or env("DONEM") or old_donem or "1",
        interval=int(env("CHECK_INTERVAL", "30")),
        verify_ssl=env("VERIFY_SSL", "false").lower() in ("1", "true", "yes"),
        check_messages=env("CHECK_MESSAGES", "true").lower() in ("1", "true", "yes"),
        check_documents=env("CHECK_DOCUMENTS", "true").lower() in ("1", "true", "yes"),
    )
    missing = [name for name, value in [
        ("IDS_COOKIE", read_env_value("IDS_COOKIE")),
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


class IdsSessionExpired(LoginFailed):
    """teknokta kimlik oturumu bitmis; kullanicinin tarayicidan tekrar girmesi gerekiyor."""


class ObsClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.session.verify = cfg.verify_ssl
        self.session.hooks["response"].append(lambda resp, *a, **kw: self._adopt_cookies(resp))
        self.last_login = 0.0
        self.chat_token = None
        self.env_raw = ""

    def _set_cookie(self, name, value):
        # Ayni isimde tek cookie kalsin (farkli alan adlarindaki kopyalar eski deger gondermesin)
        for c in [c for c in self.session.cookies if c.name == name]:
            self.session.cookies.clear(c.domain, c.path, c.name)
        self.session.cookies.set(name, value, domain=".iuc.edu.tr", path="/")

    def _adopt_cookies(self, resp):
        # Sunucu bir cookie'yi yenilediyse (or. .OGRISFormAuth) tum iuc.edu.tr alt alanlarinda gecerli yap
        for cookie in resp.cookies:
            self._set_cookie(cookie.name, cookie.value)

    def save_session(self):
        """Guncel oturum cookie'lerini diske yaz; script yeniden baslarsa kaldigi oturumdan devam eder."""
        current = {c.name: c.value for c in self.session.cookies
                   if (c.domain or "").endswith("iuc.edu.tr") and not c.name.startswith("_ga")}
        if current:
            save_ids_cookies(self.env_raw, current)

    def refresh_obs_session(self):
        resp = self.session.get(OBS_URL + "/", timeout=30)
        resp.raise_for_status()
        self._adopt_cookies(resp)
        log.debug("OBS oturumu yenilendi.")

    def _submit_auto_post_form(self, resp):
        """Kimlik sunucusu form_post kullanirsa otomatik gonderilen formu biz gonderiyoruz."""
        form = re.search(r'<form[^>]*method=["\']post["\'][^>]*action=["\']([^"\']+)["\'][^>]*>(.*?)</form>',
                         resp.text, re.S | re.I)
        if not form or 'type="password"' in resp.text:
            return None
        fields = dict(re.findall(r'<input[^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']',
                                 form.group(2), re.I))
        if "code" not in fields and "id_token" not in fields:
            return None
        action = requests.compat.urljoin(resp.url, form.group(1).replace("&amp;", "&"))
        return self.session.post(action, data=fields, timeout=30)

    def _obs_logged_in(self):
        resp = self.session.get(OBS_URL + "/", timeout=30)
        resp.raise_for_status()
        self._adopt_cookies(resp)
        return "/Dokuman/Dokuman/Index" in resp.text or "/Genel/Chat2" in resp.text

    def login(self):
        """Kayitli oturum cookie'leriyle OBS'ye girer; gerekirse Aksis uzerinden oturumu tazeler."""
        env_raw, cookies = load_ids_cookies()
        if not cookies:
            raise IdsSessionExpired(".env'de IDS_COOKIE bos.")

        self.env_raw = env_raw
        self.session.cookies.clear()
        for name, value in cookies.items():
            self._set_cookie(name, value)

        ok = self._obs_logged_in()
        if not ok:
            # OBS cookie'si dusmus olabilir; Aksis oturumu (veya teknokta) uzerinden yenile
            log.debug("OBS oturumu yok, Aksis uzerinden yenileniyor...")
            resp = self.session.get(AKSIS_LOGIN_URL, timeout=30)
            for _ in range(3):
                next_resp = self._submit_auto_post_form(resp)
                if next_resp is None:
                    break
                resp = next_resp
            resp.raise_for_status()
            if requests.utils.urlparse(resp.url).hostname == IDS_HOST:
                raise IdsSessionExpired("Aksis oturumu bitmis, kimlik sunucusu giris istiyor.")
            ok = self._obs_logged_in()
        if not ok:
            raise IdsSessionExpired("OBS oturumu acilamadi (cookie'lerin suresi dolmus olabilir).")

        self.save_session()

        self.chat_token = None
        self.last_login = time.time()
        log.info("OBS girisi basarili.")

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

    def _post_json(self, url, body):
        resp = self.session.post(url, json=body, timeout=30, headers={
            "Accept": "application/json, text/plain, */*", "Referer": DOCS_PAGE_URL, "Origin": OBS_URL})
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            raise SessionExpired("Dokuman Paylasimi JSON yerine HTML dondu (oturum dusmus).")

    def fetch_documents(self):
        """Bu donemin derslerindeki paylasilan dokumanlar: {dokumanID: dokuman}."""
        courses = self._post_json(DOCS_COURSES_URL, {"yil": int(self.cfg.yil), "donem": int(self.cfg.donem)})
        if not isinstance(courses, list):
            raise SessionExpired(f"Beklenmeyen ders listesi cevabi: {courses!r:.200}")
        documents = {}
        for course in courses:
            docs = self._post_json(DOCS_LIST_URL, {"oGRDersGrupID": course.get("OGRDersGrupID"),
                                                   "birimID": course.get("BirimID")})
            for doc in docs if isinstance(docs, list) else []:
                doc = dict(doc, DersAdi=course.get("DersAdi", ""))
                documents[str(doc.get("ID"))] = doc
        return documents


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


def find_new_documents(old, new):
    return [doc for key, doc in new.items() if key not in old]


def document_title(doc):
    return (doc.get("FileName") or doc.get("Konu") or doc.get("DokumanTuru") or "dokuman").strip()


def print_documents(documents):
    log.info("Dokuman Paylasimi: %d dokuman.", len(documents))
    for doc in sorted(documents.values(), key=lambda d: (d.get("DersAdi", ""), str(d.get("Hafta", "")))):
        hafta = f"Hafta {doc['Hafta']} - " if doc.get("Hafta") else ""
        print(f"   {doc.get('DersAdi', '').strip()}: {hafta}{document_title(doc)}")


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
    """(sinavlar, mesajlar, dokumanlar) dondurur; ilk calistirmada None olurlar."""
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None, None, None
    exams, documents = state.get("exams"), state.get("documents")
    if state.get("yil") != cfg.yil or state.get("donem") != cfg.donem:
        exams = documents = None  # donem degismis, sifirdan basla
    return exams, state.get("messages"), documents


def save_state(cfg, exams, messages, documents):
    STATE_PATH.write_text(
        json.dumps({"yil": cfg.yil, "donem": cfg.donem, "exams": exams, "messages": messages,
                    "documents": documents}, ensure_ascii=False, indent=2),
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


def notify_documents(cfg, docs):
    if len(docs) == 1:
        subject = f"Yeni dokuman: {docs[0].get('DersAdi', '').strip()} - {document_title(docs[0])}"
    else:
        subject = f"{len(docs)} yeni dokuman paylasildi"
    blocks = []
    for doc in docs:
        lines = [
            f"Ders     : {doc.get('DersAdi', '').strip()}",
            f"Dosya    : {document_title(doc)}",
            f"Tur      : {doc.get('DokumanTuru', '')}",
        ]
        for label, key in (("Hafta", "Hafta"), ("Konu", "Konu"), ("Aciklama", "Aciklama"),
                           ("Tarih", "DokumanTarih"), ("Odev son", "OdevBitisTarihi")):
            if doc.get(key):
                lines.append(f"{label:<9}: {doc[key]}")
        blocks.append("\n".join(lines))
    body = "\n\n".join(blocks) + f"\n\nDokuman Paylasimi: {DOCS_PAGE_URL}"
    try:
        send_mail(cfg, subject, body)
        log.info("Mail gonderildi: %s", subject)
    except Exception as exc:
        log.error("Mail gonderilemedi: %s", exc)


def check_documents(cfg, client, known, once):
    documents = client.fetch_documents()
    if once:
        print_documents(documents)
    elif known is None:
        log.info("Ilk dokuman sorgusu tamam, %d dokuman kayitli.", len(documents))
    else:
        new = find_new_documents(known, documents)
        for doc in new:
            log.info("Yeni dokuman: %s - %s", doc.get("DersAdi", "").strip(), document_title(doc))
        if new:
            notify_documents(cfg, new)
    # Sadece ID'leri ve mailde lazim olan alanlari sakla
    return {k: {f: d.get(f) for f in ("DersAdi", "FileName", "Konu", "Hafta")} for k, d in documents.items()}


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


def notify_session_expired(cfg, exc):
    body = (f"Sinav takip script'i OBS'ye giris yapamiyor: {exc}\n\n"
            "Yapman gereken:\n"
            "1. Tarayicida https://aksis.iuc.edu.tr/giris/main adresinden giris yap (Google ile olabilir).\n"
            "2. F12 > Network > sayfayi yenile > aksis.iuc.edu.tr istegine tikla > Request Headers > "
            "'cookie' degerinin tamamini kopyala.\n"
            "3. .env dosyasindaki IDS_COOKIE= satirina yapistir ve kaydet.\n\n"
            "Script'i yeniden baslatmana gerek yok, birkac dakika icinde kendisi tekrar dener.")
    try:
        send_mail(cfg, "Sinav takip: OBS oturumu bitti, tekrar giris gerekiyor", body)
        log.info("Oturum bitti maili gonderildi.")
    except Exception as mail_exc:
        log.error("Oturum bitti maili gonderilemedi: %s", mail_exc)


# ----------------------------------------------------------------- ana akis
def run(cfg, once=False):
    client = ObsClient(cfg)
    expiry_notified = False

    def try_login():
        nonlocal expiry_notified
        try:
            client.login()
            expiry_notified = False
            return True
        except IdsSessionExpired as exc:
            if once:
                raise
            log.error("%s Tarayicidan tekrar giris yapip .env'deki IDS_COOKIE'yi guncelle. "
                      "%d dk sonra tekrar denenecek.", exc, LOGIN_RETRY_SECONDS // 60)
            if not expiry_notified:
                notify_session_expired(cfg, exc)
                expiry_notified = True
        except (LoginFailed, requests.RequestException) as exc:
            if once:
                raise
            log.error("Giris basarisiz: %s %d dk sonra tekrar denenecek.", exc, LOGIN_RETRY_SECONDS // 60)
        return False

    while not try_login():
        time.sleep(LOGIN_RETRY_SECONDS)

    known, known_messages, known_documents = load_state(cfg)
    last_docs_check = 0.0
    check_count = 0
    backoff = cfg.interval
    extras = [name for name, on in (("mesajlar", cfg.check_messages), ("dokumanlar", cfg.check_documents)) if on]
    log.info("Takip basladi: %s-%s %s donemi%s, %d sn aralikla.",
             cfg.yil, int(cfg.yil) + 1, "Guz" if cfg.donem == "1" else "Bahar",
             "".join(" + " + e for e in extras), cfg.interval)

    while True:
        try:
            exams = client.fetch_exams()
            if cfg.check_messages:
                known_messages = check_messages(cfg, client, known_messages, once)
            if cfg.check_documents and (once or time.time() - last_docs_check >= DOCS_CHECK_EVERY_SECONDS):
                known_documents = check_documents(cfg, client, known_documents, once)
                last_docs_check = time.time()

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
            known = exams
            save_state(cfg, exams, known_messages, known_documents)
            client.save_session()
            backoff = cfg.interval

            check_count += 1
            summary = [f"{len(exams)} sinav"]
            if cfg.check_messages:
                summary.append(f"{len(known_messages or {})} konusma")
            if cfg.check_documents:
                summary.append(f"{len(known_documents or {})} dokuman")
            log.info("Kontrol #%d basarili (%s). Sonraki kontrol %d sn sonra.",
                     check_count, ", ".join(summary), cfg.interval)

        except (SessionExpired, LoginFailed) as exc:
            log.warning("%s Yeniden giris deneniyor...", exc)
            if try_login():
                continue
            backoff = LOGIN_RETRY_SECONDS
        except requests.RequestException as exc:
            log.warning("Baglanti hatasi: %s", exc)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        time.sleep(backoff)


def main():
    parser = argparse.ArgumentParser(description="IUC OBS sinav sonucu, mesaj ve dokuman takibi, mail bildirimi")
    parser.add_argument("--once", action="store_true", help="bir kez sorgula; notlari, okunmamis mesajlari ve dokumanlari yazdir, cik")
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
