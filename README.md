# Kampanya Avcısı ✈️

Finnair, SAS, Ryanair, airBaltic, Pegasus, THY ve Lufthansa'nın kampanyalarını 15 dakikada bir kontrol eder.
Yeni bir kampanya çıkınca Telegram'dan mesaj atar. GitHub'ın ücretsiz sunucularında çalışır,
bilgisayarın açık olması gerekmez.

## Neleri izliyor?

| Havayolu | Nereden |
|---|---|
| Ryanair | Ana sayfadaki kampanya kutucukları (Flash Sale, Black Friday vb.), görseliyle birlikte |
| airBaltic | Kampanya bannerları, "Sale ends in" indirim sayacı, tanıtım kutuları |
| Finnair | "Today's offers" / "In the spotlight" kutuları, Special offers sayfası, indirim kodu sayfası |
| Pegasus | Kampanyalı uçak biletleri sayfası (iş birliği kampanyaları hariç) |
| SAS, THY, Lufthansa + hepsi | Gmail'e gelen bülten mailleri (bu üç havayolunun siteleri otomatik erişimi engellediği için onları bu yakalar) |
| Hepsi | fly4free fırsat sitesinde bu havayollarının adı geçen ilanlar |

İlk çalıştığında o an görünen kampanyaları kaydeder ve tek bir "devrede" mesajı atar.
Sonra sadece **yeni** çıkanları bildirir. Her pazartesi sabahı "çalışıyorum" mesajı gelir.
Bir site 2 saat boyunca okunamazsa uyarı gelir.

## Kurulum

### 1. Telegram botu

1. Telegram'da **@BotFather**'a yazın, `/newbot` gönderin, bota bir ad verin.
   Size bir **token** verecek (`123456:ABC...` gibi). Daha önce kurduğunuz bir botu da kullanabilirsiniz.
2. Yeni botunuzla sohbeti açıp **Start**'a basın (ya da herhangi bir mesaj yazın).
3. Tarayıcıda şu adresi açın (TOKEN yerine kendi token'ınız):
   `https://api.telegram.org/botTOKEN/getUpdates`
   Çıkan yazıda `"chat":{"id":123456789` kısmındaki sayı sizin **chat id**'nizdir.

### 2. Gmail uygulama şifresi (bülten takibi için)

1. Google hesabınızda 2 adımlı doğrulama açık olmalı.
2. https://myaccount.google.com/apppasswords adresinde "Kampanya Avcısı" adıyla bir uygulama şifresi oluşturun.
   16 harflik şifreyi bir yere not edin.
3. Program mailleri sadece **okur**. Okundu olarak işaretlemez, silmez, göndermez.

### 3. GitHub'a gizli bilgileri girin

Depoda **Settings → Secrets and variables → Actions → New repository secret** ile şunları ekleyin:

| Ad | Değer |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather'ın verdiği token |
| `TELEGRAM_CHAT_ID` | getUpdates'ten aldığınız sayı |
| `GMAIL_USER` | Gmail adresiniz (isteğe bağlı) |
| `GMAIL_APP_PASSWORD` | 16 harflik uygulama şifresi (isteğe bağlı) |

Bu bilgiler şifreli saklanır. Kod ve kayıtlar herkese açık olsa da kimse göremez.

### 4. Test edin

**Actions → Kampanya Avcısı → Run workflow** menüsünde "Sadece Telegram/Gmail bağlantı testi yap" kutusunu
işaretleyip çalıştırın. Telegram'a test mesajı gelmeli. Sonra kutuyu işaretlemeden bir kez daha çalıştırın:
"Kampanya Avcısı devrede!" mesajı gelir. Bundan sonra her şey otomatik çalışır.

### 5. Bültenlere abone olun

Bülten takibinin işe yaraması için havayollarının e-bültenine Gmail adresinizle abone olun:
Finnair, SAS, Ryanair, airBaltic ve Pegasus sitelerinde e-posta kayıt kutusu var. THY (Miles&Smiles) ve
Lufthansa (Miles & More) için hesabınızdaki iletişim tercihlerinde e-posta kampanya izninin açık olması gerekir.
Kampanyalar çoğu zaman önce bülten abonelerine gidiyor.

## Durdurmak / ayarlamak

- **Durdurmak:** Actions → Kampanya Avcısı → sağ üstteki "…" → *Disable workflow*.
- **Ayarlar:** `watcher.py` dosyasının en üstündeki "Ayarlar" bölümü (izlenen Ryanair ülkeleri, mail filtreleri vb.).
- GitHub yoğun saatlerde zamanlanmış çalışmaları birkaç dakika geciktirebilir.
- Bir havayolu sitesinin yapısını değiştirirse o kaynak için uyarı mesajı gelir. Kodda küçük bir düzeltme gerekir.
