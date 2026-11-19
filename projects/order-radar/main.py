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
from config import (ADMIN_ID, ADMIN_PASSWORD, BOT_TOKEN, DIGEST_HOUR, POLL_SECONDS, REMINDER_HOUR, WEB_HOST,
                    WEB_PORT)
from web import create_app


def filter_order(o: dict, plus: list[str], minus: list[str], min_budget: int) -> list[str]:
    """Ключевые слова, по которым заказ подходит; пустой список — заказ не подходит."""
    text = f"{o['title']} {o['description']} {o['category']}"
    if sources.match(text, minus) or (min_budget and o["budget"] < min_budget):
        return []
    return sources.match(text, plus)


async def poll_once(session: aiohttp.ClientSession, bot: Bot | None) -> int:
    """Один проход по всем биржам. Возвращает число новых подходящих заказов."""
    first_run = db.is_empty()  # при первом запуске не засыпаем уведомлениями о старых заказах
    plus, minus, min_budget = db.get_words("plus"), db.get_words("minus"), db.min_budget()
    found, errors = 0, []
    for name, fetch in sources.SOURCES:
        orders = await fetch(session)
        if not orders:
            errors.append(f"{name}: нет данных")
        for o in reversed(orders):  # от старых к новым — уведомления придут по порядку
            matched = filter_order(o, plus, minus, min_budget)
            order_id = db.upsert_order(o, matched)
            if order_id and matched:
                found += 1
                if bot and not first_run:
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
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                found = await poll_once(session, bot)
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
