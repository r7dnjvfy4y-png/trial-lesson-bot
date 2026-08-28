"""
Бот записи на пробные уроки.

Флоу ученицы:
  уровень → время (слот закрепляется) → голосовое на английском →
  «Анастасия послушает и вернётся с подтверждением»

Флоу преподавателя (в Telegram, команда /admin):
  приходит карточка + голосовое + кнопки «Всё верно» / «Изменить уровень».
  Подтвердила → ученице уходят ссылка и материалы её уровня, слот занят.
  Изменила уровень → старый слот освобождается, ученица выбирает время
  заново уже под новый уровень и сразу получает ссылку и материалы.

Напоминания: за день и за час до урока, оба со ссылкой.

Расписание, ссылка, материалы и все тексты редактируются в /admin — код
для этого трогать не нужно.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import admin
import config
import database as db
import scheduling
import sheets
import student
import texts

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("trial_lesson_bot")


async def send_reminders(bot: Bot) -> None:
    """За день и за час до урока — оба раза со ссылкой."""
    now = scheduling.now_local()

    for b in await db.get_confirmed_from(now.date().isoformat()):
        starts_at = scheduling.slot_dt(b["date"], b["time"])
        left = starts_at - now
        when = scheduling.format_when(b["date"], b["time"])
        link_url = await student.lesson_link_for(b["level"])
        link = student.as_link(link_url, "zoom")

        if not b["reminder_day_sent"] and timedelta(hours=12) < left <= timedelta(hours=26):
            try:
                await bot.send_message(
                    b["telegram_id"],
                    await texts.get_text("reminder_day", when=when, link=link, link_url=link_url),
                    disable_web_page_preview=True,
                )
                await db.mark_reminder(b["id"], "reminder_day_sent")
            except Exception:
                log.exception("Напоминание за день не ушло: %s", b["telegram_id"])

        if not b["reminder_hour_sent"] and timedelta(0) < left <= timedelta(hours=1, minutes=10):
            try:
                await bot.send_message(
                    b["telegram_id"],
                    await texts.get_text("reminder_hour", when=when, link=link, link_url=link_url),
                    disable_web_page_preview=True,
                )
                await db.mark_reminder(b["id"], "reminder_hour_sent")
            except Exception:
                log.exception("Напоминание за час не ушло: %s", b["telegram_id"])


async def release_stale_holds(bot: Bot) -> None:
    """Слот, который держится под голосовое дольше HOLD_MINUTES, освобождаем."""
    threshold = (
        datetime.now(timezone.utc) - timedelta(minutes=config.HOLD_MINUTES)
    ).isoformat()
    for b in await db.get_stale_held(threshold):
        await db.set_booking_status(b["id"], "cancelled")
        await db.update_client(b["telegram_id"], status="hold_expired")
        try:
            await bot.send_message(b["telegram_id"], await texts.get_text("hold_expired"))
        except Exception:
            log.exception("Не удалось сообщить об освобождении слота: %s", b["telegram_id"])


async def nudge_silent(bot: Bot) -> None:
    """Одно мягкое напоминание тем, кто выбрал время, но молчит."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=config.VOICE_NUDGE_MINUTES)
    ).isoformat()
    for b in await db.get_stale_held(cutoff):
        client = await db.get_client(b["telegram_id"])
        if not client or client["status"] == "voice_nudged":
            continue
        try:
            await bot.send_message(b["telegram_id"], await texts.get_text("voice_reminder"))
            await db.update_client(b["telegram_id"], status="voice_nudged")
        except Exception:
            log.exception("Напоминание о голосовом не ушло: %s", b["telegram_id"])


async def main() -> None:
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан — добавьте его в переменные окружения")

    await db.init_db()

    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    admin.set_storage(storage)
    dp.include_router(admin.router)     # админка первой — её команды приоритетнее
    dp.include_router(student.router)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_reminders, "interval", minutes=5, args=[bot])
    scheduler.add_job(release_stale_holds, "interval", minutes=10, args=[bot])
    scheduler.add_job(nudge_silent, "interval", minutes=5, args=[bot])
    if config.GOOGLE_SHEET_ID and config.GOOGLE_CREDENTIALS_JSON:
        scheduler.add_job(sheets.safe_sync, "interval", minutes=config.SHEET_SYNC_MINUTES)
        log.info("Выгрузка в Google Таблицу включена, интервал %s мин", config.SHEET_SYNC_MINUTES)
    scheduler.start()

    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
