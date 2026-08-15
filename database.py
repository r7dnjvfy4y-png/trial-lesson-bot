"""
Слой работы с базой данных (SQLite, асинхронно через aiosqlite).

Таблицы:
- clients   — карточка каждого ученика/родителя, который писал боту
- bookings  — подтверждённые записи на пробный урок (группа до
              config.CAPACITY_PER_SLOT человек на один слот даты+времени)
- waitlist  — заявки в лист ожидания ("не нашёл(а) подходящее время")
- bot_texts — тексты бота, переопределённые через /texts (см. texts.py)
"""

import aiosqlite
from datetime import datetime, timezone

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE NOT NULL,
    username TEXT,
    full_name TEXT,
    contact TEXT,
    level TEXT,
    status TEXT DEFAULT 'new',
    stall_prompted INTEGER DEFAULT 0,
    voice_status TEXT DEFAULT '',
    last_activity TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    status TEXT DEFAULT 'confirmed',
    created_at TEXT,
    FOREIGN KEY (client_id) REFERENCES clients (id)
);

CREATE TABLE IF NOT EXISTS waitlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    desired_text TEXT,
    created_at TEXT,
    FOREIGN KEY (client_id) REFERENCES clients (id)
);

CREATE TABLE IF NOT EXISTS bot_texts (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
        # Миграции для баз, созданных до появления новых колонок
        for stmt in (
            "ALTER TABLE clients ADD COLUMN level TEXT",
            "ALTER TABLE clients ADD COLUMN voice_status TEXT DEFAULT ''",
        ):
            try:
                await db.execute(stmt)
                await db.commit()
            except aiosqlite.OperationalError:
                pass  # колонка уже есть


async def get_or_create_client(telegram_id: int, username: str | None) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM clients WHERE telegram_id = ?", (telegram_id,))
        row = await cur.fetchone()
        if row:
            await db.execute(
                "UPDATE clients SET username = ?, last_activity = ? WHERE telegram_id = ?",
                (username, _now(), telegram_id),
            )
            await db.commit()
            return dict(row)

        await db.execute(
            """INSERT INTO clients (telegram_id, username, status, last_activity, created_at)
               VALUES (?, ?, 'new', ?, ?)""",
            (telegram_id, username, _now(), _now()),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM clients WHERE telegram_id = ?", (telegram_id,))
        row = await cur.fetchone()
        return dict(row)


async def update_client(telegram_id: int, **fields) -> None:
    if not fields:
        return
    fields["last_activity"] = _now()
    columns = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [telegram_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE clients SET {columns} WHERE telegram_id = ?", values)
        await db.commit()


async def touch_client(telegram_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE clients SET last_activity = ? WHERE telegram_id = ?",
            (_now(), telegram_id),
        )
        await db.commit()


async def get_client(telegram_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM clients WHERE telegram_id = ?", (telegram_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def count_booked(date: str, time: str) -> int:
    """Сколько мест в слоте занято. Считаются и подтверждённые записи, и
    'pending' — те, кто выбрал время и сейчас записывает голосовое: место за
    ними держится, чтобы его не занял кто-то другой."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """SELECT COUNT(*) FROM bookings
               WHERE date = ? AND time = ? AND status IN ('confirmed', 'pending')""",
            (date, time),
        )
        row = await cur.fetchone()
        return row[0] if row else 0


async def create_booking(client_id: int, date: str, time: str, status: str = "confirmed") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO bookings (client_id, date, time, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (client_id, date, time, status, _now()),
        )
        await db.commit()
        return cur.lastrowid


async def confirm_booking(booking_id: int) -> dict | None:
    """Перевести бронь из 'pending' в 'confirmed' (после голосового)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "UPDATE bookings SET status = 'confirmed' WHERE id = ? AND status = 'pending'",
            (booking_id,),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def drop_pending_of(telegram_id: int) -> None:
    """Убрать незавершённые брони человека — например, если он начал заново."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """DELETE FROM bookings
               WHERE status = 'pending'
                 AND client_id IN (SELECT id FROM clients WHERE telegram_id = ?)""",
            (telegram_id,),
        )
        await db.commit()


async def release_stale_pending(older_than_iso: str) -> list[dict]:
    """Освободить места, забронированные под голосовое, но так и не
    подтверждённые. Возвращает список освобождённых броней."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT b.id, b.date, b.time, c.telegram_id, c.full_name
               FROM bookings b JOIN clients c ON c.id = b.client_id
               WHERE b.status = 'pending' AND b.created_at < ?""",
            (older_than_iso,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            await db.execute(
                "DELETE FROM bookings WHERE status = 'pending' AND created_at < ?",
                (older_than_iso,),
            )
            await db.commit()
        return rows


async def delete_bookings_of(telegram_id: int) -> int:
    """Полностью удалить все записи одного человека (используется для чистки
    собственных тестовых записей). Возвращает количество удалённых."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """DELETE FROM bookings
               WHERE client_id IN (SELECT id FROM clients WHERE telegram_id = ?)""",
            (telegram_id,),
        )
        await db.commit()
        return cur.rowcount or 0


async def wipe_all_data() -> dict:
    """Удалить все записи, заявки и карточки клиентов. Тексты, отредактированные
    через /texts, НЕ трогаются."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM bookings")
        bookings = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM clients")
        clients = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM waitlist")
        waits = (await cur.fetchone())[0]

        await db.execute("DELETE FROM bookings")
        await db.execute("DELETE FROM waitlist")
        await db.execute("DELETE FROM clients")
        await db.commit()
        return {"bookings": bookings, "clients": clients, "waitlist": waits}


async def add_waitlist(client_id: int, desired_text: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO waitlist (client_id, desired_text, created_at) VALUES (?, ?, ?)",
            (client_id, desired_text, _now()),
        )
        await db.commit()


async def get_stalled_clients(statuses: list[str], older_than_iso: str) -> list[dict]:
    placeholders = ",".join("?" for _ in statuses)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""SELECT * FROM clients
                WHERE status IN ({placeholders})
                  AND stall_prompted = 0
                  AND last_activity < ?""",
            (*statuses, older_than_iso),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_clients_awaiting_voice(older_than_iso: str) -> list[dict]:
    """Записавшиеся, но пока не приславшие голосовое — для одного напоминания."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM clients
               WHERE status = 'booked'
                 AND voice_status = 'awaiting'
                 AND stall_prompted = 0
                 AND last_activity < ?""",
            (older_than_iso,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_today_bookings(date: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT b.date, b.time, c.full_name, c.contact, c.username, c.level
               FROM bookings b JOIN clients c ON c.id = b.client_id
               WHERE b.date = ? AND b.status = 'confirmed'
               ORDER BY b.time""",
            (date,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_slot_roster(date: str, time: str) -> list[dict]:
    """Кто записан в конкретный слот — с id брони, чтобы её можно было
    удалить или перенести из админ-меню /manage."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT b.id AS booking_id, b.status AS booking_status, b.date, b.time,
                      c.telegram_id, c.full_name, c.contact, c.username, c.level, c.voice_status
               FROM bookings b JOIN clients c ON c.id = b.client_id
               WHERE b.date = ? AND b.time = ? AND b.status IN ('confirmed', 'pending')
               ORDER BY b.created_at""",
            (date, time),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_booking(booking_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT b.id AS booking_id, b.date, b.time, b.status AS booking_status,
                      c.telegram_id, c.full_name, c.contact, c.username, c.level
               FROM bookings b JOIN clients c ON c.id = b.client_id
               WHERE b.id = ?""",
            (booking_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def delete_booking(booking_id: int) -> None:
    """Удалить одну конкретную запись — место в слоте освобождается."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
        await db.commit()


async def move_booking(booking_id: int, date: str, time: str) -> None:
    """Перенести запись в другой слот (дата+время)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bookings SET date = ?, time = ? WHERE id = ?",
            (date, time, booking_id),
        )
        await db.commit()


async def get_recent_clients(limit: int = 30) -> list[dict]:
    """Последние по активности карточки клиентов + их ближайшая/последняя
    подтверждённая запись (если есть) — для обзора всей воронки."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT c.*,
                      (SELECT b.date FROM bookings b
                        WHERE b.client_id = c.id AND b.status = 'confirmed'
                        ORDER BY b.date DESC, b.time DESC LIMIT 1) AS booking_date,
                      (SELECT b.time FROM bookings b
                        WHERE b.client_id = c.id AND b.status = 'confirmed'
                        ORDER BY b.date DESC, b.time DESC LIMIT 1) AS booking_time
               FROM clients c
               ORDER BY c.last_activity DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_text_override(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM bot_texts WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_text_override(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO bot_texts (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, _now()),
        )
        await db.commit()


async def delete_text_override(key: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bot_texts WHERE key = ?", (key,))
        await db.commit()


async def get_all_text_overrides() -> dict[str, str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT key, value FROM bot_texts")
        rows = await cur.fetchall()
        return {k: v for k, v in rows}


async def get_all_clients_for_export() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT c.full_name, c.level, c.status, c.voice_status, c.contact, c.username,
                      c.telegram_id, c.created_at, c.last_activity,
                      (SELECT b.date FROM bookings b
                        WHERE b.client_id = c.id AND b.status = 'confirmed'
                        ORDER BY b.date DESC, b.time DESC LIMIT 1) AS booking_date,
                      (SELECT b.time FROM bookings b
                        WHERE b.client_id = c.id AND b.status = 'confirmed'
                        ORDER BY b.date DESC, b.time DESC LIMIT 1) AS booking_time
               FROM clients c
               ORDER BY c.created_at DESC"""
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
