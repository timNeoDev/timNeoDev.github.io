"""Клавиатуры бота."""
from datetime import date, timedelta

from aiogram.filters.callback_data import CallbackData
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import SERVICES, WORK_HOURS

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


class ServiceCb(CallbackData, prefix="srv"):
    idx: int


class DateCb(CallbackData, prefix="date"):
    value: str  # YYYY-MM-DD


class TimeCb(CallbackData, prefix="time"):
    hour: int  # двоеточие нельзя передавать в callback_data, поэтому храним только час


class CancelCb(CallbackData, prefix="cancel"):
    booking_id: int


main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📅 Записаться")],
        [KeyboardButton(text="📋 Мои записи"), KeyboardButton(text="💈 Услуги и цены")],
        [KeyboardButton(text="📍 Контакты")],
    ],
    resize_keyboard=True,
)

phone_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


def services_kb():
    kb = InlineKeyboardBuilder()
    for i, (name, price, minutes) in enumerate(SERVICES):
        kb.button(text=f"{name} — {price} ₽", callback_data=ServiceCb(idx=i))
    kb.adjust(1)
    return kb.as_markup()


def dates_kb(days: int = 7):
    kb = InlineKeyboardBuilder()
    today = date.today()
    for i in range(days):
        d = today + timedelta(days=i)
        label = "Сегодня" if i == 0 else f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"
        kb.button(text=label, callback_data=DateCb(value=d.isoformat()))
    kb.adjust(3)
    return kb.as_markup()


def times_kb(free_slots: list[str]):
    kb = InlineKeyboardBuilder()
    for t in free_slots:
        kb.button(text=t, callback_data=TimeCb(hour=int(t[:2])))
    kb.button(text="⬅️ Другая дата", callback_data="back_to_dates")
    kb.adjust(4)
    return kb.as_markup()


def confirm_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data="confirm")
    kb.button(text="❌ Отмена", callback_data="abort")
    kb.adjust(2)
    return kb.as_markup()


def my_bookings_kb(bookings):
    kb = InlineKeyboardBuilder()
    for b in bookings:
        kb.button(text=f"❌ Отменить {b['date'][8:]}.{b['date'][5:7]} {b['time']}", callback_data=CancelCb(booking_id=b["id"]))
    kb.adjust(1)
    return kb.as_markup()


def all_slots() -> list[str]:
    start, end = WORK_HOURS
    return [f"{h:02d}:00" for h in range(start, end)]
