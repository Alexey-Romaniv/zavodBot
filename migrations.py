"""Схема базы как список шагов. Только вперёд, без потери данных.

Правила, на которых всё держится:

1. **Ничего не удаляем.** Шаг может добавить таблицу, колонку или индекс,
   перенести данные — но не удалить то, где лежат чужие смены и штрафы.
2. **Каждый шаг идемпотентен**: `IF NOT EXISTS`, колонка добавляется только если
   её нет. Поэтому список можно смело прогонять по базе любого возраста —
   уже существующее просто не тронется.
3. **Версия хранится в самой базе** (`PRAGMA user_version`), а история
   применённых шагов — в таблице `schema_migrations`.
4. **Перед первым изменением делается бэкап** файла базы. Если шаг упадёт,
   транзакция откатится, а копия останется на всякий случай.

Как добавить изменение схемы: допиши `Migration` с очередным номером в конец
`MIGRATIONS` — и всё. Локально миграции применит `db.init()`, на сервере —
`python migrate.py` перед перезапуском бота.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


# --- Идемпотентные примитивы ----------------------------------------------

def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Добавить колонку, если её ещё нет. Данные существующих строк не трогаются:
    NOT NULL-колонки обязаны иметь DEFAULT, иначе SQLite откажется."""
    if not _table_exists(conn, table):
        return
    if column in _columns(conn, table):
        return
    log.info("Миграция: %s.%s", table, column)
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _script(sql: str) -> Callable[[sqlite3.Connection], None]:
    def run(conn: sqlite3.Connection) -> None:
        conn.executescript(sql)
    return run


def _columns_step(*specs: tuple[str, str, str]) -> Callable[[sqlite3.Connection], None]:
    def run(conn: sqlite3.Connection) -> None:
        for table, column, definition in specs:
            _add_column(conn, table, column, definition)
    return run


# --- Шаги -----------------------------------------------------------------

# 1. То, с чего бот начинался: пользователи и смены.
_V1 = _script("""
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    weekly_ask  INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS shifts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    work_date   TEXT    NOT NULL,                    -- ISO-дата выхода на смену
    shift_num   INTEGER NOT NULL,                    -- 1, 2 или 3
    status      TEXT    NOT NULL DEFAULT 'planned',  -- planned | done | absent | cancelled
    reminded    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    UNIQUE (user_id, work_date, shift_num)
);
CREATE INDEX IF NOT EXISTS idx_shifts_user_date ON shifts (user_id, work_date);
""")

# 2. Подтверждение смены: сколько часов зачли на самом деле.
_V2 = _columns_step(
    ("shifts", "worked_hours", "REAL"),
    ("shifts", "confirm_asked", "INTEGER NOT NULL DEFAULT 0"),
    ("shifts", "confirmed_at", "TEXT"),
)

# 3. Штрафы по договору и ручная отметка сниженной ставки за период.
_V3 = _script("""
CREATE TABLE IF NOT EXISTS penalties (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    at_date     TEXT    NOT NULL,   -- день нарушения: он определяет расчётный период
    kind        TEXT    NOT NULL,   -- absence | late | break | notice | other
    amount      REAL    NOT NULL,   -- сумма нетто, обычно из договора
    shift_id    INTEGER,            -- смена, если штраф привязан к ней
    note        TEXT,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_penalties_user_date ON penalties (user_id, at_date);
CREATE TABLE IF NOT EXISTS periods (
    user_id  INTEGER NOT NULL,
    ym       TEXT    NOT NULL,   -- YYYY-MM месяца-якоря периода
    rate_cut INTEGER,            -- снижена ли ставка: 1 | 0 | NULL (решаем сами)
    PRIMARY KEY (user_id, ym)
);
""")


# 4. Достижения, тон и журнал обращений, на котором держатся «мета»-ачивки.
def _v4(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS achievements (
        user_id     INTEGER NOT NULL,
        code        TEXT    NOT NULL,
        unlocked_at TEXT    NOT NULL,
        PRIMARY KEY (user_id, code)
    );
    CREATE TABLE IF NOT EXISTS events (
        user_id INTEGER NOT NULL,
        key     TEXT    NOT NULL,   -- money_view | ach_view | empty_view
        at      TEXT    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_events_user_key ON events (user_id, key);
    """)
    _add_column(conn, "users", "toxic", "INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "users", "ach_intro", "INTEGER NOT NULL DEFAULT 0")


# 5. Профиль пользователя для админки и сессии входа по паролю.
def _v5(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS admin_sessions (
        user_id    INTEGER PRIMARY KEY,
        granted_at TEXT    NOT NULL   -- когда ввели пароль; сессия живёт config.ADMIN_SESSION
    );
    """)
    for column in ("username", "first_name", "last_name"):
        _add_column(conn, "users", column, "TEXT")


# 6. Настройки уведомлений, второе напоминание и фактические выплаты.
def _v6(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS payouts (
        user_id INTEGER NOT NULL,
        ym      TEXT    NOT NULL,   -- YYYY-MM месяца-якоря периода
        amount  REAL    NOT NULL,   -- сколько реально пришло на счёт
        at      TEXT    NOT NULL,
        PRIMARY KEY (user_id, ym)
    );
    """)
    for column in ("monday_plan", "confirm_ping", "period_news", "leave_ping"):
        _add_column(conn, "users", column, "INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "users", "quiet_from", "TEXT")
    _add_column(conn, "users", "quiet_to", "TEXT")
    _add_column(conn, "users", "evening_hour", "INTEGER")
    _add_column(conn, "users", "muted_until", "TEXT")
    _add_column(conn, "shifts", "leave_reminded", "INTEGER NOT NULL DEFAULT 0")


# 7. Какую версию главного меню пользователь уже получил.
_V7 = _columns_step(
    ("users", "menu_version", "INTEGER NOT NULL DEFAULT 0"),
)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "пользователи и смены", _V1),
    Migration(2, "подтверждение смены и фактические часы", _V2),
    Migration(3, "штрафы и снижение ставки", _V3),
    Migration(4, "достижения, тон и журнал обращений", _v4),
    Migration(5, "профили пользователей и сессии админа", _v5),
    Migration(6, "настройки уведомлений, «пора выходить», выплаты", _v6),
    Migration(7, "версия главного меню у пользователя", _V7),
)

TARGET = MIGRATIONS[-1].version

HISTORY = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    applied_at TEXT    NOT NULL
);
"""


# --- Применение -----------------------------------------------------------

def version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def pending(conn: sqlite3.Connection) -> list[Migration]:
    have = version(conn)
    return [m for m in MIGRATIONS if m.version > have]


def is_empty(conn: sqlite3.Connection) -> bool:
    """Пустая база — значит бэкапить нечего, это первый запуск.

    Служебную `schema_migrations` не считаем: она появляется раньше самих шагов.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"
    ).fetchone()
    return row[0] == 0


def backup(conn: sqlite3.Connection, db_path: Path, tag: str) -> Path:
    """Снимок базы средствами SQLite: копия консистентна даже под нагрузкой."""
    folder = db_path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = folder / f"{db_path.stem}-{tag}-{stamp}.db"
    with sqlite3.connect(target) as copy:
        conn.backup(copy)
    log.info("Бэкап базы: %s", target)
    return target


def upgrade(conn: sqlite3.Connection, db_path: Path | None = None) -> list[Migration]:
    """Догнать схему до последней версии. Возвращает применённые шаги.

    Каждый шаг идёт своей транзакцией: упавший не оставит схему посередине,
    а уже применённые останутся применёнными.
    """
    empty = is_empty(conn)
    conn.executescript(HISTORY)
    todo = pending(conn)
    if not todo:
        return []

    if db_path is not None and not empty:
        # база с данными — перед изменением схемы забираем копию
        backup(conn, db_path, f"before-v{TARGET}")

    applied: list[Migration] = []
    for migration in todo:
        log.info("Применяю миграцию %s: %s", migration.version, migration.name)
        try:
            with conn:  # commit при успехе, rollback при исключении
                migration.apply(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(version) DO NOTHING",
                    (migration.version, migration.name, datetime.now().isoformat(timespec="seconds")),
                )
                conn.execute(f"PRAGMA user_version = {migration.version}")
        except Exception:
            log.exception("Миграция %s не применилась", migration.version)
            raise
        applied.append(migration)
    return applied


def describe(conn: sqlite3.Connection) -> str:
    """Строка для CLI и логов деплоя: где схема сейчас и что осталось."""
    have = version(conn)
    todo = pending(conn)
    if not todo:
        return f"схема актуальна: версия {have} из {TARGET}"
    names = ", ".join(f"{m.version} ({m.name})" for m in todo)
    return f"версия {have} из {TARGET}; не применено: {names}"
