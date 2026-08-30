"""Тесты системы достижений. Запуск: python test_achievements.py"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal

os.environ.setdefault("SHIFTBOT_DB", os.path.join(tempfile.mkdtemp(), "ach.db"))

import achievements as ach  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import domain  # noqa: E402

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=config.TZ)  # воскресенье
NO_EVENTS: dict[str, int] = {}

_next_id = iter(range(1, 10_000))


def make(work_date: date, num: int, status: str = "done", hours: float | None = 8.0,
         confirmed_at: datetime | None = None) -> db.Shift:
    """Смена для тестов. По умолчанию — подтверждённая полная."""
    return db.Shift(
        id=next(_next_id), user_id=1, work_date=work_date, shift_num=num,
        status=status, reminded=True,
        worked_hours=None if status != "done" else hours,
        confirm_asked=True, confirmed_at=confirmed_at,
    )


def stats(shifts, events=None, now=NOW) -> ach.Stats:
    return ach.build_stats(shifts, events or dict(NO_EVENTS), now)


# --- Базовый подсчёт ------------------------------------------------------

def test_worked_counts_past_only():
    past = make(date(2026, 8, 24), 1)                       # понедельник, прошла
    future = make(date(2026, 9, 7), 1, status="planned", hours=None)
    st = stats([past, future])
    assert st.worked == 1
    assert st.planned_ahead == 8   # 7 сентября — через 8 дней от 30 августа


def test_money_and_hours_use_actual_shift_rules():
    st = stats([
        make(date(2026, 8, 24), 1),                  # дневная 8 ч = 265,20
        make(date(2026, 8, 25), 3),                  # ночная 8 ч = 281,60
        make(date(2026, 8, 26), 2, hours=4.0),       # отпустили раньше
    ])
    assert st.hours == Decimal("20")
    assert st.money == int(Decimal("265.20") + Decimal("281.60") + Decimal("132.60"))
    assert st.early == 1
    assert st.nights == 1
    assert st.morning_shifts == 1


def test_unconfirmed_counted_but_still_worked():
    """Смена прошла, но часы не отмечены: в стаж идёт, в «неподтверждённые» тоже."""
    st = stats([make(date(2026, 8, 24), 1, status="planned", hours=None)])
    assert st.worked == 1
    assert st.unconfirmed == 1
    assert st.confirmed == 0
    assert st.hours == config.SHIFT_HOURS   # считаем по 8 ч, как и деньги


def test_cancelled_and_absent_are_not_work():
    st = stats([
        make(date(2026, 8, 24), 1, status="cancelled", hours=None),
        make(date(2026, 8, 25), 1, status="absent", hours=None),
    ])
    assert st.worked == 0
    assert st.cancelled == 1
    assert st.absent == 1
    assert st.money == 0


# --- Серии ----------------------------------------------------------------

def test_night_streak():
    # Вс–Чт подряд — максимум, который вообще возможен по графику
    nights = [make(date(2026, 8, 23) + timedelta(days=i), 3) for i in range(5)]
    assert stats(nights).night_streak == 5
    with_gap = [make(date(2026, 8, 23), 3), make(date(2026, 8, 26), 3)]
    assert stats(with_gap).night_streak == 1


def test_cancel_streak_counts_only_decided_shifts():
    shifts = [
        make(date(2026, 8, 17), 1),                                   # отработал
        make(date(2026, 8, 18), 1, status="cancelled", hours=None),
        make(date(2026, 8, 19), 1, status="absent", hours=None),
        make(date(2026, 8, 20), 1, status="cancelled", hours=None),
        make(date(2026, 8, 21), 1),                                   # серия прервалась
    ]
    assert stats(shifts).cancel_streak == 3


def test_week_streak_and_max_week():
    shifts = []
    for week in range(4):                                  # четыре недели подряд
        monday = date(2026, 8, 3) + timedelta(days=7 * week)
        shifts.append(make(monday, 1))
    shifts += [make(date(2026, 8, 4), 1), make(date(2026, 8, 5), 1),
               make(date(2026, 8, 6), 1), make(date(2026, 8, 7), 1)]
    st = stats(shifts)
    assert st.week_streak == 4
    assert st.max_week == 5


def test_night_sunday_belongs_to_next_week():
    """Ночная с воскресенья на понедельник — уже следующая рабочая неделя."""
    sunday = date(2026, 8, 23)
    assert ach.work_week(sunday, 3) == date(2026, 8, 24)
    assert ach.work_week(sunday, 1) == date(2026, 8, 17)   # дневная считается по календарю


def test_full_circle_week():
    monday = date(2026, 8, 24)
    shifts = [make(monday, 1), make(monday + timedelta(days=1), 2),
              make(monday + timedelta(days=2), 3)]
    assert stats(shifts).circle_weeks == 1
    assert stats(shifts[:2]).circle_weeks == 0


# --- Амбиции и перерывы ---------------------------------------------------

def test_ambitions_pair():
    """Записал пять смен на прошедшую неделю, вышел на две — обе ачивки пары."""
    monday = date(2026, 8, 17)
    shifts = [make(monday, 1), make(monday + timedelta(days=1), 1)]
    shifts += [make(monday + timedelta(days=i), 1, status="cancelled", hours=None)
               for i in (2, 3, 4)]
    st = stats(shifts)
    assert st.ambitious_weeks == 1
    assert st.failed_ambitions == 1


def test_idle_gaps():
    shifts = [make(date(2026, 7, 1), 1), make(date(2026, 7, 20), 1)]
    st = stats(shifts)
    assert st.max_idle == 41       # с 20 июля по 30 августа
    assert st.idle_now == 41
    assert stats([make(date(2026, 8, 28), 1)]).idle_now == 2


def test_confirm_speed():
    d = date(2026, 8, 24)
    end = domain.shift_end(d, 1)
    fast = make(d, 1, confirmed_at=end + timedelta(minutes=20))
    slow = make(date(2026, 8, 25), 1, confirmed_at=end + timedelta(days=9))
    st = stats([fast, slow])
    assert st.fast_confirms == 1
    assert st.slow_confirms == 1


def test_clean_period_needs_every_shift_confirmed():
    """Период 2 июля — 1 августа: закрыт, если ни одна смена не висит без ответа."""
    clean = [make(date(2026, 7, 6), 1), make(date(2026, 7, 7), 1, status="cancelled", hours=None)]
    assert stats(clean).clean_periods == 1
    dirty = clean + [make(date(2026, 7, 8), 1, status="planned", hours=None)]
    assert stats(dirty).clean_periods == 0


def test_calendar_rarities():
    st = stats([
        make(date(2026, 11, 13), 1),   # пятница, 13-е
        make(date(2025, 12, 31), 3),   # новогодняя ночь
    ], now=datetime(2026, 12, 1, 12, 0, tzinfo=config.TZ))
    assert st.friday13 == 1
    assert st.newyear_shifts == 1


def test_summer_counted_per_year():
    shifts = [make(date(2025, 7, 1) + timedelta(days=i), 1) for i in range(10)]
    shifts += [make(date(2026, 7, 1) + timedelta(days=i), 1) for i in range(20)]
    assert stats(shifts).summer_shifts == 20


# --- Выдача ---------------------------------------------------------------

def test_evaluate_unlocks_by_threshold():
    st = stats([make(date(2026, 8, 24), 1)])
    earned = ach.evaluate(st, set())
    assert "first" in earned
    assert "ten" not in earned
    assert "locked" not in earned      # 🚫 не открывается никогда


def test_collector_counts_other_achievements():
    st = ach.Stats(worked=250, nights=50, money=100000, absent=1, cancelled=5, early=10)
    earned = ach.evaluate(st, set())
    assert st.unlocked_count >= 10
    assert "collector" in earned


def test_impossible_achievement_never_unlocks():
    st = ach.Stats(worked=10_000, money=10_000_000)
    assert "locked" not in ach.evaluate(st, set())


def test_progress_is_capped_at_goal():
    st = ach.Stats(worked=500)
    assert ach.progress(st, ach.BY_CODE["hundred"]) == (100, 100)
    assert ach.progress(ach.Stats(worked=63), ach.BY_CODE["hundred"]) == (63, 100)


def test_catalog_is_consistent():
    codes = [a.code for a in ach.CATALOG]
    assert len(codes) == len(set(codes)), "дублирующиеся коды"
    fields = ach.Stats().__dict__
    for a in ach.CATALOG:
        assert a.metric in fields, f"{a.code}: нет метрики {a.metric}"
        assert a.goal > 0
        if a.soft_title:
            assert a.soft_note, f"{a.code}: мягкий заголовок без текста"


def test_achievement_titles_do_not_clash_with_ranks():
    """Ачивка и звание с одинаковым названием — в списке выглядит как ошибка."""
    ranks = {r[1].split(" ", 1)[1] for r in ach.RANKS}
    clashing = [a.title for a in ach.CATALOG if a.title in ranks]
    assert not clashing, clashing


def test_tone_switches_text():
    loh = ach.BY_CODE["loh"]
    assert loh.names(ach.TOXIC)[0] == "Капец ты лох"
    assert loh.names(ach.SOFT)[0] == "Не твой период"
    # у ачивки без мягкого варианта тон ничего не меняет
    first = ach.BY_CODE["first"]
    assert first.names(ach.SOFT) == first.names(ach.TOXIC)


# --- Звания и эквиваленты -------------------------------------------------

def test_ranks():
    """Звания — по продукции: чем выше, тем сложнее изделие."""
    assert ach.rank(Decimal(0))[0] == "🫥 Крошка"
    title, nxt, left = ach.rank(Decimal(50))
    assert title == "🧇 Вафля"
    assert (nxt, left) == ("👷 Умпа-лумпа", 50)
    assert ach.rank(Decimal(150))[0] == "👷 Умпа-лумпа"
    assert ach.rank(Decimal(5000))[1] is None    # выше Вонки некуда


def test_fun_equivalent_follows_season():
    """Зарплата считается в том, что завод как раз производит."""
    assert "адвент" in ach.fun_equivalent(Decimal("3000"), date(2026, 11, 10))
    assert "конфет" in ach.fun_equivalent(Decimal("3000"), date(2026, 2, 10))
    assert "пасхаль" in ach.fun_equivalent(Decimal("3000"), date(2026, 4, 10))
    assert "мармелад" in ach.fun_equivalent(Decimal("3000"), date(2026, 6, 10))
    assert "не хватает" in ach.fun_equivalent(Decimal("3"), date(2026, 11, 10))


# --- Конфетный цех --------------------------------------------------------

def test_work_streak_counts_calendar_days():
    """Ночная в понедельник и дневная во вторник — это две смены подряд."""
    shifts = [make(date(2026, 8, 24) + timedelta(days=i), 1) for i in range(5)]
    assert stats(shifts).work_streak == 5
    assert stats(shifts[:2] + shifts[3:]).work_streak == 2


def test_pretzel_needs_all_three_shifts_in_three_days():
    monday = date(2026, 8, 24)
    twisted = [make(monday, 1), make(monday + timedelta(days=1), 3),
               make(monday + timedelta(days=2), 2)]
    assert stats(twisted).pretzel == 1
    # те же три смены, но с разрывом — уже не крендель
    spread = [make(monday, 1), make(monday + timedelta(days=1), 3),
              make(monday + timedelta(days=4), 2)]
    assert stats(spread).pretzel == 0


def test_bird_milk_is_a_short_night():
    """🕊 «Птичье молоко» — ночная, с которой отпустили раньше."""
    assert stats([make(date(2026, 8, 24), 3, hours=5.0)]).bird_milk == 1
    assert stats([make(date(2026, 8, 24), 3)]).bird_milk == 0          # отработал полностью
    assert stats([make(date(2026, 8, 24), 2, hours=5.0)]).bird_milk == 0   # дневная не считается


def test_candy_seasons():
    advent = [make(date(2025, 11, 3) + timedelta(days=i), 1) for i in range(15)]
    easter = [make(date(2026, 3, 2) + timedelta(days=i), 1) for i in range(15)]
    holidays = [make(date(2025, 12, 24), 1), make(date(2025, 12, 5), 1),
                make(date(2026, 2, 12), 1)]
    st = stats(advent + easter + holidays, now=datetime(2026, 6, 1, 12, 0, tzinfo=config.TZ))
    assert st.advent_season == 15 + 2   # ноябрьские смены плюс 24-е и Mikołajki того же года
    assert st.easter_season == 15
    assert st.dec24 == 1
    assert st.mikolajki == 1
    assert st.valentine == 1


def test_penalty_free_periods_need_closed_period_with_shifts():
    """🛡 «Чистый лист» — закрытые периоды со сменами и без единого штрафа."""
    class FakePenalty:
        def __init__(self, at_date):
            self.at_date = at_date

    shifts = [make(date(2026, 5, 5), 1), make(date(2026, 6, 5), 1), make(date(2026, 7, 6), 1)]
    st = ach.build_stats(shifts, {}, NOW, penalties=[])
    assert st.penalty_free_periods == 3
    fined = ach.build_stats(shifts, {}, NOW, penalties=[FakePenalty(date(2026, 6, 20))])
    assert fined.penalty_free_periods == 2


# --- Хранение -------------------------------------------------------------

def test_sync_unlocks_and_can_take_away():
    db.init()
    uid = 777_001
    db.ensure_user(uid)
    # десять ночных за последний месяц — 🌙 «Сова» открывается
    for i in range(10):
        d = date(2026, 8, 2) + timedelta(days=i)
        db.add_shift(uid, d, 3)
        s = [x for x in db.all_shifts(uid) if x.work_date == d][0]
        db.confirm_shift(s.id, uid, config.SHIFT_HOURS)

    real_now = domain.now
    try:
        domain.now = lambda: NOW
        fresh, lost, st = ach.sync(uid)
        codes = {a.code for a in fresh}
        assert {"first", "ten", "owl"} <= codes
        assert st.nights_60d == 10

        # повторный вызов ничего не выдаёт заново
        again, _, _ = ach.sync(uid)
        assert again == []

        # прошло полгода — «Сова» теряется, а «Свой человек» остаётся навсегда
        domain.now = lambda: NOW + timedelta(days=180)
        fresh2, lost2, _ = ach.sync(uid)
        assert {a.code for a in lost2} == {"owl"}
        assert "ten" in db.unlocked_codes(uid)
        assert "owl" not in db.unlocked_codes(uid)
    finally:
        domain.now = real_now


def test_events_window():
    db.init()
    uid = 777_002
    db.ensure_user(uid)
    for _ in range(3):
        db.log_event(uid, "money_view")
    assert db.count_events(uid, "money_view") == 3
    assert db.count_events(uid, "money_view", since=domain.now() + timedelta(days=1)) == 0
    assert db.count_events(uid, "ach_view") == 0


def test_toxic_flag_defaults_to_on():
    db.init()
    uid = 777_003
    db.ensure_user(uid)
    assert db.toxic_enabled(uid) is True
    db.set_toxic(uid, False)
    assert db.toxic_enabled(uid) is False


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
