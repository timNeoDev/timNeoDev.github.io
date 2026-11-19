"""Telegram-бот для онлайн-записи в барбершоп.

Стек: Python 3.11+, aiogram 3, SQLite.
Запуск: python bot.py
"""
import asyncio
import logging
import re
from html import escape
from datetime import date, datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

import db
import keyboards as kb
from config import ADDRESS, ADMIN_ID, BOT_TOKEN, BUSINESS_NAME, PHONE, SERVICES

router = Router()


class Booking(StatesGroup):
    service = State()
    date = State()
    time = State()
    name = State()
    phone = State()
    confirm = State()


def fmt_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{kb.WEEKDAYS[d.weekday()]}, {d:%d.%m.%Y}"


def free_slots(day: str) -> list[str]:
    busy = db.get_busy_slots(day)
    slots = [t for t in kb.all_slots() if t not in busy]
    if day == date.today().isoformat():  # сегодня не показываем прошедшее время
        now = datetime.now().strftime("%H:%M")
        slots = [t for t in slots if t > now]
    return slots


# ---------- Общие команды ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        f"Привет, <b>{escape(message.from_user.first_name)}</b>! 👋\n\n"
        f"Я бот {BUSINESS_NAME}. Помогу записаться на стрижку за минуту — без звонков.",
        reply_markup=kb.main_menu,
    )


@router.message(F.text == "💈 Услуги и цены")
async def show_services(message: Message):
    lines = [f"• {name} — <b>{price} ₽</b> ({minutes} мин)" for name, price, minutes in SERVICES]
    await message.answer("<b>Наши услуги:</b>\n\n" + "\n".join(lines))


@router.message(F.text == "📍 Контакты")
async def show_contacts(message: Message):
    await message.answer(f"<b>{BUSINESS_NAME}</b>\n📍 {ADDRESS}\n📞 {PHONE}\n🕙 Ежедневно 10:00 — 20:00")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=kb.main_menu)


# ---------- Запись: услуга → дата → время → имя → телефон → подтверждение ----------

@router.message(F.text == "📅 Записаться")
async def booking_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Booking.service)
    await message.answer("Выберите услугу:", reply_markup=kb.services_kb())


@router.callback_query(Booking.service, kb.ServiceCb.filter())
async def booking_service(call: CallbackQuery, callback_data: kb.ServiceCb, state: FSMContext):
    service = SERVICES[callback_data.idx][0]
    await state.update_data(service=service)
    await state.set_state(Booking.date)
    await call.message.edit_text(f"Услуга: <b>{service}</b>\n\nВыберите дату:", reply_markup=kb.dates_kb())
    await call.answer()


@router.callback_query(Booking.date, kb.DateCb.filter())
async def booking_date(call: CallbackQuery, callback_data: kb.DateCb, state: FSMContext):
    slots = free_slots(callback_data.value)
    if not slots:
        await call.answer("На этот день свободных окон нет 😔", show_alert=True)
        return
    await state.update_data(date=callback_data.value)
    await state.set_state(Booking.time)
    await call.message.edit_text(
        f"Дата: <b>{fmt_date(callback_data.value)}</b>\n\nВыберите время:",
        reply_markup=kb.times_kb(slots),
    )
    await call.answer()


@router.callback_query(Booking.time, F.data == "back_to_dates")
async def booking_back(call: CallbackQuery, state: FSMContext):
    await state.set_state(Booking.date)
    await call.message.edit_text("Выберите дату:", reply_markup=kb.dates_kb())
    await call.answer()


@router.callback_query(Booking.time, kb.TimeCb.filter())
async def booking_time(call: CallbackQuery, callback_data: kb.TimeCb, state: FSMContext):
    time = f"{callback_data.hour:02d}:00"
    await state.update_data(time=time)
    await state.set_state(Booking.name)
    await call.message.edit_text(f"Время: <b>{time}</b>")
    await call.message.answer("Как вас зовут?", reply_markup=ReplyKeyboardRemove())
    await call.answer()


@router.message(Booking.name, F.text)
async def booking_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not 2 <= len(name) <= 50:
        await message.answer("Введите имя от 2 до 50 символов.")
        return
    await state.update_data(name=escape(name))  # имя попадает в HTML-сообщения
    await state.set_state(Booking.phone)
    await message.answer("Отправьте номер телефона кнопкой ниже или введите вручную:", reply_markup=kb.phone_keyboard)


@router.message(Booking.phone, F.contact | F.text)
async def booking_phone(message: Message, state: FSMContext):
    raw = message.contact.phone_number if message.contact else message.text
    digits = re.sub(r"\D", "", raw)
    if not 10 <= len(digits) <= 15:
        await message.answer("Похоже, номер некорректный. Попробуйте ещё раз, например: +79001234567")
        return
    await state.update_data(phone="+" + digits)
    data = await state.get_data()
    await state.set_state(Booking.confirm)
    await message.answer("Почти готово!", reply_markup=ReplyKeyboardRemove())
    await message.answer(
        "<b>Проверьте данные:</b>\n\n"
        f"💈 {data['service']}\n"
        f"📅 {fmt_date(data['date'])}, {data['time']}\n"
        f"👤 {data['name']}\n"
        f"📞 {data['phone']}",
        reply_markup=kb.confirm_kb(),
    )


@router.callback_query(Booking.confirm, F.data == "confirm")
async def booking_confirm(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    booking_id = db.add_booking(
        call.from_user.id, data["name"], data["phone"], data["service"], data["date"], data["time"]
    )
    await state.clear()

    if booking_id is None:
        await call.message.edit_text("Увы, это время только что заняли 😔 Попробуйте выбрать другое.")
        await call.message.answer("Главное меню:", reply_markup=kb.main_menu)
        await call.answer()
        return

    await call.message.edit_text(
        f"✅ <b>Вы записаны!</b>\n\n{data['service']}\n{fmt_date(data['date'])}, {data['time']}\n\n"
        f"Ждём вас по адресу: {ADDRESS}"
    )
    await call.message.answer("Если планы изменятся — отменить запись можно в разделе «Мои записи».",
                              reply_markup=kb.main_menu)
    await call.answer()

    if ADMIN_ID:
        await bot.send_message(
            ADMIN_ID,
            f"🆕 <b>Новая запись #{booking_id}</b>\n\n"
            f"{data['service']}\n{fmt_date(data['date'])}, {data['time']}\n"
            f"{data['name']}, {data['phone']}",
        )


@router.callback_query(Booking.confirm, F.data == "abort")
async def booking_abort(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Запись отменена.")
    await call.message.answer("Главное меню:", reply_markup=kb.main_menu)
    await call.answer()


# ---------- Мои записи ----------

@router.message(F.text == "📋 Мои записи")
async def my_bookings(message: Message):
    bookings = db.get_user_bookings(message.from_user.id, date.today().isoformat())
    if not bookings:
        await message.answer("У вас пока нет активных записей.")
        return
    lines = [f"• {fmt_date(b['date'])}, {b['time']} — {b['service']}" for b in bookings]
    await message.answer("<b>Ваши записи:</b>\n\n" + "\n".join(lines), reply_markup=kb.my_bookings_kb(bookings))


@router.callback_query(kb.CancelCb.filter())
async def cancel_booking(call: CallbackQuery, callback_data: kb.CancelCb, bot: Bot):
    row = db.delete_booking(callback_data.booking_id, call.from_user.id)
    if not row:
        await call.answer("Запись не найдена", show_alert=True)
        return
    await call.message.edit_text(f"Запись на {fmt_date(row['date'])}, {row['time']} отменена.")
    await call.answer("Отменено")
    if ADMIN_ID:
        await bot.send_message(
            ADMIN_ID, f"❌ Отмена записи #{row['id']}: {row['name']}, {fmt_date(row['date'])} {row['time']}"
        )


# ---------- Админ-команды ----------

@router.message(Command("today"), F.from_user.id == ADMIN_ID)
async def admin_today(message: Message):
    bookings = db.get_bookings_by_date(date.today().isoformat())
    if not bookings:
        await message.answer("На сегодня записей нет.")
        return
    lines = [f"{b['time']} — {b['name']} ({b['phone']}), {b['service']}" for b in bookings]
    await message.answer("<b>Записи на сегодня:</b>\n\n" + "\n".join(lines))


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и вставьте токен от @BotFather.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
