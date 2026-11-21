"""Отдельный Telegram-аккаунт для парсинга (Telethon): вход из админки, чтение каналов и чатов.

Аккаунт только читает сообщения и ничего не отправляет. Лучше подключать не основной аккаунт:
если Telegram ограничит его за частые запросы, основной не пострадает.
"""
import logging
from pathlib import Path

import db
import sources
from config import TG_API_HASH, TG_API_ID, TG_SESSION

SESSION_PATH = str(Path(__file__).parent / TG_SESSION)
CLIENT = None   # подключённый и авторизованный клиент — его берёт опрос на каждом проходе
_pending = {}   # незавершённый вход из админки: client, phone, hash, need_password


def credentials() -> tuple[int, str]:
    """api_id и api_hash: из админки, а если там пусто — из .env."""
    api_id = db.get_setting("tg_api_id") or TG_API_ID
    return int(api_id or 0), db.get_setting("tg_api_hash") or TG_API_HASH


def is_configured() -> bool:
    return Path(SESSION_PATH + ".session").exists()


def login_step() -> str:
    """'' — вход не начат, 'code' — ждём код из Telegram, 'password' — ждём пароль двухэтапной защиты."""
    if not _pending:
        return ""
    return "password" if _pending.get("need_password") else "code"


def _new_client():
    from telethon import TelegramClient
    api_id, api_hash = credentials()
    if not (api_id and api_hash):
        raise ValueError("нужны api_id и api_hash с my.telegram.org")
    return TelegramClient(SESSION_PATH, api_id, api_hash)


async def start():
    """При запуске радара: поднимает сохранённую сессию, если аккаунт уже подключали."""
    global CLIENT
    if not is_configured():
        return None
    try:
        client = _new_client()
    except ValueError:
        return None
    await client.connect()
    if not await client.is_user_authorized():
        logging.warning("Telegram-аккаунт не авторизован — подключите его заново в настройках админки")
        await client.disconnect()
        return None
    CLIENT = client
    return client


async def account_name() -> str:
    if not CLIENT:
        return ""
    me = await CLIENT.get_me()
    return f"@{me.username}" if me.username else (me.first_name or "аккаунт")


# ---------- Вход из админки: телефон → код → (пароль) ----------

async def send_code(api_id: str, api_hash: str, phone: str) -> None:
    if not api_id.strip().isdigit() or not api_hash.strip():
        raise ValueError("api_id — число, api_hash — строка с my.telegram.org")
    db.set_setting("tg_api_id", api_id.strip())
    db.set_setting("tg_api_hash", api_hash.strip())
    await cancel_login()
    client = _new_client()
    await client.connect()
    sent = await client.send_code_request(phone.strip())
    _pending.update(client=client, phone=phone.strip(), hash=sent.phone_code_hash)


async def verify_code(code: str) -> bool:
    """True — вход выполнен; False — нужен пароль двухэтапной защиты."""
    from telethon.errors import SessionPasswordNeededError
    if "client" not in _pending:
        raise ValueError("сначала запросите код")
    try:
        await _pending["client"].sign_in(_pending["phone"], code.strip(), phone_code_hash=_pending["hash"])
    except SessionPasswordNeededError:
        _pending["need_password"] = True
        return False
    _finish_login()
    return True


async def verify_password(password: str) -> None:
    if "client" not in _pending:
        raise ValueError("сначала запросите код")
    await _pending["client"].sign_in(password=password)
    _finish_login()


def _finish_login() -> None:
    global CLIENT
    CLIENT = _pending.pop("client")
    _pending.clear()


async def cancel_login() -> None:
    client = _pending.pop("client", None)
    _pending.clear()
    if client:
        await client.disconnect()


async def logout() -> None:
    """Завершает сессию на серверах Telegram и удаляет файл сессии."""
    global CLIENT
    if CLIENT:
        await CLIENT.log_out()
    CLIENT = None
    Path(SESSION_PATH + ".session").unlink(missing_ok=True)


# ---------- Чтение ----------

async def fetch(client, username: str, limit: int = 30) -> list[dict]:
    """Последние сообщения канала или чата как заказы. Только чтение — ничего не отправляет."""
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
        sender = msg.sender  # отправители приходят в том же ответе — лишних запросов нет
        author = f"@{sender.username}" if getattr(sender, "username", None) else ""
        order = sources.message_to_order(msg.message, f"https://t.me/{username}/{msg.id}", f"TG @{username}",
                                         msg.date, msg.views or 0, author)
        if order:
            orders.append(order)
    return orders
