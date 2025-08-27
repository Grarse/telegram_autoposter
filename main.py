# main.py — RSS → GPT → Telegram (полностью автономно)
import os
import time
import json
import logging
import threading
import re
import queue
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import feedparser
import httpx
from openai import OpenAI
import telebot

# ----------------- ЛОГИ -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("autoposter")

# ----------------- ENV -----------------
BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ENV = os.getenv("CHANNEL_ID", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

GPT_MODEL   = os.getenv("GPT_MODEL", "gpt-4o-mini")

# Интервалы и поведение
POST_INTERVAL_MIN = int(os.getenv("POST_INTERVAL_MIN", "30"))  # каждые 30 минут
RSS_POLL_SEC      = int(os.getenv("RSS_POLL_SEC", "120"))      # опрос лент раз в 120 сек
ALLOW_BREAKING    = os.getenv("ALLOW_BREAKING", "1") == "1"    # “срочное” — публиковать сразу

# Стиль
SARCASM_LEVEL     = os.getenv("SARCASM_LEVEL", "medium")       # none|low|medium|high
POST_HISTORY_MODE = os.getenv("POST_HISTORY_MODE", "mixed")    # off|mixed|always
LONG_POST_SHARE   = float(os.getenv("LONG_POST_SHARE", "0.20"))# 20% длинные посты
SHORT_MIN, SHORT_MAX = int(os.getenv("SHORT_MIN_CHARS", "400")), int(os.getenv("SHORT_MAX_CHARS", "600"))
LONG_MIN,  LONG_MAX  = int(os.getenv("LONG_MIN_CHARS",  "1000")), int(os.getenv("LONG_MAX_CHARS",  "1200"))

# Ключевые слова “молний”
BREAKING_WORDS = [w.strip().lower() for w in os.getenv("BREAKING_KEYWORDS_RU","срочно;молния;экстренно;urgent;breaking").split(";")]

# RSS-ленты: одна строка JSON-массива
FEEDS_JSON = os.getenv("FEEDS_JSON", "[]")

# Валидация
if not BOT_TOKEN or not CHANNEL_ENV or not OPENAI_API_KEY:
    raise SystemExit("ENV обязательны: BOT_TOKEN, CHANNEL_ID, OPENAI_API_KEY")

try:
    CHANNEL_ID: int | str = int(CHANNEL_ENV) if CHANNEL_ENV.startswith("-100") else CHANNEL_ENV
except Exception:
    CHANNEL_ID = CHANNEL_ENV  # позволяем @username

# ----------------- КЛИЕНТЫ -----------------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Жёстко отрубаем прокси из окружения (чтобы не было ошибки 'proxies')
for k in ["HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy","OPENAI_PROXY"]:
    os.environ.pop(k, None)

oai = OpenAI(api_key=OPENAI_API_KEY, http_client=httpx.Client(trust_env=False))

# ----------------- МОДЕЛИ ДАННЫХ -----------------
@dataclass
class NewsItem:
    title: str
    link: str
    summary: str
    published: Optional[datetime]
    breaking: bool

# ----------------- ОЧЕРЕДЬ/ДЕДУП -----------------
seen_links: set[str] = set()
news_q: "queue.Queue[NewsItem]" = queue.Queue()

# ----------------- ХЕЛПЕРЫ -----------------
def is_breaking(title: str, summary: str) -> bool:
    t = (title or "").lower() + " " + (summary or "").lower()
    return any(w and w in t for w in BREAKING_WORDS)

def parse_time(entry) -> Optional[datetime]:
    # feedparser возвращает published_parsed/updated_parsed как time.struct_time
    for attr in ("published_parsed", "updated_parsed"):
        ts = getattr(entry, attr, None)
        if ts:
            try:
                dt = datetime(*ts[:6], tzinfo=timezone.utc).astimezone()
                return dt
            except Exception:
                pass
    return None

def load_feeds() -> list[str]:
    try:
        arr = json.loads(FEEDS_JSON)
        return [x for x in arr if isinstance(x, str)]
    except Exception:
        return []

def pick_length() -> tuple[int,int,bool]:
    """возвращает (min, max, is_long)"""
    import random
    if random.random() < LONG_POST_SHARE:
        return (LONG_MIN, LONG_MAX, True)
    return (SHORT_MIN, SHORT_MAX, False)

def want_history() -> bool:
    if POST_HISTORY_MODE == "always": return True
    if POST_HISTORY_MODE == "off":    return False
    # mixed — примерно каждый третий
    return (int(time.time() // (POST_INTERVAL_MIN*60)) % 3) == 0

def sarcasm_hint() -> str:
    return {
        "high":   "Добавь заметный, но уместный сарказм.",
        "medium": "Добавь лёгкий сарказм.",
        "low":    "Добавь едва заметную иронию.",
        "none":   "Без сарказма."
    }.get(SARCASM_LEVEL, "Добавь лёгкий сарказм.")

# ----------------- GPT -----------------
def make_post(item: NewsItem) -> str:
    lo, hi, is_long = pick_length()
    hist = "Добавь короткую историческую параллель (1–2 предложения)." if want_history() else "Историческую справку не добавляй."
    sys = (
        "Ты редактор русскоязычного телеграм-канала о финансах/экономике. "
        "Пиши ясно, без воды, с лёгким юмором/сарказмом, но без токсичности и без хештегов."
    )
    user = (
        f"Сформируй {'мини-аналитику' if is_long else 'короткую заметку'} на русском языком.\n"
        f"Требуемый объём: {lo}-{hi} символов.\n"
        f"{sarcasm_hint()}\n{hist}\n\n"
        f"Новость:\nЗаголовок: {item.title}\n"
        f"Кратко: {item.summary[:600]}\n"
        f"Ссылка: {item.link}\n\n"
        "Выведи только готовый текст поста, без хештегов и призывов подписаться."
    )
    resp = oai.chat.completions.create(
        model=GPT_MODEL,
        messages=[{"role":"system","content":sys},{"role":"user","content":user}],
        temperature=0.7,
        max_tokens=800,
    )
    return (resp.choices[0].message.content or "").strip()

# ----------------- ПОСТИНГ -----------------
def send_text(text: str):
    msg = text[:4096]
    bot.send_message(CHANNEL_ID, msg)

def publish(item: NewsItem):
    text = make_post(item)
    send_text(text)
    log.info("Опубликовано: %s", item.title)

# ----------------- ПОТОК ОПРОСА RSS -----------------
def poller():
    feeds = load_feeds()
    if not feeds:
        log.warning("FEEDS_JSON пуст — ленты не заданы.")
        return
    log.info("RSS poller: %d лент, интервал %d сек.", len(feeds), RSS_POLL_SEC)
    while True:
        try:
            for url in feeds:
                try:
                    d = feedparser.parse(url)
                    for e in d.entries[:20]:
                        link = getattr(e, "link", "") or ""
                        if not link or link in seen_links:
                            continue
                        title = getattr(e, "title", "") or ""
                        summary = re.sub("<[^>]+>", "", getattr(e, "summary", "") or "").strip()
                        item = NewsItem(
                            title=title.strip(),
                            link=link.strip(),
                            summary=summary,
                            published=parse_time(e),
                            breaking=is_breaking(title, summary),
                        )
                        seen_links.add(link)
                        news_q.put(item)
                except Exception as fe:
                    log.warning("Feed error %s: %s", url, fe)
        except Exception as e:
            log.exception("RSS poller error: %s", e)
        time.sleep(RSS_POLL_SEC)

# ----------------- ПОТОК ПУБЛИКАЦИИ -----------------
def publisher():
    log.info("Publisher: интервал %d мин, breaking=%s", POST_INTERVAL_MIN, ALLOW_BREAKING)
    last_post_ts = 0.0
    interval = POST_INTERVAL_MIN * 60
    while True:
        try:
            # Срочные — сразу
            if ALLOW_BREAKING:
                try:
                    it = news_q.get_nowait()
                    if it.breaking:
                        log.info("BREAKING: %s", it.title)
                        publish(it)
                        last_post_ts = time.time()
                        continue
                    else:
                        # Вернём, если не срочная
                        news_q.put(it)
                except queue.Empty:
                    pass

            # Плановая публикация по интервалу
            if time.time() - last_post_ts >= interval:
                item = None
                # Пытаемся взять новость из очереди
                for _ in range(10):
                    try:
                        item = news_q.get_nowait()
                        break
                    except queue.Empty:
                        time.sleep(1)
                if item:
                    log.info("Scheduled post: %s", item.title)
                    publish(item)
                    last_post_ts = time.time()
                else:
                    log.info("Очередь пуста, ждём...")
            time.sleep(2)
        except Exception as e:
            log.exception("Publisher error: %s", e)
            time.sleep(5)

# ----------------- MAIN -----------------
def main():
    # Проверим токен Telegram
    me = bot.get_me()
    log.info("Telegram бот: @%s", me.username)

    # Запускаем два фоновых потока
    threading.Thread(target=poller,   daemon=True).start()
    threading.Thread(target=publisher, daemon=True).start()

    # Keep-alive
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
