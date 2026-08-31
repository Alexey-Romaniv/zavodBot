"""Тесты миграций: схема догоняется, данные не теряются, сбой откатывается.

Запуск: .venv/bin/python test_migrations.py
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("SHIFTBOT_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import config  # noqa: E402
import db  # noqa: E402
import migrations  # noqa: E402

# База времён первой версии бота: ни штрафов, ни достижений, ни настроек.
OLD_SCHEMA = """
CREATE TABLE users (user_id INTEGER PRIMARY KEY, weekly_ask INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL);
CREATE TABLE shifts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                     work_date TEXT NOT NULL, shift_num INTEGER NOT NULL,
                     status TEXT NOT NULL DEFAULT 'planned',
                     reminded INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                     UNIQUE (user_id, work_date, shift_num));
INSERT INTO users VALUES (42, 1, '2026-08-01T00:00:00');
INSERT INTO shifts (user_id, work_date, shift_num, status, created_at)
     VALUES (42, '2026-08-05', 2, 'planned', '2026-08-01T00:00:00'),
            (42, '2026-08-06', 3, 'done', '2026-08-01T00:00:00');
"""

ALL_TABLES = {
    "users", "shifts", "penalties", "periods", "achievements", "events",
    "admin_sessions", "payouts", "schema_migrations",
}


def fresh(name: str = "new.db") -> Path:
    return Path(tempfile.mkdtemp()) / name


def tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def test_fresh_database_gets_full_schema():
    path = fresh()
    conn = sqlite3.connect(path)
    assert migrations.is_empty(conn)
    applied = migrations.upgrade(conn, path)
    assert [m.version for m in applied] == [m.version for m in migrations.MIGRATIONS]
    assert migrations.version(conn) == migrations.TARGET
    assert ALL_TABLES <= tables(conn)
    # пустую базу бэкапить незачем — это первый запуск
    assert not (path.parent / "backups").exists()
    conn.close()


def test_old_database_keeps_data_and_gets_backup():
    path = fresh("old.db")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    assert migrations.version(conn) == 0

    applied = migrations.upgrade(conn, path)
    assert len(applied) == len(migrations.MIGRATIONS)
    assert migrations.version(conn) == migrations.TARGET

    # данные на месте, включая статусы смен
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    rows = conn.execute(
        "SELECT work_date, shift_num, status FROM shifts ORDER BY work_date"
    ).fetchall()
    assert rows == [("2026-08-05", 2, "planned"), ("2026-08-06", 3, "done")]

    # новые колонки получили значения по умолчанию, а не NULL там, где NOT NULL
    user = conn.execute(
        "SELECT weekly_ask, toxic, monday_plan, confirm_ping, period_news, leave_ping,"
        " quiet_from, evening_hour, muted_until FROM users"
    ).fetchone()
    assert user[:6] == (1, 1, 1, 1, 1, 1)
    assert user[6] is None and user[7] is None and user[8] is None
    assert conn.execute("SELECT leave_reminded FROM shifts").fetchone()[0] == 0

    # копия базы до миграции сохранена
    backups = list((path.parent / "backups").glob("old-before-v*.db"))
    assert len(backups) == 1, backups
    saved = sqlite3.connect(backups[0])
    assert saved.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] == 2
    assert "payouts" not in tables(saved)     # копия — именно та схема, что была до
    saved.close()
    conn.close()


def test_upgrade_is_idempotent():
    path = fresh()
    conn = sqlite3.connect(path)
    migrations.upgrade(conn, path)
    assert migrations.upgrade(conn, path) == []
    assert migrations.pending(conn) == []
    assert "актуальна" in migrations.describe(conn)
    # прогон шагов по второму разу ничего не ломает: они идемпотентны
    for m in migrations.MIGRATIONS:
        m.apply(conn)
    conn.commit()
    assert ALL_TABLES <= tables(conn)
    conn.close()


def test_history_records_every_step():
    path = fresh()
    conn = sqlite3.connect(path)
    migrations.upgrade(conn, path)
    rows = conn.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
    assert [r[0] for r in rows] == [m.version for m in migrations.MIGRATIONS]
    assert [r[1] for r in rows] == [m.name for m in migrations.MIGRATIONS]
    conn.close()


def test_failed_migration_rolls_back():
    """Сбой на шаге не должен оставить схему посередине."""
    path = fresh("broken.db")
    conn = sqlite3.connect(path)
    real = migrations.MIGRATIONS

    def boom(_conn):
        raise RuntimeError("нарочно")

    try:
        migrations.MIGRATIONS = real + (migrations.Migration(999, "битая", boom),)
        try:
            migrations.upgrade(conn, path)
        except RuntimeError:
            pass
        else:
            raise AssertionError("ошибка миграции должна пробрасываться наружу")
    finally:
        migrations.MIGRATIONS = real

    # рабочие шаги применились и зафиксированы, битый — нет
    assert migrations.version(conn) == real[-1].version
    assert conn.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE version = 999"
    ).fetchone()[0] == 0
    assert migrations.pending(conn) == []
    conn.close()


def test_partial_failure_keeps_previous_steps():
    """Если падает второй шаг, первый остаётся применённым — догонять можно с места."""
    path = fresh("partial.db")
    conn = sqlite3.connect(path)
    real = migrations.MIGRATIONS

    def boom(_conn):
        raise RuntimeError("нарочно")

    try:
        migrations.MIGRATIONS = (real[0], migrations.Migration(2, "битая", boom)) + real[1:]
        try:
            migrations.upgrade(conn, path)
        except RuntimeError:
            pass
        assert migrations.version(conn) == 1      # первый шаг зафиксирован
        assert "users" in tables(conn)
    finally:
        migrations.MIGRATIONS = real

    # с исправленным списком миграция догоняется до конца
    applied = migrations.upgrade(conn, path)
    assert [m.version for m in applied] == [m.version for m in real[1:]]
    assert migrations.version(conn) == migrations.TARGET
    conn.close()


def test_db_init_applies_migrations():
    """db.init() поднимает старую базу сам — бот на новой машине просто заработает."""
    path = fresh("init.db")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    conn.close()

    old_path = config.DB_PATH
    try:
        config.DB_PATH = path
        db.init()
        # старые смены читаются новым кодом и считаются полными по 8 ч
        shifts = db.shifts_in_range(42, date(2026, 8, 1), date(2026, 8, 31))
        assert len(shifts) == 2
        assert shifts[0].worked_hours is None and shifts[0].leave_reminded is False
        assert shifts[0].pay == Decimal("265.20")
        # и новые возможности сразу доступны
        prefs = db.prefs(42)
        assert prefs.monday_plan is True and prefs.quiet_from == config.QUIET_FROM
        db.set_payout(42, 2026, 8, Decimal("100"))
        assert db.get_payout(42, 2026, 8) == Decimal("100")
    finally:
        config.DB_PATH = old_path
        db.init()


def test_target_matches_last_migration():
    versions = [m.version for m in migrations.MIGRATIONS]
    assert versions == sorted(versions), "миграции должны идти по порядку"
    assert len(set(versions)) == len(versions), "номера миграций не должны повторяться"
    assert migrations.TARGET == versions[-1]


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
