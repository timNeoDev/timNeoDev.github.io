"""Telegram-бот радара: уведомления о заказах, фильтры, CRM, напоминания. Отвечает только владельцу."""
from datetime import date
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
import sources
from config import ADMIN_ID, WEB_PORT

router = Router()
router.message.filter(F.from_user.id == ADMIN_ID)
router.callback_query.filter(F.from_user.id == ADMIN_ID)

ADMIN_URL = f"http://127.0.0.1:{WEB_PORT}"


def rub(n) -> str:
    return f"{int(n or 0):,}".replace(",", " ") + " ₽"


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
        kb.button(text="✅ Откликнулся", callback_data=f"st:{o['id']}:applied")
        kb.button(text="🙈 Скрыть", callback_data=f"st:{o['id']}:hidden")
    else:
        kb.button(text=f"{db.ORDER_STATUSES[o['status']]} · вернуть", callback_data=f"st:{o['id']}:new")
    kb.button(text="💼 В CRM", callback_data=f"crm:{o['id']}")
    kb.button(text="📝 Шаблоны", callback_data="tpl")
    kb.adjust(2, 2)
    return kb.as_markup()


async def notify(bot: Bot, order_id: int) -> None:
    o = db.get_order(order_id)
    await bot.send_message(ADMIN_ID, format_order(o), reply_markup=order_kb(o), disable_web_page_preview=True)


# ---------- Ежедневные сообщения ----------

def reminder_text() -> str | None:
    due = db.due_projects(days=2)
    if not due:
        return None
    today = date.today().isoformat()
    lines = []
    for p in due:
        mark = "🔴 просрочен" if p["deadline"] < today else ("🟠 сегодня" if p["deadline"] == today else "🟡 скоро")
        lines.append(f"{mark} · <b>{escape(p['title'])}</b> — до {p['deadline']} ({db.STAGES[p['stage']]})")
    return "⏰ <b>Дедлайны</b>\n\n" + "\n".join(lines)


def digest_text() -> str:
    s = db.overview()
    hot = [o for o in db.list_orders("new", limit=50) if sources.is_hot(o, sources.msk_now())][:3]
    text = (
        "🌙 <b>Сводка за день</b>\n\n"
        f"Найдено подходящих заказов: {s['matched_today']}\n"
        f"Ждут решения: {s['new_waiting']}\n"
        f"Откликов за неделю: {s['applied_week']}\n"
        f"Проектов в работе: {s['projects_active']}\n"
        f"Доход за месяц: {rub(s['income_month'])}"
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
        "Каждые 3 минуты проверяю Kwork и присылаю подходящие заказы.\n\n"
        "<b>Фильтры</b>\n"
        "/keywords — ключевые и минус-слова\n"
        "/add бот, лендинг — добавить ключевые слова\n"
        "/del бот — удалить ключевое слово\n"
        "/minus дизайн — добавить минус-слово\n"
        "/unminus дизайн — удалить минус-слово\n"
        "/budget 3000 — минимальный бюджет (0 — без ограничения)\n\n"
        "<b>Работа</b>\n"
        "/projects — проекты в работе\n"
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
        f"Просмотрено заказов: {s['seen_total']}\n"
        f"Подходящих: {s['matched_total']} (сегодня {s['matched_today']})\n"
        f"Откликов: {s['applied_total']} (за неделю {s['applied_week']})\n"
        f"Конверсия «отклик → проект»: {s['conversion']}%\n\n"
        f"Проектов в работе: {s['projects_active']}\n"
        f"Доход за месяц: {rub(s['income_month'])}\n"
        f"Ожидается оплат: {rub(s['receivable'])}"
    )


@router.message(Command("projects"))
async def cmd_projects(message: Message):
    active = [p for p in db.list_projects() if p["stage"] in db.ACTIVE_STAGES]
    if not active:
        await message.answer("Активных проектов нет. Добавьте заказ кнопкой «💼 В CRM» или в админке.")
        return
    lines = [
        f"• <b>{escape(p['title'][:60])}</b> — {db.STAGES[p['stage']]}, {rub(p['paid'])} из {rub(p['price'])}"
        + (f", до {p['deadline']}" if p["deadline"] else "")
        for p in active
    ]
    await message.answer("💼 <b>Проекты в работе</b>\n\n" + "\n".join(lines))


@router.message(Command("digest"))
async def cmd_digest(message: Message):
    await message.answer(digest_text(), disable_web_page_preview=True)


# ---------- Кнопки под заказами ----------

@router.callback_query(F.data.startswith("st:"))
async def change_status(call: CallbackQuery):
    _, order_id, status = call.data.split(":")
    db.set_order_status(int(order_id), status)
    await call.message.edit_reply_markup(reply_markup=order_kb(db.get_order(int(order_id))))
    await call.answer(db.ORDER_STATUSES[status])


@router.callback_query(F.data.startswith("crm:"))
async def to_crm(call: CallbackQuery):
    order_id = int(call.data.split(":")[1])
    project_id = db.project_from_order(order_id)
    await call.message.edit_reply_markup(reply_markup=order_kb(db.get_order(order_id)))
    await call.answer("Добавлено в CRM")
    await call.message.answer(f"💼 Проект добавлен в CRM: {ADMIN_URL}/projects/{project_id}",
                              disable_web_page_preview=True)


@router.callback_query(F.data == "tpl")
async def show_templates(call: CallbackQuery):
    templates = db.list_templates()
    await call.answer()
    if not templates:
        await call.message.answer(f"Шаблонов пока нет — добавьте в админке: {ADMIN_URL}/templates")
        return
    for t in templates:  # <code> копируется в Telegram одним нажатием
        await call.message.answer(f"📝 <b>{escape(t['title'])}</b>\n\n<code>{escape(t['body'])}</code>")
