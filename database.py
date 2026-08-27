"""
Слой работы с базой данных (SQLite, асинхронно через aiosqlite).

Таблицы:
- clients        — карточка ученицы
- bookings       — записи на пробный урок
                   статусы: held (держим под голосовое) → review (ждёт
                   прослушивания) → confirmed (подтверждена) / cancelled
- schedule_rules — повторяющиеся правила расписания («A2 — каждую субботу
                   13:00»), из них автоматически создаются слоты на недели вперёд
- slot_blocks    — разово отменённые слоты (конкретная дата+время)
- settings       — ссылка на урок, материалы по уровням и прочие настройки,
                   редактируемые прямо в Telegram
- bot_texts      — тексты сообщений, переопределённые через админку
"""

import aiosqlite
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE NOT NULL,
    username TEXT,
    full_name TEXT,
    claimed_level TEXT,
    confirmed_level TEXT,
    status TEXT DEFAULT 'new',
    voice_chat_id INTEGER,
    voice_message_id INTEGER,
    last_activity TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    level TEXT NOT NULL,
    status TEXT DEFAULT 'held',
    created_at TEXT,
    confirmed_at TEXT,
    reminder_day_sent INTEGER DEFAULT 0,
    reminder_hour_sent INTEGER DEFAULT 0,
    FOREIGN KEY (client_id) REFERENCES clients (id)
);

CREATE TABLE IF NOT EXISTS schedule_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weekday INTEGER NOT NULL,
    time TEXT NOT NULL,
    level TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS slot_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    level TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS bot_texts (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT
);
"""

# Записи, которые занимают место в слоте
ACTIVE_STATUSES = ("held", "review", "confirmed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db():
    return aiosqlite.connect(config.DB_PATH)


async def init_db() -> None:
    async with _db() as db:
        await db.executescript(SCHEMA)
        await db.commit()

        # Первый запуск: заполняем расписание стартовыми правилами
        cur = await db.execute("SELECT COUNT(*) FROM schedule_rules")
        if (await cur.fetchone())[0] == 0:
            for weekday, time_hm, level in config.DEFAULT_RULES:
                await db.execute(
                    """INSERT INTO schedule_rules (weekday, time, level, capacity, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (weekday, time_hm, level, config.DEFAULT_CAPACITY, _now()),
                )
            await db.commit()


# ------------------------------------------------------------- настройки ---

async def get_setting(key: str, default: str = "") -> str:
    async with _db() as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    async with _db() as db:
        await db.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, value, _now()),
        )
        await db.commit()


# ----------------------------------------------------------------- тексты --

async def get_text_override(key: str) -> str | None:
    async with _db() as db:
        cur = await db.execute("SELECT value FROM bot_texts WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_text_override(key: str, value: str) -> None:
    async with _db() as db:
        await db.execute(
            """INSERT INTO bot_texts (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, value, _now()),
        )
        await db.commit()


async def delete_text_override(key: str) -> None:
    async with _db() as db:
        await db.execute("DELETE FROM bot_texts WHERE key = ?", (key,))
        await db.commit()


async def get_all_text_overrides() -> dict[str, str]:
    async with _db() as db:
        cur = await db.execute("SELECT key, value FROM bot_texts")
        return {k: v for k, v in await cur.fetchall()}


# ------------------------------------------------------ правила расписания --

async def get_rules() -> list[dict]:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM schedule_rules ORDER BY weekday, time, level"
        )
        return [dict(r) for r in await cur.fetchall()]


async def add_rule(weekday: int, time_hm: str, level: str, capacity: int) -> int:
    async with _db() as db:
        cur = await db.execute(
            """INSERT INTO schedule_rules (weekday, time, level, capacity, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (weekday, time_hm, level, capacity, _now()),
        )
        await db.commit()
        return cur.lastrowid


async def delete_rule(rule_id: int) -> None:
    async with _db() as db:
        await db.execute("DELETE FROM schedule_rules WHERE id = ?", (rule_id,))
        await db.commit()


# --------------------------------------------------- разово снятые слоты ---

async def get_blocks() -> set[tuple[str, str, str]]:
    async with _db() as db:
        cur = await db.execute("SELECT date, time, level FROM slot_blocks")
        return {(d, t, lvl) for d, t, lvl in await cur.fetchall()}


async def block_slot(date: str, time_hm: str, level: str) -> None:
    async with _db() as db:
        await db.execute(
            "INSERT INTO slot_blocks (date, time, level, created_at) VALUES (?, ?, ?, ?)",
            (date, time_hm, level, _now()),
        )
        await db.commit()


async def unblock_slot(date: str, time_hm: str, level: str) -> None:
    async with _db() as db:
        await db.execute(
            "DELETE FROM slot_blocks WHERE date = ? AND time = ? AND level = ?",
            (date, time_hm, level),
        )
        await db.commit()


# ----------------------------------------------------------------- клиенты --

async def get_or_create_client(telegram_id: int, username: str | None, full_name: str) -> dict:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM clients WHERE telegram_id = ?", (telegram_id,))
        row = await cur.fetchone()
        if row:
            await db.execute(
                "UPDATE clients SET username = ?, full_name = ?, last_activity = ? WHERE telegram_id = ?",
                (username, full_name, _now(), telegram_id),
            )
            await db.commit()
            cur = await db.execute("SELECT * FROM clients WHERE telegram_id = ?", (telegram_id,))
            return dict(await cur.fetchone())

        await db.execute(
            """INSERT INTO clients (telegram_id, username, full_name, status, last_activity, created_at)
               VALUES (?, ?, ?, 'new', ?, ?)""",
            (telegram_id, username, full_name, _now(), _now()),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM clients WHERE telegram_id = ?", (telegram_id,))
        return dict(await cur.fetchone())


async def update_client(telegram_id: int, **fields) -> None:
    if not fields:
        return
    fields["last_activity"] = _now()
    columns = ", ".join(f"{k} = ?" for k in fields)
    async with _db() as db:
        await db.execute(
            f"UPDATE clients SET {columns} WHERE telegram_id = ?",
            list(fields.values()) + [telegram_id],
        )
        await db.commit()


async def get_client(telegram_id: int) -> dict | None:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM clients WHERE telegram_id = ?", (telegram_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_client_by_id(client_id: int) -> dict | None:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM clients WHERE id = ?", (client_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


# ------------------------------------------------------------------ записи --

async def count_taken(date: str, time_hm: str, level: str) -> int:
    """Сколько мест занято в слоте: держащиеся, ждущие прослушивания и
    подтверждённые — все считаются занятыми."""
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    async with _db() as db:
        cur = await db.execute(
            f"""SELECT COUNT(*) FROM bookings
                WHERE date = ? AND time = ? AND level = ?
                  AND status IN ({placeholders})""",
            (date, time_hm, level, *ACTIVE_STATUSES),
        )
        return (await cur.fetchone())[0]


async def create_booking(client_id: int, date: str, time_hm: str, level: str, status: str) -> int:
    async with _db() as db:
        cur = await db.execute(
            """INSERT INTO bookings (client_id, date, time, level, status, created_at, confirmed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (client_id, date, time_hm, level, status, _now(),
             _now() if status == "confirmed" else None),
        )
        await db.commit()
        return cur.lastrowid


async def set_booking_status(booking_id: int, status: str) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE bookings SET status = ?, confirmed_at = ? WHERE id = ?",
            (status, _now() if status == "confirmed" else None, booking_id),
        )
        await db.commit()


async def get_booking(booking_id: int) -> dict | None:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT b.*, c.telegram_id, c.username, c.full_name,
                      c.claimed_level, c.confirmed_level,
                      c.voice_chat_id, c.voice_message_id
               FROM bookings b JOIN clients c ON c.id = b.client_id
               WHERE b.id = ?""",
            (booking_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_active_booking_of(telegram_id: int) -> dict | None:
    """Текущая (не отменённая) запись ученицы."""
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""SELECT b.*, c.telegram_id, c.username, c.full_name,
                       c.claimed_level, c.confirmed_level
                FROM bookings b JOIN clients c ON c.id = b.client_id
                WHERE c.telegram_id = ? AND b.status IN ({placeholders})
                ORDER BY b.created_at DESC LIMIT 1""",
            (telegram_id, *ACTIVE_STATUSES),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def cancel_bookings_of(telegram_id: int, statuses: tuple[str, ...] = ACTIVE_STATUSES) -> int:
    """Освободить слоты ученицы (например, при смене уровня)."""
    placeholders = ",".join("?" for _ in statuses)
    async with _db() as db:
        cur = await db.execute(
            f"""UPDATE bookings SET status = 'cancelled'
                WHERE status IN ({placeholders})
                  AND client_id IN (SELECT id FROM clients WHERE telegram_id = ?)""",
            (*statuses, telegram_id),
        )
        await db.commit()
        return cur.rowcount or 0


async def delete_booking(booking_id: int) -> None:
    async with _db() as db:
        await db.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
        await db.commit()


async def move_booking(booking_id: int, date: str, time_hm: str, level: str) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE bookings SET date = ?, time = ?, level = ? WHERE id = ?",
            (date, time_hm, level, booking_id),
        )
        await db.commit()


async def get_slot_roster(date: str, time_hm: str, level: str) -> list[dict]:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""SELECT b.id AS booking_id, b.status, b.date, b.time, b.level,
                       c.telegram_id, c.full_name, c.username,
                       c.claimed_level, c.confirmed_level
                FROM bookings b JOIN clients c ON c.id = b.client_id
                WHERE b.date = ? AND b.time = ? AND b.level = ?
                  AND b.status IN ({placeholders})
                ORDER BY b.created_at""",
            (date, time_hm, level, *ACTIVE_STATUSES),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_bookings_by_status(status: str) -> list[dict]:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT b.*, c.telegram_id, c.username, c.full_name,
                      c.claimed_level, c.confirmed_level,
                      c.voice_chat_id, c.voice_message_id
               FROM bookings b JOIN clients c ON c.id = b.client_id
               WHERE b.status = ?
               ORDER BY b.date, b.time, b.created_at""",
            (status,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_confirmed_from(date_iso: str) -> list[dict]:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT b.*, c.telegram_id, c.username, c.full_name
               FROM bookings b JOIN clients c ON c.id = b.client_id
               WHERE b.status = 'confirmed' AND b.date >= ?
               ORDER BY b.date, b.time""",
            (date_iso,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_stale_held(older_than_iso: str) -> list[dict]:
    """Слоты, которые держатся под голосовое слишком долго."""
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT b.*, c.telegram_id FROM bookings b
               JOIN clients c ON c.id = b.client_id
               WHERE b.status = 'held' AND b.created_at < ?""",
            (older_than_iso,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def mark_reminder(booking_id: int, field: str) -> None:
    assert field in ("reminder_day_sent", "reminder_hour_sent")
    async with _db() as db:
        await db.execute(f"UPDATE bookings SET {field} = 1 WHERE id = ?", (booking_id,))
        await db.commit()


async def wipe_all_data() -> dict:
    """Очистить заявки и записи. Расписание, тексты и настройки остаются."""
    async with _db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM bookings")
        bookings = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM clients")
        clients = (await cur.fetchone())[0]
        await db.execute("DELETE FROM bookings")
        await db.execute("DELETE FROM clients")
        await db.commit()
        return {"bookings": bookings, "clients": clients}
