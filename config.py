"""
Настройки бота.

Здесь только то, что задаётся один раз при деплое (токен, часовой пояс,
пути). Всё остальное — расписание, ссылка на урок, материалы по уровням и
все тексты — редактируется прямо в Telegram через /admin и хранится в базе.
"""

import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or "0")

# Часовой пояс, в котором указано расписание уроков
TIMEZONE = os.getenv("TIMEZONE", "Europe/Madrid")

# Уровни, которые ученица выбирает при записи
LEVELS = ["A2", "B1", "B2"]

# Стартовое расписание — используется ТОЛЬКО при первом запуске, чтобы база
# не была пустой. Дальше правила добавляются и удаляются через /admin.
# Формат: (день недели, время, уровень); 0=Пн ... 5=Сб, 6=Вс
DEFAULT_RULES = [
    (5, "14:30", "A2"),
    (5, "15:30", "B2"),
    (5, "16:30", "B2"),
    (6, "10:00", "B1"),
    (6, "11:00", "B2"),
]

# Сколько человек помещается в один слот (для новых правил по умолчанию)
DEFAULT_CAPACITY = int(os.getenv("DEFAULT_CAPACITY", "6"))

# На сколько дней вперёд бот показывает слоты
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "28"))

# За сколько часов до урока запись ещё возможна
MIN_NOTICE_HOURS = int(os.getenv("MIN_NOTICE_HOURS", "3"))

# Сколько минут слот держится за ученицей, пока она записывает голосовое
HOLD_MINUTES = int(os.getenv("HOLD_MINUTES", "120"))

# Через сколько минут молчания бот мягко напомнит о голосовом
VOICE_NUDGE_MINUTES = int(os.getenv("VOICE_NUDGE_MINUTES", "20"))

DB_PATH = os.getenv("DB_PATH", "bot.db")

# --------------------------------------------------------- Google Таблица --
# ID таблицы (кусок ссылки между /d/ и /edit) и JSON-ключ сервисного аккаунта
# одной строкой. Если не заданы — бот просто работает без выгрузки.
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")

# Как часто обновлять таблицу, в минутах
SHEET_SYNC_MINUTES = int(os.getenv("SHEET_SYNC_MINUTES", "5"))

# Ключи настроек, которые редактируются через /admin:
# у каждого уровня своя ссылка на урок и свои материалы.


def link_key(level: str) -> str:
    return f"lesson_link_{level}"


def materials_key(level: str) -> str:
    return f"materials_{level}"


def photo_key(level: str) -> str:
    """Фото, которое бот прикладывает к подтверждению записи этого уровня."""
    return f"photo_{level}"
