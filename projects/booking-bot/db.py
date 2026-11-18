"""Работа с базой данных SQLite."""
import sqlite3
from contextlib import closing

DB_PATH = "bookings.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(get_connection()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                name       TEXT    NOT NULL,
                phone      TEXT    NOT NULL,
                service    TEXT    NOT NULL,
                date       TEXT    NOT NULL,
                time       TEXT    NOT NULL,
                created_at TEXT    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (date, time)
            )
            """
        )


def get_busy_slots(date: str) -> set[str]:
    with closing(get_connection()) as conn:
        rows = conn.execute("SELECT time FROM bookings WHERE date = ?", (date,)).fetchall()
    return {row["time"] for row in rows}


def add_booking(user_id: int, name: str, phone: str, service: str, date: str, time: str) -> int | None:
    """Создаёт запись. Возвращает id или None, если слот уже занят."""
    try:
        with closing(get_connection()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO bookings (user_id, name, phone, service, date, time) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, name, phone, service, date, time),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def get_user_bookings(user_id: int, from_date: str) -> list[sqlite3.Row]:
    with closing(get_connection()) as conn:
        return conn.execute(
            "SELECT * FROM bookings WHERE user_id = ? AND date >= ? ORDER BY date, time",
            (user_id, from_date),
        ).fetchall()


def get_bookings_by_date(date: str) -> list[sqlite3.Row]:
    with closing(get_connection()) as conn:
        return conn.execute("SELECT * FROM bookings WHERE date = ? ORDER BY time", (date,)).fetchall()


def delete_booking(booking_id: int, user_id: int) -> sqlite3.Row | None:
    """Удаляет запись пользователя и возвращает её (или None, если не найдена)."""
    with closing(get_connection()) as conn, conn:
        row = conn.execute(
            "SELECT * FROM bookings WHERE id = ? AND user_id = ?", (booking_id, user_id)
        ).fetchone()
        if row:
            conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
        return row
