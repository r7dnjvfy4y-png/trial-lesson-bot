"""
Генерация доступных дат/времени под расписание по уровням (config.WEEKLY_SCHEDULE).
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config

WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTH_RU = [
    "", "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
]


def _tz() -> ZoneInfo:
    return ZoneInfo(config.TIMEZONE)


def now_local() -> datetime:
    return datetime.now(_tz())


def format_date_label(date_iso: str) -> str:
    d = datetime.strptime(date_iso, "%Y-%m-%d").date()
    return f"{WEEKDAY_RU[d.weekday()]}, {d.day} {MONTH_RU[d.month]}"


def slots_for_weekday_and_level(weekday: int, level_name: str) -> list[str]:
    """Все времена в этот день недели, относящиеся к заданному уровню."""
    return [t for t, lvl in config.WEEKLY_SCHEDULE.get(weekday, []) if lvl == level_name]


def candidate_date_times(level_name: str) -> list[tuple[str, str]]:
    """Все (date_iso, time_hm) в пределах DAYS_AHEAD для уровня, в
    хронологическом порядке — без учёта занятости (это фильтруется отдельно
    в bot.py по данным из базы)."""
    today = now_local().date()
    result: list[tuple[str, str]] = []
    for i in range(config.DAYS_AHEAD):
        d = today + timedelta(days=i)
        for t in slots_for_weekday_and_level(d.weekday(), level_name):
            result.append((d.isoformat(), t))
    return result


def all_candidate_date_times() -> list[tuple[str, str, str]]:
    """Все (date_iso, time_hm, level) в пределах DAYS_AHEAD по всем уровням —
    используется для админского обзора заполненности слотов."""
    today = now_local().date()
    result: list[tuple[str, str, str]] = []
    for i in range(config.DAYS_AHEAD):
        d = today + timedelta(days=i)
        for t, lvl in config.WEEKLY_SCHEDULE.get(d.weekday(), []):
            result.append((d.isoformat(), t, lvl))
    return result


def is_in_the_past_with_notice(date_iso: str, time_hm: str) -> bool:
    """True, если слот наступает раньше, чем через MIN_NOTICE_HOURS от сейчас."""
    threshold = now_local() + timedelta(hours=config.MIN_NOTICE_HOURS)
    slot_dt = datetime.strptime(f"{date_iso} {time_hm}", "%Y-%m-%d %H:%M").replace(tzinfo=_tz())
    return slot_dt < threshold
