"""SQLite: заказы с бирж, сделки (отклики → проекты), платежи, клиенты, аккаунты бирж, шаблоны, аналитика."""
import sqlite3
from contextlib import closing
from datetime import date, timedelta

import sources
from config import DEFAULT_KEYWORDS, DEFAULT_MINUS_WORDS, DEFAULT_TG_CHANNELS, ORDER_INTENT_WORDS

DB_PATH = "radar.db"

ORDER_STATUSES = {"new": "Новый", "applied": "Откликнулся", "hidden": "Скрыт"}

# Путь сделки: отклик → работа → итог
RESPONSE_STAGES = {"sent": "Отклик отправлен", "dialog": "В диалоге", "clarify": "На уточнении",
                   "approval": "На согласовании"}
WORK_STAGES = {"ready": "Можно начинать", "work": "В работе", "revisions": "Правки клиента",
               "review": "На проверке", "done": "Сдан · ждём оплату"}
FINAL_STAGES = {"paid": "Оплачен", "lost": "Отказ / закрыт"}
STAGES = {**RESPONSE_STAGES, **WORK_STAGES, **FINAL_STAGES}
STAGE_GROUPS = {"responses": ("Отклики", tuple(RESPONSE_STAGES)), "work": ("В работе", tuple(WORK_STAGES)),
                "closed": ("Завершённые", tuple(FINAL_STAGES))}
ACTIVE_STAGES = tuple(RESPONSE_STAGES) + tuple(WORK_STAGES)
FILTER_KINDS = ("plus", "minus")
EXCHANGES = {"Kwork": "сбор открытых заказов, вход не нужен", "Profi.ru": "заказы видны только после входа в аккаунт"}
SETTINGS_DEFAULTS = {"min_budget": "0", "tax_rate": "4", "income_goal": "50000", "follow_up_days": "3"}

DEFAULT_TEMPLATES = [
    ("Telegram-бот", "Здравствуйте! Сделаю бота под вашу задачу на Python (aiogram 3): удобное меню, база данных, "
                     "уведомления администратору. Пример моего бота для записи клиентов: https://t.me/zayavki_demo_pro_bot. "
                     "Портфолио: https://timneodev.github.io/. Срок — от 3 дней. Подскажите, пожалуйста, детали задачи?"),
    ("Лендинг", "Здравствуйте! Сверстаю адаптивный лендинг: быстрый, аккуратный, с формой заявки. "
                "Пример работы: https://timneodev.github.io/projects/coffee-landing/. Срок — 5–7 дней. "
                "Есть ли у вас макет или примеры сайтов, которые нравятся?"),
    ("Парсер", "Здравствуйте! Напишу парсер на Python: сбор данных по расписанию, выгрузка в CSV/Excel или Google Таблицы, "
               "уведомления в Telegram. Подскажите, пожалуйста, сайт-источник и какие поля нужны?"),
]


def rub(n) -> str:
    return f"{int(n or 0):,}".replace(",", " ") + " ₽"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _in(stages) -> str:
    """SQL-список этапов для IN (...) — только из констант выше, не из ввода пользователя."""
    return "(" + ",".join(f"'{s}'" for s in stages) + ")"


def init_db() -> None:
    with closing(_conn()) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, link TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                description TEXT, category TEXT,
                budget INTEGER DEFAULT 0, budget_max INTEGER DEFAULT 0, views INTEGER DEFAULT 0, offers INTEGER DEFAULT 0,
                published_at TEXT,                  -- время биржи (Kwork — московское)
                matched TEXT NOT NULL DEFAULT '',   -- найденные ключевые слова через запятую; '' — не подходит
                status TEXT NOT NULL DEFAULT 'new',
                applied_at TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS filters (kind TEXT NOT NULL, word TEXT NOT NULL, PRIMARY KEY (kind, word));
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS projects (   -- сделки: от отклика до оплаты
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, client TEXT DEFAULT '', contact TEXT DEFAULT '', source TEXT DEFAULT '',
                order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
                price INTEGER DEFAULT 0, stage TEXT NOT NULL DEFAULT 'sent',
                deadline TEXT, notes TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                amount INTEGER NOT NULL, paid_on TEXT NOT NULL, note TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS templates (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tg_sources ( -- Telegram: публичные каналы (t.me/s) и чаты (через аккаунт)
                username TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'channel', enabled INTEGER NOT NULL DEFAULT 1,
                last_error TEXT DEFAULT '', last_count INTEGER DEFAULT 0, added_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS accounts (   -- биржи: включена ли, данные входа, цена отклика
                exchange TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
                login TEXT DEFAULT '', secret TEXT DEFAULT '', response_price INTEGER DEFAULT 0, connected_at TEXT
            );
            """
        )
        # Перенос из версии 2.0: новые поля сделок и новые названия этапов
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
        for col, ddl in (("response_cost", "INTEGER DEFAULT 0"), ("refund", "INTEGER DEFAULT 0"),
                         ("follow_up", "TEXT"), ("via_response", "INTEGER DEFAULT 0")):
            if col not in cols:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")
        conn.execute("UPDATE projects SET stage = 'dialog' WHERE stage = 'lead'")
        conn.execute("UPDATE projects SET stage = 'lost' WHERE stage = 'cancelled'")

        if not conn.execute("SELECT 1 FROM filters LIMIT 1").fetchone():
            conn.executemany("INSERT INTO filters VALUES ('plus', ?)", [(w,) for w in DEFAULT_KEYWORDS])
            conn.executemany("INSERT INTO filters VALUES ('minus', ?)", [(w,) for w in DEFAULT_MINUS_WORDS])
        if not conn.execute("SELECT 1 FROM templates LIMIT 1").fetchone():
            conn.executemany("INSERT INTO templates (title, body) VALUES (?, ?)", DEFAULT_TEMPLATES)
        conn.executemany("INSERT OR IGNORE INTO accounts (exchange, enabled) VALUES (?, ?)",
                         [("Kwork", 1), ("Profi.ru", 0)])
        conn.executemany("INSERT OR IGNORE INTO settings VALUES (?, ?)", SETTINGS_DEFAULTS.items())
        if not conn.execute("SELECT 1 FROM tg_sources LIMIT 1").fetchone():
            conn.executemany("INSERT INTO tg_sources (username, kind) VALUES (?, 'channel')",
                             [(c,) for c in DEFAULT_TG_CHANNELS])


# ---------- Telegram-каналы и чаты ----------

TG_KINDS = {"channel": "Канал", "chat": "Чат"}


def list_tg_sources(enabled_only: bool = False):
    with closing(_conn()) as conn:
        return conn.execute("SELECT * FROM tg_sources" + (" WHERE enabled = 1" if enabled_only else "") +
                            " ORDER BY kind, username").fetchall()


def add_tg_source(username: str, kind: str) -> None:
    if kind not in TG_KINDS or not username:
        raise ValueError(kind)
    with closing(_conn()) as conn, conn:
        conn.execute("INSERT INTO tg_sources (username, kind) VALUES (?, ?) "
                     "ON CONFLICT(username) DO UPDATE SET kind = excluded.kind, enabled = 1", (username, kind))


def set_tg_source(username: str, **fields) -> None:
    allowed = {k: v for k, v in fields.items() if k in ("enabled", "last_error", "last_count")}
    with closing(_conn()) as conn, conn:
        conn.execute(f"UPDATE tg_sources SET {', '.join(f'{k} = ?' for k in allowed)} WHERE username = ?",
                     (*allowed.values(), username))


def delete_tg_source(username: str) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("DELETE FROM tg_sources WHERE username = ?", (username,))


def rematch_new_orders() -> int:
    """После смены фильтров перепроверяет заказы со статусом «Новый». Возвращает, сколько теперь подходит."""
    plus, minus, budget = get_words("plus"), get_words("minus"), min_budget()
    chats = {f"TG @{s['username']}" for s in list_tg_sources() if s["kind"] == "chat"}
    with closing(_conn()) as conn, conn:
        rows = conn.execute("SELECT * FROM orders WHERE status = 'new'").fetchall()
        updates = [(",".join(sources.apply_filters(dict(r), plus, minus, budget, r["source"] in chats,
                                                   ORDER_INTENT_WORDS)), r["id"]) for r in rows]
        conn.executemany("UPDATE orders SET matched = ? WHERE id = ?", updates)
    return sum(1 for m, _ in updates if m)


def has_orders_from(source: str) -> bool:
    """Были ли уже заказы с этого источника. Нет — первый проход: старые посты не рассылаем в Telegram."""
    with closing(_conn()) as conn:
        return conn.execute("SELECT 1 FROM orders WHERE source = ? LIMIT 1", (source,)).fetchone() is not None


# ---------- Настройки ----------

def get_setting(key: str, default: str = "") -> str:
    with closing(_conn()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else SETTINGS_DEFAULTS.get(key, default)


def set_setting(key: str, value) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, str(value)))


def int_setting(key: str) -> int:
    try:
        return int(float(get_setting(key)))
    except ValueError:
        return int(SETTINGS_DEFAULTS[key])


def min_budget() -> int:
    return int_setting("min_budget")


# ---------- Аккаунты бирж ----------

def list_accounts():
    with closing(_conn()) as conn:
        return conn.execute("SELECT * FROM accounts ORDER BY exchange").fetchall()


def get_account(exchange: str):
    with closing(_conn()) as conn:
        return conn.execute("SELECT * FROM accounts WHERE exchange = ?", (exchange,)).fetchone()


def set_account_enabled(exchange: str, enabled: bool) -> None:
    if exchange not in EXCHANGES:
        raise ValueError(exchange)
    with closing(_conn()) as conn, conn:
        conn.execute("UPDATE accounts SET enabled = ? WHERE exchange = ?", (int(enabled), exchange))


def connect_account(exchange: str, login: str, secret: str) -> None:
    if exchange not in EXCHANGES or not secret.strip():
        raise ValueError(exchange)
    with closing(_conn()) as conn, conn:
        conn.execute("UPDATE accounts SET login = ?, secret = ?, enabled = 1, connected_at = datetime('now', 'localtime') "
                     "WHERE exchange = ?", (login.strip(), secret.strip(), exchange))


def disconnect_account(exchange: str) -> None:
    """Стирает данные входа и выключает сбор с биржи."""
    with closing(_conn()) as conn, conn:
        conn.execute("UPDATE accounts SET login = '', secret = '', enabled = 0, connected_at = NULL WHERE exchange = ?",
                     (exchange,))


def set_response_price(exchange: str, price: int) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("UPDATE accounts SET response_price = ? WHERE exchange = ?", (max(price, 0), exchange))


# ---------- Заказы с бирж ----------

def is_empty() -> bool:
    with closing(_conn()) as conn:
        return conn.execute("SELECT 1 FROM orders LIMIT 1").fetchone() is None


def upsert_order(o: dict, matched: list[str]) -> int | None:
    """Новый заказ — сохраняет и возвращает id. Уже известный — обновляет просмотры, отклики и бюджет, возвращает None."""
    with closing(_conn()) as conn, conn:
        cur = conn.execute(
            """INSERT INTO orders (source, link, title, description, category, budget, budget_max, views, offers,
                                   published_at, matched)
               VALUES (:source, :link, :title, :description, :category, :budget, :budget_max, :views, :offers,
                       :published_at, :matched)
               ON CONFLICT(link) DO NOTHING""",
            {**o, "matched": ",".join(matched)},
        )
        if cur.rowcount:
            return cur.lastrowid
        conn.execute("UPDATE orders SET views = ?, offers = ?, budget = ?, budget_max = ? WHERE link = ?",
                     (o["views"], o["offers"], o["budget"], o["budget_max"], o["link"]))
        return None


def get_order(order_id: int):
    with closing(_conn()) as conn:
        return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def set_order_status(order_id: int, status: str) -> None:
    if status not in ORDER_STATUSES:
        raise ValueError(status)
    with closing(_conn()) as conn, conn:
        conn.execute(
            """UPDATE orders SET status = ?,
               applied_at = CASE WHEN ? = 'applied' THEN COALESCE(applied_at, datetime('now', 'localtime')) ELSE applied_at END
               WHERE id = ?""",
            (status, status, order_id),
        )


def list_orders(view: str = "new", q: str = "", min_budget: int = 0, limit: int = 200):
    """view: new / applied / hidden — подходящие заказы с этим статусом; all — все, включая неподходящие."""
    where, args = [], []
    if view in ORDER_STATUSES:
        where += ["matched != ''", "status = ?"]
        args.append(view)
    elif view != "all":
        raise ValueError(view)
    if q:
        where.append("(title LIKE ? OR description LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if min_budget:
        where.append("budget >= ?")
        args.append(min_budget)
    sql = "SELECT * FROM orders" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    with closing(_conn()) as conn:
        return conn.execute(sql, (*args, limit)).fetchall()


# ---------- Сделки и платежи ----------

DEAL_FIELDS = ("title", "client", "contact", "source", "price", "stage", "deadline", "notes",
               "response_cost", "refund", "follow_up")


def save_deal(data: dict, deal_id: int | None = None) -> int:
    values = {k: data.get(k) for k in DEAL_FIELDS}
    if not (values["title"] or "").strip():
        raise ValueError("title")
    if values["stage"] not in STAGES:
        raise ValueError(values["stage"])
    for k in ("price", "response_cost", "refund"):
        values[k] = max(int(values[k] or 0), 0)
    for k in ("deadline", "follow_up"):
        values[k] = date.fromisoformat(values[k]).isoformat() if values[k] else None
    with closing(_conn()) as conn, conn:
        if deal_id:
            conn.execute(
                f"UPDATE projects SET {', '.join(f'{k} = :{k}' for k in DEAL_FIELDS)}, "
                "updated_at = datetime('now', 'localtime') WHERE id = :id",
                {**values, "id": deal_id},
            )
            return deal_id
        values["order_id"] = data.get("order_id")
        values["via_response"] = int(bool(values["order_id"]) or values["stage"] in RESPONSE_STAGES)
        cur = conn.execute(
            f"INSERT INTO projects ({', '.join(values)}) VALUES ({', '.join(':' + k for k in values)})", values)
        return cur.lastrowid


def deal_from_order(order_id: int) -> int:
    """Отклик на заказ биржи: создаёт сделку «Отклик отправлен» (или возвращает уже созданную)."""
    o = get_order(order_id)
    if not o:
        raise ValueError(f"заказ {order_id} не найден")
    with closing(_conn()) as conn:
        row = conn.execute("SELECT id FROM projects WHERE order_id = ?", (order_id,)).fetchone()
    if row:
        return row["id"]
    set_order_status(order_id, "applied")
    acc = get_account(o["source"])
    return save_deal({"title": o["title"], "source": o["source"], "price": o["budget"], "stage": "sent",
                      "notes": o["link"], "order_id": order_id,
                      "response_cost": acc["response_price"] if acc else 0})


def set_stage(deal_id: int, stage: str) -> None:
    if stage not in STAGES:
        raise ValueError(stage)
    with closing(_conn()) as conn, conn:
        conn.execute("UPDATE projects SET stage = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
                     (stage, deal_id))


_DEAL_SELECT = """SELECT p.*, COALESCE((SELECT SUM(amount) FROM payments WHERE project_id = p.id), 0) AS paid
                  FROM projects p"""


def get_deal(deal_id: int):
    with closing(_conn()) as conn:
        return conn.execute(_DEAL_SELECT + " WHERE p.id = ?", (deal_id,)).fetchone()


def list_deals(stages: tuple = (), q: str = "", client: str = ""):
    where, args = [], []
    if stages:
        where.append(f"p.stage IN {_in(stages)}")
    if q:
        where.append("(p.title LIKE ? OR p.client LIKE ? OR p.notes LIKE ? OR p.contact LIKE ?)")
        args += [f"%{q}%"] * 4
    if client:
        where.append("p.client = ?")
        args.append(client)
    sql = _DEAL_SELECT + (" WHERE " + " AND ".join(where) if where else "") + \
        " ORDER BY CASE WHEN p.deadline IS NULL THEN 1 ELSE 0 END, p.deadline, p.updated_at DESC"
    with closing(_conn()) as conn:
        return conn.execute(sql, args).fetchall()


def delete_deal(deal_id: int) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (deal_id,))


def add_payment(deal_id: int, amount: int, paid_on: str, note: str = "") -> None:
    if amount <= 0:
        raise ValueError(amount)
    date.fromisoformat(paid_on)  # проверка формата даты
    with closing(_conn()) as conn, conn:
        conn.execute("INSERT INTO payments (project_id, amount, paid_on, note) VALUES (?, ?, ?, ?)",
                     (deal_id, amount, paid_on, note))


def list_payments(deal_id: int | None = None, limit: int = 200):
    sql = """SELECT pay.*, p.title, p.client FROM payments pay JOIN projects p ON p.id = pay.project_id"""
    args: tuple = ()
    if deal_id:
        sql += " WHERE pay.project_id = ?"
        args = (deal_id,)
    with closing(_conn()) as conn:
        return conn.execute(sql + " ORDER BY pay.paid_on DESC, pay.id DESC LIMIT ?", (*args, limit)).fetchall()


def delete_payment(payment_id: int) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("DELETE FROM payments WHERE id = ?", (payment_id,))


def due_deals(days: int):
    """Сделки в работе с дедлайном в ближайшие days дней (и просроченные)."""
    limit = (date.today() + timedelta(days=days)).isoformat()
    with closing(_conn()) as conn:
        return conn.execute(
            _DEAL_SELECT + f" WHERE p.stage IN {_in(WORK_STAGES)} AND p.deadline IS NOT NULL AND p.deadline <= ?"
            " ORDER BY p.deadline", (limit,)).fetchall()


def follow_ups():
    """«Пора напомнить»: отклики, где наступила дата напоминания или давно нет движения."""
    stale = f"-{int_setting('follow_up_days')} days"
    with closing(_conn()) as conn:
        return conn.execute(
            _DEAL_SELECT + f""" WHERE p.stage IN {_in(RESPONSE_STAGES)} AND (
                   (p.follow_up IS NOT NULL AND p.follow_up <= date('now', 'localtime'))
                   OR (p.follow_up IS NULL AND p.updated_at <= datetime('now', 'localtime', ?)))
               ORDER BY COALESCE(p.follow_up, p.updated_at)""", (stale,)).fetchall()


def clients():
    """Клиенты из сделок: сколько сделок, сколько заплатил, последний контакт."""
    with closing(_conn()) as conn:
        return conn.execute(
            """SELECT p.client, MAX(p.contact) AS contact, COUNT(*) AS deals,
                      SUM(p.stage = 'paid') AS paid_deals,
                      COALESCE(SUM((SELECT SUM(amount) FROM payments WHERE project_id = p.id)), 0) AS revenue,
                      MAX(p.updated_at) AS last_at
               FROM projects p WHERE p.client != '' GROUP BY p.client ORDER BY revenue DESC, last_at DESC"""
        ).fetchall()


# ---------- Шаблоны откликов ----------

def list_templates():
    with closing(_conn()) as conn:
        return conn.execute("SELECT * FROM templates ORDER BY id").fetchall()


def save_template(title: str, body: str, template_id: int | None = None) -> None:
    if not title.strip() or not body.strip():
        raise ValueError("empty template")
    with closing(_conn()) as conn, conn:
        if template_id:
            conn.execute("UPDATE templates SET title = ?, body = ? WHERE id = ?", (title, body, template_id))
        else:
            conn.execute("INSERT INTO templates (title, body) VALUES (?, ?)", (title, body))


def delete_template(template_id: int) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))


# ---------- Фильтры ----------

def get_words(kind: str) -> list[str]:
    with closing(_conn()) as conn:
        return [r["word"] for r in conn.execute("SELECT word FROM filters WHERE kind = ? ORDER BY word", (kind,))]


def add_word(kind: str, word: str) -> None:
    if kind not in FILTER_KINDS:
        raise ValueError(kind)
    with closing(_conn()) as conn, conn:
        conn.execute("INSERT OR IGNORE INTO filters VALUES (?, ?)", (kind, word))


def delete_word(kind: str, word: str) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("DELETE FROM filters WHERE kind = ? AND word = ?", (kind, word))


# ---------- Аналитика и финансы ----------

_PAID_SUB = "COALESCE((SELECT SUM(amount) FROM payments WHERE project_id = p.id), 0)"


def overview() -> dict:
    month = date.today().strftime("%Y-%m")
    with closing(_conn()) as conn:
        q = lambda sql, *a: conn.execute(sql, a).fetchone()[0] or 0  # noqa: E731
        s = {
            "seen_total": q("SELECT COUNT(*) FROM orders"),
            "matched_total": q("SELECT COUNT(*) FROM orders WHERE matched != ''"),
            "matched_today": q("SELECT COUNT(*) FROM orders WHERE matched != '' AND date(created_at) = date('now', 'localtime')"),
            "new_waiting": q("SELECT COUNT(*) FROM orders WHERE matched != '' AND status = 'new'"),
            # отклики
            "responses": q("SELECT COUNT(*) FROM projects WHERE via_response = 1"),
            "responses_week": q("SELECT COUNT(*) FROM projects WHERE via_response = 1 AND created_at >= datetime('now', 'localtime', '-7 days')"),
            "in_dialog": q(f"SELECT COUNT(*) FROM projects WHERE stage IN {_in(('dialog', 'clarify', 'approval'))}"),
            "won": q(f"SELECT COUNT(*) FROM projects WHERE via_response = 1 AND stage IN {_in((*WORK_STAGES, 'paid'))}"),
            "won_paid": q("SELECT COUNT(*) FROM projects WHERE via_response = 1 AND stage = 'paid'"),
            "response_spend": q("SELECT SUM(response_cost) FROM projects"),
            "response_refund": q("SELECT SUM(refund) FROM projects"),
            # отклики без ответа, за которые биржа может вернуть деньги
            "refund_possible": q(f"SELECT SUM(response_cost - refund) FROM projects WHERE stage IN {_in(('sent', 'lost'))} AND response_cost > refund"),
            "response_income": q(f"SELECT SUM({_PAID_SUB}) FROM projects p WHERE p.via_response = 1"),
            # деньги
            "income_month": q("SELECT SUM(amount) FROM payments WHERE substr(paid_on, 1, 7) = ?", month),
            "income_total": q("SELECT SUM(amount) FROM payments"),
            "awaiting": q(f"SELECT SUM(p.price - {_PAID_SUB}) FROM projects p WHERE p.stage IN {_in(WORK_STAGES)} AND p.price > {_PAID_SUB}"),
            "potential": q(f"SELECT SUM(price) FROM projects WHERE stage IN {_in(RESPONSE_STAGES)}"),
            "month_due": q(f"""SELECT SUM(p.price - {_PAID_SUB}) FROM projects p WHERE p.price > {_PAID_SUB} AND (
                               p.stage = 'done' OR (p.stage IN {_in(WORK_STAGES)} AND substr(p.deadline, 1, 7) = ?))""", month),
            "workload": q(f"SELECT COUNT(*) FROM projects WHERE stage IN {_in(('ready', 'work', 'revisions', 'review'))}"),
            "avg_check": q("SELECT AVG(price) FROM projects WHERE stage = 'paid'"),
            "avg_budget": q("SELECT AVG(budget) FROM orders WHERE matched != '' AND budget > 0"),
        }
    s["follow_ups"] = len(follow_ups())
    s["conversion"] = round(100 * s["won"] / s["responses"]) if s["responses"] else 0
    net_spend = s["response_spend"] - s["response_refund"]
    s["response_net_spend"] = net_spend
    s["response_profit"] = s["response_income"] - net_spend
    s["roi"] = round(s["response_income"] / net_spend, 1) if net_spend else 0
    s["forecast_month"] = s["income_month"] + s["month_due"]
    s["income_goal"] = int_setting("income_goal")
    s["goal_pct"] = min(round(100 * s["income_month"] / s["income_goal"]), 100) if s["income_goal"] else 0
    s["tax_rate"] = int_setting("tax_rate")
    s["tax_month"] = round(s["income_month"] * s["tax_rate"] / 100)
    s["avg_check"] = round(s["avg_check"])
    s["avg_budget"] = round(s["avg_budget"])
    return s


def responses_by_source() -> list[dict]:
    """Окупаемость откликов по биржам."""
    with closing(_conn()) as conn:
        rows = conn.execute(
            f"""SELECT COALESCE(NULLIF(source, ''), 'Напрямую') AS source, COUNT(*) AS responses,
                       SUM(stage IN {_in((*WORK_STAGES, 'paid'))}) AS won,
                       SUM(response_cost) AS spend, SUM(refund) AS refund,
                       SUM({_PAID_SUB}) AS income
                FROM projects p WHERE via_response = 1 GROUP BY 1 ORDER BY income DESC, responses DESC"""
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        net = (d["spend"] or 0) - (d["refund"] or 0)
        d["profit"] = (d["income"] or 0) - net
        d["conversion"] = round(100 * d["won"] / d["responses"]) if d["responses"] else 0
        result.append(d)
    return result


def income_by_month(months: int = 6) -> list[tuple[str, int]]:
    """[('2026-06', 0), ..., ('2026-11', 15000)] — последние months месяцев, включая пустые."""
    first = date.today().replace(day=1)
    keys = []
    for _ in range(months):
        keys.insert(0, first.strftime("%Y-%m"))
        first = (first - timedelta(days=1)).replace(day=1)
    with closing(_conn()) as conn:
        rows = dict(conn.execute(
            "SELECT substr(paid_on, 1, 7), SUM(amount) FROM payments WHERE substr(paid_on, 1, 7) >= ? GROUP BY 1",
            (keys[0],)).fetchall())
    return [(k, rows.get(k, 0)) for k in keys]


def found_by_day(days: int = 14) -> list[tuple[str, int]]:
    start = date.today() - timedelta(days=days - 1)
    with closing(_conn()) as conn:
        rows = dict(conn.execute(
            "SELECT date(created_at), COUNT(*) FROM orders WHERE matched != '' AND date(created_at) >= ? GROUP BY 1",
            (start.isoformat(),)).fetchall())
    days_list = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    return [(d, rows.get(d, 0)) for d in days_list]


def top_categories(limit: int = 6) -> list[tuple[str, int, int]]:
    """(категория, число подходящих заказов, средний бюджет)."""
    with closing(_conn()) as conn:
        return [tuple(r) for r in conn.execute(
            """SELECT COALESCE(NULLIF(category, ''), 'Без категории'), COUNT(*), CAST(AVG(budget) AS INTEGER)
               FROM orders WHERE matched != '' GROUP BY 1 ORDER BY 2 DESC LIMIT ?""", (limit,))]


def funnel() -> list[tuple[str, int]]:
    s = overview()
    return [("Подходящих заказов", s["matched_total"]), ("Откликов", s["responses"]),
            ("Взял в работу", s["won"]), ("Оплачено", s["won_paid"])]


def search(q: str) -> dict:
    return {"orders": list_orders("all", q=q, limit=30), "deals": list_deals(q=q),
            "clients": [c for c in clients() if q.lower() in (c["client"] + " " + (c["contact"] or "")).lower()]}
