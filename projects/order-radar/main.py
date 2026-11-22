"""Радар заказов: сбор заказов с бирж + Telegram-бот + веб-админка в одном процессе.
Запуск: python main.py
"""
import asyncio
import logging
import time
from datetime import date, datetime

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

import ai
import bot as tg
import db
import profi
import sources
import tg_user
from config import (ADMIN_ID, ADMIN_PASSWORD, BACKOFF_SECONDS, BOT_TOKEN, DIGEST_HOUR, FULL_SCAN_SECONDS, KWORK_PAGES,
                    MIN_POLL_SECONDS, ORDER_INTENT_WORDS, REMINDER_HOUR, WEB_HOST, WEB_PORT)
from web import create_app


_paused_until: dict[str, float] = {}  # источник → до какого момента (time.monotonic) его не трогаем
_ai_tasks: set[asyncio.Task] = set()        # фоновые разборы заказов нейросетью (ссылки, чтобы задачи не собрал GC)
_ai_limit = asyncio.Semaphore(3)           # не больше 3 запросов к munito.ai одновременно


def _paused(name: str) -> bool:
    return _paused_until.get(name, 0) > time.monotonic()


def _pause(name: str) -> None:
    _paused_until[name] = time.monotonic() + BACKOFF_SECONDS


async def collect(session: aiohttp.ClientSession, tg_client, full: bool = True
                  ) -> tuple[list[tuple[str, list[dict], bool]], list[str]]:
    """Заказы со всех включённых источников: [(источник, заказы, это_чат)], ошибки бирж.
    full=False — быстрая проверка: только первая страница Kwork."""
    batches, errors = [], []
    enabled = {a["exchange"] for a in db.list_accounts() if a["enabled"]}
    if "Profi.ru" in enabled:
        try:
            orders = await profi.fetch(session)  # сам решает, пора ли (не чаще раза в минуту)
            if orders is not None:
                batches.append(("Profi.ru", orders, False))
                db.set_setting("profi_error", "")
        except profi.ProfiError as e:
            logging.warning("Profi.ru: %s", e)
            db.set_setting("profi_error", str(e))
        if err := db.get_setting("profi_error"):
            errors.append(f"Profi.ru: {err}")
    for name, fetch in sources.SOURCES:
        if name in enabled and not _paused(name):
            orders = await fetch(session, KWORK_PAGES if full else 1)
            if not orders:
                errors.append(f"{name}: нет данных, пауза {BACKOFF_SECONDS} с")
                _pause(name)
            batches.append((name, orders, False))
    for src in db.list_tg_sources(enabled_only=True):  # ошибки каналов видны в настройках, не в общей плашке
        name, is_chat = src["username"], src["kind"] == "chat"
        if _paused(f"tg:{name}"):
            continue
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
            _pause(f"tg:{name}")
    return batches, errors


async def poll_once(session: aiohttp.ClientSession, bot: Bot | None, tg_client=None, full: bool = True) -> int:
    """Один проход по всем источникам. Возвращает число новых подходящих заказов."""
    plus, minus, min_budget = db.get_words("plus"), db.get_words("minus"), db.min_budget()
    batches, errors = await collect(session, tg_client, full=full)
    found = 0
    for source, orders, is_chat in batches:
        # Первый проход по источнику (новый канал или пустая база) — старые посты не рассылаем
        notify_new = db.has_orders_from(source)
        for o in reversed(orders):  # от старых к новым — уведомления придут по порядку
            matched = sources.apply_filters(o, plus, minus, min_budget, is_chat, ORDER_INTENT_WORDS)
            if o.get("cut") and notify_new and not db.link_exists(o["link"]):
                o["description"] = await sources.fetch_fl_full(session, o["link"]) or o["description"]
                matched = sources.apply_filters(o, plus, minus, min_budget, is_chat, ORDER_INTENT_WORDS)
            order_id = db.upsert_order(o, matched)
            if order_id and matched:
                found += 1
                if bot and notify_new:
                    await deliver(session, bot, order_id)
    db.set_setting("last_poll", datetime.now().strftime("%d.%m %H:%M:%S"))
    db.set_setting("last_error", "; ".join(errors))
    return found


def quiet_now(now: datetime | None = None) -> bool:
    """Тихие часы: с quiet_from до quiet_to (через полночь тоже работает). Одинаковые значения — выключены."""
    start, end = db.int_setting("quiet_from") % 24, db.int_setting("quiet_to") % 24
    if start == end:
        return False
    hour = (now or datetime.now()).hour
    return start <= hour < end if start < end else hour >= start or hour < end


def _background(coro) -> None:
    task = asyncio.create_task(coro)
    _ai_tasks.add(task)
    task.add_done_callback(_ai_tasks.discard)


async def deliver(session: aiohttp.ClientSession, bot: Bot, order_id: int) -> None:
    """Обычно — уведомление сразу (скорость важнее), разбор нейросетью следом, правкой того же сообщения.
    Тихие часы — молча копим, утром сводка. Фильтр «ИИ от N/10» — сначала разбор, в бот только подходящие."""
    o = db.get_order(order_id)
    if o["published_at"]:
        db.set_delay(order_id, int((sources.msk_now() - datetime.fromisoformat(o["published_at"])).total_seconds()))
    if quiet_now():
        queue = [x for x in (db.get_setting("quiet_queue") or "").split(",") if x]
        db.set_setting("quiet_queue", ",".join(queue[-200:] + [str(order_id)]))
        if ai.enabled():  # разберём заранее — к утру оценка будет готова
            _background(analyze_only(session, order_id))
        return
    if ai.enabled() and db.int_setting("ai_min_score") > 0:
        _background(analyze_then_notify(session, bot, order_id))  # не тормозим опрос бирж ожиданием нейросети
        return
    try:
        message = await tg.notify(bot, order_id)
    except Exception:
        logging.exception("Не удалось отправить заказ #%s в Telegram", order_id)
        return
    if ai.enabled():
        _background(analyze_and_update(session, bot, message, order_id))


async def analyze_only(session: aiohttp.ClientSession, order_id: int) -> None:
    async with _ai_limit:
        await ai.safe_analyze(session, db.get_order(order_id))


async def analyze_then_notify(session: aiohttp.ClientSession, bot: Bot, order_id: int) -> None:
    """Фильтр по оценке ИИ: присылаем, только если оценка не ниже порога. Нейросеть не ответила — присылаем всё равно."""
    async with _ai_limit:
        result = await ai.safe_analyze(session, db.get_order(order_id))
    minimum = db.int_setting("ai_min_score")
    if result and sources.ten(result["ai_score"]) < minimum:
        logging.info("Заказ #%s: ИИ %s/10 ниже порога %s — не присылаю", order_id, sources.ten(result["ai_score"]), minimum)
        return
    try:
        await tg.notify(bot, order_id)
    except Exception:
        logging.exception("Не удалось отправить заказ #%s в Telegram", order_id)


async def analyze_and_update(session: aiohttp.ClientSession, bot: Bot, message, order_id: int) -> None:
    async with _ai_limit:
        if not await ai.safe_analyze(session, db.get_order(order_id)):
            return
    try:
        await tg.refresh(bot, message.chat.id, message.message_id, order_id)
    except Exception:
        logging.exception("Не удалось дописать разбор ИИ к заказу #%s", order_id)


async def daily_messages(bot: Bot) -> None:
    """Утром — напоминание о дедлайнах, вечером — сводка. Каждое — не больше раза в день."""
    now, today = datetime.now(), date.today().isoformat()
    jobs = [("last_reminder", REMINDER_HOUR, tg.reminder_text), ("last_digest", DIGEST_HOUR, tg.digest_text)]
    if quiet_now(now):  # ночью ничего не шлём — ни сводок, ни напоминаний
        return
    for key, hour, make_text in jobs:
        if now.hour >= hour and db.get_setting(key) != today:
            db.set_setting(key, today)
            if text := make_text():
                await bot.send_message(ADMIN_ID, text, disable_web_page_preview=True)
    # Тихие часы закончились — сводка того, что пришло ночью
    if queue := [int(x) for x in (db.get_setting("quiet_queue") or "").split(",") if x.isdigit()]:
        db.set_setting("quiet_queue", "")
        if text := tg.quiet_summary_text(queue):
            await bot.send_message(ADMIN_ID, text, disable_web_page_preview=True)
    # «Заказчик ответил?» — через nudge_hours часов после отклика
    for deal in db.nudge_due():
        db.set_nudged(deal["id"])
        await bot.send_message(ADMIN_ID, tg.nudge_text(deal), reply_markup=tg.nudge_kb(deal["id"]),
                               disable_web_page_preview=True)


async def poll_forever(bot: Bot | None) -> None:
    await tg_user.start()
    logging.info("Telegram: %s", "аккаунт подключён" if tg_user.CLIENT else "аккаунт не подключён (каналы через t.me/s)")
    last_full = 0.0
    async with aiohttp.ClientSession() as session:
        while True:
            full = time.monotonic() - last_full >= FULL_SCAN_SECONDS
            try:
                # аккаунт могут подключить или отключить в админке на ходу — берём текущий
                found = await poll_once(session, bot, tg_user.CLIENT, full=full)
                if full:
                    last_full = time.monotonic()
                if found or full:  # быстрые проверки без находок не засоряют лог
                    logging.info("Проверка%s: новых подходящих заказов — %s", " (полная)" if full else "", found)
                if bot:
                    await daily_messages(bot)
            except Exception:  # опрос не должен останавливаться из-за одной ошибки
                logging.exception("Ошибка при проверке бирж")
            await asyncio.sleep(max(db.int_setting("poll_seconds"), MIN_POLL_SECONDS))


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
