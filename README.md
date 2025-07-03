# OBS Sınav Bildirim Scripti

## Gerekli Ayarlar

## Önemli: Muhtemelen dönem ayarını değiştirmen gerekecek çünkü burayı sürekli güncellemeyeceğim. en sonu oku.

### 1. Gmail SMTP Uygulama Şifresi Nasıl Alınır?

- Google hesabında **2 Adımlı Doğrulama (2FA)** aktif olmalı.
- Google hesabına giriş yap.
- Sağ üstten **Google Hesabım** → **Güvenlik** → **Uygulama Şifreleri** bölümüne git.
- **Uygulama Şifresi Oluştur** seçeneğinden bir şifre oluştur.
- Oluşan şifreyi kodda `EMAIL_PASSWORD` değişkenine yapıştır.
- **Not:** Düz mail şifresi ile çalışmaz, mutlaka uygulama şifresi olmalı!

### 2. KAUTH_COOKIE (AKSISAutKH) Nasıl Alınır?

- [https://aksis.iuc.edu.tr](https://aksis.iuc.edu.tr) adresine tarayıcıdan giriş yap.
- Telefon doğrulaması aktif edilmiş olmalı (SMS ile giriş yap).
- Giriş yaptıktan sonra **F12** ile geliştirici araçlarını aç.
- **Application** (veya bazı tarayıcılarda **Depolama**) sekmesine geç.
- Sol menüden **Cookies** → **https://aksis.iuc.edu.tr** seç.
- Sağda **AKSISAutKH** isimli cookie’yi bul, değeri kopyala.
- Bunu kodda `KAUTH_COOKIE` değişkenine yapıştır.

---

## Diğer Ayarlar

- Kullanıcı adı, şifre, mail adresleri ve alıcıları kodun en üstünden düzenleyebilirsin. Alıcı ekleyip/silebilirsin.
- Dönem/yıl ayarını `EXAM_DATA` değişkeninden değiştirebilirsin.

---
