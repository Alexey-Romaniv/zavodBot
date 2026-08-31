"""Планировщик: всё, что бот присылает сам.

Что и когда:
  • напоминание о смене — за 2 часа, а если это ночь (1я смена в 06:00) — вечером накануне;
  • «пора выходить» — за 40 минут до начала, единственное, что пробивает тихие часы;
  • «сколько часов зачли» — через 15 минут после смены, для ночной — днём;
  • «какие смены на этой неделе» — по субботам;
  • «пора взять смены на следующую неделю» — по понедельникам;
  • пинг про неподтверждённые смены — каждый вечер;
  • итоги закрытого периода — вечером 1-го числа, напоминание о выплате — в её день.

Тихие часы и /mute проверяются здесь, перед отправкой: если сейчас молчим,
напоминание не помечается отправленным и уйдёт, когда станет можно.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import db
import domain
import keyboards as kb
import notify
import reports
import texts

log = logging.getLogger(__name__)


async def _send(bot: Bot, user_id: int, text: str, markup=None, *, what: str = "сообщение") -> bool:
    """Отправить одному человеку. Сбой у одного не должен останавливать рассылку."""
    try:
        await bot.send_message(user_id, text, reply_markup=markup)
    except TelegramForbiddenError:
        log.warning("Пользователь %s заблокировал бота", user_id)
        return False
    except Exception:
        log.exception("Не смог отправить %s пользователю %s", what, user_id)
        return False
    return True


# --- Смены ----------------------------------------------------------------

async def send_due_reminders(bot: Bot) -> None:
    moment = domain.now()
    users = db.all_prefs()
    for shift in db.due_reminders(moment, users):
        prefs = users.get(shift.user_id) or notify.Prefs(user_id=shift.user_id)
        if notify.silent(prefs, moment):
            continue  # не помечаем отправленным — придёт, когда закончится тишина
        start = domain.shift_start(shift.work_date, shift.shift_num)
        s = shift.kind
        text = (
            f"⏰ {texts.shift_when(moment, start)} смена:\n"
            f"<b>{s.title}</b> {s.hours_label} · {domain.fmt_date(shift.work_date)}\n\n"
            f"{texts.good_shift()}"
        )
        if await _send(bot, shift.user_id, text, kb.reminder(shift.id), what="напоминание"):
            db.mark_reminded(shift.id)


async def send_leave_reminders(bot: Bot) -> None:
    """Короткое «пора выходить». Тихие часы и /mute его не глушат:
    в это время человек уже собирается на смену, и промолчать хуже, чем написать."""
    moment = domain.now()
    users = db.all_prefs()
    for shift in db.due_leave_reminders(moment):
        prefs = users.get(shift.user_id) or notify.Prefs(user_id=shift.user_id)
        if not prefs.leave_ping:
            db.mark_leave_reminded(shift.id)  # выключено — и переспрашивать незачем
            continue
        start = domain.shift_start(shift.work_date, shift.shift_num)
        minutes = max(1, round((start - moment).total_seconds() / 60))
        s = shift.kind
        text = (
            f"🚪 Пора выходить — начало через {minutes} мин.\n"
            f"<b>{s.title}</b> {s.hours_label} · {domain.fmt_date(shift.work_date)}"
        )
        if await _send(bot, shift.user_id, text, kb.reminder(shift.id), what="«пора выходить»"):
            db.mark_leave_reminded(shift.id)


async def ask_shift_confirmations(bot: Bot) -> None:
    """После окончания смены спрашиваем, сколько часов зачли."""
    moment = domain.now()
    users = db.all_prefs()
    for shift in db.shifts_awaiting_ask(moment, users):
        prefs = users.get(shift.user_id) or notify.Prefs(user_id=shift.user_id)
        if notify.muted(prefs, moment):
            continue  # спросим после /mute: без ответа деньги считаются по 8 ч
        text = (
            f"{texts.CONFIRM_QUESTION}\n"
            f"<b>{domain.shift_line(shift.work_date, shift.shift_num)}</b>\n\n"
            "<i>Если отпустили раньше — отметь фактические часы, посчитаю деньги по факту.</i>"
        )
        if await _send(bot, shift.user_id, text, kb.confirm(shift.id), what="вопрос о смене"):
            db.mark_confirm_asked(shift.id)


# --- Планирование недели --------------------------------------------------

async def ask_about_week(bot: Bot) -> None:
    moment = domain.now()
    users = db.all_prefs()
    for user_id in db.users_with("weekly_ask"):
        prefs = users.get(user_id) or notify.Prefs(user_id=user_id)
        if notify.muted(prefs, moment):
            continue
        try:
            await bot.send_message(user_id, texts.WEEK_QUESTION, reply_markup=kb.week_choice())
        except TelegramForbiddenError:
            db.set_weekly_ask(user_id, False)
        except Exception:
            log.exception("Не смог спросить пользователя %s про смены", user_id)


async def remind_monday_plan(bot: Bot) -> None:
    """По понедельникам — напомнить записать смены на следующую неделю.

    Если на неё уже что-то записано, человек её спланировал — молчим.
    """
    moment = domain.now()
    users = db.all_prefs()
    for user_id in db.users_with("monday_plan"):
        prefs = users.get(user_id) or notify.Prefs(user_id=user_id)
        if notify.muted(prefs, moment):
            continue
        text = reports.monday_plan_message(user_id)
        if text is None:
            continue
        await _send(bot, user_id, text, kb.week_choice(), what="напоминание про неделю")


# --- Неподтверждённые смены -----------------------------------------------

async def ping_unconfirmed(bot: Bot) -> None:
    """Раз в день напоминаем про смены без подтверждения: пока их не уточнить,
    /money и /penalties считают по 8 ч и показывают не то, что будет в расчётке."""
    moment = domain.now()
    users = db.all_prefs()
    for user_id in db.users_with("confirm_ping"):
        prefs = users.get(user_id) or notify.Prefs(user_id=user_id)
        if notify.silent(prefs, moment):
            continue
        pending = db.ignored_shifts(user_id)
        if not pending:
            continue
        await _send(
            bot, user_id, reports.unconfirmed_ping(pending),
            kb.unconfirmed(pending[:reports.PING_LIMIT]),
            what="пинг про неподтверждённые смены",
        )


# --- Календарь периода ----------------------------------------------------

async def close_period(bot: Bot) -> None:
    """Вечером 1-го числа период закрыт — присылаем итог и дату выплаты."""
    moment = domain.now()
    year, month = domain.period_anchor(moment.date())
    users = db.all_prefs()
    for user_id in db.users_with("period_news"):
        prefs = users.get(user_id) or notify.Prefs(user_id=user_id)
        if notify.muted(prefs, moment):
            continue
        text = reports.period_close_message(user_id, year, month)
        if text is None:
            continue
        await _send(bot, user_id, text, kb.money_nav(user_id, year, month), what="итоги периода")


async def remind_payout(bot: Bot) -> None:
    """В день выплаты — сколько должно прийти. Заодно предлагаем сверить с фактом."""
    moment = domain.now()
    anchor = domain.payout_anchor(moment.date())
    if anchor is None:
        return
    year, month = anchor
    users = db.all_prefs()
    for user_id in db.users_with("period_news"):
        prefs = users.get(user_id) or notify.Prefs(user_id=user_id)
        if notify.muted(prefs, moment):
            continue
        text = reports.payout_message(user_id, year, month)
        if text is None:
            continue
        await _send(bot, user_id, text, kb.payout_ask(year, month), what="напоминание о выплате")


def setup(bot: Bot) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=config.TZ)
    for job_id, fn in (
        ("reminders", send_due_reminders),
        ("leave_reminders", send_leave_reminders),
        ("confirmations", ask_shift_confirmations),
    ):
        sched.add_job(
            fn,
            "interval",
            minutes=config.REMINDER_TICK_MINUTES,
            args=[bot],
            id=job_id,
            next_run_time=domain.now() + timedelta(seconds=5),
            coalesce=True,
            max_instances=1,
        )
    sched.add_job(
        ask_about_week,
        "cron",
        day_of_week=config.WEEKLY_ASK_DOW,
        hour=config.WEEKLY_ASK_HOUR,
        minute=config.WEEKLY_ASK_MINUTE,
        args=[bot],
        id="weekly_ask",
        misfire_grace_time=3600,
    )
    sched.add_job(
        remind_monday_plan,
        "cron",
        day_of_week="mon",
        hour=config.MONDAY_PLAN_HOUR,
        args=[bot],
        id="monday_plan",
        misfire_grace_time=3600,
    )
    sched.add_job(
        ping_unconfirmed,
        "cron",
        hour=config.CONFIRM_PING_HOUR,
        args=[bot],
        id="confirm_ping",
        misfire_grace_time=3600,
    )
    sched.add_job(
        close_period,
        "cron",
        day=1,
        hour=config.PERIOD_CLOSE_HOUR,
        args=[bot],
        id="period_close",
        misfire_grace_time=3600,
    )
    sched.add_job(
        remind_payout,
        "cron",
        hour=config.PAYOUT_HOUR,
        args=[bot],
        id="payout",
        misfire_grace_time=3600,
    )
    return sched


async def shutdown(sched: AsyncIOScheduler) -> None:
    sched.shutdown(wait=False)
