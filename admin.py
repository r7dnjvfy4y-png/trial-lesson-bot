"""
Админка внутри Telegram: прослушивание заявок, подтверждение уровня,
управление расписанием, ссылкой, материалами и всеми текстами.

Всё рассчитано на телефон — только кнопки и короткие сообщения.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
import scheduling
import sheets
import student
import texts

log = logging.getLogger(__name__)
router = Router()

# Хранилище FSM диспетчера — нужно, чтобы при смене уровня выставить ученице
# состояние «выбирает время заново». Устанавливается из bot.py при старте.
STORAGE = None


def set_storage(storage) -> None:
    global STORAGE
    STORAGE = storage

STATUS_RU = {
    "held": "⏳ ждём голосовое",
    "review": "🎧 ждёт прослушивания",
    "confirmed": "✅ подтверждена",
    "cancelled": "⚪️ отменена",
}


def is_admin(user_id: int) -> bool:
    return bool(config.ADMIN_CHAT_ID) and user_id == config.ADMIN_CHAT_ID


router.message.filter(lambda m: is_admin(m.from_user.id))


class AdminFlow(StatesGroup):
    rule_time = State()
    setting_value = State()
    text_value = State()


# ------------------------------------------------------------ главное меню --

def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎧 Ждут прослушивания", callback_data="a:review")
    kb.button(text="✅ Подтверждённые записи", callback_data="a:confirmed")
    kb.button(text="🗓 Расписание и слоты", callback_data="a:slots")
    kb.button(text="🔗 Ссылка и материалы", callback_data="a:settings")
    kb.button(text="✏️ Тексты бота", callback_data="a:texts")
    kb.button(text="📊 Google Таблица", callback_data="a:sheet")
    kb.adjust(1)
    return kb.as_markup()


def back_kb(target: str = "a:root"):
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=target)
    return kb.as_markup()


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Что открыть?", reply_markup=main_menu_kb())


@router.callback_query(F.data == "a:root")
async def go_root(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Что открыть?", reply_markup=main_menu_kb())
    await callback.answer()


# --------------------------------------------- заявки, ждущие прослушивания --

@router.callback_query(F.data == "a:review")
async def list_review(callback: CallbackQuery) -> None:
    rows = await db.get_bookings_by_status("review")
    if not rows:
        await callback.message.edit_text("Заявок на прослушивание нет.", reply_markup=back_kb())
        await callback.answer()
        return

    kb = InlineKeyboardBuilder()
    for b in rows:
        kb.button(
            text=f"{b['full_name']} · {b['claimed_level']} · {scheduling.format_date(b['date'])} {b['time']}",
            callback_data=f"rv:{b['id']}",
        )
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a:root"))
    await callback.message.edit_text(
        f"Ждут прослушивания: {len(rows)}\nНажмите на заявку, чтобы послушать и решить:",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rv:"))
async def open_review(callback: CallbackQuery) -> None:
    booking_id = int(callback.data.split(":", 1)[1])
    b = await db.get_booking(booking_id)
    if not b:
        await callback.answer("Заявки больше нет", show_alert=True)
        return

    # Пересылаем голосовое ещё раз, чтобы не искать его в переписке
    if b["voice_chat_id"] and b["voice_message_id"]:
        try:
            await callback.bot.forward_message(
                config.ADMIN_CHAT_ID, b["voice_chat_id"], b["voice_message_id"]
            )
        except Exception:
            log.exception("Не удалось переслать голосовое")

    await callback.message.answer(
        f"<b>{b['full_name']}</b>\n"
        f"Заявленный уровень: {b['claimed_level']}\n"
        f"Время: {scheduling.format_when_admin(b['date'], b['time'])}\n"
        f"Telegram: @{b['username'] or '—'}",
        reply_markup=student.review_kb(booking_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rvok:"))
async def review_confirm(callback: CallbackQuery) -> None:
    booking_id = int(callback.data.split(":", 1)[1])
    b = await db.get_booking(booking_id)
    if not b:
        await callback.answer("Заявки больше нет", show_alert=True)
        return

    await db.set_booking_status(booking_id, "confirmed")
    await db.update_client(b["telegram_id"], status="confirmed", confirmed_level=b["level"])
    await student.send_confirmation(callback.bot, b["telegram_id"], booking_id)

    await callback.message.edit_text(
        f"✅ Подтверждено: {b['full_name']} · {b['level']} · "
        f"{scheduling.format_when_admin(b['date'], b['time'])}\n"
        f"Ученице отправлены ссылка и материалы."
    )
    await callback.answer("Подтверждено")


@router.callback_query(F.data.startswith("rvlvl:"))
async def review_change_level(callback: CallbackQuery) -> None:
    _, booking_id_raw, level = callback.data.split(":", 2)
    booking_id = int(booking_id_raw)
    b = await db.get_booking(booking_id)
    if not b:
        await callback.answer("Заявки больше нет", show_alert=True)
        return

    # Старый слот освобождаем — ученица выберет время заново под новый уровень
    await db.set_booking_status(booking_id, "cancelled")
    await db.update_client(b["telegram_id"], status="rechoosing", confirmed_level=level)

    # Выставляем ученице состояние «выбирает время заново»
    if STORAGE is not None:
        ctx = FSMContext(
            storage=STORAGE,
            key=StorageKey(
                bot_id=callback.bot.id, chat_id=b["telegram_id"], user_id=b["telegram_id"]
            ),
        )
        await ctx.set_state(student.Flow.rechoosing_slot)
        await ctx.update_data(level=level, page=0)

    ok = await student.start_rechoose(callback.bot, b["telegram_id"], level)
    if not ok:
        await student.notify_admin(
            callback.bot,
            f"⚠️ У {b['full_name']} сменили уровень на {level}, но свободных слотов "
            f"этого уровня нет — нужно связаться лично.",
        )

    await callback.message.edit_text(
        f"🔀 Уровень изменён на {level}: {b['full_name']}\n"
        f"Слот {scheduling.format_when_admin(b['date'], b['time'])} освобождён, "
        f"ученице отправлены слоты уровня {level}."
    )
    await callback.answer("Уровень изменён")


# ----------------------------------------------- подтверждённые записи -----

@router.callback_query(F.data == "a:confirmed")
async def list_confirmed(callback: CallbackQuery) -> None:
    today = scheduling.now_local().date().isoformat()
    rows = await db.get_confirmed_from(today)
    if not rows:
        await callback.message.edit_text("Подтверждённых записей пока нет.", reply_markup=back_kb())
        await callback.answer()
        return

    lines = [f"Подтверждённые записи: {len(rows)}", ""]
    current = None
    for b in rows:
        head = f"{scheduling.format_when_admin(b['date'], b['time'])} · {b['level']}"
        if head != current:
            current = head
            lines.append(f"\n<b>{head}</b>")
        lines.append(f"• {b['full_name']} (@{b['username'] or '—'})")

    await callback.message.edit_text("\n".join(lines), reply_markup=back_kb())
    await callback.answer()


# ------------------------------------------------ расписание и слоты -------

@router.callback_query(F.data == "a:slots")
async def slots_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Правила расписания", callback_data="a:rules")
    kb.button(text="📅 Ближайшие слоты", callback_data="a:upcoming")
    kb.button(text="⬅️ Назад", callback_data="a:root")
    kb.adjust(1)
    await callback.message.edit_text(
        "Слоты создаются автоматически по правилам расписания "
        "(например «A2 — каждую субботу 13:00»).",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "a:rules")
async def list_rules(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    rules = await db.get_rules()
    kb = InlineKeyboardBuilder()
    for r in rules:
        kb.button(
            text=f"🗑 {scheduling.WEEKDAY_FULL[r['weekday']]} {r['time']} · {r['level']} · до {r['capacity']}",
            callback_data=f"rl_del:{r['id']}",
        )
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="➕ Добавить правило", callback_data="rl_add"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a:slots"))

    text = (
        "Правила расписания — нажмите, чтобы удалить:\n\n"
        "Каждое правило повторяется еженедельно, слоты появляются сами."
        if rules else "Правил пока нет. Добавьте первое:"
    )
    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("rl_del:"))
async def delete_rule(callback: CallbackQuery, state: FSMContext) -> None:
    await db.delete_rule(int(callback.data.split(":", 1)[1]))
    await callback.answer("Правило удалено")
    await list_rules(callback, state)


@router.callback_query(F.data == "rl_add")
async def add_rule_weekday(callback: CallbackQuery) -> None:
    kb = InlineKeyboardBuilder()
    for i, name in enumerate(scheduling.WEEKDAY_FULL):
        kb.button(text=name.capitalize(), callback_data=f"rl_wd:{i}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a:rules"))
    await callback.message.edit_text("В какой день недели?", reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("rl_wd:"))
async def add_rule_level(callback: CallbackQuery, state: FSMContext) -> None:
    weekday = int(callback.data.split(":", 1)[1])
    await state.update_data(rule_weekday=weekday)

    kb = InlineKeyboardBuilder()
    for lvl in config.LEVELS:
        kb.button(text=lvl, callback_data=f"rl_lv:{lvl}")
    kb.adjust(len(config.LEVELS))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="rl_add"))
    await callback.message.edit_text(
        f"{scheduling.WEEKDAY_FULL[weekday].capitalize()} — для какого уровня?",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rl_lv:"))
async def add_rule_time(callback: CallbackQuery, state: FSMContext) -> None:
    level = callback.data.split(":", 1)[1]
    await state.update_data(rule_level=level)
    await state.set_state(AdminFlow.rule_time)
    await callback.message.edit_text(
        f"Уровень {level}. Теперь пришлите время в формате <b>13:00</b>.\n\n"
        f"Можно сразу указать вместимость через пробел: <b>13:00 8</b> "
        f"(по умолчанию {config.DEFAULT_CAPACITY})."
    )
    await callback.answer()


@router.message(AdminFlow.rule_time, F.text)
async def add_rule_save(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").split()
    time_hm = parts[0] if parts else ""
    try:
        hh, mm = time_hm.split(":")
        hh, mm = int(hh), int(mm)
        assert 0 <= hh <= 23 and 0 <= mm <= 59
        time_hm = f"{hh:02d}:{mm:02d}"
    except Exception:
        await message.answer("Не поняла время. Пришлите в формате 13:00")
        return

    capacity = config.DEFAULT_CAPACITY
    if len(parts) > 1 and parts[1].isdigit():
        capacity = max(1, int(parts[1]))

    data = await state.get_data()
    await db.add_rule(data["rule_weekday"], time_hm, data["rule_level"], capacity)
    await state.clear()

    await message.answer(
        f"Готово ✅ Теперь каждый(ую) {scheduling.WEEKDAY_FULL[data['rule_weekday']]} "
        f"в {time_hm} открыт слот для {data['rule_level']} (до {capacity} человек).",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "a:upcoming")
async def list_upcoming(callback: CallbackQuery) -> None:
    slots = await scheduling.upcoming_slots()
    if not slots:
        await callback.message.edit_text(
            "Ближайших слотов нет — проверьте правила расписания.",
            reply_markup=back_kb("a:slots"),
        )
        await callback.answer()
        return

    kb = InlineKeyboardBuilder()
    for s in slots[:24]:
        kb.button(
            text=f"{scheduling.format_date(s['date'])} {s['time']} · {s['level']} — {s['taken']}/{s['capacity']}",
            callback_data=f"sl_o:{s['date']}|{s['time']}|{s['level']}",
        )
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a:slots"))
    await callback.message.edit_text(
        "Ближайшие слоты. Нажмите, чтобы посмотреть, кто записан:",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sl_o:"))
async def open_slot(callback: CallbackQuery) -> None:
    date_iso, time_hm, level = callback.data.split(":", 1)[1].split("|")
    roster = await db.get_slot_roster(date_iso, time_hm, level)
    capacity = await scheduling.slot_capacity(date_iso, time_hm, level)

    kb = InlineKeyboardBuilder()
    for r in roster:
        kb.button(
            text=f"{STATUS_RU.get(r['status'], r['status'])[0]} {r['full_name']}",
            callback_data=f"bk:{r['booking_id']}",
        )
    kb.adjust(1)
    kb.row(InlineKeyboardButton(
        text="🚫 Отменить этот слот", callback_data=f"sl_bl:{date_iso}|{time_hm}|{level}"
    ))
    kb.row(InlineKeyboardButton(text="⬅️ К списку слотов", callback_data="a:upcoming"))

    body = "\n".join(
        f"• {r['full_name']} — {STATUS_RU.get(r['status'], r['status'])}" for r in roster
    ) or "Пока никто не записан."

    await callback.message.edit_text(
        f"<b>{scheduling.format_when_admin(date_iso, time_hm)} · {level}</b>\n"
        f"Занято {len(roster)}/{capacity}\n\n{body}\n\n"
        f"Нажмите на человека, чтобы удалить его запись.",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sl_bl:"))
async def block_slot(callback: CallbackQuery) -> None:
    date_iso, time_hm, level = callback.data.split(":", 1)[1].split("|")
    await db.block_slot(date_iso, time_hm, level)
    await callback.answer("Слот отменён")
    await callback.message.edit_text(
        f"🚫 Слот {scheduling.format_when_admin(date_iso, time_hm)} · {level} отменён — "
        f"в этот раз его не будет. Правило расписания при этом осталось.",
        reply_markup=back_kb("a:upcoming"),
    )


@router.callback_query(F.data.startswith("bk:"))
async def open_booking(callback: CallbackQuery) -> None:
    booking_id = int(callback.data.split(":", 1)[1])
    b = await db.get_booking(booking_id)
    if not b:
        await callback.answer("Записи больше нет", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Удалить запись (освободить место)", callback_data=f"bk_del:{booking_id}")
    kb.button(
        text="⬅️ Назад к слоту",
        callback_data=f"sl_o:{b['date']}|{b['time']}|{b['level']}",
    )
    kb.adjust(1)
    await callback.message.edit_text(
        f"<b>{b['full_name']}</b>\n"
        f"Уровень: {b['level']}\n"
        f"Время: {scheduling.format_when_admin(b['date'], b['time'])}\n"
        f"Статус: {STATUS_RU.get(b['status'], b['status'])}\n"
        f"Telegram: @{b['username'] or '—'}",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("bk_del:"))
async def delete_booking(callback: CallbackQuery) -> None:
    booking_id = int(callback.data.split(":", 1)[1])
    b = await db.get_booking(booking_id)
    if not b:
        await callback.answer("Записи больше нет", show_alert=True)
        return
    await db.delete_booking(booking_id)
    await callback.answer("Запись удалена")
    await callback.message.edit_text(
        f"🗑 Удалила запись: {b['full_name']} — "
        f"{scheduling.format_when_admin(b['date'], b['time'])}. Место освободилось.",
        reply_markup=back_kb("a:upcoming"),
    )


# --------------------------------------------- ссылка и материалы ----------

SETTING_LABELS = {
    **{config.link_key(lvl): f"🔗 Ссылка на урок {lvl}" for lvl in config.LEVELS},
    **{config.materials_key(lvl): f"📚 Материалы {lvl}" for lvl in config.LEVELS},
    **{config.photo_key(lvl): f"🖼 Фото к подтверждению {lvl}" for lvl in config.LEVELS},
}

# Настройки, для которых нужно прислать не текст, а картинку
PHOTO_SETTINGS = {config.photo_key(lvl) for lvl in config.LEVELS}


@router.callback_query(F.data == "a:settings")
async def settings_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    kb = InlineKeyboardBuilder()
    for key, label in SETTING_LABELS.items():
        value = await db.get_setting(key)
        mark = "✅" if value else "➖"
        kb.button(text=f"{mark} {label}", callback_data=f"st:{key}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a:root"))
    await callback.message.edit_text(
        "Ссылки на уроки и материалы — по каждому уровню своя. ✅ — уже заполнено:",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("st:"))
async def setting_open(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    value = await db.get_setting(key)
    await state.set_state(AdminFlow.setting_value)
    await state.update_data(setting_key=key)

    if key in PHOTO_SETTINGS:
        # Сначала показываем текущее фото, если оно уже загружено
        if value:
            try:
                await callback.message.answer_photo(value, caption="Сейчас стоит это фото")
            except Exception:
                log.exception("Не удалось показать текущее фото")
        await callback.message.answer(
            f"<b>{SETTING_LABELS.get(key, key)}</b>\n\n"
            f"Пришлите фото — оно будет приходить ученице вместе с подтверждением.\n\n"
            f"Чтобы убрать фото, отправьте слово: <code>удалить</code>"
        )
    else:
        await callback.message.edit_text(
            f"<b>{SETTING_LABELS.get(key, key)}</b>\n\n"
            f"Сейчас: {value or '— не задано —'}\n\n"
            f"Пришлите новое значение сообщением (обычно это ссылка).",
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.message(AdminFlow.setting_value, F.photo)
async def setting_save_photo(message: Message, state: FSMContext) -> None:
    """Фото сохраняем по его file_id — файл остаётся на серверах Telegram."""
    data = await state.get_data()
    key = data.get("setting_key")
    if key not in PHOTO_SETTINGS:
        await message.answer("Для этого пункта нужно прислать текст, а не фото.")
        return

    await db.set_setting(key, message.photo[-1].file_id)
    await state.clear()
    await message.answer(
        "Фото сохранено ✅ Теперь оно будет приходить вместе с подтверждением.",
        reply_markup=main_menu_kb(),
    )


@router.message(AdminFlow.setting_value, F.text)
async def setting_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    key = data["setting_key"]
    value = message.text.strip()

    if key in PHOTO_SETTINGS:
        if value.lower() in ("удалить", "убрать", "-"):
            await db.set_setting(key, "")
            await state.clear()
            await message.answer("Фото убрано ✅", reply_markup=main_menu_kb())
        else:
            await message.answer("Пришлите, пожалуйста, именно фото (или слово «удалить»).")
        return

    await db.set_setting(key, value)
    await state.clear()
    await message.answer("Сохранила ✅", reply_markup=main_menu_kb())


# ---------------------------------------------------- тексты сообщений -----

@router.callback_query(F.data == "a:texts")
async def texts_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    overrides = await db.get_all_text_overrides()
    kb = InlineKeyboardBuilder()
    for key, meta in texts.TEXT_DEFS.items():
        mark = "✏️ " if key in overrides else ""
        kb.button(text=f"{mark}{meta['label']}", callback_data=f"tx:{key}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a:root"))
    await callback.message.edit_text(
        "Тексты бота. ✏️ — уже изменённые вами:", reply_markup=kb.as_markup()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("tx:"))
async def text_open(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    if key not in texts.TEXT_DEFS:
        await callback.answer()
        return

    meta = texts.TEXT_DEFS[key]
    current = await texts.get_text(key)
    await state.set_state(AdminFlow.text_value)
    await state.update_data(text_key=key)

    hint = ""
    if meta["vars"]:
        placeholders = ", ".join("{" + v + "}" for v in meta["vars"])
        hint = f"\n\n⚠️ Оставьте в тексте переменные — бот подставит в них данные: {placeholders}"

    kb = InlineKeyboardBuilder()
    kb.button(text="♻️ Вернуть стандартный", callback_data=f"tx_rs:{key}")
    kb.button(text="⬅️ К списку текстов", callback_data="a:texts")
    kb.adjust(1)

    await callback.message.answer(
        f"<b>{meta['label']}</b>\n\nСейчас:\n\n{current}{hint}\n\n"
        f"Чтобы изменить — пришлите новый текст сообщением. Жирный и курсив можно "
        f"поставить прямо в Telegram: выделите текст и выберите форматирование.",
        reply_markup=kb.as_markup(),
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("tx_rs:"))
async def text_reset(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    await db.delete_text_override(key)
    await state.clear()
    await callback.answer("Вернула стандартный текст", show_alert=True)
    await callback.message.answer(
        f"Готово, текст сброшен:\n\n{texts.TEXT_DEFS[key]['default']}",
        reply_markup=main_menu_kb(),
    )


@router.message(AdminFlow.text_value, F.text)
async def text_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await db.set_text_override(data["text_key"], message.html_text)
    await state.clear()
    await message.answer("Текст обновлён ✅", reply_markup=main_menu_kb())


# -------------------------------------------------------- Google Таблица ---

@router.callback_query(F.data == "a:sheet")
async def sheet_menu(callback: CallbackQuery) -> None:
    if not (config.GOOGLE_SHEET_ID and config.GOOGLE_CREDENTIALS_JSON):
        await callback.message.edit_text(
            "Выгрузка в Google Таблицу пока не подключена.\n\n"
            "Нужно один раз добавить на Railway две переменные: GOOGLE_SHEET_ID и "
            "GOOGLE_CREDENTIALS_JSON — как это сделать, описано в README, раздел "
            "«Google Таблица».",
            reply_markup=back_kb(),
        )
        await callback.answer()
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить сейчас", callback_data="sheet_sync")
    kb.button(text="⬅️ Назад", callback_data="a:root")
    kb.adjust(1)
    await callback.message.edit_text(
        f"Таблица обновляется сама каждые {config.SHEET_SYNC_MINUTES} мин.\n\n"
        f"Листы: «Заявки», «Слоты», «Без записи».\n"
        f"https://docs.google.com/spreadsheets/d/{config.GOOGLE_SHEET_ID}/edit",
        reply_markup=kb.as_markup(),
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data == "sheet_sync")
async def sheet_sync(callback: CallbackQuery) -> None:
    await callback.answer("Обновляю…")
    try:
        url = await sheets.sync()
        await callback.message.edit_text(
            f"Таблица обновлена ✅\n{url}",
            reply_markup=back_kb(),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        await callback.message.edit_text(
            f"Не получилось обновить таблицу:\n\n{exc}\n\n"
            f"Чаще всего причина — таблица не расшарена на сервисный аккаунт "
            f"или неверный GOOGLE_SHEET_ID.",
            reply_markup=back_kb(),
        )


# ------------------------------------------------------ сервисные команды --

@router.message(Command("resetdata"))
async def cmd_resetdata(message: Message) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить все заявки", callback_data="wipe_yes")
    kb.button(text="Отмена", callback_data="a:root")
    kb.adjust(1)
    await message.answer(
        "Удалить все заявки и записи? Расписание, тексты и настройки останутся.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "wipe_yes")
async def wipe_yes(callback: CallbackQuery) -> None:
    stats = await db.wipe_all_data()
    await callback.message.edit_text(
        f"Готово ✅ Удалено записей: {stats['bookings']}, карточек: {stats['clients']}."
    )
    await callback.answer()
