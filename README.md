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

### 2. Oturum Cookie'si (IDS_COOKIE) Nasıl Alınır?

Aksis artık `teknokta.iuc.edu.tr` kimlik sunucusu üzerinden giriş yapıyor ve giriş formunda robot doğrulaması var. Bu yüzden script şifreyle giriş **yapmaz**; senin tarayıcıda açtığın oturumu kullanır.

- [https://aksis.iuc.edu.tr/giris/main](https://aksis.iuc.edu.tr/giris/main) adresinden giriş yap (Google ile de olur).
- **F12** → **Network** sekmesi → sayfayı yenile → `aksis.iuc.edu.tr` isteğine tıkla.
- **Request Headers** altındaki `cookie` değerinin tamamını kopyala.
- `.env`'de `IDS_COOKIE=` satırına yapıştır (ya da `sinav_takip.py`'deki `HARDCODED_COOKIES`'e).

Script her kontrolde oturumu kullandığı için oturum canlı kalır ve sunucunun yenilediği cookie'yi `.oturum.json`'a kaydeder. Oturum biterse sana mail atar; yeni cookie'yi yapıştırman yeterli.

---

## Diğer Ayarlar

- Alıcıları `EMAIL_TO_LIST`'e virgülle ayırarak yaz.
- `CHECK_MESSAGES`: mesaj takibini açar/kapatır (varsayılan `true`).
- `CHECK_DOCUMENTS`: doküman takibini açar/kapatır (varsayılan `true`, 5 dakikada bir kontrol edilir).
- `CHECK_INTERVAL`: kaç saniyede bir kontrol edileceği (varsayılan 30).
- Script son durumu `sinav_durumu.json`'a kaydeder; kapatıp açınca eski notlar için tekrar mail atmaz.
