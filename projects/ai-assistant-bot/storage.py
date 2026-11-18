"""Хранилище: история диалогов и лимиты — в памяти, заявки и статистика — в SQLite."""
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import date

from config import DAILY_LIMIT, HISTORY_SIZE

DB_PATH = "assistant.db"

# ---------- История диалогов (в памяти) ----------

_history: dict[int, list[dict]] = defaultdict(list)


def get_history(user_id: int) -> list[dict]:
    return _history[user_id]


def add_exchange(user_id: int, question: str, answer: str) -> None:
    h = _history[user_id]
    h.append({"role": "user", "content": question})
    h.append({"role": "assistant", "content": answer})
    del h[:-HISTORY_SIZE]  # чётный размер: история всегда начинается с сообщения пользователя


def reset_history(user_id: int) -> None:
    _history.pop(user_id, None)


# ---------- Дневной лимит (в памяти) ----------

_usage: dict[int, tuple[date, int]] = {}


def try_spend(user_id: int) -> bool:
    """Списывает одно сообщение из дневного лимита. False — лимит исчерпан."""
    today = date.today()
    day, count = _usage.get(user_id, (today, 0))
    if day != today:
        count = 0
    if count >= DAILY_LIMIT:
        return False
    _usage[user_id] = (today, count + 1)
    return True


# ---------- SQLite: заявки и статистика ----------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(_conn()) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, name TEXT, phone TEXT, details TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ai_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, cost_usd REAL,
                created_at TEXT DEFAULT (date('now', 'localtime'))
            );
            """
        )


def add_booking(user_id: int, name: str, phone: str, details: str) -> int:
    with closing(_conn()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO bookings (user_id, name, phone, details) VALUES (?, ?, ?, ?)",
            (user_id, name, phone, details),
        )
        return cur.lastrowid


def last_bookings(limit: int = 10) -> list[sqlite3.Row]:
    with closing(_conn()) as conn:
        return conn.execute("SELECT * FROM bookings ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def log_request(user_id: int, cost_usd: float) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("INSERT INTO ai_requests (user_id, cost_usd) VALUES (?, ?)", (user_id, cost_usd))


def stats() -> dict:
    with closing(_conn()) as conn:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS total, COUNT(DISTINCT user_id) AS users, COALESCE(SUM(cost_usd), 0) AS cost,
              SUM(created_at = date('now', 'localtime')) AS today,
              COALESCE(SUM(CASE WHEN created_at = date('now', 'localtime') THEN cost_usd END), 0) AS cost_today
            FROM ai_requests
            """
        ).fetchone()
        bookings = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    return {**dict(row), "bookings": bookings}
