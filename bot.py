"""
Telegram-бот для записи на бесплатный пробный урок — группами по уровням.

Логика:
  Шаг 1 — /start сразу показывает приветствие и кнопки уровня (A2/B1/B2)
  Шаг 2 — бот показывает свободные даты/время для этого уровня (эта неделя),
          с кнопкой «Посмотреть запись на следующей неделе» и кнопкой
          «Не нашли подходящее время» на каждом экране
  Шаг 3 — ученица нажимает на дату/время и сразу получает подтверждение —
          отдельного шага «оставьте контакт» нет: контакт — это её Telegram
          (username и Telegram-профиль)
  Если мест на выбранное время не осталось (набралось 6 человек) или в
  расписании вообще нет мест — бот предлагает записаться в лист ожидания.

Все тексты бота вынесены в texts.py и редактируются администратором прямо в
Telegram, командой /texts — без правки кода (см. README).

Оплаты в этом боте нет — пробный урок бесплатный.
"""

import asyncio
import csv
import io
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import database as db
import scheduling
import texts
from keyboards import (
    dates_kb,
    level_kb,
    materials_kb,
    stall_reasons_kb,
    waitlist_kb,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("trial_lesson_bot")

router = Router()

STALLED_STATUSES = ["choosing_date"]

STATUS_LABELS = {
    "new": "🆕 новый диалог",
    "choosing_date": "⏳ выбирает дату",
    "booked": "✅ записан(а)",
    "cancelled": "⚪️ передумал(а)",
    "thinking": "🟡 согласовывает",
    "waitlist_prompted": "🟠 предложили лист ожидания",
    "waitlist": "🟠 в листе ожидания",
}


class Booking(StatesGroup):
    waiting_date = State()


class EditingText(StatesGroup):
    waiting_new_text = State()


def _fmt_dt(date_iso: str, time_hm: str) -> str:
    d = datetime.strptime(date_iso, "%Y-%m-%d").date()
    return f"{scheduling.WEEKDAY_RU[d.weekday()]}, {d.day} {scheduling.MONTH_RU[d.month]} в {time_hm}"


def _is_admin(telegram_id: int) -> bool:
    return bool(config.ADMIN_CHAT_ID) and telegram_id == config.ADMIN_CHAT_ID


async def notify_admin(bot: Bot, text: str) -> None:
    if not config.ADMIN_CHAT_ID:
        return
    try:
        await bot.send_message(config.ADMIN_CHAT_ID, text)
    except Exception:
        log.exception("Не удалось отправить уведомление админу")


async def compute_available_pages(group: str) -> list[list[tuple[str, str]]]:
    """Свободные слоты для группы расписания, сгруппированные по неделям
    (страницам). Полностью занятые недели просто не попадают в список —
    так следующая страница естественным образом показывает следующую
    свободную неделю."""
    today = scheduling.now_local().date()
    by_week: dict[int, list[tuple[str, str]]] = {}
    for date_iso, time_hm in scheduling.candidate_date_times(group):
        if scheduling.is_in_the_past_with_notice(date_iso, time_hm):
            continue
        count = await db.count_booked(date_iso, time_hm)
        if count >= config.CAPACITY_PER_SLOT:
            continue
        d = datetime.strptime(date_iso, "%Y-%m-%d").date()
        week_idx = (d - today).days // 7
        by_week.setdefault(week_idx, []).append((date_iso, time_hm))
    return [by_week[w] for w in sorted(by_week.keys())]


async def send_date_page(message: Message, pages: list, page_index: int, is_first: bool) -> None:
    slots = pages[page_index]
    has_next = page_index + 1 < len(pages)
    text = await texts.get_text("choose_date_now" if is_first else "choose_date_next")
    await message.answer(text, reply_markup=dates_kb(slots, has_next, page_index + 1))


async def send_no_time_prompt(message: Message) -> None:
    text = await texts.get_text("no_time_found")
    await message.answer(text, reply_markup=waitlist_kb())


# ---------------------------------------------------------------- /start ---

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await db.get_or_create_client(message.from_user.id, message.from_user.username)
    await db.update_client(message.from_user.id, status="new", stall_prompted=0)

    text = await texts.get_text("welcome")
    await message.answer(text, reply_markup=level_kb(), disable_web_page_preview=True)


# ----------------------------------------------------------- шаг: уровень --

@router.callback_query(F.data.startswith("level:"))
async def chose_level(callback: CallbackQuery, state: FSMContext) -> None:
    level_display = callback.data.split(":", 1)[1]
    if level_display not in config.DISPLAY_LEVELS:
        await callback.answer()
        return
    group = config.LEVEL_TO_GROUP[level_display]

    await state.update_data(level_display=level_display, level_group=group)
    await db.update_client(
        callback.from_user.id, level=level_display, status="choosing_date", stall_prompted=0
    )

    pages = await compute_available_pages(group)
    if not pages:
        await db.update_client(callback.from_user.id, status="waitlist_prompted", stall_prompted=0)
        await send_no_time_prompt(callback.message)
        await callback.answer()
        return

    await state.set_state(Booking.waiting_date)
    await send_date_page(callback.message, pages, 0, is_first=True)
    await callback.answer()


# ---------------------------------------------------- шаг: следующая неделя --

@router.callback_query(Booking.waiting_date, F.data.startswith("week:"))
async def next_week(callback: CallbackQuery, state: FSMContext) -> None:
    idx = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    group = data.get("level_group")

    await db.touch_client(callback.from_user.id)

    pages = await compute_available_pages(group)
    if idx >= len(pages):
        await callback.answer("На эту неделю мест больше нет", show_alert=True)
        if pages:
            await send_date_page(callback.message, pages, len(pages) - 1, is_first=False)
        else:
            await db.update_client(callback.from_user.id, status="waitlist_prompted", stall_prompted=0)
            await send_no_time_prompt(callback.message)
        return

    await send_date_page(callback.message, pages, idx, is_first=False)
    await callback.answer()


# ---------------------------------------------------------- шаг: дата -----

@router.callback_query(Booking.waiting_date, F.data.startswith("date:"))
async def chose_date(callback: CallbackQuery, state: FSMContext) -> None:
    _, date_iso, time_hm = callback.data.split(":", 2)
    data = await state.get_data()
    group = data.get("level_group")
    level_display = data.get("level_display", "—")

    await db.touch_client(callback.from_user.id)

    count = await db.count_booked(date_iso, time_hm)
    full = count >= config.CAPACITY_PER_SLOT
    expired = scheduling.is_in_the_past_with_notice(date_iso, time_hm)

    if full or expired:
        pages = await compute_available_pages(group)
        await callback.answer("Это время уже заполнено", show_alert=True)
        if pages:
            await send_date_page(callback.message, pages, 0, is_first=True)
        else:
            await db.update_client(callback.from_user.id, status="waitlist_prompted", stall_prompted=0)
            await send_no_time_prompt(callback.message)
        return

    full_name = callback.from_user.full_name
    username = callback.from_user.username
    contact = f"@{username}" if username else f"id{callback.from_user.id}"

    await db.update_client(
        callback.from_user.id,
        full_name=full_name,
        contact=contact,
        status="booked",
        stall_prompted=0,
    )
    client = await db.get_client(callback.from_user.id)
    await db.create_booking(client["id"], date_iso, time_hm)
    await state.clear()

    when = _fmt_dt(date_iso, time_hm)
    confirm_text = await texts.get_text("booking_confirmed", when=when)

    if config.QUIZLET_LINK:
        button_text = await texts.get_text("materials_button")
        await callback.message.answer(
            confirm_text, reply_markup=materials_kb(config.QUIZLET_LINK, button_text)
        )
    else:
        await callback.message.answer(confirm_text)

    spots_left = config.CAPACITY_PER_SLOT - count - 1
    await notify_admin(
        callback.bot,
        (
            "🟢 Новая запись на пробный урок\n"
            f"Имя: {full_name}\n"
            f"Уровень: {level_display}\n"
            f"Когда: {when}\n"
            f"Осталось мест в группе: {spots_left}\n"
            f"Telegram: {contact} (id {callback.from_user.id})"
        ),
    )
    await callback.answer()


# --------------------------------------------- «не нашли время» / лист ожидания --

@router.callback_query(F.data == "no_time_found")
async def no_time_found(callback: CallbackQuery, state: FSMContext) -> None:
    await db.update_client(callback.from_user.id, status="waitlist_prompted", stall_prompted=0)
    await send_no_time_prompt(callback.message)
    await callback.answer()


@router.callback_query(F.data == "join_waitlist")
async def join_waitlist(callback: CallbackQuery, state: FSMContext) -> None:
    client = await db.get_client(callback.from_user.id)
    data = await state.get_data()
    level_display = data.get("level_display") or (client or {}).get("level") or "—"

    await db.add_waitlist(client["id"], f"уровень {level_display}")
    await db.update_client(callback.from_user.id, status="waitlist", stall_prompted=0)

    text = await texts.get_text("waitlist_confirmed")
    await callback.message.answer(text)

    await notify_admin(
        callback.bot,
        (
            "🟠 Лист ожидания\n"
            f"Имя: {client.get('full_name') or callback.from_user.full_name}\n"
            f"Уровень: {level_display}\n"
            f"Telegram: @{callback.from_user.username or '—'} (id {callback.from_user.id})"
        ),
    )
    await state.clear()
    await callback.answer()


# --------------------------------------------------- реакции "завис(ла)" ---

@router.callback_query(F.data.startswith("reason:"))
async def stall_reason(callback: CallbackQuery, state: FSMContext) -> None:
    reason = callback.data.split(":", 1)[1]
    client = await db.get_client(callback.from_user.id)
    name = (client or {}).get("full_name") or callback.from_user.full_name

    if reason == "changed_mind":
        await db.update_client(callback.from_user.id, status="cancelled", stall_prompted=1)
        text = await texts.get_text("reason_changed_mind")
        await callback.message.answer(text)
        await notify_admin(callback.bot, f"⚪️ {name} передумал(а) записываться на пробный урок.")
        await state.clear()

    elif reason == "no_time":
        await db.update_client(callback.from_user.id, status="waitlist_prompted", stall_prompted=1)
        await send_no_time_prompt(callback.message)

    elif reason == "asking_parent":
        await db.update_client(callback.from_user.id, status="thinking", stall_prompted=1)
        text = await texts.get_text("reason_asking_parent")
        await callback.message.answer(text)
        await notify_admin(callback.bot, f"🟡 {name} согласовывает время пробного урока (например, с родителями).")

    await callback.answer()


# --------------------------------------------------------- админ-команды ---
# Здесь и собираются все записи и контакты — подробнее в README, раздел
# "Где смотреть записи и контакты".

@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    today = scheduling.now_local().date().isoformat()
    rows = await db.get_today_bookings(today)
    if not rows:
        await message.answer("На сегодня записей нет.")
        return
    lines = [f"Записи на сегодня ({today}):"]
    for r in rows:
        lines.append(
            f"• {r['time']} ({r['level'] or '—'}) — {r['full_name']} ({r['contact']})"
        )
    await message.answer("\n".join(lines))


@router.message(Command("slots"))
async def cmd_slots(message: Message) -> None:
    """Заполненность ближайших слотов по каждой группе расписания."""
    if not _is_admin(message.from_user.id):
        return

    lines = ["Заполненность ближайших слотов:"]
    shown = 0
    for date_iso, time_hm, group in scheduling.all_candidate_date_times():
        if scheduling.is_in_the_past_with_notice(date_iso, time_hm):
            continue
        count = await db.count_booked(date_iso, time_hm)
        lines.append(
            f"{scheduling.format_date_label(date_iso)} {time_hm} ({group}) — "
            f"{count}/{config.CAPACITY_PER_SLOT}"
        )
        shown += 1
        if shown >= 12:
            break

    if shown == 0:
        await message.answer("В ближайшее время слотов по расписанию нет.")
        return
    await message.answer("\n".join(lines))


@router.message(Command("leads"))
async def cmd_leads(message: Message) -> None:
    """Список последних заявок и на каком они этапе — весь процесс на виду."""
    if not _is_admin(message.from_user.id):
        return

    clients = await db.get_recent_clients(limit=30)
    if not clients:
        await message.answer("Пока ни одной заявки.")
        return

    lines = ["Последние заявки (обновляется в реальном времени):"]
    for c in clients:
        label = STATUS_LABELS.get(c["status"], c["status"])
        line = f"\n{c['full_name'] or '—'} — {label}"
        if c.get("level"):
            line += f"\nУровень: {c['level']}"
        if c.get("booking_date"):
            line += f"\nЗаписан(а): {_fmt_dt(c['booking_date'], c['booking_time'])}"
        if c.get("contact"):
            line += f"\nКонтакт: {c['contact']}"
        line += f"\nTelegram: @{c['username'] or '—'}"
        lines.append(line)

    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    """Выгрузка всех заявок и контактов в CSV — можно открыть в Excel/Google Таблицах."""
    if not _is_admin(message.from_user.id):
        return

    rows = await db.get_all_clients_for_export()
    if not rows:
        await message.answer("Пока нечего выгружать.")
        return

    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "full_name", "level", "status", "contact", "username", "telegram_id",
            "booking_date", "booking_time", "created_at", "last_activity",
        ],
    )
    writer.writeheader()
    for r in rows:
        r = dict(r)
        r["status"] = STATUS_LABELS.get(r["status"], r["status"])
        writer.writerow(r)

    csv_bytes = buffer.getvalue().encode("utf-8-sig")  # BOM, чтобы Excel не ломал кириллицу
    document = BufferedInputFile(csv_bytes, filename="leads.csv")
    await message.answer_document(document, caption="Все заявки и контакты")


# --------------------------------------------------- редактирование текстов --
# /texts — самостоятельная правка всех сообщений бота прямо в Telegram, без кода.

@router.message(Command("texts"))
async def cmd_texts(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    overrides = await db.get_all_text_overrides()
    kb = InlineKeyboardBuilder()
    for key, meta in texts.TEXT_DEFS.items():
        mark = "✏️ " if key in overrides else ""
        kb.button(text=f"{mark}{meta['label']}", callback_data=f"edittext:{key}")
    kb.adjust(1)

    await message.answer(
        "Тексты бота — нажмите, чтобы посмотреть и изменить. ✏️ — уже отредактированный вами:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("edittext:"))
async def edittext_open(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return

    key = callback.data.split(":", 1)[1]
    if key not in texts.TEXT_DEFS:
        await callback.answer()
        return

    meta = texts.TEXT_DEFS[key]
    current = await texts.get_text(key)

    await state.set_state(EditingText.waiting_new_text)
    await state.update_data(text_key=key)

    hint = ""
    if meta["vars"]:
        placeholders = ", ".join(f"{{{v}}}" for v in meta["vars"])
        hint = f"\n\n⚠️ В тексте есть переменные — оставьте их как есть: {placeholders}"

    kb = InlineKeyboardBuilder()
    kb.button(text="♻️ Сбросить на стандартный текст", callback_data=f"resettext:{key}")

    await callback.message.answer(
        f"<b>{meta['label']}</b>\n\nТекущий текст:\n\n{current}{hint}\n\n"
        f"Чтобы изменить — пришлите новый текст следующим сообщением. Жирный/курсив можно "
        f"поставить прямо в Telegram: выделите текст в поле ввода и нажмите на панели "
        f"форматирования — бот сохранит форматирование как есть.",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("resettext:"))
async def edittext_reset(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return

    key = callback.data.split(":", 1)[1]
    await db.delete_text_override(key)
    await state.clear()
    default = texts.TEXT_DEFS.get(key, {}).get("default", "")
    await callback.answer("Возвращён стандартный текст", show_alert=True)
    await callback.message.answer(f"Готово, текст сброшен на стандартный:\n\n{default}")


@router.message(EditingText.waiting_new_text, F.text)
async def edittext_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return

    data = await state.get_data()
    key = data.get("text_key")
    if not key:
        await state.clear()
        return

    new_value = message.html_text  # сохраняет форматирование, применённое в Telegram
    await db.set_text_override(key, new_value)
    await state.clear()
    await message.answer("Готово, текст обновлён! ✅ Можно проверить, пройдя /start ещё раз.")


# ------------------------------------------------------ фоновая проверка ---

async def check_stalled_clients(bot: Bot) -> None:
    # last_activity хранится в UTC ISO, поэтому порог считаем тоже в UTC
    threshold_utc = (
        datetime.now(timezone.utc) - timedelta(minutes=config.STALL_TIMEOUT_MINUTES)
    ).isoformat()

    stalled = await db.get_stalled_clients(STALLED_STATUSES, threshold_utc)
    for client in stalled:
        try:
            text = await texts.get_text("stall_check_in")
            await bot.send_message(client["telegram_id"], text, reply_markup=stall_reasons_kb())
            await db.update_client(client["telegram_id"], stall_prompted=1)
        except Exception:
            log.exception("Не удалось написать клиенту %s", client["telegram_id"])


async def main() -> None:
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан — добавьте его в переменные окружения")

    await db.init_db()

    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_stalled_clients, "interval", minutes=5, args=[bot])
    scheduler.start()

    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
