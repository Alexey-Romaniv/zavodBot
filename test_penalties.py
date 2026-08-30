"""Тесты штрафов: договорные суммы, разбор ввода, база и отчёт удержаний.

Запуск: .venv/bin/python test_penalties.py
"""
from __future__ import annotations

import os
import tempfile
from datetime import date
from decimal import Decimal

os.environ.setdefault("SHIFTBOT_DB", os.path.join(tempfile.mkdtemp(), "pen.db"))

import config  # noqa: E402
import db  # noqa: E402
import domain  # noqa: E402
import parsing  # noqa: E402
import reports  # noqa: E402

db.init()

_next_uid = iter(range(900_001, 999_999))


def user() -> int:
    """Свежий пользователь: тесты не должны видеть чужие штрафы."""
    uid = next(_next_uid)
    db.ensure_user(uid)
    return uid


# --- Суммы из договора ----------------------------------------------------

def test_penalty_amounts():
    assert domain.penalty_amount("absence") == Decimal("370")   # невыполнение поручения
    assert domain.penalty_amount("notice") == Decimal("370")    # не предупредил за 7 дней
    assert domain.penalty_amount("late") == Decimal("150")      # опоздание
    assert domain.penalty_amount("break") == Decimal("150")     # затянутый перерыв
    assert set(domain.PENALTY_ORDER) == set(domain.PENALTIES)


def test_rate_cut():
    # 0,50 zł брутто с каждого отработанного часа
    assert domain.rate_cut_for(Decimal(96)) == Decimal("48.00")
    assert domain.rate_cut_for(Decimal(0)) == Decimal("0.00")
    assert domain.rate_cut_for(Decimal("6.5")) == Decimal("3.25")


def test_notice_deadline():
    shift_day = date(2026, 8, 20)
    assert domain.notice_deadline(shift_day) == date(2026, 8, 13)   # за 7 дней
    assert domain.late_notice(shift_day, date(2026, 8, 13)) is False
    assert domain.late_notice(shift_day, date(2026, 8, 14)) is True


# --- Разбор ручного ввода -------------------------------------------------

def test_parse_penalty_kinds():
    today = date(2026, 8, 30)
    parsed = parsing.parse_penalty("опоздание", 2026, 8, today)
    assert parsed == (today, "late", None, None)
    assert parsing.parse_penalty("15.08 прогул", 2026, 8, today)[:2] == (date(2026, 8, 15), "absence")
    # число без месяца — из текущего расчётного периода
    assert parsing.parse_penalty("15 перерыв", 2026, 8, today)[0] == date(2026, 8, 15)


def test_parse_penalty_amount_and_note():
    today = date(2026, 8, 30)
    at, kind, amount, note = parsing.parse_penalty("2.09 перерыв 200 склад", 2026, 8, today)
    assert (at, kind, amount, note) == (date(2026, 9, 2), "break", Decimal("200"), "склад")
    at, kind, amount, note = parsing.parse_penalty(
        "доступность не сообщил вовремя", 2026, 8, today
    )
    assert (kind, amount, note) == ("notice", None, "не сообщил вовремя")


def test_parse_penalty_rejects_garbage():
    assert parsing.parse_penalty("ерунда какая-то", 2026, 8, date(2026, 8, 30)) is None
    assert parsing.parse_penalty("", 2026, 8, date(2026, 8, 30)) is None
    assert parsing.parse_penalty("99.99 опоздание", 2026, 8, date(2026, 8, 30)) is None


# --- Хранение -------------------------------------------------------------

def test_add_and_list_penalties():
    uid = user()
    db.add_penalty(uid, date(2020, 8, 5), "late")
    db.add_penalty(uid, date(2020, 8, 20), "absence")
    db.add_penalty(uid, date(2020, 9, 10), "late")   # уже следующий период

    start, end = domain.period_bounds(2020, 8)       # 2 августа — 1 сентября
    period = db.penalties_in_range(uid, start, end)
    assert [p.kind for p in period] == ["late", "absence"]
    assert sum((p.amount for p in period), Decimal(0)) == Decimal("520")
    assert len(db.all_penalties(uid)) == 3


def test_custom_amount_and_note():
    uid = user()
    pen = db.add_penalty(uid, date(2020, 8, 5), "other", amount=Decimal("500"), note="брак")
    assert pen.amount == Decimal("500")
    assert pen.note == "брак"
    assert pen.title == domain.penalty("other").title


def test_delete_penalty_only_own():
    uid, other = user(), user()
    pen = db.add_penalty(uid, date(2020, 8, 5), "late")
    assert db.delete_penalty(pen.id, other) is False   # чужой штраф не трогаем
    assert db.get_penalty(pen.id, uid) is not None
    assert db.delete_penalty(pen.id, uid) is True
    assert db.get_penalty(pen.id, uid) is None


def test_penalty_for_shift_prevents_doubles():
    uid = user()
    db.add_shift(uid, date(2020, 8, 5), 1)
    shift = db.shifts_in_range(uid, date(2020, 8, 5), date(2020, 8, 5))[0]
    assert db.penalty_for_shift(uid, shift.id, "late") is None
    db.add_penalty(uid, shift.work_date, "late", shift_id=shift.id)
    assert db.penalty_for_shift(uid, shift.id, "late") is not None
    assert db.penalty_for_shift(uid, shift.id, "absence") is None   # другой вид — можно


def test_rate_cut_flag():
    uid = user()
    assert db.rate_cut_flag(uid, 2020, 8) is None    # решаем по данным
    db.set_rate_cut_flag(uid, 2020, 8, True)
    assert db.rate_cut_flag(uid, 2020, 8) is True
    db.set_rate_cut_flag(uid, 2020, 8, False)
    assert db.rate_cut_flag(uid, 2020, 8) is False
    db.set_rate_cut_flag(uid, 2020, 8, None)
    assert db.rate_cut_flag(uid, 2020, 8) is None


# --- Отчёт за период ------------------------------------------------------

def worked_period(uid: int, days: list[int], num: int = 1) -> None:
    """Отработанные и подтверждённые смены августа 2020 — период целиком в прошлом."""
    for day in days:
        d = date(2020, 8, day)
        db.add_shift(uid, d, num)
        shift = [s for s in db.shifts_in_range(uid, d, d) if s.shift_num == num][0]
        db.confirm_shift(shift.id, uid, config.SHIFT_HOURS)


def test_period_counts_penalties():
    uid = user()
    worked_period(uid, [3, 4, 5])                    # 3 смены по 8 ч
    db.add_penalty(uid, date(2020, 8, 4), "late")    # 150
    db.add_penalty(uid, date(2020, 8, 6), "absence")  # 370

    p = reports.period_data(uid, 2020, 8)
    assert p.worked_hours == Decimal(24)
    assert p.earned == domain.pay_for(1) * 3
    assert p.penalty_total == Decimal("520")
    assert p.rate_cut is False                       # прогулов в сменах нет
    assert p.net == p.earned - Decimal("520")


def test_absent_shift_turns_on_rate_cut():
    uid = user()
    worked_period(uid, [3, 4])
    db.add_shift(uid, date(2020, 8, 5), 1)
    missed = [s for s in db.shifts_in_range(uid, date(2020, 8, 5), date(2020, 8, 5))][0]
    db.confirm_shift(missed.id, uid, None)           # «не был»

    p = reports.period_data(uid, 2020, 8)
    assert p.absent and p.rate_cut is True           # нет 100% присутствия
    assert p.worked_hours == Decimal(16)             # прогул часов не даёт
    assert p.rate_cut_amount == Decimal("8.00")      # 0,50 × 16 ч
    assert p.net == p.earned - Decimal("8.00")


def test_manual_flag_overrides_auto():
    uid = user()
    worked_period(uid, [3, 4])
    db.add_shift(uid, date(2020, 8, 5), 1)
    missed = db.shifts_in_range(uid, date(2020, 8, 5), date(2020, 8, 5))[0]
    db.confirm_shift(missed.id, uid, None)

    db.set_rate_cut_flag(uid, 2020, 8, False)        # завод не снизил — снимаем вручную
    p = reports.period_data(uid, 2020, 8)
    assert p.rate_cut is False and p.rate_cut_amount == Decimal(0)

    db.set_rate_cut_flag(uid, 2020, 8, True)
    assert reports.period_data(uid, 2020, 8).rate_cut is True


def test_reports_mention_penalties():
    uid = user()
    worked_period(uid, [3, 4])
    db.add_penalty(uid, date(2020, 8, 4), "late", note="проходная")

    text, period = reports.penalties_report(uid, 2020, 8)
    assert "Опоздание" in text
    assert "проходная" in text
    assert domain.money(Decimal("150")) in text
    assert domain.money(period.net) in text

    money = reports.money_report(uid, 2020, 8)
    assert "Штрафы" in money and "На руки" in money


def test_clean_period_report():
    uid = user()
    worked_period(uid, [3])
    text, period = reports.penalties_report(uid, 2020, 8)
    assert "Штрафов за этот период нет" in text
    assert period.deductions == Decimal(0)
    assert period.net == period.earned


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc!r}")
            except Exception as exc:
                failed += 1
                print(f"ERR  {name}: {type(exc).__name__}: {exc}")
    print("\n" + ("все тесты прошли" if not failed else f"провалено: {failed}"))
    raise SystemExit(1 if failed else 0)
