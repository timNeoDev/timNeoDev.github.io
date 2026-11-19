"""Радар заказов: сбор заказов с бирж + Telegram-бот + веб-админка в одном процессе.
Запуск: python main.py
"""
import asyncio
import logging

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

import bot as tg
import db
import feeds
from config import ADMIN_ID, ADMIN_PASSWORD, BOT_TOKEN, FEEDS, POLL_SECONDS, WEB_HOST, WEB_PORT
from web import create_app


async def poll_once(session: aiohttp.ClientSession, bot: Bot | None) -> int:
    """Один проход по всем лентам. Возвращает число новых подходящих заказов."""
    first_run = db.is_empty()  # при первом запуске не засыпаем уведомлениями о старых заказах
    keywords = db.get_keywords()
    found = 0
    for source, url in FEEDS:
        for o in reversed(await feeds.fetch(session, source, url)):  # от старых к новым
            matched = feeds.match(f"{o['title']} {o['description']} {o['category']}", keywords)
            order_id = db.add_order(o, matched)
            if order_id and matched:
                found += 1
                if bot and not first_run:
                    try:
                        await tg.notify(bot, order_id)
                    except Exception:
                        logging.exception("Не удалось отправить заказ #%s в Telegram", order_id)
    return found


async def poll_forever(bot: Bot | None) -> None:
    async with aiohttp.ClientSession() as session:
        while True:
            found = await poll_once(session, bot)
            logging.info("Проверка лент: новых подходящих заказов — %s", found)
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
