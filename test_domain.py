"""Тесты доменной логики и парсера. Запуск: python test_domain.py"""
from __future__ import annotations

import os
import pathlib
import tempfile
from datetime import date, datetime
from decimal import Decimal

os.environ.setdefault("SHIFTBOT_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import config  # noqa: E402
import db  # noqa: E402
import domain  # noqa: E402
import parsing  # noqa: E402


def test_shift_times():
    d = date(2026, 8, 28)  # пятница
    assert domain.shift_start(d, 1).hour == 6
    assert domain.shift_end(d, 1).hour == 14
    assert domain.shift_start(d, 3) == datetime(2026, 8, 28, 22, 0, tzinfo=config.TZ)
    assert domain.shift_end(d, 3) == datetime(2026, 8, 29, 6, 0, tzinfo=config.TZ)
    # напоминание за 2 часа
    assert domain.remind_at(d, 2) == datetime(2026, 8, 28, 12, 0, tzinfo=config.TZ)


def test_rates():
    assert domain.shift(1).pay == Decimal("265.20")   # 33.15 * 8
    assert domain.shift(2).pay == Decimal("265.20")
    assert domain.shift(3).pay == Decimal("281.60")   # 35.20 * 8


def test_allowed_weekdays():
    monday = date(2026, 8, 31)
    sunday = date(2026, 8, 30)
    saturday = date(2026, 8, 29)
    friday = date(2026, 8, 28)
    assert domain.is_allowed_weekday(friday, 1) is True
    assert domain.is_allowed_weekday(saturday, 1) is False
    assert domain.is_allowed_weekday(sunday, 3) is True     # ночная с Вс на Пн
    assert domain.is_allowed_weekday(friday, 3) is False    # ночные только Вс–Чт
    assert domain.is_allowed_weekday(monday, 3) is True


def test_week_days():
    monday = date(2026, 8, 31)
    assert domain.week_days(monday, 1) == [date(2026, 8, 31 + i) if i == 0 else
                                           date(2026, 9, i) for i in range(5)]
    nights = domain.week_days(monday, 3)
    assert nights[0] == date(2026, 8, 30)  # воскресенье
    assert len(nights) == 5
    assert nights[-1] == date(2026, 9, 3)  # четверг


def test_planning_monday(monkeypatch=None):
    # planning_monday зависит от «сегодня», поэтому подменяем домен на фиксированные даты
    real_today = domain.today
    try:
        for fake, expected in [
            (date(2026, 8, 26), date(2026, 8, 24)),  # среда -> текущая неделя
            (date(2026, 8, 28), date(2026, 8, 24)),  # пятница -> текущая
            (date(2026, 8, 29), date(2026, 8, 31)),  # суббота -> следующая
            (date(2026, 8, 30), date(2026, 8, 31)),  # воскресенье -> следующая
            (date(2026, 8, 31), date(2026, 8, 31)),  # понедельник -> своя
        ]:
            domain.today = lambda f=fake: f
            assert domain.planning_monday() == expected, fake
            # current_monday в воскресенье тоже смотрит вперёд (ночная с Вс на Пн)
            if fake.weekday() == 6:
                assert domain.current_monday() == expected
    finally:
        domain.today = real_today


def test_period_and_payout():
    # период августа: 2 августа – 1 сентября
    assert domain.period_bounds(2026, 8) == (date(2026, 8, 2), date(2026, 9, 1))
    assert domain.period_anchor(date(2026, 8, 2)) == (2026, 8)
    assert domain.period_anchor(date(2026, 9, 1)) == (2026, 8)   # 1-е — ещё август
    assert domain.period_anchor(date(2026, 9, 2)) == (2026, 9)
    assert domain.period_anchor(date(2026, 1, 1)) == (2025, 12)
    # выплата за август — 17 сентября 2026 (четверг)
    assert domain.payout_date(2026, 8) == date(2026, 9, 17)
    # 17 января 2026 — суббота, значит переносится на понедельник 19-го
    assert domain.payout_date(2025, 12) == date(2026, 1, 19)
    # 17 мая 2026 — воскресенье → 18 мая
    assert domain.payout_date(2026, 4) == date(2026, 5, 18)
    # декабрь -> январь следующего года
    assert domain.period_bounds(2026, 12) == (date(2026, 12, 2), date(2027, 1, 1))
    assert domain.payout_date(2026, 12) == date(2027, 1, 18)  # 17.01.2027 — Вс


def test_money_format():
    assert domain.money(Decimal("265.2")) == f"265,20 {config.CURRENCY}"
    assert domain.money(Decimal("4236.8")) == f"4 236,80 {config.CURRENCY}"
    assert domain.money(Decimal("0")) == f"0,00 {config.CURRENCY}"


def test_parser():
    entries, errors = parsing.parse_entries("5.08 1, 12 3\n10-12.08 2", 2026, 8)
    assert errors == []
    assert (date(2026, 8, 5), 1) in entries
    assert (date(2026, 8, 12), 3) in entries
    assert (date(2026, 8, 10), 2) in entries and (date(2026, 8, 12), 2) in entries
    assert len([e for e in entries if e[1] == 2]) == 3

    entries, errors = parsing.parse_entries("32.08 1; абракадабра; 4.08 1я", 2026, 8)
    assert (date(2026, 8, 4), 1) in entries
    assert len(errors) == 2

    entries, _ = parsing.parse_entries("1.09.2026 3", 2026, 8)
    assert entries == [(date(2026, 9, 1), 3)]

    # дубли схлопываются
    entries, _ = parsing.parse_entries("5.08 1, 05.08 1", 2026, 8)
    assert entries == [(date(2026, 8, 5), 1)]


def test_db_roundtrip():
    db.init()
    uid = 777
    db.ensure_user(uid)
    db.ensure_user(uid)  # идемпотентно
    assert db.add_shift(uid, date(2026, 8, 3), 1) == "added"
    assert db.add_shift(uid, date(2026, 8, 3), 1) == "exists"
    assert db.add_shift(uid, date(2026, 8, 3), 3) == "added"

    shifts = db.shifts_in_range(uid, date(2026, 8, 1), date(2026, 8, 31))
    assert len(shifts) == 2

    s = shifts[0]
    cancelled = db.cancel_shift(s.id, uid)
    assert cancelled.cancelled
    assert len(db.shifts_in_range(uid, date(2026, 8, 1), date(2026, 8, 31))) == 1
    assert len(db.shifts_in_range(uid, date(2026, 8, 1), date(2026, 8, 31),
                                  include_cancelled=True)) == 2
    assert db.add_shift(uid, s.work_date, s.shift_num) == "restored"

    # чужие смены недоступны
    assert db.get_shift(s.id, 999) is None
    assert db.cancel_shift(s.id, 999) is None

    assert db.delete_shift(uid, date(2026, 8, 3), 3) is True
    assert db.delete_shift(uid, date(2026, 8, 3), 3) is False
    # тесты делят одну базу, поэтому проверяем только своего пользователя
    assert uid in db.users_to_ask()
    db.set_weekly_ask(uid, False)
    assert uid not in db.users_to_ask()


def test_pay_by_hours():
    # отпустили раньше — платят по факту
    assert domain.pay_for_hours(1, Decimal("8")) == Decimal("265.20")
    assert domain.pay_for_hours(1, Decimal("6")) == Decimal("198.90")
    assert domain.pay_for_hours(1, Decimal("6.5")) == Decimal("215.48")   # 33.15*6.5
    assert domain.pay_for_hours(3, Decimal("4")) == Decimal("140.80")     # ночная 35.20*4
    assert domain.pay_for_hours(2, Decimal("0")) == Decimal("0.00")
    assert domain.fmt_hours(Decimal("8")) == "8 ч"
    assert domain.fmt_hours(Decimal("6.5")) == "6,5 ч"


def test_confirm_shift():
    db.init()
    uid = 555
    db.ensure_user(uid)
    d = date(2026, 8, 10)
    db.add_shift(uid, d, 1)
    s = [x for x in db.shifts_in_range(uid, d, d)][0]
    # до подтверждения смена считается полной
    assert s.hours == Decimal("8") and s.pay == Decimal("265.20")
    assert s.confirmed is False

    # отпустили после 6 часов
    got = db.confirm_shift(s.id, uid, Decimal("6"))
    assert got.status == "done" and got.hours == Decimal("6")
    assert got.pay == Decimal("198.90")
    assert got.confirmed is True

    # «не был» — денег нет, в расчёт не идёт
    db.add_shift(uid, date(2026, 8, 11), 1)
    s2 = [x for x in db.shifts_in_range(uid, date(2026, 8, 11), date(2026, 8, 11))][0]
    got2 = db.confirm_shift(s2.id, uid, None)
    assert got2.status == "absent" and got2.pay == Decimal("0.00")
    assert got2.cancelled is True

    # чужую смену подтвердить нельзя
    assert db.confirm_shift(s.id, 999, Decimal("8")) is None


def test_awaiting_ask():
    db.init()
    uid = 666
    db.ensure_user(uid)
    d = date(2026, 8, 12)
    db.add_shift(uid, d, 1)          # смена 06:00-14:00
    mine = lambda moment: [s for s in db.shifts_awaiting_ask(moment) if s.user_id == uid]
    # смена ещё идёт — не спрашиваем
    assert mine(datetime(2026, 8, 12, 13, 0, tzinfo=config.TZ)) == []
    # сразу после конца ещё рано (ждём 15 минут)
    assert mine(datetime(2026, 8, 12, 14, 5, tzinfo=config.TZ)) == []
    # через 20 минут — пора
    ready = mine(datetime(2026, 8, 12, 14, 20, tzinfo=config.TZ))
    assert len(ready) == 1
    # спросили один раз — больше не спрашиваем
    db.mark_confirm_asked(ready[0].id)
    assert mine(datetime(2026, 8, 12, 15, 0, tzinfo=config.TZ)) == []
    # и она попадает в список неподтверждённых для /confirm
    assert [s.id for s in db.unconfirmed_shifts(uid)] == [ready[0].id]


def test_migration_from_old_db():
    """База, созданная прошлой версией бота, доживает до новой схемы без потерь."""
    import sqlite3, tempfile, os as _os
    path = _os.path.join(tempfile.mkdtemp(), "old.db")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE users (user_id INTEGER PRIMARY KEY, weekly_ask INTEGER NOT NULL DEFAULT 1,
                            created_at TEXT NOT NULL);
        CREATE TABLE shifts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                             work_date TEXT NOT NULL, shift_num INTEGER NOT NULL,
                             status TEXT NOT NULL DEFAULT 'planned',
                             reminded INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                             UNIQUE (user_id, work_date, shift_num));
        INSERT INTO users VALUES (42, 1, '2026-08-01T00:00:00');
        INSERT INTO shifts (user_id, work_date, shift_num, created_at)
             VALUES (42, '2026-08-05', 2, '2026-08-01T00:00:00');
    """)
    con.commit(); con.close()

    old_path = config.DB_PATH
    try:
        config.DB_PATH = pathlib.Path(path)
        db.init()
        rows = db.shifts_in_range(42, date(2026, 8, 1), date(2026, 8, 31))
        assert len(rows) == 1, rows
        assert rows[0].worked_hours is None and rows[0].confirm_asked is False
        assert rows[0].pay == Decimal("265.20")   # старые смены считаются полными
        assert db.confirm_shift(rows[0].id, 42, Decimal("5")).pay == Decimal("165.75")
    finally:
        config.DB_PATH = old_path
        db.init()


def test_confirmed_shift_stays_visible():
    """Подтверждённая смена не должна исчезать из выборок — это отработанная смена."""
    db.init()
    uid = 444
    db.ensure_user(uid)
    d = date(2026, 8, 18)
    db.add_shift(uid, d, 1)
    sid = db.shifts_in_range(uid, d, d)[0].id
    db.confirm_shift(sid, uid, Decimal("6"))
    visible = db.shifts_in_range(uid, d, d)
    assert len(visible) == 1 and visible[0].status == "done"
    assert visible[0].pay == Decimal("198.90")
    # а прогул и отмена — не показываются
    db.add_shift(uid, date(2026, 8, 19), 1)
    sid2 = db.shifts_in_range(uid, date(2026, 8, 19), date(2026, 8, 19))[0].id
    db.confirm_shift(sid2, uid, None)
    assert db.shifts_in_range(uid, date(2026, 8, 19), date(2026, 8, 19)) == []
    assert len(db.shifts_in_range(uid, date(2026, 8, 19), date(2026, 8, 19),
                                  include_cancelled=True)) == 1


def test_due_reminders():
    db.init()
    uid = 888
    db.ensure_user(uid)
    d = date(2026, 8, 28)
    db.add_shift(uid, d, 2)  # смена 14:00–22:00, напоминание в 12:00
    mine = [s for s in db.due_reminders(datetime(2026, 8, 28, 12, 1, tzinfo=config.TZ))
            if s.user_id == uid]
    assert len(mine) == 1
    early = [s for s in db.due_reminders(datetime(2026, 8, 28, 11, 0, tzinfo=config.TZ))
             if s.user_id == uid]
    assert early == []
    late = [s for s in db.due_reminders(datetime(2026, 8, 28, 14, 30, tzinfo=config.TZ))
            if s.user_id == uid]
    assert late == []   # смена уже началась — напоминать поздно

    db.mark_reminded(mine[0].id)
    again = [s for s in db.due_reminders(datetime(2026, 8, 28, 12, 1, tzinfo=config.TZ))
             if s.user_id == uid]
    assert again == []  # повторно не напоминаем


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
