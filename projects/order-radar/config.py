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

# Kwork: раздел «Разработка и IT» (c=11), сколько первых страниц смотреть (по 12 заказов)
KWORK_URL = "https://kwork.ru/projects?c=11&page={page}"
KWORK_PAGES = 3

# «Горячий» заказ: моложе часа и не больше 3 предложений от фрилансеров
HOT_MAX_AGE_MIN = 60
HOT_MAX_OFFERS = 3

# Во сколько (по времени компьютера) бот присылает напоминания о дедлайнах и вечернюю сводку
REMINDER_HOUR = 10
DIGEST_HOUR = 20

# Начальные фильтры (дальше меняются в админке или командами бота)
DEFAULT_KEYWORDS = ["телеграм", "telegram", "бот", "лендинг", "landing", "сайт", "верстк", "парс", "html", "python"]
DEFAULT_MINUS_WORDS = ["дизайн", "фигм", "figma", "логотип", "seo"]
