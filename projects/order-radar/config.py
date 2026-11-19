"""Настройки «Радара заказов». Секреты берутся из файла .env."""
import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
# Пароль для входа в веб-админку
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

# Как часто проверять биржи, секунд
POLL_SECONDS = 180

# Ленты заказов (RSS). У Хабр Фриланса и Kwork публичной RSS-ленты нет.
FEEDS = [
    ("FL.ru", "https://www.fl.ru/rss/all.xml"),
]

# Ключевые слова при первом запуске (дальше меняются в админке или командами бота)
DEFAULT_KEYWORDS = ["телеграм", "telegram", "бот", "лендинг", "landing", "сайт", "верстк", "парс", "html", "python"]
