"""Доменная логика: смены, недели, расчётный период, деньги.

Ничего не знает ни про Telegram, ни про базу — только календарь и арифметика.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

import config

DAY = "day"
NIGHT = "night"


@dataclass(frozen=True)
class ShiftKind:
    num: int
    start: time
    end: time
    kind: str  # DAY | NIGHT
    title: str

    @property
    def is_night(self) -> bool:
        return self.kind == NIGHT

    @property
    def rate(self) -> Decimal:
        return config.NIGHT_RATE if self.is_night else config.DAY_RATE

    @property
    def pay(self) -> Decimal:
        return (self.rate * config.SHIFT_HOURS).quantize(Decimal("0.01"), ROUND_HALF_UP)

    @property
    def hours_label(self) -> str:
        return f"{self.start:%H:%M}–{self.end:%H:%M}"


SHIFTS: dict[int, ShiftKind] = {
    1: ShiftKind(1, time(6, 0), time(14, 0), DAY, "1я смена"),
    2: ShiftKind(2, time(14, 0), time(22, 0), DAY, "2я смена"),
    3: ShiftKind(3, time(22, 0), time(6, 0), NIGHT, "3я смена"),
}

# Дневные смены — с понедельника по пятницу; ночные — с воскресенья по четверг.
DAY_WEEKDAYS = (0, 1, 2, 3, 4)          # Пн..Пт
NIGHT_WEEKDAYS = (6, 0, 1, 2, 3)        # Вс, Пн..Чт

WEEKDAY_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
MONTHS_GEN = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
MONTHS_NOM = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)


def shift(num: int) -> ShiftKind:
    return SHIFTS[num]


def now() -> datetime:
    return datetime.now(config.TZ)


def today() -> date:
    return now().date()


# --- Границы смены ---------------------------------------------------------

def shift_start(work_date: date, num: int) -> datetime:
    """Начало смены с привязкой к часовому поясу. work_date — день выхода на смену."""
    return datetime.combine(work_date, SHIFTS[num].start, tzinfo=config.TZ)


def shift_end(work_date: date, num: int) -> datetime:
    s = SHIFTS[num]
    end_date = work_date + timedelta(days=1) if s.is_night else work_date
    return datetime.combine(end_date, s.end, tzinfo=config.TZ)


def is_allowed_weekday(work_date: date, num: int) -> bool:
    allowed = NIGHT_WEEKDAYS if SHIFTS[num].is_night else DAY_WEEKDAYS
    return work_date.weekday() in allowed


# --- Недели ---------------------------------------------------------------

def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def current_monday() -> date:
    """Понедельник текущей рабочей недели.

    В воскресенье неделя считается уже начавшейся: ночная смена с Вс на Пн
    относится к неделе, которая начинается на следующий день.
    """
    d = today()
    return d + timedelta(days=1) if d.weekday() == 6 else monday_of(d)


def planning_monday() -> date:
    """Неделя, которую логично планировать сейчас.

    В субботу и воскресенье текущая календарная неделя уже прошла (ночная смена
    с Вс на Пн относится к следующей), поэтому по умолчанию планируем следующую.
    """
    d = today()
    base = monday_of(d)
    return base + timedelta(days=7) if d.weekday() >= 5 else base


def week_days(monday: date, num: int) -> list[date]:
    """Дни, в которые возможна смена num, для недели, начинающейся с monday."""
    if SHIFTS[num].is_night:
        return [monday - timedelta(days=1)] + [monday + timedelta(days=i) for i in range(4)]
    return [monday + timedelta(days=i) for i in range(5)]


def week_label(monday: date) -> str:
    sunday = monday + timedelta(days=6)
    return f"{monday:%d.%m} – {sunday:%d.%m}"


# --- Расчётный период и выплата -------------------------------------------

def period_anchor(d: date) -> tuple[int, int]:
    """(год, месяц) расчётного периода, в который попадает дата d.

    Период идёт со 2-го числа месяца по 1-е число следующего, поэтому 1-е число
    относится к периоду предыдущего месяца.
    """
    if d.day >= config.PERIOD_START_DAY:
        return d.year, d.month
    return (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)


def next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def period_bounds(year: int, month: int) -> tuple[date, date]:
    """Начало и конец периода (включительно) для месяца-якоря."""
    ny, nm = next_month(year, month)
    return date(year, month, config.PERIOD_START_DAY), date(ny, nm, 1)


def payout_date(year: int, month: int) -> date:
    """Дата выплаты за период месяца-якоря: 17-е следующего месяца,
    а если это выходной — ближайший рабочий день после."""
    ny, nm = next_month(year, month)
    d = date(ny, nm, config.PAYOUT_DAY)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def payout_anchor(d: date) -> tuple[int, int] | None:
    """Период, выплата за который приходится ровно на дату d (иначе None).

    Выплата может съехать с 17-го на 18-е или 19-е, поэтому проверяем перебором,
    а не арифметикой по числу месяца.
    """
    year, month = prev_month(d.year, d.month)
    for _ in range(2):
        if payout_date(year, month) == d:
            return year, month
        year, month = prev_month(year, month)
    return None


def period_title(year: int, month: int) -> str:
    return f"{MONTHS_NOM[month - 1]} {year}"


# --- Штрафы ---------------------------------------------------------------


@dataclass(frozen=True)
class PenaltyKind:
    """Вид нарушения из договора: сумма фиксирована, но её можно переопределить."""
    code: str
    icon: str
    title: str
    clause: str          # формулировка договора — чтобы было видно, за что именно
    default: str         # имя настройки в config с суммой по умолчанию

    @property
    def amount(self) -> Decimal:
        return getattr(config, self.default)

    @property
    def label(self) -> str:
        return f"{self.icon} {self.title}"


PENALTIES: dict[str, PenaltyKind] = {
    "absence": PenaltyKind(
        "absence", "🚫", "Прогул",
        "невыполнение поручения в данный день полностью или частично",
        "PENALTY_ABSENCE",
    ),
    "late": PenaltyKind(
        "late", "⏰", "Опоздание",
        "запоздание на смену",
        "PENALTY_LATE",
    ),
    "break": PenaltyKind(
        "break", "☕", "Долгий перерыв",
        "превышение времени перерыва",
        "PENALTY_LATE",
    ),
    "notice": PenaltyKind(
        "notice", "📵", "Не предупредил о доступности",
        f"несообщение об изменении заявленной доступности за {config.NOTICE_DAYS} дней",
        "PENALTY_ABSENCE",
    ),
    "other": PenaltyKind(
        "other", "📄", "Другое нарушение",
        "нарушение прочих условий договора",
        "PENALTY_ABSENCE",
    ),
}

# Порядок вывода в отчёте и на клавиатуре.
PENALTY_ORDER = ("absence", "late", "break", "notice", "other")

# Сколько штрафов помещаем кнопками удаления в отчёт за период.
PENALTY_BUTTONS = 8


def penalty(code: str) -> PenaltyKind:
    return PENALTIES[code]


def penalty_amount(code: str) -> Decimal:
    return PENALTIES[code].amount


def rate_cut_for(hours: Decimal) -> Decimal:
    """Сколько снимут за месяц без 100% присутствия: 0,50 zł с каждого часа."""
    return (config.RATE_CUT * Decimal(hours)).quantize(Decimal("0.01"), ROUND_HALF_UP)


def notice_deadline(work_date: date) -> date:
    """Последний день, когда об изменении доступности можно сообщить без штрафа."""
    return work_date - timedelta(days=config.NOTICE_DAYS)


def late_notice(work_date: date, told_on: date | None = None) -> bool:
    """Об изменении сообщили позже, чем за NOTICE_DAYS дней до смены?"""
    told_on = told_on or today()
    return told_on > notice_deadline(work_date)


# --- Деньги ---------------------------------------------------------------

def pay_for(num: int) -> Decimal:
    return SHIFTS[num].pay


def pay_for_hours(num: int, hours: Decimal) -> Decimal:
    """Оплата смены за фактически отработанные часы."""
    return (SHIFTS[num].rate * Decimal(hours)).quantize(Decimal("0.01"), ROUND_HALF_UP)


def fmt_hours(hours: Decimal) -> str:
    """8 -> '8 ч', 6.5 -> '6,5 ч'"""
    h = Decimal(hours).normalize()
    text = f"{h:f}".replace(".", ",")
    return f"{text} ч"


def hours_word(n: int) -> str:
    return plural(n, "час", "часа", "часов")


def money(value: Decimal) -> str:
    """1326.0 -> '1 326,00 zł'"""
    q = Decimal(value).quantize(Decimal("0.01"), ROUND_HALF_UP)
    whole, _, frac = f"{q:.2f}".partition(".")
    neg = whole.startswith("-")
    whole = whole.lstrip("-")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return ("-" if neg else "") + " ".join(groups) + f",{frac} {config.CURRENCY}"


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def shifts_word(n: int) -> str:
    return plural(n, "смена", "смены", "смен")


def penalties_word(n: int) -> str:
    return plural(n, "штраф", "штрафа", "штрафов")


def fmt_date(d: date) -> str:
    return f"{WEEKDAY_SHORT[d.weekday()]} {d:%d.%m}"


def fmt_date_long(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]} {d.year} ({WEEKDAY_SHORT[d.weekday()]})"


def shift_line(work_date: date, num: int) -> str:
    s = SHIFTS[num]
    return f"{fmt_date(work_date)} — {s.title} ({s.hours_label})"
