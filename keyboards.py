from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from scheduling import format_date_label


def level_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for lvl in config.DISPLAY_LEVELS:
        kb.button(text=lvl, callback_data=f"level:{lvl}")
    kb.adjust(len(config.DISPLAY_LEVELS))
    return kb.as_markup()


def dates_kb(
    slots: list[tuple[str, str]], has_next_week: bool, next_page_index: int
) -> InlineKeyboardMarkup:
    """slots — список (date_iso, time_hm), уже отфильтрованных свободных
    мест для текущей "страницы" (недели)."""
    kb = InlineKeyboardBuilder()
    for date_iso, time_hm in slots:
        label = f"{format_date_label(date_iso)} · {time_hm}"
        kb.button(text=label, callback_data=f"date:{date_iso}:{time_hm}")
    kb.adjust(1)
    if has_next_week:
        kb.row(
            InlineKeyboardButton(
                text="📅 Посмотреть запись на следующей неделе",
                callback_data=f"week:{next_page_index}",
            )
        )
    kb.row(InlineKeyboardButton(text="🙈 Не нашли подходящее время", callback_data="no_time_found"))
    return kb.as_markup()


def waitlist_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Записаться в лист ожидания", callback_data="join_waitlist")
    return kb.as_markup()


def materials_kb(url: str, button_text: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=button_text, url=url))
    return kb.as_markup()


def stall_reasons_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Я передумал(а)", callback_data="reason:changed_mind")
    kb.button(text="Не нашёл(ла) время", callback_data="reason:no_time")
    kb.button(text="Согласовываю с родителями", callback_data="reason:asking_parent")
    kb.adjust(1)
    return kb.as_markup()
