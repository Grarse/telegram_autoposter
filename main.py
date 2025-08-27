# main.py
import os
import time
import logging
import traceback
from datetime import datetime, timezone
from typing import Optional

import telebot
import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI

# -----------------------------
# Настройки логгирования
# -----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# -----------------------------
# Переменные окружения
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID_ENV = os.getenv("CHANNEL_ID", "").strip()
SHEET_ID = os.getenv("SHEET_ID", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

if not BOT_TOKEN or not CHANNEL_ID_ENV or not SHEET_ID or not OPENAI_API_KEY:
    logging.error("Не заданы одна или несколько переменных окружения: "
                  "BOT_TOKEN, CHANNEL_ID, SHEET_ID, OPENAI_API_KEY")
    raise SystemExit(1)

# Telegram channel id должен быть int
try:
    CHANNEL_ID = int(CHANNEL_ID_ENV)
except ValueError:
    logging.error("CHANNEL_ID должен быть целым числом (например -1001234567890). "
                  f"Сейчас: {CHANNEL_ID_ENV}")
    raise SystemExit(1)

# -----------------------------
# Инициализация клиентов
# -----------------------------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Google Sheets (service account JSON лежит в Secret Files → /etc/secrets/credentials.json)
GOOGLE_CREDS_FILE = "/etc/secrets/credentials.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
try:
    _creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=SCOPES)
    gclient = gspread.authorize(_creds)
    sh = gclient.open_by_key(SHEET_ID)
    sheet = sh.sheet1  # работаем с первым листом
    logging.info("Google Sheets: подключение успешно.")
except Exception as e:
    logging.error("Google Sheets: не удалось подключиться. " + str(e))
    raise

# OpenAI (новый SDK)
client = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------
# Утилиты
# -----------------------------
def now_utc_minutes() -> int:
    """Минуты от эпохи (для сверки внутренних таймеров)."""
    return int(datetime.now(timezone.utc).timestamp() // 60)

def parse_schedule_cell(value: str) -> Optional[datetime]:
    """
    Парсинг даты/времени из ячейки 'Время публикации'.
    Ожидается строка формата 'YYYY-MM-DD HH:MM' (UTC).
    Если формат другой — можно донастроить парсер.
    """
    try:
        value = (value or "").strip()
        if not value:
            return None
        # Предполагаем UTC. Если нужно — подстрой под свой часовой пояс.
        return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except Exception:
        return None

def pick_length_hint() -> str:
    """
    80% короткие (400–600), 20% длинные (1000–1200).
    Возвращает текстовую подсказку для промпта.
    """
    import random
    if random.random() < 0.8:
        return "Короткая заметка 400–600 символов."
    return "Расширенная мини-аналитика 1000–1200 символов."

def historical_blend_hint() -> str:
    """
    Историческая справка: mixed (иногда короткая историческая ремарка).
    """
    import random
    if random.random() < 0.5:
        return "Добавь 1–2 предложения исторического контекста (если уместно)."
    return "Без исторического экскурса, только по сути."

def build_prompt(base: str) -> str:
    """
    Формирует промпт для GPT по нашим правилам (RU, сарказм/юмор, стиль и длина).
    """
    return f"""
Ты — редактор телеграм-канала о финансах и экономике. Пиши на русском.
Правила стиля:
- 80% постов — короткие заметки, 20% — мини-аналитики (в этом задании ориентируйся на подсказку ниже).
- Лёгкий юмор и ирония допустимы, но без токсичности и перехода на личности.
- Без хэштегов, эмодзи — можно 1–2 максимум, если очень уместно.
- Ясная структура: один основной тезис + 1–2 факта/цифры/пример + один вывод/что это значит.

Тема / сырьё:
{base}

Требования к объёму: {pick_length_hint()}
{historical_blend_hint()}

Выведи только готовый текст поста без приветствий и заключительных фраз типа "подписывайтесь".
"""

def generate_text_with_gpt(seed: str) -> str:
    """
    Генерация текста через новый OpenAI SDK.
    Используем chat.completions (новый способ через client.*).
    """
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты опытный финансовый редактор телеграм-канала."},
                {"role": "user", "content": build_prompt(seed)}
            ],
            temperature=0.7,
            top_p=0.9,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text
    except Exception as e:
        logging.error("OpenAI error: %s", e)
        return ""

def send_to_channel(text: str, image_url: Optional[str]) -> bool:
    """
    Публикация в канал. Если есть картинка — отправляем фото с подписью.
    """
    try:
        if image_url:
            # caption в Telegram ограничен ~1024 символами — подрежем при необходимости
            caption = (text or "")[:1024] if text else ""
            bot.send_photo(chat_id=CHANNEL_ID, photo=image_url, caption=caption)
        else:
            # Ограничение Telegram ~4096 символов — подрежем
            msg = (text or "")[:4096]
            bot.send_message(chat_id=CHANNEL_ID, text=msg)
        return True
    except Exception as e:
        logging.error("Ошибка отправки в Telegram: %s", e)
        return False

def process_sheet_once():
    """
    Считываем все строки, ищем те, где:
      - Время публикации <= сейчас (UTC)
      - Статус пуст
    Обрабатываем последовательно.
    """
    try:
        rows = sheet.get_all_records(expected_headers=[
            "Время публикации",
            "Текст поста",
            "Ссылка на изображение",
            "Статус",
        ])
        # gspread возвращает записи без индекса строки; запрашиваем все значения,
        # чтобы знать точные номера строк для обновления «Статуса».
        all_values = sheet.get_all_values()
        headers = all_values[0]

        # Индексы столбцов (для точечного обновления)
        idx_time = headers.index("Время публикации")
        idx_text = headers.index("Текст поста")
        idx_img = headers.index("Ссылка на изображение")
        idx_status = headers.index("Статус")

        # Пройдёмся по строкам, начиная со второй (первая — заголовок)
        for row_idx in range(1, len(all_values)):
            row = all_values[row_idx]
            try:
                cell_time = row[idx_time].strip() if idx_time < len(row) else ""
                cell_text = row[idx_text].strip() if idx_text < len(row) else ""
                cell_img = row[idx_img].strip() if idx_img < len(row) else ""
                cell_status = row[idx_status].strip() if idx_status < len(row) else ""

                # пропускаем обработанные
                if cell_status:
                    continue

                # проверяем расписание
                dt = parse_schedule_cell(cell_time)
                if not dt:
                    # Нет валидного времени — пока пропускаем
                    continue

                if datetime.now(timezone.utc) < dt:
                    # Ещё не время
                    continue

                # Если текст пуст — генерируем
                text_to_send = cell_text
                if not text_to_send:
                    seed = "Срочные финансовые и экономические новости дня. Короткий вывод, что это значит для частного инвестора."
                    text_to_send = generate_text_with_gpt(seed)
                    if not text_to_send:
                        # если GPT не сгенерировал, не блокируем всю очередь — ставим статус ошибки
                        sheet.update_cell(row_idx + 1, idx_status + 1, "Ошибка GPT")
                        continue

                ok = send_to_channel(text_to_send, cell_img if cell_img else None)
                sheet.update_cell(row_idx + 1, idx_status + 1, "ОК" if ok else "Ошибка Telegram")

                # Чтобы не улететь в rate-limit Telegram при пачке постов
                time.sleep(2)

            except Exception as row_err:
                logging.error("Ошибка обработки строки %s: %s", row_idx + 1, row_err)
                try:
                    sheet.update_cell(row_idx + 1, idx_status + 1, "Ошибка обработки")
                except Exception:
                    pass

    except Exception as e:
        logging.error("Ошибка чтения/обработки Google Sheets: %s", e)
        logging.debug(traceback.format_exc())

# -----------------------------
# MAIN LOOP
# -----------------------------
if __name__ == "__main__":
    logging.info("Старт автопостера: Telegram + Google Sheets + GPT")
    while True:
        try:
            process_sheet_once()
        except Exception as loop_err:
            logging.error("Критическая ошибка цикла: %s", loop_err)
            logging.debug(traceback.format_exc())
        # Проверяем таблицу каждую минуту
        time.sleep(60)
