# OBS Sınav Bildirim Scripti

OBS'deki sınav sonuçlarını, hocalardan gelen mesajları (Yazışmalarım) ve Doküman Paylaşımı'ndaki dosyaları takip eder; yeni bir sınav açıklandığında, bir not değiştiğinde, hocadan yeni mesaj geldiğinde veya yeni doküman paylaşıldığında mail atar.

## Kurulum

```bash
pip install requests
cp .env.example .env   # sonra .env'i doldur (aşağıya bak)
```

## Kullanım

```bash
python sinav_takip.py                         # sürekli takip
python sinav_takip.py --once                  # bir kez sorgula, notları yazdır
python sinav_takip.py --test-mail             # mail ayarlarını test et
python sinav_takip.py --yil 2025 --donem 2 --once   # başka dönemi görüntüle
```

## Gerekli Ayarlar (.env)

Tüm ayarlar `.env` dosyasında. Bu dosya `.gitignore`'da, GitHub'a gitmez.

**Önemli:** Dönem ayarını (`YIL`, `DONEM`) her dönem güncellemen gerekiyor. `YIL=2026 DONEM=1` = 2026-2027 Güz, `DONEM=2` = Bahar.

### 1. Gmail SMTP Uygulama Şifresi Nasıl Alınır?

- Google hesabında **2 Adımlı Doğrulama (2FA)** aktif olmalı.
- Google hesabına giriş yap.
- Sağ üstten **Google Hesabım** → **Güvenlik** → **Uygulama Şifreleri** bölümüne git.
- **Uygulama Şifresi Oluştur** seçeneğinden bir şifre oluştur.
- Oluşan şifreyi `.env`'de `EMAIL_PASSWORD` alanına yapıştır (gönderen hesabın şifresi).
- **Not:** Düz mail şifresi ile çalışmaz, mutlaka uygulama şifresi olmalı!

### 2. KAUTH_COOKIE (AKSISAutKH) Nasıl Alınır?

- [https://aksis.iuc.edu.tr](https://aksis.iuc.edu.tr) adresine tarayıcıdan giriş yap.
- Telefon doğrulaması aktif edilmiş olmalı (SMS ile giriş yap).
- Giriş yaptıktan sonra **F12** ile geliştirici araçlarını aç.
- **Application** (veya bazı tarayıcılarda **Depolama**) sekmesine geç.
- Sol menüden **Cookies** → **https://aksis.iuc.edu.tr** seç.
- Sağda **AKSISAutKH** isimli cookie’yi bul, değeri kopyala.
- Bunu `.env`'de `KAUTH_COOKIE` alanına yapıştır.

---

## Diğer Ayarlar

- Alıcıları `EMAIL_TO_LIST`'e virgülle ayırarak yaz.
- `CHECK_MESSAGES`: mesaj takibini açar/kapatır (varsayılan `true`).
- `CHECK_DOCUMENTS`: doküman takibini açar/kapatır (varsayılan `true`, 5 dakikada bir kontrol edilir).
- `CHECK_INTERVAL`: kaç saniyede bir kontrol edileceği (varsayılan 30).
- Script son durumu `sinav_durumu.json`'a kaydeder; kapatıp açınca eski notlar için tekrar mail atmaz.
