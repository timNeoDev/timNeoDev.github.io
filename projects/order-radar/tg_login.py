"""Однократный вход в ваш Telegram-аккаунт, чтобы радар мог читать чаты с заказами.
Запуск: python tg_login.py — спросит номер телефона и код из Telegram.
"""
import asyncio

from telethon import TelegramClient

import db
from tg_user import SESSION_PATH, credentials


async def main():
    db.init_db()
    TG_API_ID, TG_API_HASH = credentials()
    if not (TG_API_ID and TG_API_HASH):
        raise SystemExit("Добавьте TG_API_ID и TG_API_HASH в .env — их выдают на https://my.telegram.org → API development tools")
    async with TelegramClient(SESSION_PATH, TG_API_ID, TG_API_HASH) as client:  # спросит телефон и код
        me = await client.get_me()
        print(f"Готово: вход выполнен как @{me.username or me.first_name}. Перезапустите радар.")


if __name__ == "__main__":
    asyncio.run(main())
