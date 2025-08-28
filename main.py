# main.py — RSS → (фильтр тем) → GPT (85% факты / 15% ирония) → комплаенс РФ → Telegram
import os
import re
import json
import time
import queue
import threading
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import feedparser
import httpx
import tldextract
from bs4 import BeautifulSoup
import telebot
from openai import OpenAI

# ----------------- ЛОГИ -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("autoposter")

# ----------------- ENV -----------------
BOT_TOKEN       = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ENV     = os.getenv("CHANNEL_ID", "").strip()
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "").strip()
GPT_MODEL       = os.getenv("GPT_MODEL", "gpt-4o-mini").strip()

# Режим постов
POST_MODE       = os.getenv("POST_MODE", "mixed").lower()  # short|long|mixed
LONG_POST_SHARE = int(os.getenv("LONG_POST_SHARE", "20"))  # % длинных постов (для mixed)

# Язык/стиль
POST_LANG       = os.getenv("POST_LANG", "ru").lower()
FACT_RATIO      = float(os.getenv("FACT_RATIO", "0.85"))   # 85% факты / 15% ирония
SARCASM_LEVEL   = os.getenv("SARCASM_LEVEL", "medium")     # none|low|medium|high

# Интервалы
POST_INTERVAL_MIN = int(os.getenv("POST_INTERVAL_MIN", "30"))  # плановый интервал
RSS_POLL_SEC      = int(os.getenv("RSS_POLL_SEC", "120"))      # частота опроса RSS

# Длины
SHORT_MIN = int(os.getenv("SHORT_MIN_CHARS", "400"))
SHORT_MAX = int(os.getenv("SHORT_MAX_CHARS", "600"))
LONG_MIN  = int(os.getenv("LONG_MIN_CHARS",  "1000"))
LONG_MAX  = int(os.getenv("LONG_MAX_CHARS",  "1200"))

# Breaking
ALLOW_BREAKING = os.getenv("ALLOW_BREAKING", "true").lower() == "true"
_breaking_raw  = os.getenv("BREAKING_KEYWORDS_RU", "срочно;молния;breaking;urgent")
BREAKING_WORDS = [w.strip().lower() for w in re.split(r"[;,]", _breaking_raw) if w.strip()]

# Источники/темы/рубрики
FEEDS_JSON       = os.getenv("FEEDS_JSON", "[]")
TOPIC_FILTERS    = json.loads(os.getenv("TOPIC_FILTERS_JSON", "[]"))  # список regex
RUBRICS          = json.loads(os.getenv("RUBRICS_JSON", "{}"))        # {regex: {emoji,title}}

# Блоки поста
SOURCE_BLOCK     = os.getenv("SOURCE_BLOCK", "on").lower() == "on"
ALLOW_IMAGES     = os.getenv("ALLOW_IMAGES", "true").lower() == "true"

# Комплаенс РФ (иноагенты/запрещённые)
LEGAL_ENABLED        = os.getenv("LEGAL_FOOTNOTES", "on").lower() == "on"
LEGAL_REFRESH_HOURS  = int(os.getenv("LEGAL_REFRESH_HOURS", "24"))
EXTRA_ALIASES_ENV    = os.getenv("COMPLIANCE_EXTRA_ALIASES", "").strip()
try:
    EXTRA_ALIASES = json.loads(EXTRA_ALIASES_ENV) if EXTRA_ALIASES_ENV else []
except Exception:
    EXTRA_ALIASES = []

# Валидация критичных ENV
if not BOT_TOKEN or not CHANNEL_ENV or not OPENAI_API_KEY:
    raise SystemExit("ENV обязательны: BOT_TOKEN, CHANNEL_ID, OPENAI_API_KEY")

try:
    CHANNEL_ID: int | str = int(CHANNEL_ENV) if CHANNEL_ENV.startswith("-100") else CHANNEL_ENV
except Exception:
    CHANNEL_ID = CHANNEL_ENV  # позволим @username

# ----------------- КЛИЕНТЫ -----------------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Отключаем любые прокси из окружения (фикс проблем с 'proxies' в SDK OpenAI)
for k in ["HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy","OPENAI_PROXY"]:
    os.environ.pop(k, None)

http_client = httpx.Client(trust_env=False, timeout=30.0)
oai = OpenAI(api_key=OPENAI_API_KEY, http_client=http_client)

# ----------------- МОДЕЛИ -----------------
@dataclass
class NewsItem:
    title: str
    summary: str
    link: str
    published: Optional[datetime]
    breaking: bool

# ----------------- ОЧЕРЕДЬ/ДЕДУП -----------------
seen_ids: set[str] = set()
news_q: "queue.Queue[NewsItem]" = queue.Queue()

# ----------------- УТИЛИТЫ -----------------
def strip_html(s: str) -> str:
    if not s:
        return ""
    # Быстро уберём теги
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def is_breaking(title: str, summary: str) -> bool:
    t = f"{title} {summary}".lower()
    return any(w and w in t for w in BREAKING_WORDS)

def allowed_topic(title: str, summary: str) -> bool:
    if not TOPIC_FILTERS:
        return True
    text = f"{title}\n{summary}".lower()
    return any(re.search(rgx, text) for rgx in TOPIC_FILTERS)

def load_feeds() -> list[str]:
    try:
        arr = json.loads(FEEDS_JSON)
        return [x for x in arr if isinstance(x, str)]
    except Exception:
        return []

def domain_of(url: str) -> str:
    try:
        return tldextract.extract(url).registered_domain or ""
    except Exception:
        return ""

def pick_rubric(title: str, summary: str, link: str) -> tuple[str, str]:
    text = f"{title}\n{summary}\n{link}".lower()
    # Приоритет: пользовательские правила
    for regex, cfg in RUBRICS.items():
        try:
            if re.search(regex, text):
                return cfg.get("emoji", ""), cfg.get("title", "")
        except re.error:
            continue
    # Fallback по домену/ключевым словам
    dom = domain_of(link)
    if "coin" in dom or "crypto" in dom:
        return "🪙", "Крипто"
    if re.search(r"\b(ai|искусств.*интеллект|нейросет|openai|nvidia)\b", text):
        return "🤖", "Технологии и AI"
    return "📈", "Рынки и экономика"

def pick_length() -> tuple[int, int, bool]:
    # учитываем POST_MODE и LONG_POST_SHARE
    if POST_MODE == "short":
        return (SHORT_MIN, SHORT_MAX, False)
    if POST_MODE == "long":
        return (LONG_MIN, LONG_MAX, True)
    # mixed
    import random
    if random.randint(1, 100) <= LONG_POST_SHARE:
        return (LONG_MIN, LONG_MAX, True)
    return (SHORT_MIN, SHORT_MAX, False)

def sarcasm_hint() -> str:
    return {
        "high":   "Добавь заметный, но уместный сарказм (не переходи на личности).",
        "medium": "Добавь лёгкую иронию/сарказм (очень умеренно).",
        "low":    "Добавь едва заметную иронию.",
        "none":   "Без сарказма.",
    }.get(SARCASM_LEVEL, "Добавь лёгкую иронию/сарказм (очень умеренно).")

# ----------------- GPT -----------------
def build_prompt(title: str, summary: str, link: str, rubric: str, lo: int, hi: int) -> str:
    return f"""
Ты — финансовый редактор русскоязычного телеграм-канала.
Стиль: {int(FACT_RATIO*100)}% фактура / {100-int(FACT_RATIO*100)}% ирония; короткие абзацы; без хэштегов.
Опирайся только на входные данные (title/summary/link). Никакой выдумки.

Выведи СТРОГО по секциям:

1) Лид — 1–2 предложения, по сути.
2) Факты — 2–3 маркера с цифрами/конкретикой исключительно из входных данных.
3) Что это значит? — 1–2 прикладных вывода для читателя/инвестора.
4) Остроумная реплика — 1 короткая строка, уместная и не токсичная.

Ограничения:
- Если цифр нет — укажи ключевые драйверы/риски вместо цифр.
- Умеренная ирония (примерно 15% тона). {sarcasm_hint()}
- Без призывов подписываться, без хэштегов.

Рубрика: {rubric}
Требуемый объём: {lo}-{hi} символов.
Заголовок: {title}
Кратко: {summary}
Ссылка: {link}
"""

def gpt_generate_post(title: str, summary: str, link: str, emoji: str, rubric: str) -> str:
    lo, hi, is_long = pick_length()
    prompt = build_prompt(title, summary, link, rubric, lo, hi)
    resp = oai.chat.completions.create(
        model=GPT_MODEL,
        messages=[
            {"role": "system", "content": "Ты опытный финансовый редактор. Пиши на русском, ясно и сдержанно."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.6, top_p=0.9, max_tokens=800
    )
    body = (resp.choices[0].message.content or "").strip()
    header = f"{emoji} <b>{rubric}</b>\n" if rubric else ""
    return header + body

# ----------------- КОМПЛАЕНС РФ -----------------
URL_MINJUST = "https://minjust.gov.ru/ru/documents/7822/"
URL_FSB     = "http://www.fsb.ru/fsb/npd/terror.htm"

# Базовые алиасы
BASE_ALIASES = [
    {"canonical": "Meta", "aliases": ["Meta", "Facebook", "Instagram", "WhatsApp", "Messenger", "Threads"]},
]

def _norm(s: str) -> str:
    s = s.lower().strip()
    s = s.replace("ё", "е")
    s = re.sub(r"[«»“”\"'`’]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s

_legal_cache = {"ts": None, "agents": set(), "banned": set(), "alias_map": {}}

def _build_alias_map(canonicals: set) -> dict:
    amap = {}
    # канонические строки тоже алиасы
    for c in canonicals:
        n = _norm(c)
        if n:
            amap[n] = c
    # базовые алиасы
    for item in BASE_ALIASES:
        canon = item.get("canonical", "").strip()
        if not canon: continue
        amap[_norm(canon)] = canon
        for a in item.get("aliases", []):
            amap[_norm(a)] = canon
    # дополнительные от пользователя
    for item in EXTRA_ALIASES:
        canon = item.get("canonical", "").strip()
        if not canon: continue
        amap[_norm(canon)] = canon
        for a in item.get("aliases", []):
            amap[_norm(a)] = canon
    return amap

def _http_get_text(url: str) -> str:
    r = http_client.get(url)
    r.raise_for_status()
    return r.text

def _extract_candidates(html_text: str) -> set:
    soup = BeautifulSoup(html_text, "lxml")
    text = soup.get_text("\n", strip=True)
    cands = set()
    for line in text.splitlines():
        line = line.strip()
        if not line: continue
        if re.search(r"[А-ЯA-Z]", line) and len(line) > 3:
            for part in re.split(r"[;,•\u2022]", line):
                p = part.strip()
                if len(p) >= 3 and re.search(r"[А-ЯA-Z]", p):
                    cands.add(p)
    return cands

def _refresh_legal_if_needed(force: bool = False):
    if not LEGAL_ENABLED:
        return
    now = datetime.utcnow()
    if not force and _legal_cache["ts"] and now - _legal_cache["ts"] < timedelta(hours=LEGAL_REFRESH_HOURS):
        return
    try:
        agents = _extract_candidates(_http_get_text(URL_MINJUST))
    except Exception as e:
        log.warning("MINJUST fetch failed: %s", e)
        agents = _legal_cache.get("agents", set())
    try:
        banned = _extract_candidates(_http_get_text(URL_FSB))
    except Exception as e:
        log.warning("FSB fetch failed: %s", e)
        banned = _legal_cache.get("banned", set())

    alias_map = _build_alias_map(agents | banned)
    _legal_cache.update({"ts": now, "agents": set(agents), "banned": set(banned), "alias_map": alias_map})
    log.info("Compliance refreshed: agents=%d, banned=%d, aliases=%d",
             len(agents), len(banned), len(alias_map))

def _apply_star_once(text: str, token: str) -> str:
    # добавляем * сразу после первого «чистого» вхождения
    pat = re.compile(rf"(?<!\*)\b{re.escape(token)}\b")
    m = pat.search(text)
    if m:
        i = m.end()
        if not (i < len(text) and text[i] == "*"):
            text = text[:i] + "*" + text[i:]
    return text

def apply_legal_marks(text: str) -> str:
    if not LEGAL_ENABLED: return text
    _refresh_legal_if_needed()

    notes = []
    out = text
    text_norm = _norm(text)

    # найдём алиасы, которые реально присутствуют
    found = {}
    for alias_norm, canonical in _legal_cache["alias_map"].items():
        if alias_norm and alias_norm in text_norm:
            typ = None
            if canonical in _legal_cache["agents"]:
                typ = "agent"
            if canonical in _legal_cache["banned"]:
                typ = "banned" if typ is None else typ
            if typ:
                found.setdefault(canonical, typ)

    # проставляем звёздочки и набираем футноты
    for canonical, typ in found.items():
        # попробуем несколько форм токена
        candidates = {canonical, canonical.capitalize()}
        for cand in sorted(candidates, key=len, reverse=True):
            out = _apply_star_once(out, cand)

        # формулировки сносок — как договорено (курсив)
        if typ == "banned":
            if _norm(canonical) == "meta":
                note = "_*Meta признана экстремистской и запрещена в РФ._"
            else:
                note = "_*Организация признана экстремистской и запрещена на территории РФ._"
        else:
            note = "_*Признан иностранным агентом на территории России_"
        if note not in notes:
            notes.append(note)

    if notes:
        out += "\n\n" + "\n".join(notes)
    return out

# ----------------- ПОСТИНГ -----------------
def send_text(text: str, image_url: Optional[str] = None):
    try:
        if image_url:
            bot.send_photo(CHANNEL_ID, image_url, caption=(text[:1024] if text else ""))
        else:
            bot.send_message(CHANNEL_ID, text[:4096])
    except Exception as e:
        log.error("Telegram send error: %s", e)
        raise

# ----------------- RSS POLLER -----------------
def poller():
    feeds = load_feeds()
    if not feeds:
        log.warning("FEEDS_JSON пуст — нет лент для опроса.")
        return
    log.info("RSS poller: %d лент, интервал %d сек.", len(feeds), RSS_POLL_SEC)

    while True:
        try:
            for url in feeds:
                try:
                    d = feedparser.parse(url)
                    for e in d.entries[:40]:
                        link = getattr(e, "link", "") or ""
                        if not link:
                            continue
                        uid = getattr(e, "id", "") or link
                        if uid in seen_ids:
                            continue

                        title = (getattr(e, "title", "") or "").strip()
                        summary = strip_html(getattr(e, "summary", "") or getattr(e, "description", "") or "")
                        if not title and not summary:
                            continue

                        if not allowed_topic(title, summary):
                            continue

                        item = NewsItem(
                            title=title,
                            summary=summary,
                            link=link,
                            published=None,
                            breaking=is_breaking(title, summary),
                        )
                        seen_ids.add(uid)
                        news_q.put(item)
                except Exception as fe:
                    log.warning("Feed error %s: %s", url, fe)
        except Exception as e:
            log.exception("RSS poller error: %s", e)

        time.sleep(RSS_POLL_SEC)

# ----------------- ПУБЛИКАТОР -----------------
def publisher():
    log.info("Publisher: интервал %d мин, breaking=%s, mode=%s", POST_INTERVAL_MIN, ALLOW_BREAKING, POST_MODE)
    last_post_ts = 0.0
    interval = max(10, POST_INTERVAL_MIN * 60)

    while True:
        try:
            # Срочные — сразу
            if ALLOW_BREAKING:
                try:
                    it = news_q.get_nowait()
                    if it.breaking:
                        log.info("BREAKING: %s", it.title[:120])
                        publish_item(it)
                        last_post_ts = time.time()
                        continue
                    else:
                        news_q.put(it)
                except queue.Empty:
                    pass

            # Плановая публикация
            if time.time() - last_post_ts >= interval:
                try:
                    it = news_q.get_nowait()
                except queue.Empty:
                    log.info("Очередь пуста, ждём...")
                    time.sleep(3)
                    continue

                log.info("Scheduled post: %s", it.title[:120])
                publish_item(it)
                last_post_ts = time.time()

            time.sleep(2)
        except Exception as e:
            log.exception("Publisher error: %s", e)
            time.sleep(5)

def publish_item(item: NewsItem):
    emoji, rubric = pick_rubric(item.title, item.summary, item.link)
    text = gpt_generate_post(item.title, item.summary, item.link, emoji, rubric)
    # Комплаенс РФ
    text = apply_legal_marks(text)

    image_url = None
    if ALLOW_IMAGES:
        # feedparser enclosures
        # многие RSS кладут картинки в e.media_content / e.enclosures — мы взяли из summary,
        # поэтому пробуем найти прямую ссылку-изображение в первом встречном img тегe
        m = re.search(r'(https?://\S+\.(?:jpg|jpeg|png|gif))', item.summary, re.IGNORECASE)
        if m:
            image_url = m.group(1)

    send_text(text, image_url)

# ----------------- MAIN -----------------
def main():
    me = bot.get_me()
    log.info("Telegram бот: @%s", me.username)

    # Принудительно обновим комплаенс-списки на старте (если включено)
    _refresh_legal_if_needed(force=True)

    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=publisher, daemon=True).start()

    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
