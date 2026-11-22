"""Telegram-бот радара: уведомления о заказах, отклики, фильтры, напоминания. Отвечает только владельцу."""
from datetime import date
from html import escape

import aiohttp
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ai
import db
import sources
from config import ADMIN_ID, WEB_PORT
from db import rub

router = Router()
router.message.filter(F.from_user.id == ADMIN_ID)
router.callback_query.filter(F.from_user.id == ADMIN_ID)

ADMIN_URL = f"http://127.0.0.1:{WEB_PORT}"


def shorten(text: str, limit: int) -> str:
    """Обрезка по границе слова с подсказкой, где читать дальше (лимит сообщения Telegram — 4096 символов)."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:") + "… (полностью — на странице заказа)"


def fmt_delay(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds} сек"
    if seconds < 3600:
        return f"{seconds // 60} мин {seconds % 60} сек"
    return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"


def format_order(o) -> str:
    now = sources.msk_now()
    score = sources.chance(o, now)
    budget = f"<b>{rub(o['budget'])}</b>" if o["budget"] else "<b>договорная</b>"
    if o["budget_max"] > o["budget"]:
        budget += f" · до {rub(o['budget_max'])}"
    offers = "?" if o["source"].startswith(sources.UNKNOWN_OFFERS) else o["offers"]
    lines = []
    if sources.is_hot(o, now):
        lines.append("🔥 <b>ГОРЯЧИЙ ЗАКАЗ</b>")
    lines += [
        f"<b>{escape(o['title'])}</b>",
        "",
        f"💰 {budget}",
        f"📍 {escape(o['source'])}" + (f" · {escape(o['category'])}" if o["category"] else ""),
        f"👁 {o['views']}   💬 откликов: {offers}   🎯 шанс: <b>{sources.chance_label(score)}</b> ({score})",
    ]
    if (delay := sources.field(o, "delay_sec")) is not None:
        lines.append(f"⚡ дошёл за <b>{fmt_delay(delay)}</b> после публикации")
    if o["description"]:
        lines += ["", f"<blockquote expandable>{escape(shorten(o['description'], 2500))}</blockquote>"]
    if sources.field(o, "ai_score") is not None:
        lines += ["", f"🤖 <b>ИИ: {o['ai_score']}/100</b> — {escape(o['ai_summary'] or '')}"]
        if o["ai_price"] or o["ai_days"]:
            lines.append(f"💵 {escape(o['ai_price'] or '—')}   ⏳ {escape(o['ai_days'] or '—')}")
        if o["ai_risks"]:
            lines.append(f"⚠️ {escape(o['ai_risks'])}")
    lines += ["", f"🔑 <i>{escape(o['matched'].replace(',', ', '))}</i>"]
    return "\n".join(lines)


def order_kb(o):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Перейти к заказу", url=o["link"])
    if o["status"] == "new":
        kb.button(text="✔ Я откликнулся", callback_data=f"resp:{o['id']}")
    else:
        kb.button(text=f"{db.ORDER_STATUSES[o['status']]} · вернуть", callback_data=f"st:{o['id']}:new")
    kb.button(text="✍️ Отклик от ИИ", callback_data=f"ai:{o['id']}")
    kb.button(text="📝 Шаблоны", callback_data="tpl")
    if o["status"] == "new":
        kb.button(text="🙈 Скрыть", callback_data=f"st:{o['id']}:hidden")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


async def notify(bot: Bot, order_id: int) -> Message:
    o = db.get_order(order_id)
    return await bot.send_message(ADMIN_ID, format_order(o), reply_markup=order_kb(o), disable_web_page_preview=True)


async def refresh(bot: Bot, chat_id: int, message_id: int, order_id: int) -> None:
    """Перерисовать уведомление (например, когда нейросеть дописала разбор)."""
    o = db.get_order(order_id)
    await bot.edit_message_text(format_order(o), chat_id=chat_id, message_id=message_id,
                                reply_markup=order_kb(o), disable_web_page_preview=True)


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
        "📡 <b>LastixParser</b> — ловлю заказы с бирж раньше всех\n\n"
        "Каждые 15–30 секунд проверяю Kwork, FL.ru и Telegram-каналы (Profi.ru — раз в минуту) и присылаю подходящие заказы.\n"
        "«🔗 Перейти к заказу» — откликаетесь на бирже, потом «✔ Я откликнулся» — отклик засчитан, сделка в админке.\n"
        "«✍️ Отклик от ИИ» пишет текст отклика под заказ.\n\n"
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
    count = db.rematch_new_orders()
    await message.answer(("Добавлено: " if add else "Удалено: ") + escape(", ".join(words)) +
                         f"\nНовых подходящих заказов теперь: {count}")


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
    count = db.rematch_new_orders()
    await message.answer(f"Минимальный бюджет: {rub(value)}\nНовых подходящих заказов теперь: {count}")


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
    await call.answer("Отклик засчитан, сделка создана")
    await call.message.answer(f"🤝 Сделка в админке: {ADMIN_URL}/deals/{deal_id}", disable_web_page_preview=True)


@router.callback_query(F.data.startswith("ai:"))
async def ai_reply(call: CallbackQuery):
    o = db.get_order(int(call.data.split(":")[1]))
    if not o:
        await call.answer("Заказ не найден — база радара была очищена", show_alert=True)
        return
    if not o["ai_reply"]:
        if not ai.settings()["key"]:
            await call.answer("Добавьте ключ munito.ai в админке: Настройки → Нейросеть", show_alert=True)
            return
        await call.answer("✍️ Пишу отклик, 5–15 секунд…")
        try:
            async with aiohttp.ClientSession() as session:
                await ai.analyze(session, o)
        except ai.AIError as e:
            await call.message.answer(f"🤖 Не получилось: {escape(str(e))}")
            return
        o = db.get_order(o["id"])
        try:  # дописать разбор и в само уведомление
            await call.message.edit_text(format_order(o), reply_markup=order_kb(o), disable_web_page_preview=True)
        except TelegramBadRequest:
            pass  # сообщение не изменилось или слишком старое — не страшно
    else:
        await call.answer()
    await call.message.answer(f"✍️ <b>Отклик на «{escape(o['title'][:80])}»</b>\n"
                              f"<i>нажмите на текст, чтобы скопировать</i>\n\n<code>{escape(o['ai_reply'])}</code>")


@router.callback_query(F.data == "tpl")
async def show_templates(call: CallbackQuery):
    templates = db.list_templates()
    await call.answer()
    if not templates:
        await call.message.answer(f"Шаблонов пока нет — добавьте в админке: {ADMIN_URL}/templates")
        return
    for t in templates:  # <code> копируется в Telegram одним нажатием
        await call.message.answer(f"📝 <b>{escape(t['title'])}</b>\n\n<code>{escape(t['body'])}</code>")
