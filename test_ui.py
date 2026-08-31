"""Тесты интерфейса: главное меню, кнопки вместо ввода, лимиты Telegram.

Запуск: .venv/bin/python test_ui.py
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, time, timedelta
from decimal import Decimal

os.environ.setdefault("SHIFTBOT_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import config  # noqa: E402
import db  # noqa: E402
import domain  # noqa: E402
import keyboards as kb  # noqa: E402
import texts  # noqa: E402

# Telegram отклоняет callback_data длиннее 64 байт и клавиатуры выше 100 рядов.
CB_LIMIT = 64
ROW_LIMIT = 100


def buttons(markup) -> list:
    return [b for row in markup.inline_keyboard for b in row]


def texts_of(markup) -> list[str]:
    return [b.text for b in buttons(markup)]


def check_limits(markup, name: str) -> None:
    assert len(markup.inline_keyboard) <= ROW_LIMIT, name
    for b in buttons(markup):
        size = len(b.callback_data.encode())
        assert size <= CB_LIMIT, f"{name}: {b.callback_data} — {size} байт"


def sample_user() -> int:
    db.init()
    uid = 7001
    db.ensure_user(uid)
    return uid


# --- Главное меню ---------------------------------------------------------

def test_main_menu_is_small_and_covers_everything():
    """Клавиатура ест экран, поэтому на ней только ежедневное, остальное — «Ещё»."""
    menu = kb.main_menu()
    rows = [[b.text for b in row] for row in menu.keyboard]
    flat = [text for row in rows for text in row]
    assert flat == [kb.BTN_WEEK, kb.BTN_MY, kb.BTN_MONEY, kb.BTN_MORE], flat
    assert menu.resize_keyboard and menu.is_persistent
    # не больше двух рядов по две кнопки: иначе меню закрывает переписку
    assert len(rows) <= 2 and all(len(row) <= 2 for row in rows), rows


def test_more_menu_has_everything_left_off_the_keyboard():
    """Всё, что делает бот, доступно кнопкой — команды набирать не нужно."""
    markup = kb.more_menu()
    check_limits(markup, "more")
    labels = texts_of(markup)
    for expected in (kb.BTN_PENALTY, kb.BTN_ACH, kb.BTN_MANUAL,
                     kb.BTN_REPORT, kb.BTN_SETTINGS):
        assert expected in labels, expected
    actions = {b.callback_data.split(":")[1] for b in buttons(markup)}
    assert "close" in actions


def test_old_button_labels_still_accepted():
    """Клавиатура живёт в клиенте: подписи прошлой версии должны работать."""
    assert "✍️ Вписать вручную" in kb.TXT_MANUAL
    assert kb.BTN_MANUAL in kb.TXT_MANUAL


def test_pref_labels_match_database_flags():
    """Каждый тумблер в меню настроек — существующее поле в базе."""
    flags = {flag for flag, *_ in texts.PREF_LABELS}
    assert flags == set(db.PREF_FLAGS)


def test_menu_version_is_delivered_once():
    """Устаревшую клавиатуру досылаем один раз, а не при каждом сообщении."""
    uid = sample_user()
    assert db.menu_version(uid) == 0          # новый пользователь ещё не видел меню
    assert db.menu_version(uid) < kb.MENU_VERSION
    db.set_menu_version(uid, kb.MENU_VERSION)
    assert db.menu_version(uid) == kb.MENU_VERSION
    # соседа это не касается
    other = 7002
    db.ensure_user(other)
    assert db.menu_version(other) == 0


# --- Настройки ------------------------------------------------------------

def test_settings_menu_has_toggle_for_every_flag():
    uid = sample_user()
    markup = kb.settings_menu(db.prefs(uid))
    check_limits(markup, "settings")
    args = {b.callback_data.split(":")[-1] for b in buttons(markup)}
    for flag in db.PREF_FLAGS:
        assert flag in args, flag
    labels = texts_of(markup)
    assert any("Тихие часы" in t for t in labels)
    assert any("Тишина до конца дня" in t for t in labels)


def test_settings_menu_shows_mute_state():
    uid = sample_user()
    db.set_muted_until(uid, domain.now() + timedelta(hours=2))
    labels = texts_of(kb.settings_menu(db.prefs(uid)))
    assert any("Снять тишину" in t for t in labels)
    assert not any("Тишина до конца дня" in t for t in labels)
    db.set_muted_until(uid, None)


def test_quiet_choice_marks_current():
    uid = sample_user()
    db.set_quiet(uid, time(23, 0), time(7, 0))
    labels = texts_of(kb.quiet_choice(db.prefs(uid)))
    assert "• 23:00–07:00" in labels
    assert "Без тихих часов" in labels
    db.set_quiet(uid, time(0, 0), time(0, 0))
    assert "• Без тихих часов" in texts_of(kb.quiet_choice(db.prefs(uid)))
    db.set_quiet(uid, None, None)


# --- Часы кнопками --------------------------------------------------------

def test_hours_keyboards_cover_halves():
    """«6,5 ч» должно набираться кнопкой, а не текстом."""
    whole = kb.confirm_hours(1, with_full=True)
    check_limits(whole, "confirm_hours")
    assert any("половинками" in t for t in texts_of(whole))
    assert not any("Другое" in t for t in texts_of(whole)), "ввода часов быть не должно"

    halves = kb.confirm_halves(1)
    check_limits(halves, "confirm_halves")
    labels = texts_of(halves)
    assert "0,5 ч" in labels and "6,5 ч" in labels and "7,5 ч" in labels
    # полные часы — отдельной кнопкой, а не в общей сетке
    assert any(domain.fmt_hours(config.SHIFT_HOURS) in t for t in labels)


# --- Вписать смены задним числом ------------------------------------------

def test_manual_flow_is_all_buttons():
    uid = sample_user()
    choice = kb.manual_shift_choice(2026, 8)
    check_limits(choice, "manual_shift_choice")
    labels = texts_of(choice)
    assert any("1я смена" in t for t in labels)
    assert any("Июль" in t for t in labels) and any("Сентябрь" in t for t in labels)

    picked = {date(2026, 8, 3), date(2026, 8, 15)}
    days = kb.manual_days(2026, 8, 1, picked)
    check_limits(days, "manual_days")
    labels = texts_of(days)
    assert "✅3 Пн" in labels and "✅15 Сб" in labels
    assert "4 Вт" in labels
    # в августе 31 день — все на клавиатуре, вместе с кнопками сохранения
    assert sum(1 for t in labels if t[0].isdigit() or t.startswith("✅")) == 31
    assert any("Сохранить (2)" in t for t in labels)


def test_manual_days_offers_weekends():
    """Задним числом важно записать что было: смены случаются и в выходные."""
    labels = texts_of(kb.manual_days(2026, 8, 1, set()))
    assert any(t.endswith("Сб") for t in labels)
    assert any(t.endswith("Вс") for t in labels)


# --- Штрафы кнопками ------------------------------------------------------

def test_penalty_kinds_and_days():
    kinds = kb.penalty_kinds("2026-08")
    check_limits(kinds, "penalty_kinds")
    labels = texts_of(kinds)
    assert any("Прогул" in t for t in labels)
    assert any("Другой день" in t for t in labels)
    assert any("Своя сумма" in t for t in labels)

    days = kb.penalty_days(2026, 8)
    check_limits(days, "penalty_days")
    # период со 2 августа по 1 сентября — 31 день
    day_buttons = [b for b in buttons(days) if "day_set" in b.callback_data]
    assert len(day_buttons) == 31
    assert day_buttons[0].callback_data.endswith("2026-08-02")
    assert day_buttons[-1].callback_data.endswith("2026-09-01")

    # выбранная дата уезжает в кнопку вида нарушения — вводить её не нужно
    dated = kb.penalty_kinds("2026-08", date(2026, 9, 1))
    check_limits(dated, "penalty_kinds_dated")
    assert any(b.callback_data.endswith("absence|2026-09-01") for b in buttons(dated))
    assert not any("Другой день" in t for t in texts_of(dated))


# --- Деньги и выгрузка ----------------------------------------------------

def test_money_nav_switches_on_payout():
    uid = sample_user()
    db.add_shift(uid, date(2026, 8, 3), 1)
    markup = kb.money_nav(uid, 2026, 8)
    check_limits(markup, "money_nav")
    labels = texts_of(markup)
    assert any("Пришла выплата" in t for t in labels)
    assert any("Все смены списком" in t for t in labels)
    assert any("Выгрузить в таблицу" in t for t in labels)

    db.set_payout(uid, 2026, 8, Decimal("4520"))
    labels = texts_of(kb.money_nav(uid, 2026, 8))
    assert any("Сверка:" in t for t in labels)
    assert any("Изменить сумму" in t for t in labels)
    assert any("Убрать" in t for t in labels)
    db.delete_payout(uid, 2026, 8)


def test_export_done_keyboard():
    markup = kb.export_done(2026, 8)
    check_limits(markup, "export_done")
    assert any("Показать в чате" in t for t in texts_of(markup))


# --- Неделя ---------------------------------------------------------------

def test_week_choice_can_prefer_next_week():
    monday = domain.monday_of(domain.today())
    nxt = monday + timedelta(days=7)
    first = texts_of(kb.week_choice(nxt))[0]
    assert domain.week_label(nxt) in first
    # без подсказки первой идёт та неделя, которую логично планировать сейчас
    plain = texts_of(kb.week_choice())[0]
    assert domain.week_label(domain.planning_monday()) in plain


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
