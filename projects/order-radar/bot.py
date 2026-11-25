"""Telegram-бот радара: уведомления о заказах, отклики, фильтры, напоминания. Отвечает только владельцу."""
import logging
import time
from datetime import date, datetime
from html import escape

import aiohttp
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, BufferedInputFile, CallbackQuery, Message
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


fmt_delay = sources.fmt_duration  # «1 мин 15 сек»

# Цвета кнопок Telegram (Bot API 9.4+): синяя — главное действие, зелёная — «готово», красная — убрать.
# Старые клиенты Telegram просто покажут обычные кнопки.
BLUE, GREEN, RED = "primary", "success", "danger"


def format_order(o) -> str:
    now = sources.msk_now()
    score = sources.chance(o, now)
    views, offers = sources.counts(o)
    rank = sources.order_rank(o, now)
    head = (f"⭐ <b>ТОП-ЗАКАЗ</b> · {escape(o['source'])}", f"🔥 <b>ГОРЯЧИЙ</b> · {escape(o['source'])}",
            f"📥 <b>{escape(o['source'])}</b>")[rank]
    if o["category"]:
        head += f" · {escape(o['category'][:60])}"
    lines = [head, "", f"<b>{escape(o['title'])}</b>", ""]

    budget = f"<b>{rub(o['budget'])}</b>" if o["budget"] else "<b>договорная</b>"
    if o["budget_max"] > o["budget"]:
        budget += f" · можно до {rub(o['budget_max'])}"
    lines.append(f"💰 {budget}")
    marks = f"🎯 шанс: <b>{sources.chance_label(score)}</b> ({sources.ten(score)}/10)"
    if sources.field(o, "ai_score") is not None:
        marks += f"  ·  🤖 ИИ: <b>{sources.ten(o['ai_score'])}/10</b>"
    lines.append(marks)
    lines.append(f"👥 откликов: <b>{offers}</b>  ·  👁 {views}")
    timing = []
    if (delay := sources.field(o, "delay_sec")) is not None:
        timing.append(f"⚡ дошёл за <b>{fmt_delay(delay)}</b>")
    if o["published_at"]:
        age = int((now - datetime.fromisoformat(o["published_at"])).total_seconds())
        timing.append(f"опубликован {fmt_delay(age)} назад")
    if timing:
        lines.append("  ·  ".join(timing))

    extra = []
    if verdict := sources.client_verdict(o):
        name = sources.field(o, "client_name")
        extra.append(f"👤 {escape(name) + ' — ' if name else ''}{verdict[0]}: {escape(verdict[1])}")
    elif name := sources.field(o, "client_name"):
        extra.append(f"👤 {escape(name)}")
    acc = db.get_account(o["source"])
    if acc and acc["response_price"]:
        extra.append(f"💸 отклик стоит {rub(acc['response_price'])}")
    if term := sources.field(o, "term_days"):
        extra.append(f"⏳ срок по заказу: {escape(term)} дн.")
    if (left := sources.time_left(o, now)) is not None and left > 0:
        extra.append(f"⌛ приём откликов: ещё {fmt_delay(left)}")
    if flags := sources.scam_flags(o, db.scam_words()):
        extra.append(f"⚠️ <b>Осторожно:</b> {escape(', '.join(f'«{w}»' for w in flags))}")
    if extra:
        lines += [""] + extra

    if o["description"]:
        lines += ["", f"<blockquote expandable>{escape(shorten(o['description'], 2500))}</blockquote>"]
    if sources.field(o, "ai_score") is not None:
        lines += ["", f"🤖 <b>Разбор ИИ:</b> {escape(o['ai_summary'] or '')}"]
        if o["ai_price"] or o["ai_days"]:
            lines.append(f"💵 {escape(o['ai_price'] or '—')}   ⏳ {escape(o['ai_days'] or '—')}")
        if o["ai_risks"]:
            lines.append(f"⚠️ {escape(o['ai_risks'])}")
    lines += ["", f"🔑 <i>{escape(o['matched'].replace(',', ', '))}</i>",
              f"📋 подробная карточка: {ADMIN_URL}/orders/{o['id']}"]  # текстом: на localhost Telegram ссылки не делает
    return "\n".join(lines)


def order_kb(o):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Перейти к заказу", url=o["link"], style=BLUE)
    if o["status"] == "new":
        kb.button(text="✔ Я откликнулся", callback_data=f"resp:{o['id']}", style=GREEN)
    else:
        kb.button(text=f"{db.ORDER_STATUSES[o['status']]} · вернуть", callback_data=f"st:{o['id']}:new")
    kb.button(text="✍️ Отклик от ИИ", callback_data=f"ai:{o['id']}", style=BLUE)
    kb.button(text="💡 Совет ИИ", callback_data=f"adv:{o['id']}")
    kb.button(text="💬 Ответ заказчику", callback_data=f"ask:{o['id']}")
    kb.button(text="📝 Шаблоны", callback_data=f"tpl:{o['id']}")
    kb.button(text="🔄 Обновить цифры", callback_data=f"upd:{o['id']}")
    if o["status"] == "new":
        kb.button(text="🙈 Скрыть", callback_data=f"st:{o['id']}:hidden", style=RED)
    kb.adjust(2, 2, 2, 2)
    return kb.as_markup()


def reply_kb(order_id: int):
    """Под готовым откликом ИИ: переписать короче или подробнее."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✂️ Короче", callback_data=f"rw:{order_id}:short")
    kb.button(text="📝 Подробнее", callback_data=f"rw:{order_id}:long")
    return kb.as_markup()


# ---------- Тихие часы и «заказчик ответил?» ----------

def quiet_summary_text(order_ids: list[int]) -> str | None:
    """Утро после тихих часов: что пришло ночью, лучшие — сверху."""
    now = sources.msk_now()
    orders = [o for o in (db.get_order(i) for i in dict.fromkeys(order_ids)) if o and o["status"] == "new"]
    if not orders:
        return None
    orders.sort(key=lambda o: (sources.order_rank(o, now), -sources.chance(o, now)))
    lines = [f"☀️ <b>Пока вы спали — подходящих заказов: {len(orders)}</b>"
             + (f", топ и горячих: {n}" if (n := sum(sources.order_rank(o, now) < 2 for o in orders)) else ""),
             ""]
    for o in orders[:10]:
        ai_mark = f" · 🤖 {sources.ten(o['ai_score'])}/10" if sources.field(o, "ai_score") is not None else ""
        lines.append(f"{('⭐ ', '🔥 ', '• ')[sources.order_rank(o, now)]}<a href=\"{escape(o['link'])}\">{escape(o['title'][:70])}</a>"
                     f" — {rub(o['budget']) if o['budget'] else 'договорная'}{ai_mark}")
    if len(orders) > 10:
        lines.append(f"\n…и ещё {len(orders) - 10} — в админке: {ADMIN_URL}/orders")
    return "\n".join(lines)


def weekly_text() -> str:
    """Итоги недели и сравнение с прошлой."""
    cur, prev = db.week_stats(0), db.week_stats(7)

    def trend(key: str, money: bool = False) -> str:
        a, b = cur[key], prev[key]
        value = rub(a) if money else str(a)
        if a == b:
            return f"{value} (как на прошлой неделе)"
        arrow = "📈" if a > b else "📉"
        diff = rub(abs(a - b)) if money else str(abs(a - b))
        return f"{value} {arrow} {'+' if a > b else '−'}{diff}"

    conversion = round(100 * cur["won"] / cur["responses"]) if cur["responses"] else 0
    return ("📊 <b>Итоги недели</b>\n\n"
            f"🎯 Поймано подходящих заказов: {trend('found')}\n"
            f"✍️ Откликов: {trend('responses')}\n"
            f"🤝 Взяли в работу: {trend('won')}" + (f" · конверсия {conversion}%" if cur["responses"] else "") + "\n"
            f"💰 Заработано: {trend('income', money=True)}\n"
            f"🏆 Больше всего заказов: {cur['best_source']}\n\n"
            + ("Откликов не было — загляните в /orders, там ждут свежие заказы." if not cur["responses"] else
               "Так держать! 💪" if cur["income"] >= prev["income"] and cur["income"] else
               "Совет: откликайтесь на ⭐ ТОП и 🔥 заказы с оценкой ИИ 7+ — у них самый высокий шанс."))


NUDGE_TEMPLATE = ("Здравствуйте! Подскажите, пожалуйста, задача ещё актуальна? Готов приступить в ближайшее время "
                  "и ответить на вопросы по реализации.")


def nudge_text(deal) -> str:
    return (f"⏳ <b>Заказчик молчит</b>\n\nВы откликнулись на «{escape(deal['title'][:80])}» "
            f"({escape(deal['source'] or 'биржа')}), ответа пока нет. Можно напомнить о себе — "
            f"нажмите на текст, чтобы скопировать:\n\n<code>{escape(NUDGE_TEMPLATE)}</code>")


def nudge_kb(deal_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Заказчик ответил", callback_data=f"ng:{deal_id}:dialog", style=GREEN)
    kb.button(text="✔ Напомнил", callback_data=f"ng:{deal_id}:ok", style=BLUE)
    kb.button(text="✖ Отказ", callback_data=f"ng:{deal_id}:lost", style=RED)
    kb.adjust(1, 2)
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
    hot = sorted((o for o in db.list_orders("new", limit=50) if sources.order_rank(o, now) < 2),
                 key=lambda o: sources.order_rank(o, now))[:3]
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
            f"{('⭐', '🔥')[sources.order_rank(o, now)]} <a href=\"{escape(o['link'])}\">{escape(o['title'][:60])}</a> — {rub(o['budget'])}" for o in hot)
    return text


# ---------- Команды ----------

MENU = [
    ("start", "Справка"),
    ("deals", "Сделки в работе"),
    ("remind", "Кому напомнить, дедлайны"),
    ("stats", "Статистика"),
    ("digest", "Сводка сейчас"),
    ("week", "Отчёт за неделю"),
    ("keywords", "Ключевые и минус-слова"),
    ("backup", "Копия базы"),
    ("cancel", "Отменить ответ заказчику"),
]


async def set_menu(bot: Bot) -> None:
    """Меню команд у кнопки «/» в Telegram."""
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in MENU])
    except Exception as e:  # меню — не критично, радар работает и без него
        logging.warning("Не удалось поставить меню команд: %s", e)


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "📡 <b>LastixParser</b> — ловлю заказы с бирж раньше всех\n\n"
        "Каждые 15–30 секунд проверяю Kwork, FL.ru, freelance.ru, Weblancer и Telegram-каналы (Profi.ru — раз в минуту) и присылаю подходящие заказы.\n"
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
        "/digest — сводка прямо сейчас\n"
        "/week — отчёт за неделю\n"
        "/backup — прислать копию базы\n"
        "/cancel — отменить «💬 Ответ заказчику»\n\n"
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


@router.message(Command("week"))
async def cmd_week(message: Message):
    await message.answer(weekly_text())


@router.message(Command("backup"))
async def cmd_backup(message: Message):
    await send_backup(message.bot)


async def send_backup(bot: Bot) -> None:
    """Копия базы радара (сделки, финансы, клиенты, заказы) — без паролей и ключей."""
    data = db.backup_bytes()
    await bot.send_document(ADMIN_ID, BufferedInputFile(data, filename=f"lastixparser-{date.today().isoformat()}.db"),
                            caption=f"💾 Резервная копия базы · {len(data) // 1024} КБ\n"
                                    "Сделки, финансы, клиенты и заказы. Ключи и cookie в копию не попадают — "
                                    "после восстановления их нужно ввести заново.")


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
                              f"<i>нажмите на текст, чтобы скопировать</i>\n\n<code>{escape(o['ai_reply'])}</code>",
                              reply_markup=reply_kb(o["id"]))


@router.callback_query(F.data.startswith("upd:"))
async def refresh_numbers(call: CallbackQuery):
    """Перерисовать уведомление свежими просмотрами, откликами и шансом (радар обновляет их при каждом опросе)."""
    o = db.get_order(int(call.data.split(":")[1]))
    if not o:
        await call.answer("Заказ не найден — база радара была очищена", show_alert=True)
        return
    views, offers = sources.counts(o)
    try:
        await call.message.edit_text(format_order(o), reply_markup=order_kb(o), disable_web_page_preview=True)
        await call.answer(f"Сейчас: 👁 {views} · 💬 {offers}")
    except TelegramBadRequest:  # ничего не изменилось
        await call.answer(f"Без изменений: 👁 {views} · 💬 {offers}")


@router.callback_query(F.data.startswith("rw:"))
async def ai_rewrite(call: CallbackQuery):
    """Переписать отклик ИИ короче или подробнее."""
    _, order_id, mode = call.data.split(":")
    o = db.get_order(int(order_id))
    if not o:
        await call.answer("Заказ не найден — база радара была очищена", show_alert=True)
        return
    await call.answer("✂️ Сокращаю…" if mode == "short" else "📝 Расписываю подробнее…")
    try:
        async with aiohttp.ClientSession() as session:
            text = await ai.rewrite(session, o, mode)
    except ai.AIError as e:
        await call.message.answer(f"🤖 Не получилось: {escape(str(e))}")
        return
    label = "короче" if mode == "short" else "подробнее"
    await call.message.answer(f"✍️ <b>Отклик {label}</b> · <i>нажмите на текст, чтобы скопировать</i>\n\n"
                              f"<code>{escape(text)}</code>", reply_markup=reply_kb(o["id"]))


# ---------- «Ответ заказчику»: вопрос из чата биржи → ответ от ИИ ----------

_asking: dict[str, float | int] = {}  # заказ, по которому ждём вопрос заказчика, и до какого времени


@router.callback_query(F.data.startswith("ask:"))
async def ask_client_question(call: CallbackQuery):
    o = db.get_order(int(call.data.split(":")[1]))
    if not o:
        await call.answer("Заказ не найден — база радара была очищена", show_alert=True)
        return
    if not ai.settings()["key"]:
        await call.answer("Добавьте ключ munito.ai в админке: Настройки → Нейросеть", show_alert=True)
        return
    _asking.update(order=o["id"], until=time.monotonic() + 900)
    await call.answer()
    await call.message.answer(f"💬 Пришлите следующим сообщением, что написал заказчик по «{escape(o['title'][:80])}» — "
                              "отвечу с учётом заказа. Передумали — /cancel")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    _asking.clear()
    await message.answer("Ок, отменил.")


@router.message(F.text & ~F.text.startswith("/"))
async def client_question(message: Message):
    """Любой текст (не команда) — вопрос заказчика, если до этого нажали «💬 Ответ заказчику»."""
    if not _asking or _asking["until"] < time.monotonic():
        _asking.clear()
        await message.answer("Чтобы ИИ ответил заказчику, нажмите «💬 Ответ заказчику» под нужным заказом.")
        return
    o = db.get_order(int(_asking["order"]))
    _asking.clear()
    if not o:
        await message.answer("Заказ не найден — база радара была очищена")
        return
    wait = await message.answer("💬 Пишу ответ, 5–20 секунд…")
    try:
        async with aiohttp.ClientSession() as session:
            text = await ai.answer_client(session, o, message.text)
    except ai.AIError as e:
        await wait.edit_text(f"🤖 Не получилось: {escape(str(e))}")
        return
    await wait.edit_text(f"💬 <b>Ответ заказчику</b> · «{escape(o['title'][:60])}»\n"
                         f"<i>нажмите на текст, чтобы скопировать</i>\n\n<code>{escape(text)}</code>")


@router.callback_query(F.data.startswith("ng:"))
async def nudge_answer(call: CallbackQuery):
    _, deal_id, action = call.data.split(":")
    if action == "dialog":
        db.set_stage(int(deal_id), "dialog")
        note = "Отлично! Сделка → «В диалоге»"
    elif action == "lost":
        db.set_stage(int(deal_id), "lost")
        note = "Записал отказ"
    else:
        note = "Хорошо, напомню в утренней сводке, если ответа так и не будет"
    await call.answer(note)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("adv:"))
async def ai_advice(call: CallbackQuery):
    """Совет нейросети: брать ли заказ, на что надавить, что спросить, как назвать цену, где подвох."""
    o = db.get_order(int(call.data.split(":")[1]))
    if not o:
        await call.answer("Заказ не найден — база радара была очищена", show_alert=True)
        return
    text = sources.field(o, "ai_advice")
    if not text:
        if not ai.settings()["key"]:
            await call.answer("Добавьте ключ munito.ai в админке: Настройки → Нейросеть", show_alert=True)
            return
        await call.answer("💡 Думаю над заказом, 5–20 секунд…")
        try:
            async with aiohttp.ClientSession() as session:
                text = await ai.advise(session, o)
        except ai.AIError as e:
            await call.message.answer(f"🤖 Не получилось: {escape(str(e))}")
            return
    else:
        await call.answer()
    await call.message.answer(f"💡 <b>Совет по заказу «{escape(o['title'][:80])}»</b>\n\n{escape(text)}",
                              disable_web_page_preview=True)


@router.callback_query(F.data.startswith("tpl"))
async def show_templates(call: CallbackQuery):
    """Шаблоны под заказ: данные подставлены, подходящие по нише — первыми (старые кнопки без id — как есть)."""
    templates = db.list_templates()
    await call.answer()
    if not templates:
        await call.message.answer(f"Шаблонов пока нет — добавьте в админке: {ADMIN_URL}/templates")
        return
    _, _, order_id = call.data.partition(":")
    o = db.get_order(int(order_id)) if order_id.isdigit() else None
    if not o:
        rows = [(t, t["body"], []) for t in templates]
    else:
        rows = sources.rank_templates(templates, o)
        rows = [r for r in rows if r[2]][:3] or rows[:3]  # не заваливаем чат: до трёх лучших
    for t, text, fits in rows:  # <code> копируется в Telegram одним нажатием
        mark = "✅ подходит · " if fits else ""
        await call.message.answer(f"📝 {mark}<b>{escape(t['title'])}</b>\n\n<code>{escape(text)}</code>")
    if o and len(templates) > len(rows):
        await call.message.answer(f"Все шаблоны — на странице заказа: {ADMIN_URL}/orders/{o['id']}")
