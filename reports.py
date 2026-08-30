"""Сборка текстовых отчётов: неделя, предстоящие смены, деньги за период."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import achievements
import config
import db
import domain


def week_summary(user_id: int, monday: date | None = None) -> str:
    """Смены на неделе, начинающейся с monday (по умолчанию — текущая)."""
    monday = monday or domain.current_monday()
    start = monday - timedelta(days=1)  # ночная с Вс относится к этой неделе
    end = monday + timedelta(days=6)
    shifts = db.shifts_in_range(user_id, start, end)
    header = f"<b>Неделя {domain.week_label(monday)}</b>"
    if not shifts:
        return f"{header}\nСмены не записаны."
    lines = [header]
    total = Decimal(0)
    for s in shifts:
        mark = ""
        if s.status == "done" and s.hours != config.SHIFT_HOURS:
            mark = f" — {domain.fmt_hours(s.hours)}"
        elif s.status == "done":
            mark = " ✅"
        lines.append("• " + domain.shift_line(s.work_date, s.shift_num) + mark)
        total += s.pay
    lines.append(f"\nВсего: {len(shifts)} {domain.shifts_word(len(shifts))} · {domain.money(total)}")
    return "\n".join(lines)


def upcoming_summary(user_id: int) -> tuple[str, list[db.Shift]]:
    shifts = db.upcoming_shifts(user_id)
    if not shifts:
        return "Предстоящих смен нет. Записать: /week", []
    lines = ["<b>Предстоящие смены</b>"]
    for s in shifts:
        start = domain.shift_start(s.work_date, s.shift_num)
        left = start - domain.now()
        when = f"через {left.days} дн." if left.days >= 1 else f"через {left.seconds // 3600} ч."
        if left.total_seconds() < 0:
            when = "идёт сейчас"
        lines.append(f"• {domain.shift_line(s.work_date, s.shift_num)} — {when}")
    return "\n".join(lines), shifts


@dataclass
class Period:
    """Всё, что нужно знать о расчётном периоде: смены, деньги и удержания."""
    year: int
    month: int
    start: date
    end: date
    done: list[db.Shift]          # подтверждённые, с фактическими часами
    unconfirmed: list[db.Shift]   # прошли, но не подтверждены — считаем по 8 ч
    planned: list[db.Shift]       # ещё впереди
    absent: list[db.Shift]        # прогулы: смена была, работника не было
    cancelled: list[db.Shift]     # отменённые — вины работника нет
    penalties: list[db.Penalty]
    rate_cut: bool                # снижена ли ставка за неполный месяц
    rate_cut_manual: bool | None  # ручная отметка пользователя, если он её ставил

    @property
    def skipped(self) -> int:
        return len(self.absent) + len(self.cancelled)

    @property
    def counted(self) -> list[db.Shift]:
        """Смены, за которые идут деньги: отработанные и запланированные."""
        return self.done + self.unconfirmed + self.planned

    @property
    def worked_hours(self) -> Decimal:
        """Часы, которые уже отработаны — с них и считается снижение ставки."""
        return sum((s.hours for s in self.done + self.unconfirmed), Decimal(0))

    @property
    def earned(self) -> Decimal:
        return sum((s.pay for s in self.counted), Decimal(0))

    @property
    def penalty_total(self) -> Decimal:
        return sum((p.amount for p in self.penalties), Decimal(0))

    @property
    def rate_cut_amount(self) -> Decimal:
        return domain.rate_cut_for(self.worked_hours) if self.rate_cut else Decimal(0)

    @property
    def deductions(self) -> Decimal:
        return self.penalty_total + self.rate_cut_amount

    @property
    def net(self) -> Decimal:
        """Сколько останется после всех удержаний."""
        return self.earned - self.deductions


def period_data(user_id: int, year: int, month: int) -> Period:
    """Собрать период: смены разложены по состояниям, штрафы подтянуты."""
    start, end = domain.period_bounds(year, month)
    now = domain.now()
    done, unconfirmed, planned, absent, cancelled = [], [], [], [], []
    for s in db.shifts_in_range(user_id, start, end, include_cancelled=True):
        if s.status == "absent":
            absent.append(s)
        elif s.status == "cancelled":
            cancelled.append(s)
        elif s.status == "done":
            done.append(s)
        elif domain.shift_end(s.work_date, s.shift_num) <= now:
            unconfirmed.append(s)
        else:
            planned.append(s)

    manual = db.rate_cut_flag(user_id, year, month)
    # Без ручной отметки считаем так: был прогул — значит 100% присутствия не вышло.
    rate_cut = manual if manual is not None else bool(absent)
    return Period(
        year=year, month=month, start=start, end=end,
        done=done, unconfirmed=unconfirmed, planned=planned,
        absent=absent, cancelled=cancelled,
        penalties=db.penalties_in_range(user_id, start, end),
        rate_cut=rate_cut, rate_cut_manual=manual,
    )


def money_report(user_id: int, year: int, month: int) -> str:
    period = period_data(user_id, year, month)
    start, end = period.start, period.end
    shifts = period.counted + period.absent + period.cancelled
    done, unconfirmed, planned = period.done, period.unconfirmed, period.planned
    skipped = period.skipped

    def total(items: list[db.Shift]) -> Decimal:
        return sum((s.pay for s in items), Decimal(0))

    def hours(items: list[db.Shift]) -> Decimal:
        return sum((s.hours for s in items), Decimal(0))

    def breakdown(items: list[db.Shift]) -> list[str]:
        out = []
        for label, subset in (
            ("☀️ дневных", [s for s in items if not s.kind.is_night]),
            ("🌙 ночных", [s for s in items if s.kind.is_night]),
        ):
            if subset:
                out.append(
                    f"    {label}: {len(subset)} {domain.shifts_word(len(subset))},"
                    f" {domain.fmt_hours(hours(subset))} = {domain.money(total(subset))}"
                )
        return out

    lines = [
        f"<b>💰 Период: {domain.period_title(year, month)}</b>",
        f"<i>{domain.fmt_date_long(start)} — {domain.fmt_date_long(end)}</i>",
        "",
    ]
    if not shifts:
        lines.append("Смен за этот период не записано.")
        lines.append("Записать вручную: /add")
    else:
        if done:
            lines.append(
                f"✅ Отработано: {len(done)} {domain.shifts_word(len(done))},"
                f" {domain.fmt_hours(hours(done))} — <b>{domain.money(total(done))}</b>"
            )
            lines += breakdown(done)
        if unconfirmed:
            lines.append(
                f"⏳ Без подтверждения: {len(unconfirmed)}"
                f" {domain.shifts_word(len(unconfirmed))} — {domain.money(total(unconfirmed))}"
            )
            lines.append("    <i>считаю по 8 ч, уточнить: /confirm</i>")
        if planned:
            lines.append(
                f"🕒 Запланировано: {len(planned)} {domain.shifts_word(len(planned))}"
                f" — {domain.money(total(planned))}"
            )
        if skipped:
            parts = []
            if period.absent:
                parts.append(f"прогулов {len(period.absent)}")
            if period.cancelled:
                parts.append(f"отменено {len(period.cancelled)}")
            lines.append(f"❌ Не в счёт: {skipped} ({', '.join(parts)})")
        grand = period.earned
        lines.append(f"\n📊 Итого за период: <b>{domain.money(grand)}</b>")
        if period.deductions:
            lines += deduction_lines(period)
            lines.append(f"💵 На руки: <b>{domain.money(period.net)}</b>")
            lines.append("<i>Подробнее по штрафам: /penalties</i>")
        lines.append(f"<i>{achievements.fun_equivalent(period.net)}</i>")
    lines.append(
        f"\n🗓 Выплата: <b>{domain.fmt_date_long(domain.payout_date(year, month))}</b>"
    )
    lines.append(
        f"<i>Ставки: день {domain.money(config.DAY_RATE)}/час, "
        f"ночь {domain.money(config.NIGHT_RATE)}/час.</i>"
    )
    return "\n".join(lines)


# --- Штрафы ---------------------------------------------------------------

def deduction_lines(period: Period) -> list[str]:
    """Короткая сводка удержаний — общая для /money и /penalties."""
    out = []
    if period.penalties:
        n = len(period.penalties)
        out.append(
            f"⚖️ Штрафы: {n} {domain.penalties_word(n)}"
            f" — <b>−{domain.money(period.penalty_total)}</b>"
        )
    if period.rate_cut:
        out.append(
            f"📉 Ставка снижена: {domain.money(config.RATE_CUT)}/ч"
            f" × {domain.fmt_hours(period.worked_hours)}"
            f" — <b>−{domain.money(period.rate_cut_amount)}</b>"
        )
    return out


def penalties_report(user_id: int, year: int, month: int) -> tuple[str, Period]:
    """Полный отчёт по штрафам за период: что, когда, за сколько и почему."""
    period = period_data(user_id, year, month)
    lines = [
        f"<b>⚖️ Штрафы: {domain.period_title(year, month)}</b>",
        f"<i>{domain.fmt_date_long(period.start)} — {domain.fmt_date_long(period.end)}</i>",
        "",
    ]

    if period.penalties:
        by_kind: dict[str, list[db.Penalty]] = {}
        for pen in period.penalties:
            by_kind.setdefault(pen.kind, []).append(pen)
        for code in domain.PENALTY_ORDER:
            items = by_kind.get(code)
            if not items:
                continue
            kind = domain.penalty(code)
            amount = sum((x.amount for x in items), Decimal(0))
            same = len({x.amount for x in items}) == 1
            count = (
                f"{len(items)} × {domain.money(items[0].amount)} = "
                if same and len(items) > 1 else ""
            )
            lines.append(f"{kind.icon} <b>{kind.title}</b> — {count}<b>{domain.money(amount)}</b>")
            for pen in items:
                tail = f" — {pen.note}" if pen.note else ""
                lines.append(
                    f"    • {domain.fmt_date(pen.at_date)}"
                    f" · {domain.money(pen.amount)}{tail}"
                )
            lines.append(f"    <i>{kind.clause}</i>")
        lines.append(f"\nВсего штрафов: <b>{domain.money(period.penalty_total)}</b> нетто")
        if len(period.penalties) > domain.PENALTY_BUTTONS:
            lines.append(
                f"<i>Кнопками 🗑 удаляются первые {domain.PENALTY_BUTTONS} —"
                " остальные после того, как уберёшь эти.</i>"
            )
    else:
        lines.append("Штрафов за этот период нет. Так и держи 👌")

    lines.append("")
    if period.rate_cut:
        reason = (
            "отмечено вручную" if period.rate_cut_manual
            else f"в периоде есть прогул ({len(period.absent)})"
        )
        lines.append(
            f"📉 <b>Ставка снижена</b> на {domain.money(config.RATE_CUT)} брутто за час:"
            f" {domain.fmt_hours(period.worked_hours)}"
            f" — <b>{domain.money(period.rate_cut_amount)}</b>"
        )
        lines.append(f"    <i>{reason}; условие — нет 100% присутствия по графику</i>")
    else:
        lines.append(
            "📈 Ставка не снижена: присутствие по графику полное"
            + (" (отмечено вручную)" if period.rate_cut_manual is False else "")
        )

    lines += [
        "",
        f"Заработано: {domain.money(period.earned)}",
        f"Удержания: −{domain.money(period.deductions)}",
        f"💵 <b>На руки: {domain.money(period.net)}</b>",
    ]
    if period.deductions:
        lines.append(
            "<i>Штрафы по договору — нетто, заработок — брутто,"
            " так что итог приблизительный.</i>"
        )
    return "\n".join(lines), period


# --- Достижения -----------------------------------------------------------

def tone_for(user_id: int) -> str:
    return achievements.TOXIC if db.toxic_enabled(user_id) else achievements.SOFT


def unlock_message(items: list[achievements.Achievement], tone: str) -> str:
    """Сообщение о только что открытых ачивках."""
    head = "🏅 <b>Новое достижение</b>" if len(items) == 1 else "🏅 <b>Новые достижения</b>"
    lines = [head, ""]
    for a in items:
        title, note = a.names(tone)
        lines.append(f"{a.icon(tone)} <b>{title}</b>")
        lines.append(f"<i>{note}</i>")
        lines.append("")
    lines.append("Все достижения: /achievements")
    return "\n".join(lines).strip()


def lost_message(items: list[achievements.Achievement], tone: str) -> str:
    lines = ["💔 <b>Достижение потеряно</b>", ""]
    for a in items:
        title, _ = a.names(tone)
        lines.append(f"{a.icon(tone)} <s>{title}</s>")
    if tone == achievements.TOXIC:
        lines.append("\n<i>Ачивки не выдаются пожизненно. Возвращайся на смены.</i>")
    else:
        lines.append("\n<i>Вернётся сама, как только снова выйдешь на смены.</i>")
    return "\n".join(lines)


def intro_message(count: int, tone: str) -> str:
    """Сводка при первом расчёте: сразу открылась куча ачивок за старые смены."""
    word = domain.plural(count, "достижение", "достижения", "достижений")
    if tone == achievements.TOXIC:
        tail = "Ты их даже не заслужил — просто вёл учёт. Смотри: /achievements"
    else:
        tail = "Всё это ты уже отработал. Смотри: /achievements"
    return f"🏅 За прошлые смены тебе сразу открылось <b>{count}</b> {word}.\n{tail}"


def rank_line(hours: Decimal) -> str:
    title, nxt, left = achievements.rank(hours)
    if nxt is None:
        return f"🎖 Звание: <b>{title}</b> — выше некуда."
    return (
        f"🎖 Звание: <b>{title}</b>"
        f" <i>(до «{nxt}» ещё {left} {domain.hours_word(left)})</i>"
    )


def achievements_report(user_id: int) -> str:
    """Открытые достижения, ближайшие цели и текущее звание."""
    fresh, lost, stats = achievements.sync(user_id)
    tone = tone_for(user_id)
    opened = db.unlocked_codes(user_id)
    total = len(achievements.CATALOG)

    lines = [f"🏅 <b>Достижения — {len(opened)} из {total}</b>", ""]
    if opened:
        for a in achievements.CATALOG:
            if a.code not in opened:
                continue
            title, note = a.names(tone)
            lines.append(f"{a.icon(tone)} <b>{title}</b> — <i>{note}</i>")
    else:
        lines.append("Пока ни одного. Отработай смену — что-нибудь да упадёт.")

    # Ближайшие цели: три самых близких по проценту, скрытые не раскрываем
    pending = []
    for a in achievements.CATALOG:
        if a.code in opened or a.impossible or a.hidden:
            continue
        have, goal = achievements.progress(stats, a)
        pending.append((have / goal, have, goal, a))
    pending.sort(key=lambda x: -x[0])
    if pending:
        lines += ["", "🔒 <b>Ближайшие</b>"]
        for _, have, goal, a in pending[:3]:
            unit = achievements.unit_label(a, goal)
            lines.append(
                f"{a.icon(tone)} {a.names(tone)[0]} — {have}/{goal}" + (f" {unit}" if unit else "")
            )

    hidden_left = sum(1 for a in achievements.CATALOG if a.hidden and a.code not in opened)
    if hidden_left:
        word = domain.plural(hidden_left, "скрытое", "скрытых", "скрытых")
        lines.append(f"❓ Ещё {hidden_left} {word} — условия не скажу.")

    locked = achievements.BY_CODE["locked"]
    if locked.code not in opened:
        lines.append(f"{locked.emoji} <b>{locked.title}</b> — <i>{locked.note}</i>")

    lines += ["", rank_line(stats.hours)]
    return "\n".join(lines)
