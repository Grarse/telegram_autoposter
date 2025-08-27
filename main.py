import os
import time
import feedparser
import telebot
import openai
import gspread
from google.oauth2.service_account import Credentials

# === ENV ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
SHEET_ID = os.getenv("SHEET_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")
POST_MODE = os.getenv("POST_MODE", "online")
POST_INTERVAL_MIN = int(os.getenv("POST_INTERVAL_MIN", "30"))
POST_LANG = os.getenv("POST_LANG", "ru")
SARCASM_LEVEL = os.getenv("SARCASM_LEVEL", "medium")
HASHTAGS = os.getenv("HASHTAGS", "off")
LONG_POST_SHARE = float(os.getenv("LONG_POST_SHARE", "0.2"))
SHORT_MIN_CHARS = int(os.getenv("SHORT_MIN_CHARS", "400"))
SHORT_MAX_CHARS = int(os.getenv("SHORT_MAX_CHARS", "600"))
LONG_MIN_CHARS = int(os.getenv("LONG_MIN_CHARS", "1000"))
LONG_MAX_CHARS = int(os.getenv("LONG_MAX_CHARS", "1200"))
POST_HISTORY_MODE = os.getenv("POST_HISTORY_MODE", "mixed")
RSS_POLL_SEC = int(os.getenv("RSS_POLL_SEC", "120"))

FEEDS_JSON = os.getenv("FEEDS_JSON", "[\"https://lenta.ru/rss\", \"https://www.rbc.ru/rss/\", \"https://tass.ru/rss/v2.xml\"]")
FEEDS = eval(FEEDS_JSON)

openai.api_key = OPENAI_API_KEY
bot = telebot.TeleBot(BOT_TOKEN)

# === Google Sheets ===
creds = Credentials.from_service_account_file("/etc/secrets/credentials.json", scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
client = gspread.authorize(creds)
sheet = client.open_by_key(SHEET_ID).sheet1

# === Функция генерации поста GPT ===
def generate_post(title, content):
    prompt = f"Напиши новостной пост на русском языке с юмором и сарказмом. Тема: {title}. Краткое содержание: {content}. Короткая заметка: {SHORT_MIN_CHARS}-{SHORT_MAX_CHARS} символов, длинная заметка: {LONG_MIN_CHARS}-{LONG_MAX_CHARS} символов."
    response = openai.ChatCompletion.create(
        model=GPT_MODEL,
        messages=[{"role": "system", "content": "Ты — новостной редактор с сарказмом."},
                  {"role": "user", "content": prompt}]
    )
    return response["choices"][0]["message"]["content"]

# === Основной цикл ===
seen_links = set()

def poll_rss():
    for url in FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries[:3]:
            if entry.link not in seen_links:
                seen_links.add(entry.link)
                text = generate_post(entry.title, entry.summary)
                try:
                    bot.send_message(CHANNEL_ID, f"{text}\n\nИсточник: {entry.link}")
                    print(f"Posted: {entry.title}")
                except Exception as e:
                    print(f"Telegram error: {e}")

if __name__ == "__main__":
    print("RSS poller started...")
    while True:
        poll_rss()
        time.sleep(RSS_POLL_SEC)
