"""
Сторона ученицы: выбор уровня → выбор времени → голосовое → ожидание
подтверждения от преподавателя → (после подтверждения) ссылка и материалы.
"""

import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import config
import database as db
import scheduling
import texts
from keyboards import levels_kb, materials_kb, slots_kb

log = logging.getLogger(__name__)
router = Router()


class Flow(StatesGroup):
    choosing_slot = State()      # выбирает время (первичная запись)
    waiting_voice = State()      # записывает голосовое
    rechoosing_slot = State()    # выбирает время заново после смены уровня


async def notify_admin(bot, text: str, reply_markup=None) -> None:
    if not config.ADMIN_CHAT_ID:
        return
    try:
        await bot.send_message(config.ADMIN_CHAT_ID, text, reply_markup=reply_markup)
    except Exception:
        log.exception("Не удалось написать администратору")


def review_kb(booking_id: int):
    """Кнопки под голосовым: подтвердить уровень или изменить его."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Всё верно", callback_data=f"rvok:{booking_id}")
    for lvl in config.LEVELS:
        kb.button(text=f"Изменить на {lvl}", callback_data=f"rvlvl:{booking_id}:{lvl}")
    kb.adjust(1, len(config.LEVELS))
    return kb.as_markup()


async def send_slots_page(
    message: Message,
    level: str,
    page: int,
    state: FSMContext,
    prefix: str = "sl",
    header: str | None = None,
    edit: bool = False,
) -> bool:
    """Показать страницу (неделю) свободных слотов. False — если слотов нет."""
    weeks = scheduling.group_by_week(await scheduling.free_slots(level))
    if not weeks:
        return False

    page = min(page, len(weeks) - 1)
    await state.update_data(level=level, page=page)

    if header is not None:
        text = header
    elif page == 0:
        text = await texts.get_text("choose_slot", level=level)
    else:
        text = await texts.get_text("choose_slot_next_week")

    kb = slots_kb(
        weeks[page],
        page=page,
        has_next=page + 1 < len(weeks),
        no_time_text=await texts.get_text("no_time_button"),
        prefix=prefix,
    )
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)
    return True


# ------------------------------------------------------------------ /start --

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await db.get_or_create_client(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    await db.update_client(message.from_user.id, status="new")
    await message.answer(
        await texts.get_text("welcome"),
        reply_markup=levels_kb(),
        disable_web_page_preview=True,
    )


# ----------------------------------------------------------- выбор уровня ---

@router.callback_query(F.data.startswith("lvl:"))
async def chose_level(callback: CallbackQuery, state: FSMContext) -> None:
    level = callback.data.split(":", 1)[1]
    if level not in config.LEVELS:
        await callback.answer()
        return

    await db.update_client(callback.from_user.id, claimed_level=level, status="choosing_slot")
    await state.set_state(Flow.choosing_slot)

    ok = await send_slots_page(callback.message, level, 0, state, edit=True)
    if not ok:
        await callback.message.edit_text(await texts.get_text("no_slots_for_level"))
        client = await db.get_client(callback.from_user.id)
        await notify_admin(
            callback.bot,
            f"⚠️ Нет свободных слотов уровня {level}\n"
            f"{client['full_name']} (@{client['username'] or '—'}) хотела записаться, "
            f"но свободного времени нет.",
        )
    await callback.answer()


# ------------------------------------------------- листание недель вперёд ---

@router.callback_query(F.data.startswith("slw:"))
async def next_week(callback: CallbackQuery, state: FSMContext) -> None:
    page = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    await send_slots_page(callback.message, data.get("level"), page, state, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("rsw:"))
async def next_week_rechoose(callback: CallbackQuery, state: FSMContext) -> None:
    page = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    await send_slots_page(
        callback.message, data.get("level"), page, state, prefix="rs", edit=True
    )
    await callback.answer()


# ------------------------------------------- выбор времени (первый заход) ---

@router.callback_query(Flow.choosing_slot, F.data.startswith("sl:"))
async def chose_slot(callback: CallbackQuery, state: FSMContext) -> None:
    date_iso, time_hm, level = callback.data.split(":", 1)[1].split("|")

    if not await scheduling.is_slot_available(date_iso, time_hm, level):
        await callback.answer("Это время уже заняли", show_alert=True)
        await send_slots_page(callback.message, level, 0, state, edit=True)
        return

    client = await db.get_client(callback.from_user.id)
    await db.cancel_bookings_of(callback.from_user.id, statuses=("held",))
    booking_id = await db.create_booking(client["id"], date_iso, time_hm, level, status="held")

    await db.update_client(callback.from_user.id, status="waiting_voice")
    await state.set_state(Flow.waiting_voice)
    await state.update_data(booking_id=booking_id)

    await callback.message.edit_text(
        await texts.get_text("voice_request", when=scheduling.format_when(date_iso, time_hm))
    )
    await callback.answer()


# ---------------------------------------------------------- голосовое -------

@router.message(Flow.waiting_voice, F.voice | F.audio | F.video_note)
async def got_voice(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    booking = await db.get_booking(data.get("booking_id"))

    if not booking or booking["status"] != "held":
        await state.clear()
        await message.answer(await texts.get_text("hold_expired"))
        return

    await db.set_booking_status(booking["id"], "review")
    await db.update_client(
        message.from_user.id,
        status="review",
        voice_chat_id=message.chat.id,
        voice_message_id=message.message_id,
    )
    await state.clear()

    await message.answer(await texts.get_text("voice_thanks"))

    # Преподавателю — карточка, само голосовое и кнопки решения
    if config.ADMIN_CHAT_ID:
        try:
            await message.forward(config.ADMIN_CHAT_ID)
            await message.bot.send_message(
                config.ADMIN_CHAT_ID,
                (
                    "🎧 Заявка на пробный — послушайте голосовое выше\n\n"
                    f"Имя: {booking['full_name']}\n"
                    f"Заявленный уровень: {booking['claimed_level']}\n"
                    f"Выбранное время: {scheduling.format_when(booking['date'], booking['time'])}\n"
                    f"Telegram: @{booking['username'] or '—'}"
                ),
                reply_markup=review_kb(booking["id"]),
            )
        except Exception:
            log.exception("Не удалось отправить заявку администратору")


@router.message(Flow.waiting_voice, ~F.text.startswith("/"))
async def voice_expected(message: Message) -> None:
    await message.answer(await texts.get_text("voice_nudge"))


# -------------------------------- выбор времени после смены уровня ---------

@router.callback_query(Flow.rechoosing_slot, F.data.startswith("rs:"))
async def chose_slot_after_change(callback: CallbackQuery, state: FSMContext) -> None:
    date_iso, time_hm, level = callback.data.split(":", 1)[1].split("|")

    if not await scheduling.is_slot_available(date_iso, time_hm, level):
        await callback.answer("Это время уже заняли", show_alert=True)
        await send_slots_page(callback.message, level, 0, state, prefix="rs", edit=True)
        return

    client = await db.get_client(callback.from_user.id)
    # Уровень уже проверен по голосовому — подтверждаем сразу
    booking_id = await db.create_booking(
        client["id"], date_iso, time_hm, level, status="confirmed"
    )
    await db.update_client(callback.from_user.id, status="confirmed", confirmed_level=level)
    await state.clear()

    await callback.message.edit_text(f"Готово — {scheduling.format_when(date_iso, time_hm)} ✅")
    await send_confirmation(callback.bot, callback.from_user.id, booking_id)
    await callback.answer()


# ----------------------------------------- «не подходит ни одно время» -----

@router.callback_query(F.data == "notime")
async def no_time_fits(callback: CallbackQuery, state: FSMContext) -> None:
    client = await db.get_client(callback.from_user.id)
    await db.update_client(callback.from_user.id, status="no_time")
    await state.clear()

    await callback.message.answer(await texts.get_text("no_time_fits"))
    await notify_admin(
        callback.bot,
        (
            "🙈 Не подошло ни одно время\n"
            f"Имя: {(client or {}).get('full_name') or callback.from_user.full_name}\n"
            f"Уровень (заявленный): {(client or {}).get('claimed_level') or '—'}\n"
            f"Telegram: @{callback.from_user.username or '—'}"
        ),
    )
    await callback.answer()


# ------------------------------------------------- отправка подтверждения --

async def lesson_link_for(level: str) -> str:
    """Ссылка на урок этого уровня — задаётся в /admin."""
    return await db.get_setting(config.link_key(level)) or "(ссылка ещё не задана)"


async def send_confirmation(bot, telegram_id: int, booking_id: int) -> None:
    """Ссылка + материалы уровня. Вызывается после решения преподавателя."""
    booking = await db.get_booking(booking_id)
    if not booking:
        return

    level = booking["level"]
    link = await lesson_link_for(level)
    materials = await db.get_setting(config.materials_key(level))

    text = await texts.get_text(
        texts.confirmed_key(level),
        when=scheduling.format_when(booking["date"], booking["time"]),
        link=link,
        materials=materials or "пришлю отдельно",
    )
    kb = materials_kb(materials) if materials.startswith("http") else None
    try:
        await bot.send_message(telegram_id, text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        log.exception("Не удалось отправить подтверждение %s", telegram_id)


# ------------------- запуск повторного выбора времени после смены уровня ---

async def start_rechoose(bot, telegram_id: int, level: str, state: FSMContext | None = None):
    """Отправить ученице слоты нового уровня. Состояние выставляется через
    storage, потому что вызов идёт из админского обработчика."""
    weeks = scheduling.group_by_week(await scheduling.free_slots(level))
    header = await texts.get_text("level_changed", level=level)

    if not weeks:
        await bot.send_message(telegram_id, header)
        await bot.send_message(telegram_id, await texts.get_text("no_slots_for_level"))
        return False

    from keyboards import slots_kb as _slots_kb

    kb = _slots_kb(
        weeks[0],
        page=0,
        has_next=len(weeks) > 1,
        no_time_text=await texts.get_text("no_time_button"),
        prefix="rs",
    )
    await bot.send_message(telegram_id, header, reply_markup=kb)
    return True
