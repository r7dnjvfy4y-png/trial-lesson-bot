"""
Слоты рассчитываются на лету из повторяющихся правил расписания (таблица
schedule_rules) — вручную ничего создавать не нужно. Разово отменённые
слоты берутся из slot_blocks.
"""

from datetime import datetime, timedelta  # noqa: F401 — datetime используется и в sheets.py
from zoneinfo import ZoneInfo

import config
import database as db

WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WEEKDAY_FULL = [
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
]
MONTH_RU = [
    "", "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
]


def tz() -> ZoneInfo:
    return ZoneInfo(config.TIMEZONE)


def now_local() -> datetime:
    return datetime.now(tz())


def slot_dt(date_iso: str, time_hm: str) -> datetime:
    return datetime.strptime(f"{date_iso} {time_hm}", "%Y-%m-%d %H:%M").replace(tzinfo=tz())


def format_date(date_iso: str) -> str:
    d = datetime.strptime(date_iso, "%Y-%m-%d").date()
    return f"{WEEKDAY_RU[d.weekday()]}, {d.day} {MONTH_RU[d.month]}"


def format_when(date_iso: str, time_hm: str) -> str:
    """Для сообщений ученицам: «30.08 в 10:00»."""
    d = datetime.strptime(date_iso, "%Y-%m-%d").date()
    return f"{d.day:02d}.{d.month:02d} в {time_hm}"


def format_when_admin(date_iso: str, time_hm: str) -> str:
    """Для админки: с днём недели — «Вс, 30 авг в 10:00»."""
    d = datetime.strptime(date_iso, "%Y-%m-%d").date()
    return f"{WEEKDAY_RU[d.weekday()]}, {d.day} {MONTH_RU[d.month]} в {time_hm}"


def is_too_late(date_iso: str, time_hm: str) -> bool:
    return slot_dt(date_iso, time_hm) < now_local() + timedelta(hours=config.MIN_NOTICE_HOURS)


async def upcoming_slots(level: str | None = None) -> list[dict]:
    """Все слоты на ближайшие DAYS_AHEAD дней: дата, время, уровень,
    вместимость и сколько мест уже занято."""
    rules = await db.get_rules()
    if level:
        rules = [r for r in rules if r["level"] == level]
    if not rules:
        return []

    blocks = await db.get_blocks()
    by_weekday: dict[int, list[dict]] = {}
    for r in rules:
        by_weekday.setdefault(r["weekday"], []).append(r)

    today = now_local().date()
    slots = []
    for offset in range(config.DAYS_AHEAD):
        day = today + timedelta(days=offset)
        for rule in sorted(by_weekday.get(day.weekday(), []), key=lambda x: x["time"]):
            date_iso = day.isoformat()
            if (date_iso, rule["time"], rule["level"]) in blocks:
                continue
            if is_too_late(date_iso, rule["time"]):
                continue
            taken = await db.count_taken(date_iso, rule["time"], rule["level"])
            slots.append({
                "date": date_iso,
                "time": rule["time"],
                "level": rule["level"],
                "capacity": rule["capacity"],
                "taken": taken,
                "free": max(rule["capacity"] - taken, 0),
            })
    return slots


async def free_slots(level: str) -> list[dict]:
    return [s for s in await upcoming_slots(level) if s["free"] > 0]


async def slot_capacity(date_iso: str, time_hm: str, level: str) -> int:
    """Вместимость конкретного слота по правилу расписания."""
    weekday = datetime.strptime(date_iso, "%Y-%m-%d").weekday()
    for r in await db.get_rules():
        if r["weekday"] == weekday and r["time"] == time_hm and r["level"] == level:
            return r["capacity"]
    return config.DEFAULT_CAPACITY


async def is_slot_available(date_iso: str, time_hm: str, level: str) -> bool:
    if is_too_late(date_iso, time_hm):
        return False
    if (date_iso, time_hm, level) in await db.get_blocks():
        return False
    capacity = await slot_capacity(date_iso, time_hm, level)
    return await db.count_taken(date_iso, time_hm, level) < capacity


def group_by_week(slots: list[dict]) -> list[list[dict]]:
    """Разбить слоты по неделям — для кнопки «на следующей неделе»."""
    today = now_local().date()
    weeks: dict[int, list[dict]] = {}
    for s in slots:
        d = datetime.strptime(s["date"], "%Y-%m-%d").date()
        weeks.setdefault((d - today).days // 7, []).append(s)
    return [weeks[k] for k in sorted(weeks)]
