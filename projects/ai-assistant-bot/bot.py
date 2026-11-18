"""ИИ-консультант кофейни в Telegram.

Стек: Python 3.11+, aiogram 3, Claude API (Anthropic SDK), SQLite.
Запуск: python bot.py
"""
import asyncio
import logging
import re
import time
from collections import defaultdict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup,
                           ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ai
import storage
from config import ADDRESS, ADMIN_ID, BOT_TOKEN, BUSINESS_NAME, DAILY_LIMIT, PHONE

router = Router()
TG_LIMIT = 4096          # максимальная длина сообщения в Telegram
EDIT_INTERVAL = 1.0      # как часто обновлять сообщение во время стриминга, сек

BTN_MENU = "☕ Меню"
BTN_BOOK = "📅 Забронировать столик"
BTN_WAY = "📍 Как добраться"
BTN_RESET = "🔄 Новый диалог"

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_MENU), KeyboardButton(text=BTN_BOOK)],
        [KeyboardButton(text=BTN_WAY), KeyboardButton(text=BTN_RESET)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Спросите что угодно о кофейне…",
)
phone_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
    resize_keyboard=True, one_time_keyboard=True,
)


def book_inline():
    kb = InlineKeyboardBuilder()
    kb.button(text=BTN_BOOK, callback_data="book")
    return kb.as_markup()


def confirm_inline():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить", callback_data="book_confirm")
    kb.button(text="❌ Отмена", callback_data="book_cancel")
    return kb.as_markup()


MENU_TEXT = (
    "<b>Горячий кофе</b>\n"
    "Эспрессо — 150 ₽\nКортадо — 210 ₽\nКапучино — 220 ₽\nФильтр V60 — 230 ₽\n"
    "Флэт уайт — 240 ₽\nРаф лаванда — 290 ₽\n\n"
    "<b>Холодное</b>\nАйс-латте — 260 ₽\nКолд брю — 250 ₽\n\n"
    "<b>Десерты</b>\nКруассан — 190 ₽\nЧерничный чизкейк — 290 ₽\n"
    "Шоколадный торт — 320 ₽\nПанна-котта — 270 ₽\n\n"
    "Не знаете, что выбрать? Просто напишите, что любите, — я подскажу 😊"
)


class Booking(StatesGroup):
    name = State()
    phone = State()
    details = State()
    confirm = State()


# Один запрос к ИИ на пользователя за раз — чтобы ответы не перемешивались
_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# ---------- Команды и кнопки ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    storage.reset_history(message.from_user.id)
    await message.answer(
        f"Здравствуйте, <b>{message.from_user.first_name}</b>! 👋\n\n"
        f"Я ИИ-консультант кофейни {BUSINESS_NAME}. Спросите меня о меню, ценах, доставке "
        f"или попросите посоветовать напиток — отвечу за пару секунд.\n\n"
        f"Например: <i>«Что взять, если я не люблю горький кофе?»</i>",
        reply_markup=main_kb,
    )


@router.message(F.text == BTN_MENU)
async def show_menu(message: Message):
    await message.answer(MENU_TEXT)


@router.message(F.text == BTN_WAY)
async def show_way(message: Message):
    await message.answer(
        f"📍 {ADDRESS}\n5 минут пешком от метро «Центральная», вход со двора.\n"
        f"🕗 Ежедневно 08:00 — 22:00\n📞 {PHONE}"
    )


@router.message(F.text == BTN_RESET)
async def reset(message: Message, state: FSMContext):
    await state.clear()
    storage.reset_history(message.from_user.id)
    await message.answer("Начнём заново! Чем могу помочь?", reply_markup=main_kb)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_kb)


# ---------- Бронирование: имя → телефон → дата/время/гости → подтверждение ----------

async def start_booking(message: Message, state: FSMContext):
    await state.set_state(Booking.name)
    await message.answer("Отлично! Как к вам обращаться?", reply_markup=ReplyKeyboardRemove())


@router.message(F.text == BTN_BOOK)
async def booking_button(message: Message, state: FSMContext):
    await start_booking(message, state)


@router.callback_query(F.data == "book")
async def booking_inline(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await start_booking(call.message, state)


@router.message(Booking.name, F.text)
async def booking_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not 2 <= len(name) <= 50:
        await message.answer("Введите имя от 2 до 50 символов.")
        return
    await state.update_data(name=name)
    await state.set_state(Booking.phone)
    await message.answer("Ваш номер телефона? Можно отправить кнопкой ниже.", reply_markup=phone_kb)


@router.message(Booking.phone, F.contact | F.text)
async def booking_phone(message: Message, state: FSMContext):
    raw = message.contact.phone_number if message.contact else message.text
    digits = re.sub(r"\D", "", raw)
    if not 10 <= len(digits) <= 15:
        await message.answer("Похоже, номер некорректный. Пример: +79001234567")
        return
    await state.update_data(phone="+" + digits)
    await state.set_state(Booking.details)
    await message.answer(
        "Когда вас ждать и сколько будет гостей?\nНапример: <i>завтра в 19:00, 2 человека</i>",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Booking.details, F.text)
async def booking_details(message: Message, state: FSMContext):
    details = message.text.strip()[:200]
    await state.update_data(details=details)
    data = await state.get_data()
    await state.set_state(Booking.confirm)
    await message.answer(
        f"<b>Проверьте заявку:</b>\n\n👤 {data['name']}\n📞 {data['phone']}\n🗓 {details}",
        reply_markup=confirm_inline(),
    )


@router.callback_query(Booking.confirm, F.data == "book_confirm")
async def booking_confirm(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    booking_id = storage.add_booking(call.from_user.id, data["name"], data["phone"], data["details"])
    await call.message.edit_text(
        "✅ Заявка отправлена! Администратор перезвонит в течение 15 минут и подтвердит бронь."
    )
    await call.message.answer("Могу ещё чем-нибудь помочь?", reply_markup=main_kb)
    await call.answer()
    if ADMIN_ID:
        username = f" (@{call.from_user.username})" if call.from_user.username else ""
        await bot.send_message(
            ADMIN_ID,
            f"🆕 <b>Бронь #{booking_id}</b>\n\n👤 {data['name']}{username}\n📞 {data['phone']}\n🗓 {data['details']}",
        )


@router.callback_query(Booking.confirm, F.data == "book_cancel")
async def booking_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Заявка отменена.")
    await call.message.answer("Чем ещё могу помочь?", reply_markup=main_kb)
    await call.answer()


# ---------- Админ-команды ----------

@router.message(Command("stats"), F.from_user.id == ADMIN_ID)
async def admin_stats(message: Message):
    s = storage.stats()
    await message.answer(
        "<b>Статистика ИИ-консультанта</b>\n\n"
        f"Запросов к ИИ: {s['total']} (сегодня {s['today'] or 0})\n"
        f"Пользователей: {s['users']}\n"
        f"Заявок на бронь: {s['bookings']}\n"
        f"Расход: ≈ ${s['cost']:.2f} (сегодня ≈ ${s['cost_today']:.2f})"
    )


@router.message(Command("bookings"), F.from_user.id == ADMIN_ID)
async def admin_bookings(message: Message):
    rows = storage.last_bookings()
    if not rows:
        await message.answer("Заявок пока нет.")
        return
    lines = [f"#{r['id']} · {r['created_at'][:16]} · {r['name']}, {r['phone']} — {r['details']}" for r in rows]
    await message.answer("<b>Последние заявки:</b>\n\n" + "\n".join(lines))


# ---------- Свободный текст → ИИ ----------

async def safe_edit(msg: Message, text: str, **kwargs) -> None:
    """Редактирует сообщение, не падая на «message is not modified» и флуд-лимитах."""
    try:
        await msg.edit_text(text[:TG_LIMIT], parse_mode=None, **kwargs)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
    except TelegramBadRequest:
        pass


@router.message(F.text, ~F.text.startswith("/"))
async def ask_ai(message: Message, bot: Bot):
    user_id = message.from_user.id
    question = message.text.strip()[:1500]

    lock = _locks[user_id]
    if lock.locked():
        await message.answer("Секунду, я ещё отвечаю на предыдущий вопрос 🙂")
        return

    async with lock:
        if user_id != ADMIN_ID and not storage.try_spend(user_id):
            await message.answer(
                f"На сегодня лимит вопросов исчерпан ({DAILY_LIMIT}). "
                f"Возвращайтесь завтра или позвоните нам: {PHONE}"
            )
            return

        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        reply_msg = await message.answer("✍️ …", parse_mode=None)
        last_edit = 0.0

        async def on_update(text: str):
            nonlocal last_edit
            if time.monotonic() - last_edit >= EDIT_INTERVAL:
                last_edit = time.monotonic()
                shown, _ = ai.clean(text)
                if shown:
                    await safe_edit(reply_msg, shown + " ▌")

        history = storage.get_history(user_id) + [{"role": "user", "content": question}]
        try:
            reply = await ai.ask(history, on_update)
        except ai.AIUnavailable:
            await safe_edit(reply_msg, f"Извините, я сейчас не могу ответить 😔 Попробуйте через минуту или позвоните: {PHONE}")
            return

        storage.log_request(user_id, reply.cost_usd)
        if reply.refused or not reply.text:
            await safe_edit(reply_msg, f"С этим я помочь не смогу. Давайте лучше про кофе? ☕ Или позвоните нам: {PHONE}")
            return

        storage.add_exchange(user_id, question, reply.text)
        await safe_edit(reply_msg, reply.text, reply_markup=book_inline() if reply.wants_booking else None)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN в .env (токен от @BotFather).")
    if ai.demo_mode():
        logging.warning("ANTHROPIC_API_KEY не задан — бот работает в демо-режиме (ответы из demo.py)")
    else:
        logging.info("Claude API подключён, модель %s", ai.MODEL)
    storage.init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
