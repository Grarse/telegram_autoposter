import os
import time
import telebot
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Настройки
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
SHEET_ID = os.getenv("SHEET_ID")

bot = telebot.TeleBot(BOT_TOKEN)

# Авторизация Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
sheet = client.open_by_key(SHEET_ID).sheet1

def send_post(text, image_url):
    if image_url:
        bot.send_photo(CHANNEL_ID, image_url, caption=text)
    else:
        bot.send_message(CHANNEL_ID, text)

while True:
    rows = sheet.get_all_records()
    for i, row in enumerate(rows, start=2):
        if row['Статус'].lower() != 'отправлено':
            if row['Ссылка на изображение']:
                send_post(row['Текст поста'], row['Ссылка на изображение'])
            else:
                send_post(row['Текст поста'], None)
            sheet.update_cell(i, 4, 'Отправлено')
    time.sleep(60)
