"""Планировщик: напоминания перед сменой и еженедельный вопрос про смены."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import db
import domain
import keyboards as kb
import texts

log = logging.getLogger(__name__)


async def send_due_reminders(bot: Bot) -> None:
    moment = domain.now()
    for shift in db.due_reminders(moment):
        start = domain.shift_start(shift.work_date, shift.shift_num)
        hours = max(1, round((start - moment).total_seconds() / 3600))
        s = shift.kind
        text = (
            f"⏰ Через ~{hours} ч. смена:\n"
            f"<b>{s.title}</b> {s.hours_label} · {domain.fmt_date(shift.work_date)}\n\n"
            f"{texts.good_shift()}"
        )
        try:
            await bot.send_message(shift.user_id, text, reply_markup=kb.reminder(shift.id))
        except TelegramForbiddenError:
            log.warning("Пользователь %s заблокировал бота", shift.user_id)
        except Exception:  # не даём одному сбою остановить рассылку
            log.exception("Не смог отправить напоминание по смене %s", shift.id)
            continue
        db.mark_reminded(shift.id)


async def ask_shift_confirmations(bot: Bot) -> None:
    """После окончания смены спрашиваем, сколько часов зачли."""
    moment = domain.now()
    for shift in db.shifts_awaiting_ask(moment):
        text = (
            f"{texts.CONFIRM_QUESTION}\n"
            f"<b>{domain.shift_line(shift.work_date, shift.shift_num)}</b>\n\n"
            "<i>Если отпустили раньше — отметь фактические часы, посчитаю деньги по факту.</i>"
        )
        try:
            await bot.send_message(shift.user_id, text, reply_markup=kb.confirm(shift.id))
        except TelegramForbiddenError:
            log.warning("Пользователь %s заблокировал бота", shift.user_id)
        except Exception:
            log.exception("Не смог спросить про смену %s", shift.id)
            continue
        db.mark_confirm_asked(shift.id)


async def ask_about_week(bot: Bot) -> None:
    for user_id in db.users_to_ask():
        try:
            await bot.send_message(user_id, texts.WEEK_QUESTION, reply_markup=kb.week_choice())
        except TelegramForbiddenError:
            db.set_weekly_ask(user_id, False)
        except Exception:
            log.exception("Не смог спросить пользователя %s про смены", user_id)


def setup(bot: Bot) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=config.TZ)
    sched.add_job(
        send_due_reminders,
        "interval",
        minutes=config.REMINDER_TICK_MINUTES,
        args=[bot],
        id="reminders",
        next_run_time=domain.now(),
        coalesce=True,
        max_instances=1,
    )
    sched.add_job(
        ask_shift_confirmations,
        "interval",
        minutes=config.REMINDER_TICK_MINUTES,
        args=[bot],
        id="confirmations",
        next_run_time=domain.now(),
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
    return sched


async def shutdown(sched: AsyncIOScheduler) -> None:
    sched.shutdown(wait=False)
