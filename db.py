"""Слой доступа к SQLite. Синхронный, под личный бот этого более чем достаточно.

Схема и её изменения — в migrations.py.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import config
import domain
import migrations
import notify

log = logging.getLogger(__name__)

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


@dataclass(frozen=True)
class Shift:
    id: int
    user_id: int
    work_date: date
    shift_num: int
    status: str
    reminded: bool
    worked_hours: float | None = None
    confirm_asked: bool = False
    confirmed_at: datetime | None = None
    leave_reminded: bool = False

    @property
    def kind(self) -> domain.ShiftKind:
        return domain.shift(self.shift_num)

    @property
    def cancelled(self) -> bool:
        return self.status in ("cancelled", "absent")

    @property
    def confirmed(self) -> bool:
        return self.status in ("done", "absent")

    @property
    def hours(self) -> Decimal:
        """Часы для расчёта: подтверждённые фактические либо плановые 8."""
        if self.status == "absent":
            return Decimal(0)
        if self.worked_hours is None:
            return config.SHIFT_HOURS
        return Decimal(str(self.worked_hours))

    @property
    def pay(self) -> Decimal:
        return domain.pay_for_hours(self.shift_num, self.hours)


@dataclass(frozen=True)
class Penalty:
    id: int
    user_id: int
    at_date: date
    kind: str
    amount: Decimal
    shift_id: int | None = None
    note: str | None = None

    @property
    def title(self) -> str:
        return domain.penalty(self.kind).title

    @property
    def icon(self) -> str:
        return domain.penalty(self.kind).icon


@dataclass(frozen=True)
class UserRow:
    """Строка из users — то, что админке нужно знать о пользователе самого по себе."""
    user_id: int
    weekly_ask: bool
    toxic: bool
    created_at: datetime | None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


def _row(r: sqlite3.Row) -> Shift:
    return Shift(
        id=r["id"],
        user_id=r["user_id"],
        work_date=date.fromisoformat(r["work_date"]),
        shift_num=r["shift_num"],
        status=r["status"],
        reminded=bool(r["reminded"]),
        worked_hours=r["worked_hours"],
        confirm_asked=bool(r["confirm_asked"]),
        confirmed_at=_dt(r["confirmed_at"]),
        leave_reminded=bool(r["leave_reminded"]),
    )


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _penalty(r: sqlite3.Row) -> Penalty:
    return Penalty(
        id=r["id"],
        user_id=r["user_id"],
        at_date=date.fromisoformat(r["at_date"]),
        kind=r["kind"],
        amount=Decimal(str(r["amount"])),
        shift_id=r["shift_id"],
        note=r["note"],
    )


def init() -> None:
    """Открыть базу и догнать схему до актуальной версии.

    Схема живёт в migrations.py: там же и бэкап перед изменениями. На сервере
    миграции прогоняются отдельной командой до перезапуска бота, здесь — на всякий
    случай, чтобы запуск на чистой машине не требовал лишних шагов.
    """
    global _conn
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        applied = migrations.upgrade(_conn, config.DB_PATH)
        if applied:
            log.info(
                "Схема обновлена до версии %s (%s)",
                migrations.TARGET, ", ".join(m.name for m in applied),
            )


def _c() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("db.init() не вызван")
    return _conn


def _now_iso() -> str:
    return domain.now().isoformat(timespec="seconds")


# --- Пользователи ---------------------------------------------------------

def ensure_user(user_id: int) -> None:
    with _lock:
        _c().execute(
            "INSERT INTO users (user_id, created_at) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO NOTHING",
            (user_id, _now_iso()),
        )
        _c().commit()


def remember_profile(
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> None:
    """Запомнить, как зовут пользователя: в админке имя понятнее, чем голый id.

    Зовётся на каждом апдейте — ник и имя в Telegram меняются в любой момент,
    и держать их свежими проще, чем гадать, когда именно они поменялись.
    """
    with _lock:
        _c().execute(
            "INSERT INTO users (user_id, username, first_name, last_name, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "username = excluded.username, "
            "first_name = excluded.first_name, "
            "last_name = excluded.last_name",
            (user_id, username, first_name, last_name, _now_iso()),
        )
        _c().commit()


def set_weekly_ask(user_id: int, enabled: bool) -> None:
    set_flag(user_id, "weekly_ask", enabled)


def weekly_ask_enabled(user_id: int) -> bool:
    with _lock:
        r = _c().execute("SELECT weekly_ask FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return bool(r["weekly_ask"]) if r else True


def users_to_ask() -> list[int]:
    return users_with("weekly_ask")


# --- Настройки уведомлений ------------------------------------------------

# Тумблеры «вкл/выкл» из таблицы users. Имена совпадают с полями notify.Prefs
# и с колонками — так /settings обходится одним обработчиком на все переключатели.
PREF_FLAGS = (
    "weekly_ask", "monday_plan", "confirm_ping", "period_news", "leave_ping", "toxic",
)

PREF_COLUMNS = (
    "user_id, weekly_ask, monday_plan, confirm_ping, period_news, leave_ping, toxic, "
    "quiet_from, quiet_to, evening_hour, muted_until"
)


def _hhmm(value: str | None) -> time | None:
    try:
        return time.fromisoformat(value) if value else None
    except ValueError:  # ручная правка базы не повод падать
        return None


def _prefs(r: sqlite3.Row) -> notify.Prefs:
    """Строка users -> Prefs. NULL в колонке означает «как в config»."""
    quiet_from = _hhmm(r["quiet_from"])
    quiet_to = _hhmm(r["quiet_to"])
    return notify.Prefs(
        user_id=r["user_id"],
        weekly_ask=bool(r["weekly_ask"]),
        monday_plan=bool(r["monday_plan"]),
        confirm_ping=bool(r["confirm_ping"]),
        period_news=bool(r["period_news"]),
        leave_ping=bool(r["leave_ping"]),
        toxic=bool(r["toxic"]),
        quiet_from=config.QUIET_FROM if quiet_from is None else quiet_from,
        quiet_to=config.QUIET_TO if quiet_to is None else quiet_to,
        evening_hour=(
            config.EVENING_HOUR if r["evening_hour"] is None else int(r["evening_hour"])
        ),
        muted_until=_dt(r["muted_until"]),
    )


def prefs(user_id: int) -> notify.Prefs:
    with _lock:
        r = _c().execute(
            f"SELECT {PREF_COLUMNS} FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return _prefs(r) if r else notify.Prefs(user_id=user_id)


def all_prefs() -> dict[int, notify.Prefs]:
    """Настройки всех пользователей сразу — планировщику дешевле одним запросом."""
    with _lock:
        rows = _c().execute(f"SELECT {PREF_COLUMNS} FROM users").fetchall()
    return {r["user_id"]: _prefs(r) for r in rows}


def set_flag(user_id: int, name: str, value: bool) -> None:
    if name not in PREF_FLAGS:
        raise ValueError(f"неизвестная настройка: {name}")
    with _lock:
        _c().execute(f"UPDATE users SET {name} = ? WHERE user_id = ?", (int(value), user_id))
        _c().commit()


def users_with(flag: str) -> list[int]:
    """Кому включено это уведомление."""
    if flag not in PREF_FLAGS:
        raise ValueError(f"неизвестная настройка: {flag}")
    with _lock:
        rows = _c().execute(f"SELECT user_id FROM users WHERE {flag} = 1").fetchall()
    return [r["user_id"] for r in rows]


def set_quiet(user_id: int, quiet_from: time | None, quiet_to: time | None) -> None:
    """Тихие часы. None/None — вернуться к значениям из config."""
    with _lock:
        _c().execute(
            "UPDATE users SET quiet_from = ?, quiet_to = ? WHERE user_id = ?",
            (
                quiet_from.isoformat(timespec="minutes") if quiet_from else None,
                quiet_to.isoformat(timespec="minutes") if quiet_to else None,
                user_id,
            ),
        )
        _c().commit()


def set_evening_hour(user_id: int, hour: int | None) -> None:
    with _lock:
        _c().execute(
            "UPDATE users SET evening_hour = ? WHERE user_id = ?", (hour, user_id)
        )
        _c().commit()


def set_muted_until(user_id: int, until: datetime | None) -> None:
    with _lock:
        _c().execute(
            "UPDATE users SET muted_until = ? WHERE user_id = ?",
            (until.isoformat(timespec="seconds") if until else None, user_id),
        )
        _c().commit()


# --- Смены ---------------------------------------------------------------

def add_shift(user_id: int, work_date: date, shift_num: int) -> str:
    """Вернёт 'added' — записали новую, 'restored' — вернули отменённую,
    'exists' — такая смена уже запланирована."""
    iso = work_date.isoformat()
    with _lock:
        cur = _c().execute(
            "SELECT id, status FROM shifts WHERE user_id = ? AND work_date = ? AND shift_num = ?",
            (user_id, iso, shift_num),
        ).fetchone()
        if cur is None:
            _c().execute(
                "INSERT INTO shifts (user_id, work_date, shift_num, created_at) VALUES (?, ?, ?, ?)",
                (user_id, iso, shift_num, _now_iso()),
            )
            result = "added"
        elif cur["status"] == "cancelled":
            _c().execute(
                "UPDATE shifts SET status = 'planned', reminded = 0, leave_reminded = 0 "
                "WHERE id = ?", (cur["id"],)
            )
            result = "restored"
        else:
            result = "exists"
        _c().commit()
    return result


def get_shift(shift_id: int, user_id: int) -> Shift | None:
    with _lock:
        r = _c().execute(
            "SELECT * FROM shifts WHERE id = ? AND user_id = ?", (shift_id, user_id)
        ).fetchone()
    return _row(r) if r else None


def cancel_shift(shift_id: int, user_id: int) -> Shift | None:
    with _lock:
        r = _c().execute(
            "SELECT * FROM shifts WHERE id = ? AND user_id = ?", (shift_id, user_id)
        ).fetchone()
        if r is None or r["status"] == "cancelled":
            return _row(r) if r else None
        _c().execute("UPDATE shifts SET status = 'cancelled' WHERE id = ?", (shift_id,))
        _c().commit()
        r = _c().execute("SELECT * FROM shifts WHERE id = ?", (shift_id,)).fetchone()
    return _row(r)


def delete_shift(user_id: int, work_date: date, shift_num: int) -> bool:
    with _lock:
        cur = _c().execute(
            "DELETE FROM shifts WHERE user_id = ? AND work_date = ? AND shift_num = ?",
            (user_id, work_date.isoformat(), shift_num),
        )
        _c().commit()
    return cur.rowcount > 0


ACTIVE_STATUSES = ("planned", "done")


def shifts_in_range(
    user_id: int, start: date, end: date, *, include_cancelled: bool = False
) -> list[Shift]:
    """Смены за период. По умолчанию без отменённых и прогулянных,
    но подтверждённые (status='done') входят — это отработанные смены."""
    sql = (
        "SELECT * FROM shifts WHERE user_id = ? AND work_date BETWEEN ? AND ? "
        + ("" if include_cancelled else "AND status IN ('planned', 'done') ")
        + "ORDER BY work_date, shift_num"
    )
    with _lock:
        rows = _c().execute(sql, (user_id, start.isoformat(), end.isoformat())).fetchall()
    return [_row(r) for r in rows]


def upcoming_shifts(user_id: int, limit: int = 20) -> list[Shift]:
    """Смены, которые ещё не начались (или идут прямо сейчас)."""
    with _lock:
        rows = _c().execute(
            "SELECT * FROM shifts WHERE user_id = ? AND status = 'planned' AND work_date >= ? "
            "ORDER BY work_date, shift_num LIMIT ?",
            (user_id, (domain.today()).isoformat(), limit),
        ).fetchall()
    shifts = [_row(r) for r in rows]
    n = domain.now()
    return [s for s in shifts if domain.shift_end(s.work_date, s.shift_num) > n]


def _pending_shifts(moment: datetime, column: str) -> list[Shift]:
    """Запланированные смены рядом с `moment`, по которым напоминание ещё не ушло.

    Окно с запасом в сутки: напоминание про утреннюю смену уезжает на вечер
    накануне, а пропущенное из-за простоя бота досылается позже.
    """
    with _lock:
        rows = _c().execute(
            f"SELECT * FROM shifts WHERE status = 'planned' AND {column} = 0 "
            "AND work_date BETWEEN ? AND ?",
            (
                (moment.date() - timedelta(days=1)).isoformat(),
                (moment.date() + timedelta(days=2)).isoformat(),
            ),
        ).fetchall()
    return [_row(r) for r in rows]


def due_reminders(moment: datetime, by_user: dict[int, notify.Prefs] | None = None) -> list[Shift]:
    """Смены, по которым пора напомнить: время напоминания наступило,
    смена ещё не началась, напоминание не отправлено.

    Время напоминания зависит от настроек пользователя (тихие часы), поэтому
    берём их одним запросом и складываем в словарь.
    """
    users = all_prefs() if by_user is None else by_user
    due = []
    for s in _pending_shifts(moment, "reminded"):
        p = users.get(s.user_id) or notify.Prefs(user_id=s.user_id)
        if notify.remind_at(p, s.work_date, s.shift_num) <= moment < domain.shift_start(
            s.work_date, s.shift_num
        ):
            due.append(s)
    return due


def due_leave_reminders(moment: datetime) -> list[Shift]:
    """Смены, до начала которых остались минуты — время короткого «пора выходить».

    Настройки тут не нужны: это напоминание приходит всегда в одно и то же время.
    """
    return [
        s for s in _pending_shifts(moment, "leave_reminded")
        if notify.leave_at(s.work_date, s.shift_num) <= moment
        < domain.shift_start(s.work_date, s.shift_num)
    ]


def mark_leave_reminded(shift_id: int) -> None:
    with _lock:
        _c().execute("UPDATE shifts SET leave_reminded = 1 WHERE id = ?", (shift_id,))
        _c().commit()


def confirm_shift(shift_id: int, user_id: int, hours: Decimal | None) -> Shift | None:
    """Подтвердить смену. hours=None -> «не был» (0 оплаты)."""
    status = "absent" if hours is None else "done"
    value = None if hours is None else float(hours)
    with _lock:
        cur = _c().execute(
            "UPDATE shifts SET status = ?, worked_hours = ?, confirm_asked = 1, confirmed_at = ? "
            "WHERE id = ? AND user_id = ?",
            (status, value, _now_iso(), shift_id, user_id),
        )
        _c().commit()
        if cur.rowcount == 0:
            return None
        r = _c().execute("SELECT * FROM shifts WHERE id = ?", (shift_id,)).fetchone()
    return _row(r)


def shifts_awaiting_ask(
    moment: datetime, by_user: dict[int, notify.Prefs] | None = None
) -> list[Shift]:
    """Закончившиеся смены, о которых бот ещё не спрашивал «как прошла».

    Момент вопроса тоже зависит от настроек: после ночной смены человек спит,
    поэтому её переносим на день.
    """
    users = all_prefs() if by_user is None else by_user
    with _lock:
        rows = _c().execute(
            "SELECT * FROM shifts WHERE status = 'planned' AND confirm_asked = 0 "
            "AND work_date BETWEEN ? AND ?",
            ((moment.date() - timedelta(days=7)).isoformat(), moment.date().isoformat()),
        ).fetchall()
    ready = []
    for r in rows:
        s = _row(r)
        p = users.get(s.user_id) or notify.Prefs(user_id=s.user_id)
        if notify.confirm_at(p, s.work_date, s.shift_num) <= moment:
            ready.append(s)
    return ready


def mark_confirm_asked(shift_id: int) -> None:
    with _lock:
        _c().execute("UPDATE shifts SET confirm_asked = 1 WHERE id = ?", (shift_id,))
        _c().commit()


def unconfirmed_shifts(user_id: int, limit: int = 30) -> list[Shift]:
    """Прошедшие смены без подтверждения — для /confirm."""
    with _lock:
        rows = _c().execute(
            "SELECT * FROM shifts WHERE user_id = ? AND status = 'planned' AND work_date <= ? "
            "ORDER BY work_date DESC, shift_num LIMIT ?",
            (user_id, domain.today().isoformat(), limit),
        ).fetchall()
    now = domain.now()
    return [s for s in (_row(r) for r in rows) if domain.shift_end(s.work_date, s.shift_num) <= now]


def ignored_shifts(user_id: int) -> list[Shift]:
    """Смены, про которые бот уже спрашивал, а ответа так и нет.

    Именно они портят расчёт: пока часы не подтверждены, считаются полные 8 ч.
    """
    return [s for s in unconfirmed_shifts(user_id) if s.confirm_asked]


def mark_reminded(shift_id: int) -> None:
    with _lock:
        _c().execute("UPDATE shifts SET reminded = 1 WHERE id = ?", (shift_id,))
        _c().commit()


# --- Штрафы ---------------------------------------------------------------

def add_penalty(
    user_id: int,
    at_date: date,
    kind: str,
    amount: Decimal | None = None,
    shift_id: int | None = None,
    note: str | None = None,
) -> Penalty:
    """Записать штраф. amount=None — берём сумму из договора для этого вида."""
    value = float(domain.penalty_amount(kind) if amount is None else amount)
    with _lock:
        cur = _c().execute(
            "INSERT INTO penalties (user_id, at_date, kind, amount, shift_id, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, at_date.isoformat(), kind, value, shift_id, note, _now_iso()),
        )
        _c().commit()
        r = _c().execute("SELECT * FROM penalties WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _penalty(r)


def penalties_in_range(user_id: int, start: date, end: date) -> list[Penalty]:
    with _lock:
        rows = _c().execute(
            "SELECT * FROM penalties WHERE user_id = ? AND at_date BETWEEN ? AND ? "
            "ORDER BY at_date, id",
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchall()
    return [_penalty(r) for r in rows]


def all_penalties(user_id: int) -> list[Penalty]:
    with _lock:
        rows = _c().execute(
            "SELECT * FROM penalties WHERE user_id = ? ORDER BY at_date, id", (user_id,)
        ).fetchall()
    return [_penalty(r) for r in rows]


def get_penalty(penalty_id: int, user_id: int) -> Penalty | None:
    with _lock:
        r = _c().execute(
            "SELECT * FROM penalties WHERE id = ? AND user_id = ?", (penalty_id, user_id)
        ).fetchone()
    return _penalty(r) if r else None


def delete_penalty(penalty_id: int, user_id: int) -> bool:
    with _lock:
        cur = _c().execute(
            "DELETE FROM penalties WHERE id = ? AND user_id = ?", (penalty_id, user_id)
        )
        _c().commit()
    return cur.rowcount > 0


def penalty_for_shift(user_id: int, shift_id: int, kind: str) -> Penalty | None:
    """Уже записанный штраф такого вида по этой смене — чтобы не задваивать."""
    with _lock:
        r = _c().execute(
            "SELECT * FROM penalties WHERE user_id = ? AND shift_id = ? AND kind = ?",
            (user_id, shift_id, kind),
        ).fetchone()
    return _penalty(r) if r else None


# --- Фактические выплаты --------------------------------------------------

def set_payout(user_id: int, year: int, month: int, amount: Decimal) -> None:
    """Запомнить, сколько реально пришло за период, — для сверки с расчётом."""
    with _lock:
        _c().execute(
            "INSERT INTO payouts (user_id, ym, amount, at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, ym) DO UPDATE SET "
            "amount = excluded.amount, at = excluded.at",
            (user_id, f"{year}-{month:02d}", float(amount), _now_iso()),
        )
        _c().commit()


def get_payout(user_id: int, year: int, month: int) -> Decimal | None:
    with _lock:
        r = _c().execute(
            "SELECT amount FROM payouts WHERE user_id = ? AND ym = ?",
            (user_id, f"{year}-{month:02d}"),
        ).fetchone()
    return Decimal(str(r["amount"])) if r else None


def delete_payout(user_id: int, year: int, month: int) -> bool:
    with _lock:
        cur = _c().execute(
            "DELETE FROM payouts WHERE user_id = ? AND ym = ?",
            (user_id, f"{year}-{month:02d}"),
        )
        _c().commit()
    return cur.rowcount > 0


def rate_cut_flag(user_id: int, year: int, month: int) -> bool | None:
    """Ручная отметка «ставка снижена» за период. None — решаем по данным."""
    with _lock:
        r = _c().execute(
            "SELECT rate_cut FROM periods WHERE user_id = ? AND ym = ?",
            (user_id, f"{year}-{month:02d}"),
        ).fetchone()
    if r is None or r["rate_cut"] is None:
        return None
    return bool(r["rate_cut"])


def set_rate_cut_flag(user_id: int, year: int, month: int, value: bool | None) -> None:
    with _lock:
        _c().execute(
            "INSERT INTO periods (user_id, ym, rate_cut) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, ym) DO UPDATE SET rate_cut = excluded.rate_cut",
            (user_id, f"{year}-{month:02d}", None if value is None else int(value)),
        )
        _c().commit()


# --- Достижения -----------------------------------------------------------

def all_shifts(user_id: int) -> list[Shift]:
    """Вся история пользователя, включая отменённые — основа для статистики ачивок."""
    with _lock:
        rows = _c().execute(
            "SELECT * FROM shifts WHERE user_id = ? ORDER BY work_date, shift_num", (user_id,)
        ).fetchall()
    return [_row(r) for r in rows]


def log_event(user_id: int, key: str) -> None:
    """Отметить обращение к команде — на этом держатся «мета»-ачивки."""
    with _lock:
        _c().execute(
            "INSERT INTO events (user_id, key, at) VALUES (?, ?, ?)", (user_id, key, _now_iso())
        )
        _c().commit()


def count_events(user_id: int, key: str, since: datetime | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM events WHERE user_id = ? AND key = ?"
    args: list = [user_id, key]
    if since is not None:
        sql += " AND at >= ?"
        args.append(since.isoformat(timespec="seconds"))
    with _lock:
        r = _c().execute(sql, args).fetchone()
    return r["n"]


def unlocked_codes(user_id: int) -> set[str]:
    with _lock:
        rows = _c().execute(
            "SELECT code FROM achievements WHERE user_id = ?", (user_id,)
        ).fetchall()
    return {r["code"] for r in rows}


def unlocked_at(user_id: int) -> dict[str, datetime]:
    with _lock:
        rows = _c().execute(
            "SELECT code, unlocked_at FROM achievements WHERE user_id = ?", (user_id,)
        ).fetchall()
    return {r["code"]: datetime.fromisoformat(r["unlocked_at"]) for r in rows}


def unlock_achievements(user_id: int, codes) -> None:
    now = _now_iso()
    with _lock:
        _c().executemany(
            "INSERT INTO achievements (user_id, code, unlocked_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, code) DO NOTHING",
            [(user_id, code, now) for code in codes],
        )
        _c().commit()


def lock_achievements(user_id: int, codes) -> None:
    """Снять ачивку — так теряются те, что помечены losable."""
    with _lock:
        _c().executemany(
            "DELETE FROM achievements WHERE user_id = ? AND code = ?",
            [(user_id, code) for code in codes],
        )
        _c().commit()


def set_toxic(user_id: int, enabled: bool) -> None:
    set_flag(user_id, "toxic", enabled)


def toxic_enabled(user_id: int) -> bool:
    with _lock:
        r = _c().execute("SELECT toxic FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return bool(r["toxic"]) if r else True


def ach_intro_shown(user_id: int) -> bool:
    with _lock:
        r = _c().execute("SELECT ach_intro FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return bool(r["ach_intro"]) if r else False


def mark_ach_intro(user_id: int) -> None:
    with _lock:
        _c().execute("UPDATE users SET ach_intro = 1 WHERE user_id = ?", (user_id,))
        _c().commit()


# --- Админка --------------------------------------------------------------

def grant_admin(user_id: int) -> None:
    """Отметить успешный вход. Повторный вход просто продлевает сессию."""
    with _lock:
        _c().execute(
            "INSERT INTO admin_sessions (user_id, granted_at) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET granted_at = excluded.granted_at",
            (user_id, _now_iso()),
        )
        _c().commit()


def revoke_admin(user_id: int) -> None:
    with _lock:
        _c().execute("DELETE FROM admin_sessions WHERE user_id = ?", (user_id,))
        _c().commit()


def admin_granted_at(user_id: int) -> datetime | None:
    with _lock:
        r = _c().execute(
            "SELECT granted_at FROM admin_sessions WHERE user_id = ?", (user_id,)
        ).fetchone()
    return _dt(r["granted_at"]) if r else None


def list_users() -> list[UserRow]:
    with _lock:
        rows = _c().execute(
            "SELECT user_id, weekly_ask, toxic, created_at, username, first_name, last_name "
            "FROM users ORDER BY user_id"
        ).fetchall()
    return [
        UserRow(
            user_id=r["user_id"],
            weekly_ask=bool(r["weekly_ask"]),
            toxic=bool(r["toxic"]),
            created_at=_dt(r["created_at"]),
            username=r["username"],
            first_name=r["first_name"],
            last_name=r["last_name"],
        )
        for r in rows
    ]


def all_user_ids() -> list[int]:
    """Все, кто хоть раз запускал бота — адресаты рассылки."""
    with _lock:
        rows = _c().execute("SELECT user_id FROM users ORDER BY user_id").fetchall()
    return [r["user_id"] for r in rows]


def shifts_by_user() -> dict[int, list[Shift]]:
    """Вся история смен, разложенная по пользователям."""
    with _lock:
        rows = _c().execute("SELECT * FROM shifts ORDER BY work_date, shift_num").fetchall()
    out: dict[int, list[Shift]] = {}
    for r in rows:
        out.setdefault(r["user_id"], []).append(_row(r))
    return out


def penalties_by_user() -> dict[int, list[Penalty]]:
    with _lock:
        rows = _c().execute("SELECT * FROM penalties ORDER BY at_date, id").fetchall()
    out: dict[int, list[Penalty]] = {}
    for r in rows:
        out.setdefault(r["user_id"], []).append(_penalty(r))
    return out


def achievement_counts() -> dict[int, int]:
    with _lock:
        rows = _c().execute(
            "SELECT user_id, COUNT(*) AS n FROM achievements GROUP BY user_id"
        ).fetchall()
    return {r["user_id"]: r["n"] for r in rows}


def last_seen_by_user() -> dict[int, datetime]:
    """Последний след пользователя: просмотр, запись смены, штраф или ачивка."""
    with _lock:
        rows = _c().execute(
            "SELECT user_id, MAX(at) AS at FROM ("
            "  SELECT user_id, at FROM events"
            "  UNION ALL SELECT user_id, created_at FROM shifts"
            "  UNION ALL SELECT user_id, confirmed_at FROM shifts WHERE confirmed_at IS NOT NULL"
            "  UNION ALL SELECT user_id, created_at FROM penalties"
            "  UNION ALL SELECT user_id, unlocked_at FROM achievements"
            ") GROUP BY user_id"
        ).fetchall()
    return {r["user_id"]: datetime.fromisoformat(r["at"]) for r in rows if r["at"]}
