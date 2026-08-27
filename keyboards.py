from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import scheduling


def slot_key(slot: dict) -> str:
    return f"{slot['date']}|{slot['time']}|{slot['level']}"


def levels_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for lvl in config.LEVELS:
        kb.button(text=lvl, callback_data=f"lvl:{lvl}")
    kb.adjust(len(config.LEVELS))
    return kb.as_markup()


def slots_kb(
    slots: list[dict],
    page: int,
    has_next: bool,
    no_time_text: str,
    prefix: str = "sl",
) -> InlineKeyboardMarkup:
    """prefix: 'sl' — обычная запись, 'rs' — выбор времени после смены уровня."""
    kb = InlineKeyboardBuilder()
    for s in slots:
        kb.button(
            text=f"{scheduling.format_date(s['date'])} · {s['time']}",
            callback_data=f"{prefix}:{slot_key(s)}",
        )
    kb.adjust(1)
    if has_next:
        kb.row(InlineKeyboardButton(
            text="📅 Посмотреть следующую неделю",
            callback_data=f"{prefix}w:{page + 1}",
        ))
    kb.row(InlineKeyboardButton(text=no_time_text, callback_data="notime"))
    return kb.as_markup()


def materials_kb(url: str, text: str = "📚 Материалы к уроку") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=text, url=url))
    return kb.as_markup()
