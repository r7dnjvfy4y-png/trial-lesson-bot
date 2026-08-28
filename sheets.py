"""
Автоматическая выгрузка в Google Таблицу.

Бот сам, раз в несколько минут, переписывает таблицу актуальными данными —
никаких кнопок нажимать не нужно, просто открываете таблицу и видите
общую картину.

Три листа:
- «Заявки»   — все ученицы: уровень, время, статус, контакт
- «Слоты»    — расписание с заполненностью и списком записанных
- «Без записи» — кто зашёл в бота, но не записался

Настройка (один раз) описана в README, раздел «Google Таблица».
Если доступ не настроен, бот просто работает без таблицы.
"""

import asyncio
import json
import logging

import config
import database as db
import scheduling

log = logging.getLogger(__name__)

STATUS_RU = {
    "held": "⏳ ждём голосовое",
    "review": "🎧 ждёт прослушивания",
    "confirmed": "✅ подтверждена",
    "cancelled": "⚪️ отменена",
}

_client = None
_unavailable_reason = ""


def _get_client():
    """Ленивая авторизация в Google по сервисному аккаунту."""
    global _client, _unavailable_reason
    if _client is not None:
        return _client
    if not config.GOOGLE_CREDENTIALS_JSON or not config.GOOGLE_SHEET_ID:
        _unavailable_reason = "не заданы GOOGLE_CREDENTIALS_JSON или GOOGLE_SHEET_ID"
        return None

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        info = json.loads(config.GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive.file",
            ],
        )
        _client = gspread.authorize(creds)
        return _client
    except Exception as exc:
        _unavailable_reason = str(exc)
        log.exception("Не удалось авторизоваться в Google Sheets")
        return None


def _write_sheet(rows_bookings, rows_slots, rows_idle) -> str:
    """Синхронная часть: пишем три листа. Возвращает ссылку на таблицу."""
    client = _get_client()
    if client is None:
        raise RuntimeError(_unavailable_reason or "Google Sheets недоступен")

    sh = client.open_by_key(config.GOOGLE_SHEET_ID)

    def put(title: str, values: list[list]):
        try:
            ws = sh.worksheet(title)
        except Exception:
            ws = sh.add_worksheet(
                title=title, rows=max(len(values) + 10, 50), cols=max(len(values[0]), 10)
            )
        ws.clear()
        ws.update(values, "A1")
        try:
            ws.freeze(rows=1)
        except Exception:
            pass

    put("Заявки", rows_bookings)
    put("Слоты", rows_slots)
    put("Без записи", rows_idle)
    return sh.url


async def build_rows():
    """Готовим данные для всех трёх листов."""
    bookings = await db.get_all_bookings_full()
    rows_bookings = [[
        "Имя", "Telegram", "Заявленный уровень", "Уровень после прослушивания",
        "Дата", "Время", "Статус", "Записалась", "Подтверждена",
    ]]
    for b in bookings:
        rows_bookings.append([
            b["full_name"] or "",
            f"@{b['username']}" if b["username"] else str(b["telegram_id"]),
            b["claimed_level"] or "",
            b["confirmed_level"] or "",
            b["date"],
            b["time"],
            STATUS_RU.get(b["status"], b["status"]),
            (b["created_at"] or "")[:16].replace("T", " "),
            (b["confirmed_at"] or "")[:16].replace("T", " "),
        ])

    slots = await scheduling.upcoming_slots()
    rows_slots = [["Дата", "День", "Время", "Уровень", "Занято", "Мест всего", "Свободно", "Кто записан"]]
    for s in slots:
        roster = await db.get_slot_roster(s["date"], s["time"], s["level"])
        names = ", ".join(
            f"{r['full_name']}{'' if r['status'] == 'confirmed' else ' (не подтв.)'}"
            for r in roster
        )
        weekday = scheduling.WEEKDAY_FULL[
            scheduling.datetime.strptime(s["date"], "%Y-%m-%d").weekday()
        ]
        rows_slots.append([
            s["date"], weekday, s["time"], s["level"],
            s["taken"], s["capacity"], s["free"], names,
        ])

    idle = await db.get_clients_without_booking()
    rows_idle = [["Имя", "Telegram", "Заявленный уровень", "Статус", "Последняя активность"]]
    for c in idle:
        rows_idle.append([
            c["full_name"] or "",
            f"@{c['username']}" if c["username"] else str(c["telegram_id"]),
            c["claimed_level"] or "",
            c["status"] or "",
            (c["last_activity"] or "")[:16].replace("T", " "),
        ])

    return rows_bookings, rows_slots, rows_idle


async def sync() -> str:
    """Обновить таблицу. Возвращает ссылку на неё."""
    rows = await build_rows()
    return await asyncio.to_thread(_write_sheet, *rows)


async def safe_sync() -> None:
    """Фоновая синхронизация — ошибки не должны ронять бота."""
    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_JSON:
        return
    try:
        await sync()
    except Exception:
        log.exception("Не удалось обновить Google Таблицу")
