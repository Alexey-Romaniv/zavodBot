"""Когда бот пишет и когда молчит.

Ничего не знает ни про Telegram, ни про базу: на входе настройки пользователя
(`Prefs`) и смена, на выходе — момент времени. Здесь же живут тихие часы,
из-за которых напоминание про утреннюю смену уезжает на вечер накануне.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import config
import domain


@dataclass(frozen=True)
class Prefs:
    """Настройки уведомлений одного пользователя. Значения по умолчанию — из config."""
    user_id: int = 0
    weekly_ask: bool = True       # вопрос «какие смены на этой неделе» по субботам
    monday_plan: bool = True      # понедельничное «пора взять смены на следующую неделю»
    confirm_ping: bool = True     # ежедневный пинг про неподтверждённые смены
    period_news: bool = True      # итоги закрытого периода и день выплаты
    leave_ping: bool = True       # короткое «пора выходить» перед сменой
    toxic: bool = True
    quiet_from: time = config.QUIET_FROM
    quiet_to: time = config.QUIET_TO
    evening_hour: int = config.EVENING_HOUR
    muted_until: datetime | None = None

    @property
    def quiet_off(self) -> bool:
        return self.quiet_from == self.quiet_to

    @property
    def quiet_label(self) -> str:
        if self.quiet_off:
            return "выключены"
        return f"{self.quiet_from:%H:%M}–{self.quiet_to:%H:%M}"


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=config.TZ)


# --- Тихие часы -----------------------------------------------------------

def in_quiet(prefs: Prefs, moment: datetime) -> bool:
    """Момент попадает в тихие часы? Окно обычно перекидывается через полночь."""
    if prefs.quiet_off:
        return False
    t = moment.time()
    if prefs.quiet_from < prefs.quiet_to:
        return prefs.quiet_from <= t < prefs.quiet_to
    return t >= prefs.quiet_from or t < prefs.quiet_to


def quiet_end(prefs: Prefs, moment: datetime) -> datetime:
    """Первый момент после тихих часов. Если тихо не сейчас — сам момент."""
    if not in_quiet(prefs, moment):
        return moment
    end = _at(moment.date(), prefs.quiet_to)
    if end <= moment:
        end += timedelta(days=1)
    return end


def awake_before(prefs: Prefs, moment: datetime) -> datetime:
    """Последний момент до `moment`, когда человек ещё не спит.

    Нужен как страховка: если и «за 2 часа», и вечер накануне попали в тихие часы,
    лучше предупредить перед их началом, чем не предупредить вовсе.
    """
    if not in_quiet(prefs, moment):
        return moment
    start = _at(moment.date(), prefs.quiet_from)
    if start > moment:
        start -= timedelta(days=1)
    return start - timedelta(minutes=1)


def muted(prefs: Prefs, moment: datetime) -> bool:
    return prefs.muted_until is not None and moment < prefs.muted_until


def silent(prefs: Prefs, moment: datetime) -> bool:
    """Бот сейчас молчит: либо тихие часы, либо включён /mute."""
    return muted(prefs, moment) or in_quiet(prefs, moment)


def mute_until(prefs: Prefs, moment: datetime) -> datetime:
    """До какого момента глушит /mute: до конца дня, но не меньше пары часов тишины.

    В 23:30 «до конца дня» — это полчаса, поэтому вечером глушим до утра.
    """
    end_of_day = _at(moment.date() + timedelta(days=1), time(0, 0))
    if end_of_day - moment >= timedelta(hours=3):
        return end_of_day
    return mute_until_morning(prefs, moment)


def mute_until_morning(prefs: Prefs, moment: datetime) -> datetime:
    """До конца тихих часов следующего утра (без тихих часов — до 8:00)."""
    wake = prefs.quiet_to if not prefs.quiet_off else time(8, 0)
    morning = _at(moment.date(), wake)
    if morning <= moment:
        morning += timedelta(days=1)
    return morning


# --- Времена по смене -----------------------------------------------------

def remind_at(prefs: Prefs, work_date: date, num: int) -> datetime:
    """Когда предупредить о смене.

    Обычно за `REMIND_BEFORE` до начала. Но для 1й смены это 04:00 — вместо
    ночного будильника предупреждаем вечером накануне.
    """
    start = domain.shift_start(work_date, num)
    base = start - config.REMIND_BEFORE
    if not in_quiet(prefs, base):
        return base
    evening = _at(base.date(), time(prefs.evening_hour % 24))
    if evening > base:
        evening -= timedelta(days=1)
    if not in_quiet(prefs, evening):
        return evening
    return awake_before(prefs, base)


def leave_at(work_date: date, num: int) -> datetime:
    """Короткое «пора выходить». Тихие часы и /mute его не глушат —
    в это время человек уже собирается на смену."""
    return domain.shift_start(work_date, num) - config.LEAVE_BEFORE


def confirm_at(prefs: Prefs, work_date: date, num: int) -> datetime:
    """Когда спросить, сколько часов зачли.

    После ночной смены человек идёт спать, а не считать деньги, — спрашиваем днём.
    """
    end = domain.shift_end(work_date, num)
    at = end + config.CONFIRM_AFTER
    if domain.shift(num).is_night:
        at = max(at, _at(end.date(), time(config.NIGHT_CONFIRM_HOUR % 24)))
    return quiet_end(prefs, at)
