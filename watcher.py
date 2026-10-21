#!/usr/bin/env python3
"""Kampanya Avcısı

Finnair, SAS, Ryanair, airBaltic, Pegasus, THY ve Lufthansa'nın kampanyalarını izler; yeni bir kampanya
görünce Telegram'dan haber verir. GitHub Actions'ta 15 dakikada bir çalışır ve daha önce
gördüklerini state.json dosyasında hatırlar.

Yerel deneme (Telegram'a göndermeden):  python watcher.py --dry-run --state deneme_state.json
Bağlantı testi:                          python watcher.py --test
"""

import argparse
import email
import email.header
import email.utils
import hashlib
import html
import imaplib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Ayarlar
# ---------------------------------------------------------------------------

# Ryanair'in hangi ülke sitelerindeki kampanya kutucuklarına bakılacağı
RYANAIR_MARKETS = ["en-gb", "en-ee", "en-lv", "en-fi"]
# Ryanair kutucuklarından sadece adresi bunlardan birini içerenler uçuş kampanyası sayılır
# (araç kiralama, otel, seyahat kuralları gibi kutucuklar atlanır)
RYANAIR_PROMO = re.compile(r"promo|sale|deal|discount|offer|cheap|black-friday|cyber", re.I)

# Pegasus kampanya sayfası. Türkçe sayfa hem yurt içi hem yurt dışı kampanyaları içeriyor;
# İngilizce sayfa aynı kampanyaları farklı kimlikle yayınladığı için çift bildirim olmasın diye eklenmedi.
PEGASUS_PAGE = "https://www.flypgs.com/kampanyali-ucak-biletleri"
# Bilet indirimi olmayan BolBol ortaklık kampanyaları atlanır
PEGASUS_SKIP = re.compile(r"iş birliği|partnership|bolbol oyna", re.I)

# THY (yanıt vermiyor) ve Lufthansa (GitHub sunucularına 403 veriyor) siteleri otomatik erişime kapalı;
# bu iki havayolu sadece bültenlerden ve fırsat sitelerinden izlenir

# Fırsat sitelerinin RSS akışları: başlığında takip edilen havayollarından biri geçen ilanlar bildirilir
DEAL_FEEDS = {"fly4free": "https://www.fly4free.com/feed/"}

# Gmail'de bülten aranacak gönderenler (adreste geçen parça -> havayolu adı)
MAIL_SENDERS = {
    "finnair": "Finnair",
    "flysas": "SAS",
    "sas.se": "SAS",
    "sas.dk": "SAS",
    "sas.no": "SAS",
    "ryanair": "Ryanair",
    "airbaltic": "airBaltic",
    "flypgs": "Pegasus",
    "turkishairlines": "THY",
    "lufthansa": "Lufthansa",
    "milesandmore": "Lufthansa",
}
# Konusu bunlardan birini içeren mailler (rezervasyon, check-in, hesap işlemleri vb.) kampanya sayılmaz
MAIL_SKIP = re.compile(
    r"booking|reservation|confirm|check-?in|boarding|receipt|invoice|itinerary|password|verif|"
    r"account|registration|security|log ?in|sign ?in|hesabınız|bilet bilgi|elektronik bilet|"
    r"balance|bakiye|hesap özet|[iİ]şlem bilgi|bilet detay|travel id|ticari elektronik ileti|"
    r"e-?ticket|refund|rezervasyon|onay|fatura|biniş|şifre|iade|varaus|kuitti|vahvist|lähtöselvitys",
    re.I,
)

# airBaltic ve Finnair'in tanıtım kutularından sadece bu kelimeleri içerenler kampanya sayılır
CAMPAIGN_WORDS = re.compile(
    r"sale|discount|offer|deal|promo|code|bonus|double|\bwin\b|black friday|cyber|limited|cheap|price|€|\d\s?%|from \d",
    re.I,
)

FAIL_ALERT_AFTER = 8  # bir kaynak art arda bu kadar okunamazsa (15 dk x 8 = 2 saat) uyarı gelir
FORGET_AFTER_DAYS = 180  # bu kadar gündür görünmeyen kampanyalar unutulur
REAPPEAR_AFTER_DAYS = 7  # bu kadar gün görünmeyip aynı adla geri gelen kampanya yeniden bildirilir
MAX_MESSAGES = 15  # tek çalışmada gönderilecek en fazla mesaj
REQUEST_TIMEOUT = 20  # tek bir istekte en fazla kaç saniye beklenir
SOURCE_TIME_LIMIT = 60  # bir kaynak toplamda en fazla kaç saniye sürebilir; takılan site diğerlerini bekletmesin

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
DRY_RUN = False
SOURCE_DEADLINE = None  # o an okunan kaynağın bitmesi gereken an (time.monotonic)


@dataclass
class Item:
    id: str
    airline: str
    title: str
    text: str = ""
    url: str = ""
    image: str = ""
    kind: str = "site"  # site | mail | feed


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------


def now_utc():
    return datetime.now(timezone.utc)


def sha(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def clean(text, limit=300):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fetch(url, accept="text/html,application/json;q=0.9,*/*;q=0.8", raw=False):
    if SOURCE_DEADLINE and time.monotonic() > SOURCE_DEADLINE:
        raise TimeoutError(f"kaynak {SOURCE_TIME_LIMIT} saniyede tamamlanamadı (site yavaş ya da cevap vermiyor)")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        body = resp.read()
    return body if raw else body.decode("utf-8", "replace")


def fetch_json(url):
    return json.loads(fetch(url, "application/json"))


def airline_in(text):
    names = {"finnair": "Finnair", "ryanair": "Ryanair", "airbaltic": "airBaltic", "pegasus": "Pegasus",
             "turkish airlines": "THY", "lufthansa": "Lufthansa"}
    match = re.search(r"\b(finnair|ryanair|airbaltic|pegasus|turkish airlines|lufthansa)\b", text or "", re.I)
    if match:
        return names[match.group(1).lower()]
    match = re.search(r"\b(SAS|THY)\b", text or "")  # kısaltmalar sadece büyük harfle yazılmışsa sayılır
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Kaynaklar: her biri o an görünen kampanyaların listesini döndürür
# ---------------------------------------------------------------------------


def ryanair():
    items, errors = {}, []
    for market in RYANAIR_MARKETS:
        try:
            data = fetch_json(f"https://www.ryanair.com/ryanair3/data/home-page/desktop.{market}.json")
        except Exception as exc:
            errors.append(f"{market}: {exc}")
            continue
        for comp in data.get("components", []):
            if "deals-sheet" not in comp.get("type", ""):
                continue
            for tile in comp.get("tiles", []) + comp.get("offersTiles", []):
                url = tile.get("url") or ""
                parsed = urllib.parse.urlparse(url)
                promo = tile.get("promotionName") or ""
                if parsed.netloc != "www.ryanair.com" or not RYANAIR_PROMO.search(f"{parsed.path} {promo}"):
                    continue
                # Aynı kampanya her ülkede farklı adla çıkıyor (FS211026, F211026): ülke ve harfler atılıp birleştirilir
                path = re.sub(r"^/[a-z]{2}/[a-z]{2}(?=/)", "", parsed.path)
                key = f"ryanair:{path}:{re.sub(r'[^0-9]', '', promo) or promo}"
                slug = path.rstrip("/").rsplit("/", 1)[-1]
                title = slug.replace("-", " ").title() if slug else promo
                image = (tile.get("image") or {}).get("url") or ""
                items.setdefault(key, Item(key, "Ryanair", title, "", url, image))
    if len(errors) == len(RYANAIR_MARKETS):
        raise RuntimeError("; ".join(errors))
    return list(items.values())


def nuxt_payload(page):
    """airBaltic sayfasındaki __NUXT_DATA__ verisini normal bir sözlüğe çevirir."""
    match = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not match:
        raise RuntimeError("sayfada __NUXT_DATA__ bulunamadı (site yapısı değişmiş olabilir)")
    data = json.loads(match.group(1))
    memo = {}

    def resolve(i):
        if not isinstance(i, int) or i < 0:
            return None
        if i in memo:
            return memo[i]
        value = data[i]
        if isinstance(value, list):
            if value and isinstance(value[0], str):  # ["Reactive", 5], ["Set", ...], ["Date", "..."] gibi özel tipler
                kind, rest = value[0], value[1:]
                if kind == "Set":
                    out = [resolve(x) for x in rest]
                elif kind == "Map":
                    out = {str(resolve(rest[j])): resolve(rest[j + 1]) for j in range(0, len(rest) - 1, 2)}
                elif kind == "Date":
                    out = rest[0] if rest else None
                else:
                    out = resolve(rest[0]) if rest else None
                memo[i] = out
                return out
            out = memo[i] = []
            out.extend(resolve(x) for x in value)
            return out
        if isinstance(value, dict):
            out = memo[i] = {}
            for key, ref in value.items():
                out[key] = resolve(ref)
            return out
        memo[i] = value
        return value

    return resolve(0)


def walk(obj):
    stack, seen = [obj], set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, dict):
            yield current
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))


AIRBALTIC_HOME = "https://www.airbaltic.com/en/index"


def airbaltic_time(value):
    try:  # site saatleri Riga saatiyle yazılıyor
        return datetime.strptime(str(value)[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=2)))
    except ValueError:
        return None


def airbaltic_link(obj):
    candidates = []
    link = obj.get("link")
    if isinstance(link, dict):
        candidates.append(((link.get("content") or {}).get("href") or {}).get("cached_url"))
    for key in ("action", "buttonHref", "ctaLink"):
        value = obj.get(key)
        if isinstance(value, dict):
            candidates += [value.get("cached_url"), value.get("url")]
    return next((c for c in candidates if isinstance(c, str) and c.startswith("http")), AIRBALTIC_HOME)


def airbaltic():
    root = nuxt_payload(fetch(AIRBALTIC_HOME))
    now = now_utc()
    items, components = {}, 0

    def add(key, title, text, obj):
        items.setdefault(key, Item(key, "airBaltic", clean(title, 150), clean(text), airbaltic_link(obj)))

    for obj in walk(root):
        comp = obj.get("component")
        if not comp:
            continue
        components += 1
        if comp == "promotional-banner":
            start, end = airbaltic_time(obj.get("startDate")), airbaltic_time(obj.get("endDate"))
            if (end and end < now) or (start and start > now):
                continue
            price = f"{obj.get('priceMain') or ''}{obj.get('priceUnit') or ''}"
            title = " ".join(x for x in (obj.get("labelText"), obj.get("mainText"), price) if x)
            add(f"airbaltic:banner:{obj.get('_uid')}:{obj.get('startDate')}", title, obj.get("iconTooltipText"), obj)
        elif comp == "campaign-counter":
            end = airbaltic_time(obj.get("expirationDateTime"))
            if end and end > now:
                add(f"airbaltic:sale:{obj.get('expirationDateTime')}", f"İndirim dönemi başladı (bitiş: {obj.get('expirationDateTime')})", "", obj)
        elif comp == "HeroWideItem" and obj.get("campaignTitle"):
            end = airbaltic_time(obj.get("bookFlightTo"))
            if end and end + timedelta(days=1) < now:
                continue
            title = obj["campaignTitle"]
            add(f"airbaltic:hero:{sha(title)}:{obj.get('bookFlightFrom')}", title, obj.get("campaignDescription"), obj)
        elif comp in ("big-campaign-block-item", "small-campaign-block-item"):
            title, text = obj.get("title") or "", obj.get("subtext") or ""
            if CAMPAIGN_WORDS.search(f"{title} {text}"):
                add(f"airbaltic:block:{sha(title)}", title, text, obj)
        elif comp == "header-offers":
            for offer in obj.get("offers") or []:
                title = isinstance(offer, dict) and (offer.get("title") or offer.get("name") or offer.get("text"))
                if title:
                    add(f"airbaltic:offer:{sha(title)}", title, offer.get("description") or offer.get("subtext"), offer)
    if not components:
        raise RuntimeError("sayfada içerik bulunamadı (site yapısı değişmiş olabilir)")
    return list(items.values())


FINNAIR_CMS = "https://cms.finnair.com"


def finnair_cms(path):
    if not path.startswith("/service/"):
        path += ("&" if "?" in path else "?") + "view=fin-p13n-json&tier="
    return fetch_json(FINNAIR_CMS + path)


def finnair_main_items(page):
    for row in (page.get("pageGrid") or {}).get("rows", []):
        for placement in row.get("placements", []):
            if placement.get("name") == "main":
                for ref in placement.get("items", []):
                    yield finnair_cms(ref["jsonUrl"])


def finnair_children(obj):
    for ref in obj.get("items") or []:
        if isinstance(ref, dict) and ref.get("jsonUrl"):
            yield finnair_cms(ref["jsonUrl"])


def finnair_titles(obj, depth=0):
    title = obj.get("teaserTitle") or obj.get("htmlTitle") or obj.get("title")
    titles = [title] if title else []
    if depth < 3:
        for child in finnair_children(obj):
            titles += finnair_titles(child, depth + 1)
    return titles


def finnair_link(obj):
    target = obj.get("target")
    if isinstance(target, dict) and target.get("jsonUrl") and target.get("contentId") != obj.get("contentId"):
        obj = finnair_cms(target["jsonUrl"])
    url = obj.get("url") or ""
    if url.startswith("http"):
        return url
    if url.startswith("/") and not re.fullmatch(r"/\w+/\d+-\d+", url):
        return "https://www.finnair.com" + url
    return "https://www.finnair.com/en"


def finnair():
    items = []
    # Ana sayfadaki "Today's offers" (hepsi) ve "In the spotlight" (kampanya kelimesi geçenler) kutuları
    for block in finnair_main_items(finnair_cms("/en")):
        if block.get("viewTypeName") != "marketing-activity-blocks":
            continue
        all_offers = "offer" in (block.get("teaserTitle") or "").lower()
        for teaser in finnair_children(block):
            title = teaser.get("teaserTitle") or teaser.get("title")
            if title and (all_offers or CAMPAIGN_WORDS.search(f"{title} {teaser.get('teaserText') or ''}")):
                key = f"finnair:teaser:{teaser.get('contentId')}"
                items.append(Item(key, "Finnair", clean(title, 150), clean(teaser.get("teaserText")), finnair_link(teaser)))
    # Özel teklifler sayfası: indirim yokken "Our latest sale has ended" yazıyor
    special = [clean(t, 150) for block in finnair_main_items(finnair_cms("/en/special-offers")) for t in finnair_titles(block)]
    if special and not any("has ended" in t.lower() for t in special):
        key = f"finnair:special:{sha('|'.join(special))}"
        items.append(Item(key, "Finnair", special[0], " · ".join(special[1:3]), "https://www.finnair.com/en/special-offers"))
    # İndirim kodu sayfası: kod yokken "There are currently no discount codes" yazıyor
    for block in finnair_main_items(finnair_cms("/en/discount-code")):
        if block.get("viewTypeName") == "pick-first-item":
            for article in finnair_children(block):
                text = clean(article.get("teaserText") or article.get("detailText"), 500)
                if text and "no discount code" not in text.lower():
                    key = f"finnair:code:{sha(text)}"
                    items.append(Item(key, "Finnair", "İndirim kodu paylaşıldı", text, "https://www.finnair.com/en/discount-code"))
                break
    return items


def pegasus():
    tags = re.findall(r'<a\b[^>]*data-gtm-promotion-id="[^"]+"[^>]*>', fetch(PEGASUS_PAGE))
    if not tags:
        raise RuntimeError("kampanya listesi bulunamadı (site yapısı değişmiş olabilir)")
    items = {}
    for tag in tags:
        attrs = {k: html.unescape(v) for k, v in re.findall(r'([\w-]+)="([^"]*)"', tag)}
        pid, name = attrs.get("data-gtm-promotion-id"), clean(attrs.get("data-gtm-promotion-name"), 150)
        if pid and name and not PEGASUS_SKIP.search(name):
            link = urllib.parse.urljoin("https://www.flypgs.com", attrs.get("href") or "")
            items.setdefault(f"pegasus:{pid}", Item(f"pegasus:{pid}", "Pegasus", name, "", link))
    return list(items.values())


def decode_mime(value):
    return str(email.header.make_header(email.header.decode_header(value or "")))


def mail_sender(value):
    # Adres <...> içinde durduğu için clean() kullanılmaz; o, < > arasını HTML etiketi sanıp siler
    name, address = email.utils.parseaddr(decode_mime(value))
    return f"{name} {address}".lower()


def gmail():
    user, password = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    if not (user and password):
        return None  # Gmail ayarlanmamış: kaynak kapalı
    query = "from:(" + " OR ".join(MAIL_SENDERS) + ") newer_than:3d"
    imap = imaplib.IMAP4_SSL("imap.gmail.com", timeout=REQUEST_TIMEOUT)
    try:
        imap.login(user, password.replace(" ", ""))
        imap.select("INBOX", readonly=True)  # salt okunur: mailler okunmuş olarak işaretlenmez
        _, data = imap.uid("SEARCH", "X-GM-RAW", f'"{query}"')
        uids = (data[0].split() if data and data[0] else [])[-50:]
        items, no_airline, filtered = [], 0, 0
        for uid in uids:
            _, parts = imap.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM MESSAGE-ID)])")
            raw = next((part[1] for part in parts if isinstance(part, tuple)), b"")
            headers = email.message_from_bytes(raw)
            subject, sender = clean(decode_mime(headers.get("Subject")), 200), mail_sender(headers.get("From"))
            airline = next((name for key, name in MAIL_SENDERS.items() if key in sender), None)
            if not airline:
                no_airline += 1
                continue
            if not subject or MAIL_SKIP.search(subject):  # konusuz mailler de bülten değil
                filtered += 1
                continue
            msgid = (headers.get("Message-ID") or uid.decode()).strip()
            link = "https://mail.google.com/mail/u/0/#search/" + urllib.parse.quote(f"rfc822msgid:{msgid.strip('<>')}")
            items.append(Item(f"mail:{sha(msgid)}", airline, subject, "", link, kind="mail"))
        # Kayıtlar herkese açık olduğu için mail konuları yazılmaz, sadece sayılar
        print(f"[Gmail] arama {len(uids)} mail buldu: {no_airline} gönderen eşleşmedi, {filtered} konu filtresine takıldı, "
              f"kalan: {', '.join(it.airline for it in items) or '-'}")
        return items
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def deal_feeds():
    items = []
    for name, url in DEAL_FEEDS.items():
        root = ET.fromstring(fetch(url, "application/rss+xml,application/xml", raw=True))
        for entry in root.iter("item"):
            title = clean(entry.findtext("title"), 200)
            airline = airline_in(title) or airline_in(clean(entry.findtext("description"), 600))
            if not airline:
                continue
            link = (entry.findtext("link") or "").strip()
            guid = (entry.findtext("guid") or link or title).strip()
            items.append(Item(f"feed:{sha(guid)}", airline, title, f"{name} fırsat sitesinde", link, kind="feed"))
    return items


SOURCES = [
    ("Ryanair", ryanair),
    ("airBaltic", airbaltic),
    ("Finnair", finnair),
    ("Pegasus", pegasus),
    ("Gmail", gmail),
    ("Fırsat siteleri", deal_feeds),
]


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def esc(text):
    return html.escape(text or "", quote=False)


def format_item(item):
    head = {
        "site": f"🔥 <b>{esc(item.airline)}</b> · yeni kampanya",
        "mail": f"📧 <b>{esc(item.airline)}</b> · yeni bülten maili",
        "feed": f"💡 <b>{esc(item.airline)}</b> · fırsat sitesinde",
    }[item.kind]
    lines = [head, f"<b>{esc(item.title)}</b>"]
    if item.text:
        lines.append(esc(item.text))
    if item.url:
        label = "Gmail'de aç" if item.kind == "mail" else "Kampanyaya git"
        lines.append(f'👉 <a href="{html.escape(item.url)}">{label}</a>')
    return "\n".join(lines)


def telegram_call(method, fields):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=urllib.parse.urlencode(fields).encode())
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:  # hata metninde token geçmesin diye sadece Telegram'ın açıklaması alınır
            try:
                body = json.loads(exc.read())
            except Exception:
                body = {}
            wait = (body.get("parameters") or {}).get("retry_after")
            if exc.code == 429 and wait and attempt < 2:  # "çok hızlı" cevabında Telegram'ın söylediği kadar beklenir
                time.sleep(min(int(wait), 60) + 1)
                continue
            raise RuntimeError(f"Telegram hatası ({exc.code}): {body.get('description') or exc.reason}") from None


def send(text, photo=""):
    if DRY_RUN:
        print("---- TELEGRAM" + (f" [foto: {photo}]" if photo else "") + "\n" + text + "\n")
        return
    if not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        raise RuntimeError("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID tanımlı değil")
    chat = os.environ["TELEGRAM_CHAT_ID"]
    if photo and len(text) <= 1024:
        try:
            return telegram_call("sendPhoto", {"chat_id": chat, "photo": photo, "caption": text, "parse_mode": "HTML"})
        except RuntimeError as exc:
            print(f"Fotoğraf gönderilemedi, sadece metin gidiyor: {exc}")
    telegram_call("sendMessage", {"chat_id": chat, "text": text[:4096], "parse_mode": "HTML"})


# ---------------------------------------------------------------------------
# Ana akış
# ---------------------------------------------------------------------------


def welcome_message(intro, first_install):
    if first_install:
        lines = ["✅ <b>Kampanya Avcısı devrede!</b>", "Yeni bir kampanya çıktığında buraya yazacağım.", ""]
    else:
        lines = ["➕ <b>Yeni kaynak eklendi.</b>", "Bundan sonra bunların yeni kampanyalarını da yazacağım.", ""]
    for name, items in intro:
        if name == "Gmail":
            lines.append(f"📧 Gmail bağlı: son 3 günde {len(items)} havayolu bülteni var.")
        elif name == "Fırsat siteleri":
            lines.append(f"💡 Fırsat sitelerinde şu an bu havayollarıyla ilgili {len(items)} ilan var.")
        elif items:
            lines.append(f"<b>{esc(name)}</b> şu an:")
            lines += [f"• {esc(it.title)}" for it in items[:8]]
        else:
            lines.append(f"<b>{esc(name)}</b>: şu an görünen kampanya yok.")
    return "\n".join(lines)


def weekly_message(state, statuses):
    week_ago = (now_utc().date() - timedelta(days=7)).isoformat()
    sent = sum(n for day, n in state.get("sent", {}).items() if day > week_ago)
    lines = ["🟢 <b>Haftalık kontrol:</b> Kampanya Avcısı çalışıyor.", f"Son 7 günde {sent} bildirim gönderildi.", ""]
    lines += [f"{icon} {esc(name)}" for name, icon in statuses]
    return "\n".join(lines)


def run(state):
    global SOURCE_DEADLINE
    now = now_utc()
    today = now.date().isoformat()
    first_install = not state.get("seen")
    seen = state.setdefault("seen", {})
    fails = state.setdefault("fail", {})
    last_ok = state.setdefault("ok", {})  # her kaynağın en son başarıyla okunduğu gün
    for name in [n for n in fails if n not in dict(SOURCES)]:  # kaldırılmış kaynakların sayaçları
        del fails[name]
    alerts, news, intro, statuses = [], [], [], []

    for name, source in SOURCES:
        started = time.monotonic()
        SOURCE_DEADLINE = started + SOURCE_TIME_LIMIT
        try:
            items = source()
        except Exception as exc:
            count = fails.get(name, 0)
            if count < FAIL_ALERT_AFTER:
                fails[name] = count + 1
                if count + 1 == FAIL_ALERT_AFTER:
                    alerts.append(
                        f"⚠️ <b>{esc(name)}</b> son {FAIL_ALERT_AFTER} denemede okunamadı.\n"
                        f"Hata: {esc(str(exc)[:200])}\nSite yapısı değişmiş ya da erişimi engellemiş olabilir."
                    )
            print(f"[{name}] okunamadı ({time.monotonic() - started:.0f} sn): {exc}")
            statuses.append((name, "⚠️"))
            continue
        finally:
            SOURCE_DEADLINE = None
        if items is None:
            print(f"[{name}] kapalı")
            statuses.append((name + " (kapalı)", "⚪"))
            continue
        statuses.append((name, "✅"))
        if fails.pop(name, 0) >= FAIL_ALERT_AFTER:
            alerts.append(f"✅ <b>{esc(name)}</b> yeniden okunabiliyor.")
        first_time = name not in seen
        known = seen.setdefault(name, {})
        # Kaynak okunurken bir haftadan uzun süre görünmeyip geri gelen kampanya da yeni sayılır.
        # Kaynağın kendisi uzun süre okunamadıysa bu kural işlemez, yoksa her şey yeniden bildirilirdi.
        previous_ok = last_ok.get(name)
        gone_before = (date.fromisoformat(previous_ok) - timedelta(days=REAPPEAR_AFTER_DAYS)).isoformat() if previous_ok else ""
        last_ok[name] = today
        new = {it.id: it for it in items if it.id not in known or known[it.id] < gone_before}
        for it in items:
            known[it.id] = today
        print(f"[{name}] {len(items)} kampanya, {len(new)} yeni ({time.monotonic() - started:.0f} sn)"
              + (" (ilk çalışma, kaydedildi)" if first_time else ""))
        if first_time:
            intro.append((name, items))
        else:
            news += new.values()

    # Uzun süredir görünmeyen kampanyaları unut
    cutoff = (now.date() - timedelta(days=FORGET_AFTER_DAYS)).isoformat()
    for known in seen.values():
        for key in [k for k, day in known.items() if day < cutoff]:
            del known[key]

    news.sort(key=lambda it: ["site", "mail", "feed"].index(it.kind))
    outbox = [(text, "") for text in alerts]
    if intro:
        outbox.append((welcome_message(intro, first_install), ""))
    outbox += [(format_item(it), it.image) for it in news]
    if len(outbox) > MAX_MESSAGES:
        extra = len(outbox) - (MAX_MESSAGES - 1)
        outbox = outbox[: MAX_MESSAGES - 1] + [(f"…ve {extra} bildirim daha var, hepsini göndermemek için kısalttım.", "")]

    if news:
        sent = state.setdefault("sent", {})
        sent[today] = sent.get(today, 0) + len(news)
        for day in [d for d in sent if d < (now.date() - timedelta(days=14)).isoformat()]:
            del sent[day]

    # Pazartesi sabahları "çalışıyorum" mesajı
    if intro:
        state["last_heartbeat"] = today
    elif now.weekday() == 0 and now.hour >= 6 and state.get("last_heartbeat") != today:
        state["last_heartbeat"] = today
        outbox.append((weekly_message(state, statuses), ""))
    return outbox


def self_test():
    lines = ["🧪 <b>Kampanya Avcısı test mesajı</b>", "Telegram bağlantısı çalışıyor ✅"]
    if os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD"):
        try:
            lines.append(f"Gmail bağlantısı çalışıyor ✅ (son 3 günde {len(gmail() or [])} havayolu bülteni)")
        except Exception as exc:
            lines.append(f"Gmail'e bağlanılamadı ❌ {esc(str(exc)[:200])}")
    else:
        lines.append("Gmail bağlı değil (isteğe bağlı).")
    send("\n".join(lines))


def main():
    global DRY_RUN
    parser = argparse.ArgumentParser(description="Havayolu kampanyalarını izleyip Telegram'a bildirir.")
    parser.add_argument("--dry-run", action="store_true", help="mesajları Telegram yerine ekrana yaz")
    parser.add_argument("--test", action="store_true", help="sadece Telegram ve Gmail bağlantısını test et")
    parser.add_argument("--state", default="state.json", help="hafıza dosyası (varsayılan: state.json)")
    args = parser.parse_args()
    DRY_RUN = args.dry_run
    sys.setrecursionlimit(20000)

    if args.test:
        self_test()
        return
    try:
        with open(args.state, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        state = {}
    for text, photo in run(state):
        try:
            send(text, photo)
        except RuntimeError as exc:
            if "(400)" not in str(exc):
                raise  # geçici hata: hafıza kaydedilmez, sonraki çalışmada yeniden denenir
            # Telegram mesajı kalıcı olarak reddettiyse atlanır; yoksa diğer bildirimler de her seferinde tekrarlanırdı
            print(f"Mesaj Telegram'a kabul edilmedi, atlandı: {exc}")
        if not DRY_RUN:
            time.sleep(1)  # Telegram aynı sohbete saniyede en fazla ~1 mesaj ister
    with open(args.state, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")


if __name__ == "__main__":
    main()
