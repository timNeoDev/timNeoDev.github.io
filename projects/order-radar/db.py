"""SQLite: заказы с бирж, фильтры, CRM-проекты, платежи, шаблоны откликов, настройки и аналитика."""
import sqlite3
from contextlib import closing
from datetime import date, timedelta

from config import DEFAULT_KEYWORDS, DEFAULT_MINUS_WORDS

DB_PATH = "radar.db"

ORDER_STATUSES = {"new": "Новый", "applied": "Откликнулся", "hidden": "Скрыт"}
STAGES = {"lead": "Переговоры", "work": "В работе", "review": "На проверке",
          "done": "Сдан", "paid": "Оплачен", "cancelled": "Отменён"}
ACTIVE_STAGES = ("lead", "work", "review", "done")
FILTER_KINDS = ("plus", "minus")

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


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with closing(_conn()) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                link TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                description TEXT,
                category TEXT,
                budget INTEGER DEFAULT 0,
                budget_max INTEGER DEFAULT 0,
                views INTEGER DEFAULT 0,
                offers INTEGER DEFAULT 0,
                published_at TEXT,                  -- время биржи (Kwork — московское)
                matched TEXT NOT NULL DEFAULT '',   -- найденные ключевые слова через запятую; '' — не подходит
                status TEXT NOT NULL DEFAULT 'new',
                applied_at TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS filters (kind TEXT NOT NULL, word TEXT NOT NULL, PRIMARY KEY (kind, word));
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                client TEXT DEFAULT '',
                contact TEXT DEFAULT '',
                source TEXT DEFAULT '',
                order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
                price INTEGER DEFAULT 0,
                stage TEXT NOT NULL DEFAULT 'lead',
                deadline TEXT,                      -- YYYY-MM-DD
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                amount INTEGER NOT NULL,
                paid_on TEXT NOT NULL,              -- YYYY-MM-DD
                note TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                body TEXT NOT NULL
            );
            """
        )
        if not conn.execute("SELECT 1 FROM filters LIMIT 1").fetchone():
            conn.executemany("INSERT INTO filters VALUES ('plus', ?)", [(w,) for w in DEFAULT_KEYWORDS])
            conn.executemany("INSERT INTO filters VALUES ('minus', ?)", [(w,) for w in DEFAULT_MINUS_WORDS])
        if not conn.execute("SELECT 1 FROM templates LIMIT 1").fetchone():
            conn.executemany("INSERT INTO templates (title, body) VALUES (?, ?)", DEFAULT_TEMPLATES)


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
    if view == "all":
        pass
    elif view in ORDER_STATUSES:
        where += ["matched != ''", "status = ?"]
        args.append(view)
    else:
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


# ---------- Фильтры и настройки ----------

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


def get_setting(key: str, default: str = "") -> str:
    with closing(_conn()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, str(value)))


def min_budget() -> int:
    return int(get_setting("min_budget", "0") or 0)


# ---------- CRM: проекты и платежи ----------

PROJECT_FIELDS = ("title", "client", "contact", "source", "price", "stage", "deadline", "notes")


def save_project(data: dict, project_id: int | None = None) -> int:
    values = {k: data.get(k) for k in PROJECT_FIELDS}
    if not values["title"]:
        raise ValueError("title")
    if values["stage"] not in STAGES:
        raise ValueError(values["stage"])
    values["price"] = int(values["price"] or 0)
    values["deadline"] = values["deadline"] or None
    with closing(_conn()) as conn, conn:
        if project_id:
            conn.execute(
                f"UPDATE projects SET {', '.join(f'{k} = :{k}' for k in PROJECT_FIELDS)}, "
                "updated_at = datetime('now', 'localtime') WHERE id = :id",
                {**values, "id": project_id},
            )
            return project_id
        values["order_id"] = data.get("order_id")
        cur = conn.execute(
            "INSERT INTO projects (title, client, contact, source, price, stage, deadline, notes, order_id) "
            "VALUES (:title, :client, :contact, :source, :price, :stage, :deadline, :notes, :order_id)",
            values,
        )
        return cur.lastrowid


def project_from_order(order_id: int) -> int:
    """Создаёт проект в CRM из заказа биржи (или возвращает уже созданный)."""
    with closing(_conn()) as conn:
        row = conn.execute("SELECT id FROM projects WHERE order_id = ?", (order_id,)).fetchone()
    if row:
        return row["id"]
    o = get_order(order_id)
    set_order_status(order_id, "applied")
    return save_project({"title": o["title"], "source": o["source"], "price": o["budget"], "stage": "lead",
                         "notes": o["link"], "order_id": order_id})


def set_stage(project_id: int, stage: str) -> None:
    if stage not in STAGES:
        raise ValueError(stage)
    with closing(_conn()) as conn, conn:
        conn.execute("UPDATE projects SET stage = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
                     (stage, project_id))


_PROJECT_SELECT = """SELECT p.*, COALESCE((SELECT SUM(amount) FROM payments WHERE project_id = p.id), 0) AS paid
                     FROM projects p"""


def get_project(project_id: int):
    with closing(_conn()) as conn:
        return conn.execute(_PROJECT_SELECT + " WHERE p.id = ?", (project_id,)).fetchone()


def list_projects(stage: str = ""):
    sql = _PROJECT_SELECT + (" WHERE p.stage = ?" if stage else "") + \
        " ORDER BY CASE WHEN p.deadline IS NULL THEN 1 ELSE 0 END, p.deadline, p.id DESC"
    with closing(_conn()) as conn:
        return conn.execute(sql, (stage,) if stage else ()).fetchall()


def delete_project(project_id: int) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


def add_payment(project_id: int, amount: int, paid_on: str, note: str = "") -> None:
    if amount <= 0:
        raise ValueError(amount)
    date.fromisoformat(paid_on)  # проверка формата даты
    with closing(_conn()) as conn, conn:
        conn.execute("INSERT INTO payments (project_id, amount, paid_on, note) VALUES (?, ?, ?, ?)",
                     (project_id, amount, paid_on, note))


def list_payments(project_id: int):
    with closing(_conn()) as conn:
        return conn.execute("SELECT * FROM payments WHERE project_id = ? ORDER BY paid_on DESC, id DESC",
                            (project_id,)).fetchall()


def delete_payment(payment_id: int) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("DELETE FROM payments WHERE id = ?", (payment_id,))


def due_projects(days: int):
    """Активные проекты с дедлайном в ближайшие days дней (и просроченные)."""
    limit = (date.today() + timedelta(days=days)).isoformat()
    with closing(_conn()) as conn:
        return conn.execute(
            _PROJECT_SELECT + f" WHERE p.stage IN {ACTIVE_STAGES} AND p.deadline IS NOT NULL AND p.deadline <= ?"
            " ORDER BY p.deadline", (limit,)
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


# ---------- Аналитика ----------

def overview() -> dict:
    month = date.today().strftime("%Y-%m")
    with closing(_conn()) as conn:
        q = lambda sql, *a: conn.execute(sql, a).fetchone()[0] or 0  # noqa: E731
        stats = {
            "seen_total": q("SELECT COUNT(*) FROM orders"),
            "matched_total": q("SELECT COUNT(*) FROM orders WHERE matched != ''"),
            "matched_today": q("SELECT COUNT(*) FROM orders WHERE matched != '' AND date(created_at) = date('now', 'localtime')"),
            "new_waiting": q("SELECT COUNT(*) FROM orders WHERE matched != '' AND status = 'new'"),
            "applied_total": q("SELECT COUNT(*) FROM orders WHERE status = 'applied'"),
            "applied_week": q("SELECT COUNT(*) FROM orders WHERE applied_at >= datetime('now', 'localtime', '-7 days')"),
            "projects_active": q(f"SELECT COUNT(*) FROM projects WHERE stage IN {ACTIVE_STAGES}"),
            "projects_from_orders": q("SELECT COUNT(*) FROM projects WHERE order_id IS NOT NULL"),
            "projects_paid": q("SELECT COUNT(*) FROM projects WHERE stage = 'paid' AND order_id IS NOT NULL"),
            "income_month": q("SELECT SUM(amount) FROM payments WHERE substr(paid_on, 1, 7) = ?", month),
            "income_total": q("SELECT SUM(amount) FROM payments"),
            "avg_check": q("SELECT AVG(price) FROM projects WHERE stage = 'paid'"),
            "avg_budget": q("SELECT AVG(budget) FROM orders WHERE matched != '' AND budget > 0"),
            "receivable": q(f"""SELECT SUM(p.price - COALESCE((SELECT SUM(amount) FROM payments WHERE project_id = p.id), 0))
                                FROM projects p WHERE p.stage IN {ACTIVE_STAGES}"""),
        }
    applied = stats["applied_total"]
    stats["conversion"] = round(100 * stats["projects_from_orders"] / applied) if applied else 0
    stats["avg_check"] = round(stats["avg_check"])
    stats["avg_budget"] = round(stats["avg_budget"])
    return stats


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
    return [((start + timedelta(days=i)).isoformat(), rows.get((start + timedelta(days=i)).isoformat(), 0))
            for i in range(days)]


def top_categories(limit: int = 6) -> list[tuple[str, int, int]]:
    """(категория, число подходящих заказов, средний бюджет)."""
    with closing(_conn()) as conn:
        return [tuple(r) for r in conn.execute(
            """SELECT COALESCE(NULLIF(category, ''), 'Без категории'), COUNT(*), CAST(AVG(budget) AS INTEGER)
               FROM orders WHERE matched != '' GROUP BY 1 ORDER BY 2 DESC LIMIT ?""", (limit,))]


def funnel() -> list[tuple[str, int]]:
    s = overview()
    return [("Найдено", s["matched_total"]), ("Откликнулся", s["applied_total"]),
            ("Взял в работу", s["projects_from_orders"]), ("Оплачено", s["projects_paid"])]
