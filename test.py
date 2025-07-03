# -*- coding: utf-8 -*-
import requests
import time
import json
import copy
import urllib3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import re

# --- KULLANICI AYARLARI ---
USERNAME = ""  # Aksis Kullanici adi (TC)
PASSWORD = ""  # Sifre
KAUTH_COOKIE = ""  # AKSISAutKH Cookie (telefon dogrulamasi gitmemesi icin. nasil alincagini ogrenmek icin readme.md oku)


EMAIL_ADDRESS = 'mailgonderici@gmail.com'  # Gönderen mail adresi (Google hesabı)
EMAIL_PASSWORD = ''  # Uygulama sifresi (2FA Aktif ederek google ayarlarindan Uygulama Sifreleri kısmından alınıyor. Duz email sifresi degil.)
EMAIL_TO_LIST = ['alici1@gmail.com', 'alici2@gmail.com']  # Alicilar
# --------------------------
# yil ve donem bilgilerini burdan guncelle (or: yil=2024&donem=2 = 2024-2025 Bahar donemi, yil=2024&donem=1 = 2024-2025 Guz donemi)
EXAM_DATA = "sort=&group=DersAdi-asc&filter=&yil=2024&donem=2"

# --- SABIT URL ve HEADERLAR ---
OBS_URL = "https://obs.iuc.edu.tr/"
LOGIN_URL = "https://aksis.iuc.edu.tr/Account/LogOn"
EXAM_URL = "https://obs.iuc.edu.tr/OgrenimBilgileri/SinavSonuclariVeNotlar/GetOgrenciSinavSonuc"
HEADERS = {
    "Host": "obs.iuc.edu.tr",
    "Connection": "keep-alive",
    "sec-ch-ua-platform": '"Windows"',
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "sec-ch-ua": '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "sec-ch-ua-mobile": "?0",
    "Origin": "https://obs.iuc.edu.tr",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Referer": "https://obs.iuc.edu.tr/OgrenimBilgileri/SinavSonuclariVeNotlar/Index",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
}
SMTP_SERVER = 'smtp.gmail.com'
SMTP_PORT = 587
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
# -------------------------------

url = EXAM_URL
headers = HEADERS.copy()
data = EXAM_DATA

session_url = "https://obs.iuc.edu.tr/Data/SessionCookieControl"
session_headers = headers.copy()
session_headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
session_headers["Referer"] = "https://obs.iuc.edu.tr/OgrenimBilgileri/SinavSonuclariVeNotlar/Index"

def get_exam_ids(response_json):
    data = response_json.get("Data", [])
    if not isinstance(data, list):
        return set()
    exam_ids = set()
    for course in data:
        for item in course.get("Items", []):
            exam_ids.add(item["SinavID"])
    return exam_ids

def get_new_exams(old, new):
    old_ids = get_exam_ids(old)
    data = new.get("Data", [])
    if not isinstance(data, list):
        return []
    new_exams = []
    for course in data:
        for item in course.get("Items", []):
            if item["SinavID"] not in old_ids:
                new_exams.append(item)
    return new_exams

def send_exam_mail(exam):
    subject = f"New Exam Announced: {exam.get('DersAdi', '')} - {exam.get('SinavAdi', '')}"
    body = f"""
Course Name: {exam.get('DersAdi', '')}
Exam Name: {exam.get('SinavAdi', '')}
Type: {exam.get('SinavTuru', '')}
Date: {exam.get('SinavTarihiString', '')}
Grade: {exam.get('Notu', '')}
Effect Ratio: {exam.get('EtkiOrani', '')}
Attended: {exam.get('SinavaGirdiMi', '')}
Sinav ID: {exam.get('SinavID', '')}
Can Egrisi (varsa): {OBS_URL}/OgrenimBilgileri/SinavSonuclariVeNotlar/HarfNotDagilimGoster?SinavID={exam.get('SinavID', '')}
"""
    msg = MIMEMultipart()
    msg['From'] = EMAIL_ADDRESS
    msg['To'] = ', '.join(EMAIL_TO_LIST)
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.sendmail(EMAIL_ADDRESS, EMAIL_TO_LIST, msg.as_string())
        print(f"Mail sent: {subject}")
    except Exception as e:
        print(f"Mail could not be sent: {e}")

def print_all_exams(response_json):
    print("Current Exams and Grades:")
    data = response_json.get("Data", [])
    if not isinstance(data, list):
        print("No exam data found or session expired.")
        return
    for course in data:
        for item in course.get("Items", []):
            course_name = item.get('DersAdi', '')
            exam_name = item.get('SinavAdi', '')
            grade = item.get('Notu', '')
            print(f"{course_name} - {exam_name}: {grade}")

def get_verification_token(session):
    login_page_url = LOGIN_URL + "?returnUrl=%2FAccount%2FLogOff"
    resp = session.get(login_page_url, verify=False)
    resp.raise_for_status()
    set_cookie = resp.headers.get('Set-Cookie')
    if set_cookie:
        for part in set_cookie.split(','):
            if 'ASP.NET_SessionId' in part:
                cookie_pair = part.split(';')[0]
                if '=' in cookie_pair:
                    k, v = cookie_pair.split('=', 1)
                    session.cookies.set(k.strip(), v.strip())
    match = re.search(r'name="__RequestVerificationToken" type="hidden" value="([^"]+)"', resp.text)
    if match:
        return match.group(1)
    else:
        raise Exception("Verification token bulunamadi!")

def perform_login(session, username, password):
    token = get_verification_token(session)
    login_url = LOGIN_URL + "?returnUrl=%2FAccount%2FLogOff"
    session.cookies.set("AKSISAutKH", KAUTH_COOKIE)
    login_data = {
        "__RequestVerificationToken": token,
        "UserName": username,
        "Password": password,
        "IpAddr": "",
        "SmsDogrulaModal": "0",
        "SmsDogrulaSecond": "0",
        "PhoneNumber": ""
    }
    login_headers = {
        "Origin": "https://aksis.iuc.edu.tr",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        "Referer": "https://aksis.iuc.edu.tr/Account/LogOn?ReturnUrl=%2fAccount%2fLogOff",
    }
    resp = session.post(login_url, data=login_data, headers=login_headers, allow_redirects=False, verify=False)
    resp.raise_for_status()
    set_cookie = resp.headers.get('Set-Cookie')
    if set_cookie:
        for part in set_cookie.split(','):
            if '.OGRISFormAuth' in part or 'ASP.NET_SessionId' in part:
                cookie_pair = part.split(';')[0]
                if '=' in cookie_pair:
                    k, v = cookie_pair.split('=', 1)
                    session.cookies.set(k.strip(), v.strip())
    print("Login successful, cookies updated.")

def get_obs_session_id(session):
    obs_url = OBS_URL
    resp = session.get(obs_url, verify=False)
    resp.raise_for_status()
    set_cookie = resp.headers.get('Set-Cookie')
    if set_cookie:
        for part in set_cookie.split(','):
            if 'ASP.NET_SessionId' in part:
                cookie_pair = part.split(';')[0]
                if '=' in cookie_pair:
                    k, v = cookie_pair.split('=', 1)
                    session.cookies.set(k.strip(), v.strip())
    print("OBS ASP.NET_SessionId alindi ve session'a eklendi.")

print("Sorgu baslatildi, yeni sinav aciklaninca bildirim gonderilecek.")

session = requests.Session()
get_obs_session_id(session) 
perform_login(session, USERNAME, PASSWORD)  
headers_no_cookie = headers.copy()
headers_no_cookie.pop("Cookie", None)
session_headers_no_cookie = session_headers.copy()
session_headers_no_cookie.pop("Cookie", None)

prev_response = None
login_refresh_counter = 0
while True:
    try:
        resp = session.post(EXAM_URL, headers=headers_no_cookie, data=EXAM_DATA, verify=False)
        resp.raise_for_status()
        current_json = resp.json()
        if (
            isinstance(current_json, dict)
            and current_json.get("Errors")
            and "tirilemedi" in str(current_json["Errors"])
        ):
            print("OBS ASP.NET_SessionId expired, refreshing...")
            get_obs_session_id(session)
            continue  
        if prev_response is not None:
            new_exams = get_new_exams(prev_response, current_json)
            if new_exams:
                print("New exam(s) announced!")
                for exam in new_exams:
                    print(json.dumps(exam, ensure_ascii=False, indent=2))
                    send_exam_mail(exam)
            else:
                print_all_exams(current_json)
                print("No new exam.")
                print(login_refresh_counter)
        else:
            print_all_exams(current_json)
            print("First query done, monitoring started.")
        prev_response = copy.deepcopy(current_json)
        login_refresh_counter += 1
        # 20 saatte bir login cookie'sini yenile (20*60*60/30 = 2400 dongude bir.)
        if login_refresh_counter >= 2400:
            print("20 saat doldu, login cookie'si yenileniyor...")
            perform_login(session, USERNAME, PASSWORD)
            login_refresh_counter = 0
    except Exception as e:
        print(f"Error occurred: {e}")
        if any(err in str(e) for err in ["Oturum suresi doldu", "401", "403"]):
            print("Session expired, logging in again...")
            perform_login(session, USERNAME, PASSWORD)
    time.sleep(30)
