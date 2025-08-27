# main.py
import os
import time
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
import telebot
import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI

# ------------------------------------
# ЛОГИРОВАНИЕ
# ------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# ------------------------------------
# ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID_ENV = os.getenv("CHANNEL_ID", "").strip()
SHEET_ID = os.getenv("SHEET_ID", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

if not BOT_TOKEN or not CHANNEL_ID_ENV or not SHEET_ID or not OPENAI_API_KEY:
    logging.error("Не заданы одна или несколько переменных окружения: "
                  "BOT_TOKEN, CHANNEL_ID, SHEET_ID, OPENAI_API_KEY")
    raise SystemExit(1)

# CHANNEL_ID строго int (формат для каналов: -100xxxxxxxxxx)
try:
    CHANNEL_ID = int(CHANNEL_ID_ENV)
except ValueError:
    logging.error(
        "CHANNEL_ID должен быть целым числом (например -1001234567890). "
        f"Сейчас: {CHANNEL_ID_ENV}"
    )
    raise SystemExit(1)

# ------------------------------------
# ИНИЦИАЛИЗАЦИЯ TELEGRAM
# ------------------------------------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ------------------------------------
# Google Sheets
# Секрет с ключом сервисного аккаунта лежит в Secret Files как: credentials.json
# Render смонтирует его по пути: /etc/secrets/credentials.json
# ------------------------------------
GOOGLE_CREDS_FILE = "/etc/secrets/credentials.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

try:
    _creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=SCOPES)
    _gclient = gspread.authorize(_creds)
    sh = _gclient.open_by_key(SHEET_ID)
    sheet = sh.sheet1  # работаем с первым листом
    logging.info("Google Sheets: подключение успешно.")
except Exception as e:
    logging.error("Google Sheets: не удалось подключиться. " + str(e))
    raise

# ------------------------------------
# OpenAI (НОВЫЙ SDK)
# Вырезаем любые прокси-переменные из окружения и говорим клиенту их игнорировать
# ------------------------------------
for _v in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
           "http_proxy", "https_proxy", "all_proxy", "OPENAI_PROXY"]:
    os.environ.pop(_v, None)

client = OpenAI(
    api_key=OPENAI_API_KEY,
    http_client=httpx.Client(trust_env=False)  # критично: не подтягивать прокси из окружения
)

# ------------------------------------
# УТИЛИТЫ
# ------------------------------------
def now_utc_minutes() -> int:
    """Текущие минуты с эпохи (для внутренних таймеров)."""
    return int(datetime.now(timezone.utc).timestamp() // 60)

def parse_schedule_cell(value: str) -> Optional[datetime]:
    """
    Парсинг даты/времени из ячейки «Время публикации».
    Ожидается строка формата 'YYYY-MM-DD HH:MM' (UTC).
    Если формат другой — возвращаем None и публикуем сразу,
    как только дойдём до этой строки.
    """
    v = (value or "").strip()
    if not v:
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except Exception:
        return None

def normalize_text(x: str) -> str:
    return (x or "").strip()

# ------------------------------------
# GPT: генерация текста поста
# ------------------------------------
def generate_post(theme_hint: str) -> str:
    """
    Генерируем пост по краткой подсказке (theme_hint) на русском.
    80% – короткие заметки (400–600 символов), 20% – мини-аналитика (1000–1200).
    Без хештегов. Допускаем юмор и лёгкий сарказм. Историческая справка – mixed.
    """
    prompt = f"""
Ты — автор телеграм-канала о финансах и бизнес-трендах. Пиши на русском.
Тема/повод: «{theme_hint}».

Правила:
1) 80% постов — 400–600 символов, 20% — 1000–1200 символов.
2) Без хештегов, эмодзи можно, но умеренно.
3) Стиль: живо, умно, чуть саркастично; без перехода на личности.
4) Если уместно — дай короткую историческую ремарку (1–2 предложения).
5) Не дублируй тему в заголовке; сразу в суть.
6) Не проси подписаться, не делай CTA.

Выдай только чистый текст поста (без префиксов и маркировок).
"""
    try:
        resp = client.responses.create(
            model="gpt-4o-mini",
            input=prompt
        )
        text = (resp.output_text or "").strip()
        if not text:
            raise ValueError("Пустой ответ GPT")
        return text
    except Exception as e:
        logging.error(f"GPT: ошибка генерации — {e}")
        # Фолбэк: хотя бы вернём тему
        return f"Короткая заметка: {theme_hint}"

# ------------------------------------
# Telegram отправка
# ------------------------------------
def send_to_telegram(text: str, image_url: str = "") -> None:
    """
    Отправка в канал: с изображением (если есть) или просто текст.
    """
    try:
        if normalize_text(image_url):
            # Сначала фото, текст в caption
            bot.send_photo(CHANNEL_ID, image_url, caption=text)
        else:
            bot.send_message(CHANNEL_ID, text)
    except Exception as e:
        logging.error(f"Telegram: ошибка отправки — {e}")
        raise

# ------------------------------------
# Основной цикл:
# Таблица (первый лист) с колонками:
# A: «Время публикации» (UTC, 'YYYY-MM-DD HH:MM') — можно пусто
# B: «Текст поста» — если пусто, сгенерируем через GPT
# C: «Ссылка на изображение» — можно пусто
# D: «Статус» — пусто = не опубликовано; пишем OK / ERROR:...
# ------------------------------------
HEADER_TIME = "Время публикации"
HEADER_TEXT = "Текст поста"
HEADER_IMAGE = "Ссылка на изображение"
HEADER_STATUS = "Статус"

def read_rows():
    """Чтение всех строк с учётом заголовка."""
    rows = sheet.get_all_records(default_blank="")
    return rows

def write_status(row_index_one_based: int, status_text: str):
    """
    Запись статуса в колонку D (Статус).
    row_index_one_based — индекс строки с 1 с учётом заголовков.
    """
    try:
        sheet.update_cell(row_index_one_based, 4, status_text)
    except Exception as e:
        logging.error(f"Sheets: не удалось записать статус для строки {row_index_one_based}: {e}")

def process_row(row: dict, row_index_one_based: int):
    """
    Обработка одной строки.
    Если 'Статус' пуст, а время <= сейчас (или пусто) — публикуем.
    """
    status = normalize_text(row.get(HEADER_STATUS, ""))
    if status:
        return  # уже обработано

    time_cell = normalize_text(row.get(HEADER_TIME, ""))
    text_cell = normalize_text(row.get(HEADER_TEXT, ""))
    image_cell = normalize_text(row.get(HEADER_IMAGE, ""))

    # Проверяем расписание (UTC)
    when_dt = parse_schedule_cell(time_cell)
    if when_dt and datetime.now(timezone.utc) < when_dt:
        # Ещё не настало
        return

    # Если текста нет — просим GPT сделать пост по намёку
    # Намёком считаем либо указанный «Текст поста» (как тема),
    # либо «Публикация», если вообще пусто.
    if not text_cell:
        text_cell = generate_post(theme_hint="Публикация для канала о финансах и трендах")
    else:
        # Если автор сам дал тему-рыбу (очень коротко), можно попросить GPT развернуть.
        if len(text_cell) < 60:
            text_cell = generate_post(theme_hint=text_cell)

    # Отправляем в Telegram
    try:
        send_to_telegram(text_cell, image_cell)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        write_status(row_index_one_based, f"OK {stamp}")
        logging.info(f"Опубликовано: строка {row_index_one_based}")
    except Exception as e:
        write_status(row_index_one_based, f"ERROR: {e}")
        logging.error(f"Публикация НЕ удалась (строка {row_index_one_based}): {e}")

def main_loop():
    logging.info("Сервис запущен. Работаем по таблице Google Sheets.")
    while True:
        try:
            rows = read_rows()
            # get_all_records возвращает данные без строки заголовка,
            # поэтому первая «данная» строка = 2 в таблице
            for i, row in enumerate(rows, start=2):
                process_row(row, i)
        except Exception as e:
            logging.error(f"Главный цикл: ошибка чтения/обработки — {e}")

        # Проверяем таблицу каждую минуту
        time.sleep(60)

if __name__ == "__main__":
    main_loop()
