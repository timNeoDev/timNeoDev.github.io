"""Telegram-бот радара: уведомления о заказах, отклики, фильтры, напоминания. Отвечает только владельцу."""
from datetime import date
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
import sources
from config import ADMIN_ID, WEB_PORT
from db import rub

router = Router()
router.message.filter(F.from_user.id == ADMIN_ID)
router.callback_query.filter(F.from_user.id == ADMIN_ID)

ADMIN_URL = f"http://127.0.0.1:{WEB_PORT}"


def format_order(o) -> str:
    hot = "🔥 " if sources.is_hot(o, sources.msk_now()) else ""
    budget = rub(o["budget"]) + (f" (до {rub(o['budget_max'])})" if o["budget_max"] > o["budget"] else "")
    return (
        f"{hot}<b>{escape(o['title'])}</b>\n"
        f"💰 {budget} · 👁 {o['views']} · 💬 откликов: {o['offers']}\n"
        f"<i>{escape(o['source'])} · {escape(o['category'] or 'без категории')}</i>\n\n"
        f"{escape((o['description'] or '')[:400])}\n\n"
        f"🔑 {escape(o['matched'])}\n"
        f"<a href=\"{escape(o['link'])}\">Открыть заказ →</a>"
    )


def order_kb(o):
    kb = InlineKeyboardBuilder()
    if o["status"] == "new":
        kb.button(text="✅ Откликнулся", callback_data=f"resp:{o['id']}")
        kb.button(text="🙈 Скрыть", callback_data=f"st:{o['id']}:hidden")
    else:
        kb.button(text=f"{db.ORDER_STATUSES[o['status']]} · вернуть", callback_data=f"st:{o['id']}:new")
    kb.button(text="📝 Шаблоны", callback_data="tpl")
    kb.adjust(2, 1)
    return kb.as_markup()


async def notify(bot: Bot, order_id: int) -> None:
    o = db.get_order(order_id)
    await bot.send_message(ADMIN_ID, format_order(o), reply_markup=order_kb(o), disable_web_page_preview=True)


# ---------- Ежедневные сообщения ----------

def reminder_text() -> str | None:
    """Утро: дедлайны и клиенты, которым пора напомнить о себе."""
    today = date.today().isoformat()
    parts = []
    if due := db.due_deals(days=2):
        lines = [f"{'🔴 просрочен' if d['deadline'] < today else '🟠 сегодня' if d['deadline'] == today else '🟡 скоро'}"
                 f" · <b>{escape(d['title'])}</b> — до {d['deadline']}" for d in due]
        parts.append("⏰ <b>Дедлайны</b>\n" + "\n".join(lines))
    if follow := db.follow_ups():
        lines = [f"• <b>{escape(d['title'][:60])}</b> — {escape(d['client'] or 'клиент')} "
                 f"({db.STAGES[d['stage']]}){' · ' + escape(d['contact']) if d['contact'] else ''}" for d in follow[:10]]
        parts.append("🔔 <b>Пора напомнить о себе</b>\n" + "\n".join(lines))
    return "\n\n".join(parts) or None


def digest_text() -> str:
    s = db.overview()
    now = sources.msk_now()
    hot = [o for o in db.list_orders("new", limit=50) if sources.is_hot(o, now)][:3]
    text = (
        "🌙 <b>Сводка за день</b>\n\n"
        f"Найдено подходящих заказов: {s['matched_today']} (ждут решения: {s['new_waiting']})\n"
        f"Откликов за неделю: {s['responses_week']} · в диалоге: {s['in_dialog']}\n"
        f"Пора напомнить: {s['follow_ups']}\n"
        f"Ждём оплаты: {rub(s['awaiting'])}\n"
        f"Доход за месяц: {rub(s['income_month'])} из {rub(s['income_goal'])} ({s['goal_pct']}%)"
    )
    if hot:
        text += "\n\n🔥 <b>Ещё можно успеть:</b>\n" + "\n".join(
            f"• <a href=\"{escape(o['link'])}\">{escape(o['title'][:60])}</a> — {rub(o['budget'])}" for o in hot)
    return text


# ---------- Команды ----------

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "📡 <b>Радар заказов</b>\n\n"
        "Каждые 3 минуты проверяю биржи и присылаю подходящие заказы.\n"
        "«✅ Откликнулся» сразу создаёт сделку в админке.\n\n"
        "<b>Фильтры</b>\n"
        "/keywords — ключевые и минус-слова\n"
        "/add бот, лендинг — добавить ключевые слова\n"
        "/del бот — удалить ключевое слово\n"
        "/minus дизайн — добавить минус-слово\n"
        "/unminus дизайн — удалить минус-слово\n"
        "/budget 3000 — минимальный бюджет (0 — без ограничения)\n\n"
        "<b>Работа</b>\n"
        "/deals — сделки в работе\n"
        "/remind — кому пора напомнить и дедлайны\n"
        "/stats — статистика\n"
        "/digest — сводка прямо сейчас\n\n"
        f"Админка: {ADMIN_URL}",
        disable_web_page_preview=True,
    )


@router.message(Command("keywords"))
async def cmd_keywords(message: Message):
    plus, minus = db.get_words("plus"), db.get_words("minus")
    await message.answer(
        f"🔑 <b>Ключевые слова:</b>\n{escape(', '.join(plus)) or '—'}\n\n"
        f"🚫 <b>Минус-слова:</b>\n{escape(', '.join(minus)) or '—'}\n\n"
        f"💰 Минимальный бюджет: {rub(db.min_budget())}"
    )


async def _change_words(message: Message, args: str | None, kind: str, add: bool):
    words = [w for w in (sources.clean_word(x) for x in (args or "").split(",")) if w]
    if not words:
        await message.answer("Напишите слова после команды через запятую, например: /add бот, парсер")
        return
    for w in words:
        db.add_word(kind, w) if add else db.delete_word(kind, w)
    await message.answer(("Добавлено: " if add else "Удалено: ") + escape(", ".join(words)))


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject):
    await _change_words(message, command.args, "plus", add=True)


@router.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject):
    await _change_words(message, command.args, "plus", add=False)


@router.message(Command("minus"))
async def cmd_minus(message: Message, command: CommandObject):
    await _change_words(message, command.args, "minus", add=True)


@router.message(Command("unminus"))
async def cmd_unminus(message: Message, command: CommandObject):
    await _change_words(message, command.args, "minus", add=False)


@router.message(Command("budget"))
async def cmd_budget(message: Message, command: CommandObject):
    value = (command.args or "").replace(" ", "")
    if not value.isdigit():
        await message.answer(f"Сейчас: {rub(db.min_budget())}. Пример: /budget 3000")
        return
    db.set_setting("min_budget", int(value))
    await message.answer(f"Минимальный бюджет: {rub(value)}")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    s = db.overview()
    await message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"Подходящих заказов: {s['matched_total']} (сегодня {s['matched_today']})\n"
        f"Откликов: {s['responses']} · в диалоге: {s['in_dialog']}\n"
        f"Доходят до проекта: {s['conversion']}%\n"
        f"Потрачено на отклики: {rub(s['response_spend'])} · вернули: {rub(s['response_refund'])}\n"
        f"Прибыль с откликов: {rub(s['response_profit'])} (окупаемость {s['roi']}×)\n\n"
        f"Доход за месяц: {rub(s['income_month'])} · прогноз: {rub(s['forecast_month'])}\n"
        f"Ждём оплаты: {rub(s['awaiting'])}"
    )


@router.message(Command("deals", "projects"))
async def cmd_deals(message: Message):
    active = db.list_deals(tuple(db.WORK_STAGES))
    if not active:
        await message.answer("Сделок в работе нет.")
        return
    lines = [
        f"• <b>{escape(d['title'][:60])}</b> — {db.STAGES[d['stage']]}, {rub(d['paid'])} из {rub(d['price'])}"
        + (f", до {d['deadline']}" if d["deadline"] else "")
        for d in active
    ]
    await message.answer("🤝 <b>Сделки в работе</b>\n\n" + "\n".join(lines))


@router.message(Command("remind"))
async def cmd_remind(message: Message):
    await message.answer(reminder_text() or "Дедлайнов нет, напоминать никому не нужно 👌")


@router.message(Command("digest"))
async def cmd_digest(message: Message):
    await message.answer(digest_text(), disable_web_page_preview=True)


# ---------- Кнопки под заказами ----------

@router.callback_query(F.data.startswith("st:"))
async def change_status(call: CallbackQuery):
    _, order_id, status = call.data.split(":")
    o = db.get_order(int(order_id))
    if not o:
        await call.answer("Заказ не найден — база радара была очищена", show_alert=True)
        return
    db.set_order_status(o["id"], status)
    await call.message.edit_reply_markup(reply_markup=order_kb(db.get_order(o["id"])))
    await call.answer(db.ORDER_STATUSES[status])


@router.callback_query(F.data.startswith("resp:"))
async def respond(call: CallbackQuery):
    order_id = int(call.data.split(":")[1])
    try:
        deal_id = db.deal_from_order(order_id)
    except ValueError:
        await call.answer("Заказ не найден — база радара была очищена", show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=order_kb(db.get_order(order_id)))
    await call.answer("Сделка создана")
    await call.message.answer(f"🤝 Сделка в админке: {ADMIN_URL}/deals/{deal_id}", disable_web_page_preview=True)


@router.callback_query(F.data == "tpl")
async def show_templates(call: CallbackQuery):
    templates = db.list_templates()
    await call.answer()
    if not templates:
        await call.message.answer(f"Шаблонов пока нет — добавьте в админке: {ADMIN_URL}/templates")
        return
    for t in templates:  # <code> копируется в Telegram одним нажатием
        await call.message.answer(f"📝 <b>{escape(t['title'])}</b>\n\n<code>{escape(t['body'])}</code>")
