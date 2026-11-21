"""Радар заказов: сбор заказов с бирж + Telegram-бот + веб-админка в одном процессе.
Запуск: python main.py
"""
import asyncio
import logging
from datetime import date, datetime

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

import bot as tg
import db
import sources
import tg_user
from config import (ADMIN_ID, ADMIN_PASSWORD, BOT_TOKEN, DIGEST_HOUR, ORDER_INTENT_WORDS, POLL_SECONDS,
                    REMINDER_HOUR, WEB_HOST, WEB_PORT)
from web import create_app


async def collect(session: aiohttp.ClientSession, tg_client) -> tuple[list[tuple[str, list[dict], bool]], list[str]]:
    """Заказы со всех включённых источников: [(источник, заказы, это_чат)], ошибки бирж."""
    batches, errors = [], []
    enabled = {a["exchange"] for a in db.list_accounts() if a["enabled"]}
    if "Profi.ru" in enabled:
        errors.append("Profi.ru: сбор ещё не настроен")
    for name, fetch in sources.SOURCES:
        if name in enabled:
            orders = await fetch(session)
            if not orders:
                errors.append(f"{name}: нет данных")
            batches.append((name, orders, False))
    for src in db.list_tg_sources(enabled_only=True):  # ошибки каналов видны в настройках, не в общей плашке
        name, is_chat = src["username"], src["kind"] == "chat"
        try:
            if tg_client:  # подключён аккаунт для парсинга — читаем через него (видит и закрытые каналы, и чаты)
                orders = await tg_user.fetch(tg_client, name)
            elif is_chat:
                raise ValueError("чаты читаются через Telegram-аккаунт — подключите его в настройках")
            else:
                orders = await sources.fetch_tg_channel(session, name)
            db.set_tg_source(name, last_error="", last_count=len(orders))
            batches.append((f"TG @{name}", orders, is_chat))
        except ValueError as e:
            db.set_tg_source(name, last_error=str(e)[:200])
    return batches, errors


async def poll_once(session: aiohttp.ClientSession, bot: Bot | None, tg_client=None) -> int:
    """Один проход по всем источникам. Возвращает число новых подходящих заказов."""
    plus, minus, min_budget = db.get_words("plus"), db.get_words("minus"), db.min_budget()
    batches, errors = await collect(session, tg_client)
    found = 0
    for source, orders, is_chat in batches:
        # Первый проход по источнику (новый канал или пустая база) — старые посты не рассылаем
        notify_new = db.has_orders_from(source)
        for o in reversed(orders):  # от старых к новым — уведомления придут по порядку
            matched = sources.apply_filters(o, plus, minus, min_budget, is_chat, ORDER_INTENT_WORDS)
            order_id = db.upsert_order(o, matched)
            if order_id and matched:
                found += 1
                if bot and notify_new:
                    try:
                        await tg.notify(bot, order_id)
                    except Exception:
                        logging.exception("Не удалось отправить заказ #%s в Telegram", order_id)
    db.set_setting("last_poll", datetime.now().strftime("%d.%m %H:%M"))
    db.set_setting("last_error", "; ".join(errors))
    return found


async def daily_messages(bot: Bot) -> None:
    """Утром — напоминание о дедлайнах, вечером — сводка. Каждое — не больше раза в день."""
    now, today = datetime.now(), date.today().isoformat()
    jobs = [("last_reminder", REMINDER_HOUR, tg.reminder_text), ("last_digest", DIGEST_HOUR, tg.digest_text)]
    for key, hour, make_text in jobs:
        if now.hour >= hour and db.get_setting(key) != today:
            db.set_setting(key, today)
            if text := make_text():
                await bot.send_message(ADMIN_ID, text, disable_web_page_preview=True)


async def poll_forever(bot: Bot | None) -> None:
    await tg_user.start()
    logging.info("Telegram: %s", "аккаунт подключён" if tg_user.CLIENT else "аккаунт не подключён (каналы через t.me/s)")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # аккаунт могут подключить или отключить в админке на ходу — берём текущий
                found = await poll_once(session, bot, tg_user.CLIENT)
                logging.info("Проверка бирж: новых подходящих заказов — %s", found)
                if bot:
                    await daily_messages(bot)
            except Exception:  # опрос не должен останавливаться из-за одной ошибки
                logging.exception("Ошибка при проверке бирж")
            await asyncio.sleep(POLL_SECONDS)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not ADMIN_PASSWORD:
        raise SystemExit("Задайте ADMIN_PASSWORD в .env — без него веб-админка будет открыта всем.")
    db.init_db()

    runner = web.AppRunner(create_app())
    await runner.setup()
    await web.TCPSite(runner, WEB_HOST, WEB_PORT).start()
    logging.info("Веб-админка: http://%s:%s", WEB_HOST, WEB_PORT)

    if BOT_TOKEN and ADMIN_ID:
        bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher()
        dp.include_router(tg.router)
        await bot.delete_webhook(drop_pending_updates=True)
        await asyncio.gather(poll_forever(bot), dp.start_polling(bot))
    else:
        logging.warning("BOT_TOKEN или ADMIN_ID не заданы — работаю без Telegram, только веб-админка")
        await poll_forever(None)


if __name__ == "__main__":
    asyncio.run(main())
