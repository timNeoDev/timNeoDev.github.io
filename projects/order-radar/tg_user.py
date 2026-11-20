"""Чтение Telegram-чатов от имени вашего аккаунта (Telethon). Вход — один раз через tg_login.py."""
import logging
from pathlib import Path

import sources
from config import TG_API_HASH, TG_API_ID, TG_SESSION

SESSION_PATH = str(Path(__file__).parent / TG_SESSION)


def is_configured() -> bool:
    return bool(TG_API_ID and TG_API_HASH and Path(SESSION_PATH + ".session").exists())


async def start():
    """Подключённый клиент Telethon или None, если аккаунт не подключён."""
    if not is_configured():
        return None
    from telethon import TelegramClient
    client = TelegramClient(SESSION_PATH, TG_API_ID, TG_API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        logging.warning("Telegram-аккаунт не авторизован — запустите python tg_login.py")
        await client.disconnect()
        return None
    return client


async def fetch_chat(client, username: str, limit: int = 30) -> list[dict]:
    """Последние сообщения публичного чата (группы) как заказы. Только чтение — ничего не отправляет."""
    from telethon.errors import RPCError
    try:
        entity = await client.get_entity(username)
        messages = await client.get_messages(entity, limit=limit)
    except (RPCError, ValueError) as e:
        raise ValueError(f"недоступен: {e}") from e
    orders = []
    for msg in messages:
        if not msg.message:
            continue
        sender = msg.sender  # Telethon приходит с отправителями в том же ответе — лишних запросов нет
        author = f"@{sender.username}" if getattr(sender, "username", None) else ""
        order = sources.message_to_order(msg.message, f"https://t.me/{username}/{msg.id}", f"TG @{username}",
                                         msg.date, msg.views or 0, author)
        if order:
            orders.append(order)
    return orders
