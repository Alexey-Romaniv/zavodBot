"""Хендлеры Telegram: команды, выбор смен на неделю, отмена, деньги, ручной ввод."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import achievements
import config
import db
import domain
import keyboards as kb
import parsing
import reports
import texts

router = Router()


class Flow(StatesGroup):
    picking_days = State()
    manual_entry = State()
    custom_hours = State()
    penalty_entry = State()


# --- Команды --------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    db.ensure_user(message.from_user.id)
    await message.answer(
        "Привет! Я веду твой график смен и считаю зарплату.\n\n" + texts.HELP,
        reply_markup=kb.main_menu(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP, reply_markup=kb.main_menu())


# --- Достижения -----------------------------------------------------------

async def award(target: Message | CallbackQuery, user_id: int) -> None:
    """Пересчитать достижения после события и сообщить, если что-то изменилось.

    Вызывается там, где статистика реально могла сдвинуться: подтверждение смены,
    сохранение недели, ручной ввод, отмена.
    """
    fresh, lost, _ = achievements.sync(user_id)
    if not fresh and not lost:
        return
    chat = target if isinstance(target, Message) else target.message
    tone = reports.tone_for(user_id)

    if not db.ach_intro_shown(user_id):
        db.mark_ach_intro(user_id)
        if len(fresh) > 3:  # первый расчёт по накопленной истории — не заваливаем списком
            await chat.answer(reports.intro_message(len(fresh), tone))
            fresh = []

    if fresh:
        await chat.answer(reports.unlock_message(fresh, tone))
    if lost:
        await chat.answer(reports.lost_message(lost, tone))


@router.message(Command("achievements"))
@router.message(F.text == kb.BTN_ACH)
async def cmd_achievements(message: Message, state: FSMContext) -> None:
    await state.clear()
    db.ensure_user(message.from_user.id)
    db.log_event(message.from_user.id, "ach_view")
    db.mark_ach_intro(message.from_user.id)  # список и есть сводка, отдельная не нужна
    await message.answer(reports.achievements_report(message.from_user.id))


@router.message(Command("toxic_on"))
async def cmd_toxic_on(message: Message) -> None:
    db.ensure_user(message.from_user.id)
    db.set_toxic(message.from_user.id, True)
    await message.answer("Ладно, буду говорить как есть 💀")


@router.message(Command("toxic_off"))
async def cmd_toxic_off(message: Message) -> None:
    db.ensure_user(message.from_user.id)
    db.set_toxic(message.from_user.id, False)
    await message.answer("Хорошо, без подколов 💚 Вернуть обратно: /toxic_on")


@router.message(Command("ask_on"))
async def cmd_ask_on(message: Message) -> None:
    db.ensure_user(message.from_user.id)
    db.set_weekly_ask(message.from_user.id, True)
    await message.answer("Буду спрашивать про смены раз в неделю ✅")


@router.message(Command("ask_off"))
async def cmd_ask_off(message: Message) -> None:
    db.set_weekly_ask(message.from_user.id, False)
    await message.answer("Больше не буду спрашивать сам. Записать смены: /week")


# --- Смены на неделю ------------------------------------------------------

@router.message(Command("week"))
@router.message(F.text == kb.BTN_WEEK)
async def cmd_week(message: Message, state: FSMContext) -> None:
    await state.clear()
    db.ensure_user(message.from_user.id)
    await message.answer(texts.WEEK_QUESTION, reply_markup=kb.week_choice())


@router.callback_query(kb.WeekCb.filter(F.action == "back_week"))
async def cb_back_week(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(texts.WEEK_QUESTION, reply_markup=kb.week_choice())
    await call.answer()


@router.callback_query(kb.WeekCb.filter(F.action == "week"))
async def cb_pick_week(call: CallbackQuery, callback_data: kb.WeekCb) -> None:
    monday = date.fromisoformat(callback_data.arg)
    await call.message.edit_text(
        f"Неделя <b>{domain.week_label(monday)}</b>\nКакие смены?",
        reply_markup=kb.shift_choice(monday),
    )
    await call.answer()


@router.callback_query(kb.WeekCb.filter(F.action == "shift_back"))
async def cb_shift_back(call: CallbackQuery, callback_data: kb.WeekCb, state: FSMContext) -> None:
    await state.set_state(None)
    monday = date.fromisoformat(callback_data.arg)
    await call.message.edit_text(
        f"Неделя <b>{domain.week_label(monday)}</b>\nКакие смены?",
        reply_markup=kb.shift_choice(monday),
    )
    await call.answer()


@router.callback_query(kb.WeekCb.filter(F.action.in_({"shift", "more"})))
async def cb_pick_shift(call: CallbackQuery, callback_data: kb.WeekCb, state: FSMContext) -> None:
    if callback_data.action == "more":
        monday = date.fromisoformat(callback_data.arg)
        await state.clear()
        await call.message.edit_text(
            f"Неделя <b>{domain.week_label(monday)}</b>\nКакие смены?",
            reply_markup=kb.shift_choice(monday),
        )
        await call.answer()
        return

    monday_iso, num_raw = callback_data.arg.split("|")
    monday = date.fromisoformat(monday_iso)
    num = int(num_raw)

    days = domain.week_days(monday, num)
    already = {
        s.work_date
        for s in db.shifts_in_range(user_id=call.from_user.id, start=days[0], end=days[-1])
        if s.shift_num == num
    }
    await state.set_state(Flow.picking_days)
    await state.update_data(monday=monday_iso, num=num, selected=[d.isoformat() for d in already])
    s = domain.shift(num)
    has_past = any(domain.shift_end(d, num) <= domain.now() for d in days)
    legend = "\n<i>⌛ — день уже прошёл, смена сразу попадёт в «отработано».</i>" if has_past else ""
    await call.message.edit_text(
        f"<b>{s.title}</b> {s.hours_label} · {domain.money(s.pay)} за смену\n"
        f"Неделя {domain.week_label(monday)}\n\nОтметь дни и нажми «Сохранить»." + legend,
        reply_markup=kb.days_choice(monday, num, already),
    )
    await call.answer()


@router.callback_query(Flow.picking_days, kb.WeekCb.filter(F.action == "day"))
async def cb_toggle_day(call: CallbackQuery, callback_data: kb.WeekCb, state: FSMContext) -> None:
    data = await state.get_data()
    selected = set(data["selected"])
    selected.symmetric_difference_update({callback_data.arg})
    await state.update_data(selected=sorted(selected))
    monday = date.fromisoformat(data["monday"])
    await call.message.edit_reply_markup(
        reply_markup=kb.days_choice(
            monday, data["num"], {date.fromisoformat(x) for x in selected}
        )
    )
    await call.answer()


@router.callback_query(Flow.picking_days, kb.WeekCb.filter(F.action == "save"))
async def cb_save_days(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    monday = date.fromisoformat(data["monday"])
    num = data["num"]
    selected = {date.fromisoformat(x) for x in data["selected"]}
    days = domain.week_days(monday, num)

    added, removed, kept = [], [], []
    for d in days:
        existing = [
            s for s in db.shifts_in_range(call.from_user.id, d, d) if s.shift_num == num
        ]
        if d in selected and not existing:
            db.add_shift(call.from_user.id, d, num)
            added.append(d)
        elif d not in selected and existing:
            # подтверждённую смену не трогаем — это уже история, а не план
            if existing[0].confirmed:
                kept.append(d)
            else:
                db.delete_shift(call.from_user.id, d, num)
                removed.append(d)

    s = domain.shift(num)
    lines = [f"<b>{s.title}</b> {s.hours_label} — сохранено."]
    if added:
        lines.append("Добавлено: " + ", ".join(domain.fmt_date(d) for d in added))
    if removed:
        lines.append("Убрано: " + ", ".join(domain.fmt_date(d) for d in removed))
    if kept:
        lines.append(
            "Оставил как есть (уже подтверждены): "
            + ", ".join(domain.fmt_date(d) for d in kept)
        )
    if not added and not removed:
        lines.append("Изменений нет.")
    if selected:
        lines.append(f"\nЗа эти смены: {domain.money(s.pay * len(selected))}")
    lines.append("\n" + reports.week_summary(call.from_user.id, monday))
    await call.message.edit_text("\n".join(lines), reply_markup=kb.after_save(monday))
    await call.answer("Сохранено")
    await award(call, call.from_user.id)


@router.callback_query(kb.WeekCb.filter(F.action.in_({"day", "save"})))
async def cb_stale_picker(call: CallbackQuery) -> None:
    """Кнопки из старого сообщения: состояние выбора уже потеряно (перезапуск бота)."""
    await call.message.edit_text(
        "Это сообщение устарело — выбор сбросился. Начни заново: /week"
    )
    await call.answer("Сообщение устарело", show_alert=True)


@router.callback_query(kb.WeekCb.filter(F.action == "close"))
async def cb_close(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Ок. Записать смены: /week")
    await call.answer()


# --- Предстоящие смены и отмена -------------------------------------------

@router.message(Command("shifts"))
@router.message(F.text == kb.BTN_MY)
async def cmd_shifts(message: Message, state: FSMContext) -> None:
    await state.clear()
    db.ensure_user(message.from_user.id)
    text, shifts = reports.upcoming_summary(message.from_user.id)
    if not shifts:
        db.log_event(message.from_user.id, "empty_view")
    await message.answer(text, reply_markup=kb.upcoming(shifts) if shifts else None)


@router.callback_query(kb.WeekCb.filter(F.action == "show_my"))
async def cb_show_my(call: CallbackQuery) -> None:
    text, shifts = reports.upcoming_summary(call.from_user.id)
    await call.message.answer(text, reply_markup=kb.upcoming(shifts) if shifts else None)
    await call.answer()


@router.callback_query(kb.ShiftCb.filter(F.action == "cancel"))
async def cb_cancel_shift(call: CallbackQuery, callback_data: kb.ShiftCb) -> None:
    shift = db.get_shift(callback_data.shift_id, call.from_user.id)
    if shift is None:
        await call.answer("Смена не найдена", show_alert=True)
        return
    if shift.cancelled:
        await call.answer("Эта смена уже отменена")
        return
    shift = db.cancel_shift(callback_data.shift_id, call.from_user.id)
    await call.message.answer(
        f"❌ Отменена: {domain.shift_line(shift.work_date, shift.shift_num)}\n\n"
        + texts.cancelled_cheer(),
        reply_markup=kb.restore(shift.id),
    )
    await call.answer("Смена отменена")
    await award(call, call.from_user.id)


@router.callback_query(kb.ShiftCb.filter(F.action == "restore"))
async def cb_restore_shift(call: CallbackQuery, callback_data: kb.ShiftCb) -> None:
    shift = db.get_shift(callback_data.shift_id, call.from_user.id)
    if shift is None:
        await call.answer("Смена не найдена", show_alert=True)
        return
    db.add_shift(call.from_user.id, shift.work_date, shift.shift_num)
    await call.message.edit_text(
        f"↩️ Смена возвращена: {domain.shift_line(shift.work_date, shift.shift_num)}"
    )
    await call.answer("Вернул")


# --- Деньги ---------------------------------------------------------------

@router.message(Command("money"))
@router.message(F.text == kb.BTN_MONEY)
async def cmd_money(message: Message, state: FSMContext) -> None:
    await state.clear()
    db.ensure_user(message.from_user.id)
    db.log_event(message.from_user.id, "money_view")
    year, month = domain.period_anchor(domain.today())
    await message.answer(
        reports.money_report(message.from_user.id, year, month),
        reply_markup=kb.money_nav(year, month),
    )


@router.callback_query(kb.MoneyCb.filter())
async def cb_money(call: CallbackQuery, callback_data: kb.MoneyCb) -> None:
    year, month = (int(x) for x in callback_data.ym.split("-"))
    await call.message.edit_text(
        reports.money_report(call.from_user.id, year, month),
        reply_markup=kb.money_nav(year, month),
    )
    await call.answer()


# --- Штрафы ---------------------------------------------------------------

async def _show_penalties(target: Message | CallbackQuery, year: int, month: int, edit: bool) -> None:
    user_id = target.from_user.id
    text, period = reports.penalties_report(user_id, year, month)
    markup = kb.penalties_nav(year, month, period.penalties, period.rate_cut)
    if edit:
        await target.message.edit_text(text, reply_markup=markup)
    else:
        chat = target if isinstance(target, Message) else target.message
        await chat.answer(text, reply_markup=markup)


@router.message(Command("penalties"))
@router.message(F.text == kb.BTN_PENALTY)
async def cmd_penalties(message: Message, state: FSMContext) -> None:
    await state.clear()
    db.ensure_user(message.from_user.id)
    year, month = domain.period_anchor(domain.today())
    await _show_penalties(message, year, month, edit=False)


@router.callback_query(kb.PenaltyCb.filter(F.action == "nav"))
async def cb_penalties_nav(call: CallbackQuery, callback_data: kb.PenaltyCb, state: FSMContext) -> None:
    await state.clear()
    year, month = (int(x) for x in callback_data.ym.split("-"))
    await _show_penalties(call, year, month, edit=True)
    await call.answer()


@router.callback_query(kb.PenaltyCb.filter(F.action == "ratecut"))
async def cb_penalties_ratecut(call: CallbackQuery, callback_data: kb.PenaltyCb) -> None:
    """Снижение ставки на 0,50 zł/ч бот угадать не может — тут его включают руками."""
    year, month = (int(x) for x in callback_data.ym.split("-"))
    period = reports.period_data(call.from_user.id, year, month)
    db.set_rate_cut_flag(call.from_user.id, year, month, not period.rate_cut)
    await _show_penalties(call, year, month, edit=True)
    await call.answer("Ставка снижена" if not period.rate_cut else "Снижение убрано")


@router.callback_query(kb.PenaltyCb.filter(F.action == "del"))
async def cb_penalty_delete(call: CallbackQuery, callback_data: kb.PenaltyCb) -> None:
    year, month = (int(x) for x in callback_data.ym.split("-"))
    pen = db.get_penalty(int(callback_data.arg), call.from_user.id)
    if pen is None:
        await call.answer("Штраф не найден", show_alert=True)
        return
    db.delete_penalty(pen.id, call.from_user.id)
    await _show_penalties(call, year, month, edit=True)
    await call.answer(f"Убрал: {pen.title}")


@router.callback_query(kb.PenaltyCb.filter(F.action == "menu"))
async def cb_penalty_menu(call: CallbackQuery, callback_data: kb.PenaltyCb, state: FSMContext) -> None:
    year, month = (int(x) for x in callback_data.ym.split("-"))
    await state.set_state(Flow.penalty_entry)
    await state.update_data(year=year, month=month)
    await call.message.edit_text(
        "<b>Записать штраф</b>\n"
        f"Кнопкой — на сегодня ({domain.fmt_date(domain.today())}).\n\n"
        "Другой день или своя сумма — напиши строкой:\n"
        "<code>15.08 опоздание</code>\n"
        "<code>15 прогул 200</code>\n"
        "<code>2.09 доступность не сообщил вовремя</code>\n\n"
        "Отмена — /cancel",
        reply_markup=kb.penalty_kinds(callback_data.ym),
    )
    await call.answer()


@router.callback_query(kb.PenaltyCb.filter(F.action == "kind"))
async def cb_penalty_kind(call: CallbackQuery, callback_data: kb.PenaltyCb, state: FSMContext) -> None:
    await state.clear()
    year, month = (int(x) for x in callback_data.ym.split("-"))
    code = callback_data.arg
    if code not in domain.PENALTIES:
        await call.answer("Неизвестный вид", show_alert=True)
        return
    at_date = domain.today()
    db.add_penalty(call.from_user.id, at_date, code)
    kind = domain.penalty(code)
    await call.answer(f"{kind.title}: {domain.money(kind.amount)}")
    # штраф мог попасть в другой период — показываем тот, к которому он относится
    year, month = domain.period_anchor(at_date)
    await _show_penalties(call, year, month, edit=True)


@router.message(Flow.penalty_entry, F.text)
async def on_penalty_entry(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    parsed = parsing.parse_penalty(
        message.text, data["year"], data["month"], domain.today()
    )
    if parsed is None:
        await message.answer(
            "Не понял. Например: <code>15.08 опоздание</code>,"
            " <code>прогул</code>, <code>2.09 перерыв 200</code>.\nОтмена — /cancel"
        )
        return
    await state.clear()
    at_date, code, amount, note = parsed
    pen = db.add_penalty(message.from_user.id, at_date, code, amount=amount, note=note)
    kind = domain.penalty(code)
    await message.answer(
        f"{kind.icon} Записал: <b>{kind.title}</b> — {domain.money(pen.amount)}\n"
        f"{domain.fmt_date_long(at_date)}" + (f"\n<i>{note}</i>" if note else ""),
        reply_markup=kb.main_menu(),
    )
    year, month = domain.period_anchor(at_date)
    await _show_penalties(message, year, month, edit=False)


# --- Ручной ввод смен -----------------------------------------------------

@router.message(Command("add"))
@router.message(F.text == kb.BTN_MANUAL)
async def cmd_add(message: Message, state: FSMContext) -> None:
    db.ensure_user(message.from_user.id)
    year, month = domain.period_anchor(domain.today())
    await state.set_state(Flow.manual_entry)
    await state.update_data(year=year, month=month)
    await message.answer(
        "Впиши смены — одну на строку или через запятую.\n\n"
        "<b>Формат:</b> <code>дата номер смены</code>\n"
        "<code>5.08 1</code> — 5 августа, 1я смена\n"
        "<code>12 3</code> — 12-е число текущего периода, ночная\n"
        "<code>10-14.08 2</code> — с 10 по 14 августа, 2я смена\n\n"
        f"Месяц по умолчанию: <b>{domain.MONTHS_NOM[month - 1]} {year}</b>.\n"
        "Отмена — /cancel"
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменил ввод.", reply_markup=kb.main_menu())


@router.message(Flow.manual_entry, F.text)
async def on_manual_entry(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    entries, errors = parsing.parse_entries(message.text, data["year"], data["month"])
    if not entries and errors:
        await message.answer(
            "Не понял ни одной строки. Пример: <code>5.08 1</code> или <code>10-14.08 2</code>.\n"
            "Отмена — /cancel"
        )
        return

    await state.clear()
    added, exists = [], 0
    for work_date, num in entries:
        result = db.add_shift(message.from_user.id, work_date, num)
        if result == "exists":
            exists += 1
        else:
            added.append((work_date, num))

    total = sum((domain.shift(n).pay for _, n in added), Decimal(0))
    lines = [f"Записал: <b>{len(added)}</b> {domain.shifts_word(len(added))}"]
    for work_date, num in added:
        lines.append("• " + domain.shift_line(work_date, num))
    if added:
        lines.append(f"\nЗа них: <b>{domain.money(total)}</b>")
    if exists:
        lines.append(f"Уже были записаны: {exists}")
    if errors:
        lines.append("\n⚠️ Не понял: " + ", ".join(f"<code>{e}</code>" for e in errors))
    await message.answer("\n".join(lines), reply_markup=kb.main_menu())

    year, month = data["year"], data["month"]
    await message.answer(
        reports.money_report(message.from_user.id, year, month),
        reply_markup=kb.money_nav(year, month),
    )
    await award(message, message.from_user.id)


# --- Подтверждение прошедших смен ------------------------------------------

def _confirm_text(shift: db.Shift) -> str:
    return (
        f"{texts.CONFIRM_QUESTION}\n"
        f"<b>{domain.shift_line(shift.work_date, shift.shift_num)}</b>"
    )


@router.message(Command("confirm"))
async def cmd_confirm(message: Message, state: FSMContext) -> None:
    await state.clear()
    db.ensure_user(message.from_user.id)
    pending = db.unconfirmed_shifts(message.from_user.id)
    if not pending:
        await message.answer("Все прошедшие смены подтверждены 👌")
        return
    lines = ["<b>Смены без подтверждения</b>", ""]
    for s in pending:
        lines.append("• " + domain.shift_line(s.work_date, s.shift_num))
    lines.append("\n<i>Пока не подтверждены — считаю их полными по 8 ч.</i>")
    await message.answer("\n".join(lines), reply_markup=kb.unconfirmed(pending))


@router.callback_query(kb.ConfirmCb.filter(F.action == "pick"))
async def cb_confirm_pick(call: CallbackQuery, callback_data: kb.ConfirmCb) -> None:
    shift = db.get_shift(callback_data.shift_id, call.from_user.id)
    if shift is None:
        await call.answer("Смена не найдена", show_alert=True)
        return
    await call.message.edit_text(_confirm_text(shift), reply_markup=kb.confirm(shift.id))
    await call.answer()


@router.callback_query(kb.ConfirmCb.filter(F.action == "late"))
async def cb_confirm_late(call: CallbackQuery, callback_data: kb.ConfirmCb) -> None:
    shift = db.get_shift(callback_data.shift_id, call.from_user.id)
    if shift is None:
        await call.answer("Смена не найдена", show_alert=True)
        return
    await call.message.edit_text(
        f"{domain.shift_line(shift.work_date, shift.shift_num)}\n\n"
        "Что было? Запишу штраф, потом спрошу про часы.",
        reply_markup=kb.confirm_late(shift.id),
    )
    await call.answer()


@router.callback_query(kb.ConfirmCb.filter(F.action == "late_kind"))
async def cb_confirm_late_kind(call: CallbackQuery, callback_data: kb.ConfirmCb) -> None:
    shift = db.get_shift(callback_data.shift_id, call.from_user.id)
    if shift is None:
        await call.answer("Смена не найдена", show_alert=True)
        return
    code = callback_data.arg if callback_data.arg in domain.PENALTIES else "late"
    kind = domain.penalty(code)
    if db.penalty_for_shift(call.from_user.id, shift.id, code):
        await call.answer(f"{kind.title} по этой смене уже записан", show_alert=True)
    else:
        db.add_penalty(call.from_user.id, shift.work_date, code, shift_id=shift.id)
        await call.answer(f"Записал штраф {domain.money(kind.amount)}")
    await call.message.edit_text(
        f"{kind.icon} <b>{kind.title}</b> — штраф {domain.money(kind.amount)}\n"
        f"{domain.shift_line(shift.work_date, shift.shift_num)}\n\n"
        f"{texts.penalty_cheer()}\n\nТеперь — сколько часов зачли?",
        reply_markup=kb.confirm_hours(shift.id, with_full=True),
    )


@router.callback_query(kb.ConfirmCb.filter(F.action == "early"))
async def cb_confirm_early(call: CallbackQuery, callback_data: kb.ConfirmCb) -> None:
    shift = db.get_shift(callback_data.shift_id, call.from_user.id)
    if shift is None:
        await call.answer("Смена не найдена", show_alert=True)
        return
    await call.message.edit_text(
        f"{domain.shift_line(shift.work_date, shift.shift_num)}\n\nСколько часов зачли?",
        reply_markup=kb.confirm_hours(shift.id),
    )
    await call.answer()


async def _save_confirmation(
    call: CallbackQuery | Message, user_id: int, shift_id: int, hours: Decimal | None
) -> str:
    shift = db.confirm_shift(shift_id, user_id, hours)
    if shift is None:
        return ""
    if hours is None:
        return (
            f"❌ Записал: не был.\n{domain.shift_line(shift.work_date, shift.shift_num)}\n"
            "За эту смену денег не считаю."
        )
    full = hours >= config.SHIFT_HOURS
    head = "✅ Записал" if full else "🏃 Записал (отпустили раньше)"
    text = (
        f"{head}: {domain.fmt_hours(hours)} — <b>{domain.money(shift.pay)}</b>\n"
        f"{domain.shift_line(shift.work_date, shift.shift_num)}"
    )
    if not full:
        text += "\n\n" + texts.early_cheer()
    return text


@router.callback_query(kb.ConfirmCb.filter(F.action == "absent"))
async def cb_confirm_absent(call: CallbackQuery, callback_data: kb.ConfirmCb) -> None:
    """За пропуск по договору штраф 370 zł — но бывает, что вины работника нет."""
    shift = db.get_shift(callback_data.shift_id, call.from_user.id)
    if shift is None:
        await call.answer("Смена не найдена", show_alert=True)
        return
    fine = domain.penalty("absence")
    await call.message.edit_text(
        f"{domain.shift_line(shift.work_date, shift.shift_num)}\n\n"
        f"Смены не было. Штраф по договору — <b>{domain.money(fine.amount)}</b>"
        f" <i>({fine.clause})</i>.\nЗаписывать?",
        reply_markup=kb.confirm_absent(shift.id),
    )
    await call.answer()


@router.callback_query(kb.ConfirmCb.filter(F.action.in_({"full", "hours", "absent_fine", "absent_free"})))
async def cb_confirm_save(call: CallbackQuery, callback_data: kb.ConfirmCb) -> None:
    absent = callback_data.action.startswith("absent")
    if absent:
        hours = None
    elif callback_data.action == "full":
        hours = config.SHIFT_HOURS
    else:
        hours = Decimal(callback_data.hours.replace(",", "."))
    text = await _save_confirmation(call, call.from_user.id, callback_data.shift_id, hours)
    if not text:
        await call.answer("Смена не найдена", show_alert=True)
        return
    if callback_data.action == "absent_fine":
        shift = db.get_shift(callback_data.shift_id, call.from_user.id)
        fine = domain.penalty("absence")
        if db.penalty_for_shift(call.from_user.id, shift.id, "absence"):
            text += "\n\n⚖️ Штраф по этой смене уже был записан."
        else:
            db.add_penalty(call.from_user.id, shift.work_date, "absence", shift_id=shift.id)
            text += (
                f"\n\n{fine.icon} Штраф: <b>{domain.money(fine.amount)}</b> нетто."
                "\nВсе штрафы за период: /penalties"
            )
    await call.message.edit_text(text)
    await call.answer("Записал")
    await award(call, call.from_user.id)


@router.callback_query(kb.ConfirmCb.filter(F.action == "custom"))
async def cb_confirm_custom(call: CallbackQuery, callback_data: kb.ConfirmCb, state: FSMContext) -> None:
    shift = db.get_shift(callback_data.shift_id, call.from_user.id)
    if shift is None:
        await call.answer("Смена не найдена", show_alert=True)
        return
    await state.set_state(Flow.custom_hours)
    await state.update_data(shift_id=shift.id)
    await call.message.edit_text(
        f"{domain.shift_line(shift.work_date, shift.shift_num)}\n\n"
        "Напиши, сколько часов зачли — например <code>6,5</code>. Отмена — /cancel"
    )
    await call.answer()


@router.message(Flow.custom_hours, F.text)
async def on_custom_hours(message: Message, state: FSMContext) -> None:
    raw = message.text.strip().replace(",", ".").replace("ч", "").strip()
    try:
        hours = Decimal(raw)
    except Exception:
        await message.answer("Не понял число. Например: <code>6,5</code>. Отмена — /cancel")
        return
    if not (Decimal(0) <= hours <= Decimal(24)):
        await message.answer("Часы должны быть от 0 до 24. Отмена — /cancel")
        return
    data = await state.get_data()
    await state.clear()
    text = await _save_confirmation(
        message, message.from_user.id, data["shift_id"], None if hours == 0 else hours
    )
    await message.answer(text or "Смена не найдена.", reply_markup=kb.main_menu())
    await award(message, message.from_user.id)
