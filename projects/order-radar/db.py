"""SQLite: заказы и ключевые слова."""
import sqlite3
from contextlib import closing

from config import DEFAULT_KEYWORDS

DB_PATH = "radar.db"
STATUSES = ("new", "applied", "hidden")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
                published_at TEXT,
                matched TEXT NOT NULL DEFAULT '',   -- найденные ключевые слова через запятую
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS keywords (word TEXT PRIMARY KEY);
            """
        )
        if not conn.execute("SELECT 1 FROM keywords LIMIT 1").fetchone():
            conn.executemany("INSERT INTO keywords VALUES (?)", [(w,) for w in DEFAULT_KEYWORDS])


def is_empty() -> bool:
    with closing(_conn()) as conn:
        return conn.execute("SELECT 1 FROM orders LIMIT 1").fetchone() is None


def add_order(o: dict, matched: list[str]) -> int | None:
    """Сохраняет заказ. Возвращает id нового заказа или None, если он уже был."""
    with closing(_conn()) as conn, conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO orders (source, link, title, description, category, published_at, matched)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (o["source"], o["link"], o["title"], o["description"], o["category"], o["published_at"], ",".join(matched)),
        )
        return cur.lastrowid if cur.rowcount else None


def get_order(order_id: int) -> sqlite3.Row | None:
    with closing(_conn()) as conn:
        return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def set_status(order_id: int, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(status)
    with closing(_conn()) as conn, conn:
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))


def list_orders(view: str, limit: int = 100) -> list[sqlite3.Row]:
    """view: new / applied / hidden — подходящие заказы с этим статусом; all — вообще все."""
    with closing(_conn()) as conn:
        if view == "all":
            return conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return conn.execute(
            "SELECT * FROM orders WHERE matched != '' AND status = ? ORDER BY id DESC LIMIT ?", (view, limit)
        ).fetchall()


def stats() -> dict:
    with closing(_conn()) as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(matched != '') AS matched,
                      SUM(matched != '' AND status = 'new') AS new,
                      SUM(status = 'applied') AS applied,
                      SUM(status = 'hidden') AS hidden,
                      SUM(matched != '' AND date(created_at) = date('now')) AS today
               FROM orders"""
        ).fetchone()
    return {k: row[k] or 0 for k in row.keys()}


def get_keywords() -> list[str]:
    with closing(_conn()) as conn:
        return [r["word"] for r in conn.execute("SELECT word FROM keywords ORDER BY word")]


def add_keyword(word: str) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("INSERT OR IGNORE INTO keywords VALUES (?)", (word,))


def delete_keyword(word: str) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("DELETE FROM keywords WHERE word = ?", (word,))
