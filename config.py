"""Настройки бота: читаются из окружения / .env, имеют разумные значения по умолчанию."""
from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = Path(os.getenv("SHIFTBOT_DB") or BASE_DIR / "shifts.db")

TZ = ZoneInfo(os.getenv("SHIFTBOT_TZ", "Europe/Warsaw"))
CURRENCY = os.getenv("SHIFTBOT_CURRENCY", "zł")

DAY_RATE = Decimal(os.getenv("SHIFTBOT_DAY_RATE", "33.15"))
NIGHT_RATE = Decimal(os.getenv("SHIFTBOT_NIGHT_RATE", "35.20"))
SHIFT_HOURS = Decimal(os.getenv("SHIFTBOT_SHIFT_HOURS", "8"))

# Ходить к Telegram только по IPv6. Нужно там, где IPv4-маршрут есть, но никуда не ведёт
# (например VM в GCP без внешнего IPv4): иначе каждая попытка соединения ждёт таймаута.
FORCE_IPV6 = os.getenv("SHIFTBOT_FORCE_IPV6", "0").strip().lower() in ("1", "true", "yes")

# За сколько часов до начала смены присылать напоминание.
REMIND_BEFORE = timedelta(hours=float(os.getenv("SHIFTBOT_REMIND_HOURS", "2")))
# Через сколько после конца смены спрашивать «как прошла».
CONFIRM_AFTER = timedelta(minutes=int(os.getenv("SHIFTBOT_CONFIRM_AFTER_MIN", "15")))

# Как часто планировщик проверяет, кому пора напомнить.
REMINDER_TICK_MINUTES = int(os.getenv("SHIFTBOT_TICK_MINUTES", "5"))

# Еженедельный вопрос «какие у тебя смены на этой неделе?»
WEEKLY_ASK_DOW = os.getenv("SHIFTBOT_ASK_DOW", "sat")  # день недели для cron
WEEKLY_ASK_HOUR = int(os.getenv("SHIFTBOT_ASK_HOUR", "18"))
WEEKLY_ASK_MINUTE = int(os.getenv("SHIFTBOT_ASK_MINUTE", "0"))

# --- Админка --------------------------------------------------------------
# Пароль для входа в /admin. Пусто — админка выключена совсем.
ADMIN_PASSWORD: str = os.getenv("SHIFTBOT_ADMIN_PASSWORD", "").strip()
# Вместо открытого пароля можно положить его sha256 — тогда на сервере пароля нет.
# Посчитать: python -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" мойпароль
ADMIN_PASSWORD_SHA256: str = os.getenv("SHIFTBOT_ADMIN_PASSWORD_SHA256", "").strip().lower()


def _user_ids(raw: str) -> frozenset[int]:
    parts = raw.replace(";", ",").replace(" ", ",").split(",")
    return frozenset(int(x) for x in parts if x.strip().lstrip("-").isdigit())


# Кому вообще разрешено пробовать войти. Пусто — любому, кто знает пароль.
ADMIN_IDS: frozenset[int] = _user_ids(os.getenv("SHIFTBOT_ADMIN_IDS", ""))

# Сколько живёт вход, прежде чем пароль спросят снова.
ADMIN_SESSION = timedelta(minutes=int(os.getenv("SHIFTBOT_ADMIN_TTL_MIN", "60")))
# Защита от перебора: сколько попыток подряд и на сколько блокируем после них.
ADMIN_MAX_ATTEMPTS = int(os.getenv("SHIFTBOT_ADMIN_ATTEMPTS", "5"))
ADMIN_LOCKOUT = timedelta(minutes=int(os.getenv("SHIFTBOT_ADMIN_LOCKOUT_MIN", "15")))

# Сколько пользователей показываем кнопками в списке админки.
ADMIN_USER_BUTTONS = 12
# Пауза между сообщениями рассылки, чтобы не упереться в лимиты Telegram.
BROADCAST_DELAY = float(os.getenv("SHIFTBOT_BROADCAST_DELAY", "0.06"))

# --- Штрафы по договору ---------------------------------------------------
# Суммы нетто. 370 zł — за невыполнение поручения в день (полностью или частично),
# за несообщение об изменении заявленной доступности и прочие нарушения условий.
PENALTY_ABSENCE = Decimal(os.getenv("SHIFTBOT_PENALTY_ABSENCE", "370"))
# 150 zł — за опоздание на смену и за превышение времени перерыва.
PENALTY_LATE = Decimal(os.getenv("SHIFTBOT_PENALTY_LATE", "150"))
# Снижение ставки (брутто за каждый отработанный час) в месяце, где не было
# 100% присутствия по графику при подтверждённой диспозиции и свободных местах.
RATE_CUT = Decimal(os.getenv("SHIFTBOT_RATE_CUT", "0.50"))
# За сколько дней нужно предупреждать об изменении заявленной доступности.
NOTICE_DAYS = int(os.getenv("SHIFTBOT_NOTICE_DAYS", "7"))

# Расчётный период: со 2-го числа месяца по 1-е число следующего.
PERIOD_START_DAY = 2
# Выплата 17-го числа следующего месяца (переносится на ближайший рабочий день).
PAYOUT_DAY = 17
