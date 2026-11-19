"""Telegram-бот: уведомления о заказах и управление ключевыми словами. Отвечает только владельцу."""
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
import feeds
from config import ADMIN_ID, WEB_PORT

router = Router()
router.message.filter(F.from_user.id == ADMIN_ID)
router.callback_query.filter(F.from_user.id == ADMIN_ID)

STATUS_LABEL = {"new": "🆕 Новый", "applied": "✅ Откликнулся", "hidden": "🙈 Скрыт"}


def format_order(o) -> str:
    snippet = (o["description"] or "")[:350]
    return (
        f"<b>{escape(o['title'])}</b>\n"
        f"<i>{escape(o['source'])} · {escape(o['category'] or 'без категории')}</i>\n\n"
        f"{escape(snippet)}\n\n"
        f"🔑 {escape(o['matched'])}\n"
        f"<a href=\"{escape(o['link'])}\">Открыть заказ →</a>"
    )


def order_kb(o):
    kb = InlineKeyboardBuilder()
    if o["status"] == "new":
        kb.button(text="✅ Откликнулся", callback_data=f"st:{o['id']}:applied")
        kb.button(text="🙈 Скрыть", callback_data=f"st:{o['id']}:hidden")
    else:
        kb.button(text=f"{STATUS_LABEL[o['status']]} · вернуть в новые", callback_data=f"st:{o['id']}:new")
    return kb.as_markup()


async def notify(bot: Bot, order_id: int) -> None:
    o = db.get_order(order_id)
    await bot.send_message(ADMIN_ID, format_order(o), reply_markup=order_kb(o), disable_web_page_preview=True)


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "📡 <b>Радар заказов</b>\n\n"
        "Каждые 3 минуты проверяю FL.ru и присылаю заказы с вашими ключевыми словами.\n\n"
        "/keywords — список ключевых слов\n"
        "/add бот, лендинг — добавить слова\n"
        "/del бот — удалить слово\n"
        "/stats — статистика\n\n"
        f"Веб-админка: http://127.0.0.1:{WEB_PORT}"
    )


@router.message(Command("keywords"))
async def cmd_keywords(message: Message):
    words = db.get_keywords()
    await message.answer("🔑 Ключевые слова:\n" + (escape(", ".join(words)) if words else "пусто — добавьте через /add"))


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject):
    words = [feeds.clean_keyword(w) for w in (command.args or "").split(",")]
    words = [w for w in words if w]
    if not words:
        await message.answer("Пример: /add бот, лендинг, парсер")
        return
    for w in words:
        db.add_keyword(w)
    await message.answer("Добавлено: " + escape(", ".join(words)))


@router.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject):
    word = feeds.clean_keyword(command.args or "")
    if not word:
        await message.answer("Пример: /del бот")
        return
    db.delete_keyword(word)
    await message.answer(f"Удалено: {escape(word)}")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    s = db.stats()
    await message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"Просмотрено заказов: {s['total']}\n"
        f"Подходящих: {s['matched']} (сегодня {s['today']})\n"
        f"Новых: {s['new']} · Откликнулся: {s['applied']} · Скрыто: {s['hidden']}"
    )


@router.callback_query(F.data.startswith("st:"))
async def change_status(call: CallbackQuery):
    _, order_id, status = call.data.split(":")
    db.set_status(int(order_id), status)
    o = db.get_order(int(order_id))
    await call.message.edit_reply_markup(reply_markup=order_kb(o))
    await call.answer(STATUS_LABEL[status])
