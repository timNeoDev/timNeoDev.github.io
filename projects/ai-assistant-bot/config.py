"""Настройки бота. Секреты берутся из файла .env."""
import os

from dotenv import load_dotenv

load_dotenv()  # также загружает ANTHROPIC_API_KEY для Claude SDK

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

BUSINESS_NAME = "«Зерно»"
ADDRESS = "ул. Примерная, 10"
PHONE = "+7 (900) 000-00-00"

# Сколько сообщений ИИ может получить один пользователь за сутки (защита бюджета)
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "30"))
# Сколько последних сообщений диалога помнит бот
HISTORY_SIZE = 20
