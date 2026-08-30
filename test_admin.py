"""Тесты админки: пароль, защита от перебора, сессии и статистика.

Запуск: .venv/bin/python test_admin.py
"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from decimal import Decimal

os.environ.setdefault("SHIFTBOT_DB", os.path.join(tempfile.mkdtemp(), "adm.db"))
os.environ.setdefault("SHIFTBOT_ADMIN_PASSWORD", "тайна-42")

import admin  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import domain  # noqa: E402

db.init()

_next_uid = iter(range(800_001, 899_999))


def user() -> int:
    uid = next(_next_uid)
    db.ensure_user(uid)
    return uid


def worked(uid: int, days_ago: int, num: int, hours: Decimal | None = None) -> None:
    """Отработанная смена в прошлом — сразу подтверждённая."""
    d = domain.today() - timedelta(days=days_ago)
    db.add_shift(uid, d, num)
    shift = next(s for s in db.shifts_in_range(uid, d, d) if s.shift_num == num)
    db.confirm_shift(shift.id, uid, config.SHIFT_HOURS if hours is None else hours)


# --- Пароль ---------------------------------------------------------------

def test_password_plain():
    assert admin.configured()
    assert admin.password_ok("тайна-42")          # кириллица не должна ломать сравнение
    assert admin.password_ok("  тайна-42  ")      # пробелы по краям обрезаем
    assert not admin.password_ok("тайна-43")
    assert not admin.password_ok("")


def test_password_sha256_wins():
    """Если задан хэш, открытый пароль из настроек уже не подходит."""
    original = config.ADMIN_PASSWORD_SHA256
    config.ADMIN_PASSWORD_SHA256 = admin._sha256("другой")
    try:
        assert admin.password_ok("другой")
        assert not admin.password_ok("тайна-42")
    finally:
        config.ADMIN_PASSWORD_SHA256 = original


def test_disabled_without_password():
    original = (config.ADMIN_PASSWORD, config.ADMIN_PASSWORD_SHA256)
    config.ADMIN_PASSWORD, config.ADMIN_PASSWORD_SHA256 = "", ""
    try:
        assert not admin.configured()
        assert not admin.password_ok("тайна-42")   # выключенная админка не пускает никого
        assert not admin.is_admin(user())
    finally:
        config.ADMIN_PASSWORD, config.ADMIN_PASSWORD_SHA256 = original


def test_whitelist():
    uid = user()
    original = config.ADMIN_IDS
    config.ADMIN_IDS = frozenset({uid})
    try:
        assert admin.allowed(uid)
        assert not admin.allowed(uid + 1)
        admin.login(uid + 1)                       # вход есть, но id не в списке
        assert not admin.is_admin(uid + 1)
    finally:
        config.ADMIN_IDS = original
        admin.logout(uid + 1)


def test_whitelist_empty_allows_everyone():
    assert config.ADMIN_IDS == frozenset()
    assert admin.allowed(12345)


# --- Перебор --------------------------------------------------------------

def test_lockout_after_attempts():
    uid = user()
    for i in range(config.ADMIN_MAX_ATTEMPTS - 1):
        left = admin.note_failure(uid)
        assert left == config.ADMIN_MAX_ATTEMPTS - 1 - i
        assert admin.lock_left(uid) is None
    assert admin.note_failure(uid) == 0
    left = admin.lock_left(uid)
    assert left is not None and left <= config.ADMIN_LOCKOUT


def test_login_resets_attempts():
    uid = user()
    admin.note_failure(uid)
    admin.note_failure(uid)
    admin.login(uid)
    assert admin.lock_left(uid) is None
    assert admin._attempts.get(uid) is None
    admin.logout(uid)


# --- Сессии ---------------------------------------------------------------

def test_session_lifecycle():
    uid = user()
    assert not admin.is_admin(uid)
    admin.login(uid)
    assert admin.is_admin(uid)
    assert timedelta(0) < admin.session_left(uid) <= config.ADMIN_SESSION
    admin.logout(uid)
    assert not admin.is_admin(uid)
    assert admin.session_left(uid) == timedelta(0)


def test_session_expires():
    uid = user()
    admin.login(uid)
    original = config.ADMIN_SESSION
    config.ADMIN_SESSION = timedelta(seconds=-1)   # как будто срок уже вышел
    try:
        assert not admin.is_admin(uid)
        assert db.admin_granted_at(uid) is None    # просроченную сессию сразу убираем
    finally:
        config.ADMIN_SESSION = original


# --- Статистика -----------------------------------------------------------

def test_collect_counts_shifts_and_money():
    uid = user()
    worked(uid, 3, 1)
    worked(uid, 2, 3, Decimal("6.5"))
    db.add_shift(uid, domain.today() + timedelta(days=4), 2)
    db.add_penalty(uid, domain.today() - timedelta(days=2), "late")

    stat = admin.find(admin.collect(), uid)
    assert stat is not None
    assert len(stat.shifts) == 3
    assert len(stat.done) == 2 and len(stat.planned) == 1 and stat.unconfirmed == []
    assert stat.hours == Decimal("14.5")           # 8 + 6,5, запланированная не в счёт
    assert stat.earned == (
        domain.pay_for_hours(1, Decimal(8)) + domain.pay_for_hours(3, Decimal("6.5"))
    )
    assert stat.penalty_total == domain.penalty_amount("late")
    assert stat.last_seen is not None


def test_past_shift_without_confirmation_counts_as_money():
    """Прошедшая, но неподтверждённая смена идёт по 8 ч — как и в /money."""
    uid = user()
    db.add_shift(uid, domain.today() - timedelta(days=4), 1)   # прошла, не подтверждена
    db.add_shift(uid, domain.today() + timedelta(days=4), 1)   # ещё впереди

    stat = admin.find(admin.collect(), uid)
    assert stat.done == []
    assert len(stat.unconfirmed) == 1 and len(stat.planned) == 1
    assert stat.hours == config.SHIFT_HOURS
    assert stat.earned == domain.pay_for_hours(1, config.SHIFT_HOURS)

    # то же самое, что насчитает /money за период этой смены
    import reports
    year, month = domain.period_anchor(domain.today() - timedelta(days=4))
    period = reports.period_data(uid, year, month)
    assert period.earned >= stat.earned


def test_absent_and_cancelled_split():
    uid = user()
    d = domain.today() - timedelta(days=5)
    db.add_shift(uid, d, 1)
    shift = db.shifts_in_range(uid, d, d)[0]
    db.confirm_shift(shift.id, uid, None)          # не был

    other = domain.today() + timedelta(days=2)
    db.add_shift(uid, other, 2)
    db.cancel_shift(db.shifts_in_range(uid, other, other)[0].id, uid)

    stat = admin.find(admin.collect(), uid)
    assert len(stat.absent) == 1 and len(stat.cancelled) == 1
    assert stat.done == [] and stat.unconfirmed == []
    assert stat.hours == Decimal(0) and stat.earned == Decimal(0)


# --- Имена пользователей --------------------------------------------------

def test_display_name_prefers_username():
    uid = user()
    db.remember_profile(uid, "oleksii", "Олексій", "Романів")
    u = admin.find(admin.collect(), uid)
    assert u.name == "@oleksii"
    assert u.title == "Олексій Романів (@oleksii)"
    assert u.full_name == "Олексій Романів"


def test_display_name_falls_back_to_full_name():
    uid = user()
    db.remember_profile(uid, None, "Марта", None)
    u = admin.find(admin.collect(), uid)
    assert u.name == "Марта" and u.title == "Марта"


def test_display_name_falls_back_to_id():
    uid = user()                                   # профиля нет вовсе — старый пользователь
    u = admin.find(admin.collect(), uid)
    assert u.name == f"id {uid}" and u.full_name == ""


def test_name_is_html_escaped():
    """Имя приходит от пользователя, а сообщения уходят с parse_mode=HTML."""
    uid = user()
    db.remember_profile(uid, None, "<b>жирный</b> & <script>", None)
    users = admin.collect()
    u = admin.find(users, uid)
    assert "<b>" not in u.html_name and "&lt;b&gt;" in u.html_name
    card = admin.user_card(uid, users)
    assert "<script>" not in card and "&lt;script&gt;" in card


def test_profile_update_keeps_registration_date():
    uid = user()
    db.remember_profile(uid, "first_nick", "Имя", None)
    created = db.list_users()
    was = next(r.created_at for r in created if r.user_id == uid)
    db.remember_profile(uid, "second_nick", "Имя", None)
    now = next(r.created_at for r in db.list_users() if r.user_id == uid)
    assert now == was                              # дата регистрации не перезаписывается
    assert admin.find(admin.collect(), uid).name == "@second_nick"


def test_remember_profile_creates_missing_user():
    """Профиль может прийти раньше, чем сработает ensure_user."""
    uid = next(_next_uid)
    assert uid not in db.all_user_ids()
    db.remember_profile(uid, "novichok", "Новичок", None)
    assert uid in db.all_user_ids()
    assert admin.find(admin.collect(), uid).name == "@novichok"


def test_collect_sorted_by_activity():
    users = admin.collect()
    activity = [u.activity for u in users]
    assert activity == sorted(activity, reverse=True)


def test_reports_render():
    uid = user()
    worked(uid, 1, 2)
    users = admin.collect()

    overview = admin.overview_report(users)
    assert "Пользователей" in overview and domain.money(Decimal(0)) not in overview.split("\n")[0]

    listing = admin.users_report(users)
    assert str(uid) in listing or len(users) > config.ADMIN_USER_BUTTONS

    card = admin.user_card(uid, users)
    assert str(uid) in card
    assert domain.period_title(*domain.period_anchor(domain.today())) in card
    assert admin.user_card(1, users) == "Пользователь не найден."


def test_broadcast_recipients():
    uid = user()
    assert uid in db.all_user_ids()
    assert len(db.all_user_ids()) == len(set(db.all_user_ids()))


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
