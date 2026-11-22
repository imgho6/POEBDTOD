#!/usr/bin/env python3
"""Editorial Telegram posts for Poltava Light.

Runs alongside the schedule publisher. News is text-only: source photos,
previews, thumbnails, and logos are never downloaded or forwarded.
"""
import hashlib
from difflib import SequenceMatcher
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from bot_db import load_news_state, save_news_state, record_publication, record_error
except ImportError:
    def load_news_state(): return None
    def save_news_state(value): return False
    def record_publication(*args, **kwargs): return False
    def record_error(*args, **kwargs): return False

ROOT = Path("public/data/schedules")
STATE_PATH = Path(os.environ.get("TELEGRAM_STATE_PATH", str(ROOT / "telegram-state.json")))
CHANNEL_ID = os.environ.get("TELEGRAM_CHAT_ID", "@PoltavaLight")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
KYIV = ZoneInfo("Europe/Kyiv")
USER_AGENT = "Mozilla/5.0 (compatible; PoltavaLightBot/1.0; +https://svitlo-ua.vercel.app)"

# Вищий пріоритет мають першоджерела та офіційні установи.
SOURCES = (
    ("poltavskaoda", "Полтавська ОВА", "local", 5),
    ("ukrinform_news", "Укрінформ", "national", 5),
    ("suspilnepoltava", "Суспільне Полтава", "local", 4),
    ("suspilnenews", "Суспільне Новини", "national", 4),
    ("tch_channel", "ТСН", "national", 2),
)

LOCAL_TERMS = (
    "полтав", "кременчук", "миргород", "лубн", "гадяч", "лохвиц",
    "решетил", "карлів", "глобин", "зіньків", "кобеляк", "нові санжари",
    "гребін", "пирятин", "чутов", "дикан", "шишак", "опішн", "хорол",
    "семенів", "великі сорочинці",
)
POWER_TERMS = (
    "електроенерг", "електропостач", "електромереж", "електроенергет",
    "відключенн", "графік відключ", "графіки відключ", "черг відключ",
    "обленерго", "енергосистем", "енергопостач",
    "генерац", "дефіцит потуж", "аварійна мереж", "аварійні мереж",
    "тариф на електро", "електричн.*тариф",
)
NATIONAL_SCOPE_TERMS = (
    "в україні", "по україні", "по всій україні", "українськ.*енергосистем",
    "енергосистем.*країн", "для споживачів україни",
)
CALL_TO_ACTION_TERMS = (
    "підпишись", "підписуйтесь", "підписуйтеся", "читайте нас",
    "ми у facebook", "ми в facebook", "facebook |", "youtube |",
    "підписатися на", "наш telegram", "наш телеграм",
)
EXCLUDE_TERMS = (
    "повітрян", "бпла", "шахед", "реактивн", "курс на",
    "рух ракет", "пуск ракет", "ппо", "дрон курсом", "влучання",
    "влучив", "обстріл", "атакували", "атакує", "вибух",
    "загроза балістики", "ракета", "падіння уламків", "ворог",
    "російськ", "військ", "зсу", "загинув", "загибл", "поранен",
    "повітряна тривога", "відбій тривоги",
    "#реклама", "рекламний", "рекламна", "рекламний матеріал",
    "партнерський матеріал", "промокод", "купи зараз", "замовити зараз",
    "насильств", "судитим", "вбивств", "злочин", "підозрю", "затримали",
    "пограбув", "народил", "немовля", "пологов",
)

TIPS = (
    ("Як зберегти інтернет під час відключення",
     "Перевірте, від якої напруги живиться ваш роутер. Для резервного живлення потрібен сумісний mini-UPS або перетворювач. Перед підключенням обов’язково звірте напругу, полярність і роз’єм із маркуванням пристрою."),
    ("Як довше зберегти холод у холодильнику",
     "Під час відключення намагайтеся зайвий раз не відчиняти дверцята холодильника й морозильної камери. Так холод зберігатиметься довше. Не кладіть гарячі страви всередину."),
    ("Підготуйте важливі документи заздалегідь",
     "Збережіть на телефоні офлайн-копії потрібних документів, квитків, адрес і контактів. Так вони будуть доступні, навіть коли інтернет тимчасово не працює."),
    ("Не вмикайте всі потужні прилади одночасно",
     "Після відновлення електропостачання розподіліть запуск бойлера, обігрівача, пральної машини та інших потужних приладів у часі. Це допоможе не перевантажувати домашню електромережу."),
    ("Перевірте аварійне освітлення",
     "Тримайте ліхтарик у відомому всім мешканцям місці. Періодично перевіряйте заряд акумуляторного ліхтаря та справність батарейок, щоб не шукати їх у темряві."),
    ("Павербанк теж потребує перевірки",
     "Час від часу перевіряйте заряд павербанка, кабель і роз’єми. Не користуйтеся пристроєм із пошкодженим корпусом, здуттям або незвичним нагріванням."),
    ("Генератору не місце в приміщенні",
     "Бензинові та дизельні генератори можна використовувати лише надворі, подалі від вікон і дверей, відповідно до інструкції виробника. Вихлопні гази містять чадний газ, який може бути смертельно небезпечним."),
    ("Зробіть запас води на випадок знеструмлення",
     "Якщо у вашому будинку водопостачання залежить від насосів, тримайте невеликий запас питної та технічної води. Слідкуйте за офіційними повідомленнями місцевого водоканалу."),
)

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def load_state():
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state = value if isinstance(value, dict) else {"entries": {}}
    except (OSError, json.JSONDecodeError):
        state = {"entries": {}}
    database_state = load_news_state()
    if isinstance(database_state, dict):
        state["editorial"] = database_state
    return state


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    save_news_state(state.get("editorial", {}))


def telegram_request(method, payload, content_type="application/json"):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задано")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=payload,
        headers={"Content-Type": content_type},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=35) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method}: {result}")
    return result["result"]


def send_message(text):
    payload = json.dumps({
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    result = telegram_request("sendMessage", payload)
    record_publication("editorial", CHANNEL_ID, result.get("message_id"),
                      body=text[:1800])
    return result


def fetch_text(url, timeout=20):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        if "html" not in content_type.lower():
            raise RuntimeError(f"очікувався HTML, отримано {content_type}")
        return response.read(4 * 1024 * 1024).decode("utf-8", errors="replace")


class TelegramPostParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts = []
        self.current = None
        self.root_div_depth = 0
        self.text_depth = 0

    def _finish(self):
        if self.current:
            text = "".join(self.current["parts"])
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r" *\n *", "\n", text)
            self.current["text"] = text.strip()
            self.current["parts"] = []
            self.posts.append(self.current)
        self.current = None
        self.root_div_depth = 0
        self.text_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())

        if tag == "div" and "tgme_widget_message" in classes:
            if self.current:
                self._finish()
            self.current = {
                "post": attrs.get("data-post", ""),
                "parts": [],
                "links": [],
                "published": "",
                "text": "",
            }
            self.root_div_depth = 1
            self.text_depth = 0
            return

        if not self.current:
            return

        if tag == "div":
            self.root_div_depth += 1

        if tag == "time":
            self.current["published"] = attrs.get("datetime", "")

        if "tgme_widget_message_text" in classes:
            self.text_depth = 1
        elif self.text_depth and tag not in VOID_TAGS:
            self.text_depth += 1

        if tag == "br" and self.text_depth:
            self.current["parts"].append("\n")

        if tag == "a":
            href = attrs.get("href", "")
            if href.startswith("http://") or href.startswith("https://"):
                self.current["links"].append(href)


    def handle_data(self, data):
        if self.current and self.text_depth:
            self.current["parts"].append(data)

    def handle_endtag(self, tag):
        if not self.current:
            return
        if self.text_depth and tag not in VOID_TAGS:
            self.text_depth -= 1
            if self.text_depth == 0:
                self.current["parts"].append("\n")
        if tag == "div":
            self.root_div_depth -= 1
            if self.root_div_depth <= 0:
                self._finish()


def parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=KYIV)
        return parsed.astimezone(KYIV)
    except ValueError:
        return None


def get_post_url(handle, post):
    path = post.get("post", "")
    if path and "/" in path:
        return "https://t.me/" + path
    return f"https://t.me/s/{handle}"


def article_url(post):
    for link in post.get("links", []):
        parsed = urllib.parse.urlparse(link)
        host = (parsed.hostname or "").lower()
        if host.endswith(("suspilne.media", "poda.gov.ua", "gov.ua", "ukrinform.ua", "tsn.ua")):
            return link
    return get_post_url("", post)


def classify_news(handle, text, category):
    """Окремо оцінює зв'язок з електропостачанням і важливість новини."""
    lowered = " ".join(text.casefold().split())
    if any(term in lowered for term in EXCLUDE_TERMS):
        return {"relevant": False, "relevance": 0, "importance": 0}

    title, body = news_parts(text)
    lead = " ".join([title or "", *body[:2]]).casefold()[:900]
    power_hits = sum(
        1 for term in POWER_TERMS
        if (re.search(term, lead) if ".*" in term else term in lead)
    )
    if power_hits == 0:
        return {"relevant": False, "relevance": 0, "importance": 0}

    # Публікуємо лише новини з конкретною інформацією про графіки,
    # черги або фактичне знеструмлення/відновлення. Побутові наслідки
    # ГПВ (опалення, гуртожитки тощо) не є новинами для цього каналу.
    direct_power_terms = (
        "графік відключ", "графіки відключ", "погодинних відключ",
        "черг відключ", "підчерг", "застосують гпв", "скасували гпв",
        "аварійно знеструм", "знеструмлено", "без електропостач",
        "відновили електропостач", "відновлюють електропостач",
        "відключатимуть світло", "вимикатимуть світло",
    )
    if not any(term in lead for term in direct_power_terms):
        return {"relevant": False, "relevance": 0, "importance": 0}

    is_local = any(term in lowered for term in LOCAL_TERMS)
    is_national = any(re.search(term, lowered) for term in NATIONAL_SCOPE_TERMS)
    relevant = is_local if category == "local" else (is_local or is_national)
    if not relevant:
        return {"relevant": False, "relevance": min(100, power_hits * 25), "importance": 0}

    relevance = min(100, 45 + power_hits * 12 + (20 if is_local else 0) + (10 if is_national else 0))
    urgent_terms = (
        "аварійн", "масован", "пошкоджен", "знеструмлен", "без електроенергі",
        "відновлен", "обмеженн", "дефіцит", "екстрен", "скасуван", "застосуван",
    )
    impact_terms = (
        "споживач", "побутов", "населен", "област", "громад", "черг",
        "графік", "відключ", "електропостач",
    )
    urgency = sum(1 for term in urgent_terms if term in lead)
    impact = sum(1 for term in impact_terms if term in lead)
    importance = min(100, 25 + urgency * 14 + impact * 6 + (12 if is_local else 0))
    return {"relevant": True, "relevance": relevance, "importance": importance}


def relevant_post(handle, text, category):
    return classify_news(handle, text, category)["relevant"]

def clean_source_text(text):
    """Прибирає рекламні/підписні хвости, не додаючи їх до допису каналу."""
    paragraphs = [part.strip() for part in text.splitlines() if part.strip()]
    cleaned = []
    for paragraph in paragraphs:
        lowered = paragraph.casefold()
        cut_positions = [
            lowered.find(term)
            for term in CALL_TO_ACTION_TERMS
            if lowered.find(term) >= 0
        ]
        if cut_positions:
            paragraph = paragraph[:min(cut_positions)].rstrip(" |·-–—")
        if paragraph:
            cleaned.append(paragraph)
    return "\n".join(cleaned)


def complete_sentences(paragraph):
    """Повертає лише завершені речення, відкидаючи обрізаний хвіст джерела."""
    paragraph = re.sub(r"\s*🔹\s*", " ", paragraph).strip()
    if not paragraph:
        return []

    parts = re.split(r"(?<=[.!?])\s+", paragraph)
    return [
        part.strip()
        for part in parts
        if re.search(r"[.!?][»”\"')\]]*$", part.strip())
    ]


def news_parts(text):
    source_text = clean_source_text(text)
    paragraphs = [part.strip() for part in source_text.splitlines() if part.strip()]
    if not paragraphs:
        return None, []

    first = re.sub(r"^(?:❗️|🔹)\s*", "", paragraphs[0]).strip()
    first_sentences = complete_sentences(first)
    if first_sentences:
        title = first_sentences[0]
        body = first_sentences[1:]
    elif len(first) <= 180 and not first.endswith(("…", "...")):
        # Деякі джерела оформлюють заголовок без крапки.
        title = first
        body = []
    else:
        return None, []

    for paragraph in paragraphs[1:]:
        body.extend(complete_sentences(paragraph))

    title = re.sub(r"https?://\S+", "", title).strip()
    body = [re.sub(r"https?://\S+", "", item).strip() for item in body]
    return title, [item for item in body if item]


def make_news_lead(text):
    title, _ = news_parts(text)
    if not title:
        return ""
    return re.sub(r"[^\w]+", " ", title.casefold()).strip()


NEWS_STOPWORDS = {
    "та", "або", "але", "для", "про", "після", "перед", "через", "щодо",
    "який", "яка", "яке", "які", "це", "цей", "ця", "що", "у", "в", "на",
    "до", "від", "із", "за", "по", "як", "буде", "будуть", "стало",
}


NEWS_SUFFIXES = (
    "ування", "ювання", "ення", "ання", "іння", "ями", "ами", "ого", "ому",
    "ими", "ій", "ої", "ою", "ів", "ей", "ах", "ях", "ам", "ям", "ом",
    "ем", "ею", "ий", "а", "я", "і", "и", "у", "ю", "о", "е",
)


def normalize_news_token(token):
    for suffix in NEWS_SUFFIXES:
        if len(token) - len(suffix) >= 4 and token.endswith(suffix):
            return token[:-len(suffix)]
    return token


def news_fingerprint(text):
    title, body = news_parts(text)
    source = " ".join([title or "", *body[:2]]).casefold()
    source = re.sub(r"https?://\\S+", " ", source)
    tokens = {
        normalize_news_token(token)
        for token in re.findall(r"[а-яіїєґa-z0-9]{3,}", source)
        if token not in NEWS_STOPWORDS
    }
    return " ".join(sorted(tokens))

def same_news_lead(left, right):
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) < 28:
        return False
    if SequenceMatcher(None, left, right).ratio() >= 0.84:
        return True
    left_words, right_words = set(left.split()), set(right.split())
    union = left_words | right_words
    return bool(union) and len(left_words & right_words) / len(union) >= 0.72


def same_news_content(left, right):
    """Порівнює заголовок і зміст, щоб ловити перефразовані передруки."""
    if same_news_lead(make_news_lead(left), make_news_lead(right)):
        return True
    left_words = set(news_fingerprint(left).split())
    right_words = set(news_fingerprint(right).split())
    if len(left_words) < 5 or len(right_words) < 5:
        return False
    union = left_words | right_words
    exact_overlap = len(left_words & right_words) / len(union) if union else 0
    if exact_overlap >= 0.50:
        return True

    # Порівнюємо словоформи та відмінки, не вимагаючи однакового тексту.
    unmatched = set(right_words)
    matched = 0
    for left_word in left_words:
        best = next((
            right_word for right_word in unmatched
            if (
                left_word.startswith(right_word) or right_word.startswith(left_word)
                or SequenceMatcher(None, left_word, right_word).ratio() >= 0.78
            )
        ), None)
        if best:
            unmatched.remove(best)
            matched += 1
    return matched / max(len(left_words), len(right_words)) >= 0.40

def make_caption(post, source_name=None, handle=None):
    """Формує коротку новину з не більш ніж двох завершених речень."""
    title, body_sentences = news_parts(post.get("text", ""))
    if not title or len(title) > 180:
        return None

    body = next(
        (
            sentence for sentence in body_sentences
            if len(sentence) <= 230 and len(title) + len(sentence) <= 420
        ),
        "",
    )

    safe_title = html.escape(title)
    caption = f"❗️ <b>{safe_title}</b>"
    if body:
        caption += "\n\n" + html.escape(body)
    return caption

def make_news_message(post):
    """Повертає лише текст новини без видимого посилання на джерело."""
    return make_caption(post)


def maybe_morning(state, now):
    editorial = state.setdefault("editorial", {})
    date_key = now.date().isoformat()
    if not (7 <= now.hour < 11) or editorial.get("morningDate") == date_key:
        return

    text = (
        "☀️ <b>Ранок на Полтавщині</b>\n\n"
        "Доброго ранку! Плануючи день, перевірте графік саме для своєї черги "
        "та збережіть час на заряджання необхідних пристроїв.\n\n"
        "Актуальний розклад для всієї області – на нашому сайті:\n"
        '<a href="https://svitlo-ua.vercel.app/poltava"><b>Світло.ua · Полтавщина</b></a>\n\n'
        "Якщо протягом дня графіки зміняться, повідомимо тут."
    )
    send_message(text)
    editorial["morningDate"] = date_key
    save_state(state)
    print(f"Ранковий допис на {date_key} надіслано")


def maybe_tip(state, now):
    editorial = state.setdefault("editorial", {})
    date_key = now.date().isoformat()
    if now.weekday() not in (0, 2, 4) or not (11 <= now.hour < 14):
        return
    if editorial.get("tipDate") == date_key:
        return

    index = int(editorial.get("tipIndex", 0)) % len(TIPS)
    title, body = TIPS[index]
    send_message(f"💡 <b>Корисно знати: {html.escape(title)}</b>\n\n{html.escape(body)}")
    editorial["tipDate"] = date_key
    editorial["tipIndex"] = (index + 1) % len(TIPS)
    save_state(state)
    print(f"Корисну пораду №{index + 1} надіслано")


def maybe_news(state, now):
    editorial = state.setdefault("editorial", {})
    news_state = editorial.setdefault("news", {})
    last_sent = parse_datetime(news_state.get("lastSentAt", ""))
    if last_sent and now - last_sent < timedelta(minutes=110):
        print("Новини пропущено: діє інтервал між публікаціями")
        return

    seen = set(news_state.get("seen", []))
    seen_leads = list(news_state.get("seenLeads", []))
    seen_stories = list(news_state.get("seenStories", []))
    candidates = []
    source_errors = []
    for handle, source_name, category, source_priority in SOURCES:
        try:
            page = fetch_text(f"https://t.me/s/{handle}")
            parser = TelegramPostParser()
            parser.feed(page)
            parser.close()
            for post in parser.posts:
                published = parse_datetime(post.get("published", ""))
                if not published or published > now + timedelta(minutes=5):
                    continue
                if now - published > timedelta(hours=10):
                    continue
                if not post.get("text"):
                    continue
                assessment = classify_news(handle, post["text"], category)
                if not assessment["relevant"]:
                    continue
                digest = hashlib.sha256(
                    re.sub(r"\s+", " ", post["text"].casefold()).encode("utf-8")
                ).hexdigest()
                lead = make_news_lead(post["text"])
                if digest in seen or not lead:
                    continue
                if any(same_news_lead(lead, previous) for previous in seen_leads):
                    continue
                if any(same_news_content(post["text"], previous.get("text", "")) for previous in seen_stories):
                    continue
                post["digest"] = digest
                post["lead"] = lead
                post["published_dt"] = published
                post["source_name"] = source_name
                post["source_priority"] = source_priority
                post["relevance_score"] = assessment["relevance"]
                post["importance_score"] = assessment["importance"]
                post["handle"] = handle
                candidates.append(post)
        except Exception as error:
            source_errors.append(f"@{handle}: {error}")
            record_error("editorial_source", error, {"handle": handle})
            print(f"Новини: не вдалося прочитати @{handle}: {error}")

    if source_errors:
        label = (
            "Не вдалося прочитати жодне джерело новин."
            if len(source_errors) == len(SOURCES)
            else "Частина джерел новин недоступна."
        )
        notify_editorial_error(label + "\n" + "\n".join(source_errors[:5]))

    if not candidates:
        print("Новини: немає свіжої доречної текстової публікації")
        return

    candidates.sort(
        key=lambda item: (
            item["importance_score"],
            item["relevance_score"],
            item["source_priority"],
            item["published_dt"],
        ),
        reverse=True,
    )
    post = candidates[0]
    message = make_news_message(post)
    if not message:
        print("Новини: пропущено публікацію без придатного заголовка")
        return

    try:
        result = send_message(message)
    except Exception as error:
        print(f"Новини: не вдалося надіслати текстову публікацію: {error}")
        record_error("editorial_publish", error, {"source": post.get("handle"), "post": post.get("post")})
        notify_editorial_error("Не вдалося опублікувати новину: " + str(error))
        return

    post_id = result.get("message_id")
    title, _ = news_parts(post["text"])
    record_publication(
        "news",
        CHANNEL_ID,
        post_id,
        source_handle=post["handle"],
        source_post=get_post_url(post["handle"], post),
        content_hash=post["digest"],
        title=title,
        body=post["text"][:1800],
        importance_score=post["importance_score"],
        relevance_score=post["relevance_score"],
        source_priority=post["source_priority"],
    )
    # Позначаємо як прочитані й інші джерела з тією самою новиною,
    # щоб майже однакові передруки не прийшли наступним запуском.
    related = [
        candidate for candidate in candidates
        if same_news_content(candidate["text"], post["text"])
    ]
    seen.update(candidate["digest"] for candidate in related)
    news_state["seen"] = list(dict.fromkeys([
        *news_state.get("seen", []),
        *(candidate["digest"] for candidate in related),
    ]))[-150:]
    news_state["seenLeads"] = list(dict.fromkeys([
        *seen_leads, post["lead"],
    ]))[-100:]
    news_state["seenStories"] = [
        *seen_stories,
        {"text": post["text"][:1800], "lead": post["lead"], "at": now.isoformat()},
    ][-100:]
    news_state["lastSentAt"] = now.isoformat()
    news_state["lastMessageId"] = post_id
    news_state["lastSourcePost"] = get_post_url(post["handle"], post)
    save_state(state)
    print(
        f"Текстову новину надіслано: @{post['handle']} "
        f"(публікація {post.get('post', 'невідома')})"
    )


def notify_editorial_error(message):
    """Надсилає окреме сповіщення, не блокуючи публікацію графіків."""
    if not TOKEN:
        print("Сповіщення редакційної помилки не надіслано: немає TELEGRAM_BOT_TOKEN")
        return
    try:
        state = load_state()
        editorial = state.setdefault("editorial", {})
        fingerprint = hashlib.sha256(message.encode("utf-8")).hexdigest()
        previous_at = parse_datetime(editorial.get("lastErrorAlertAt", ""))
        if (
            editorial.get("lastErrorFingerprint") == fingerprint
            and previous_at
            and datetime.now(KYIV) - previous_at < timedelta(hours=6)
        ):
            print("Сповіщення про цю помилку вже надсилалося протягом останніх 6 годин")
            return
        send_message(
            "⚠️ <b>Помилка редакційного модуля</b>\n\n"
            + html.escape(message[:2500])
        )
        editorial["lastErrorFingerprint"] = fingerprint
        editorial["lastErrorAlertAt"] = datetime.now(KYIV).isoformat()
        save_state(state)
    except Exception as notify_error:
        print(f"Не вдалося надіслати сповіщення редакційної помилки: {notify_error}")

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задано")
    now = datetime.now(KYIV)
    state = load_state()
    maybe_morning(state, now)
    maybe_tip(state, now)
    maybe_news(state, now)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Помилка редакційних дописів: {error}")
        record_error("editorial", error)
        notify_editorial_error(str(error))
        raise
