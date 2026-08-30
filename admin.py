"""Админка: вход по паролю, статистика по всем пользователям, рассылка.

Про Telegram ничего не знает — только проверка пароля, сессии и сборка текстов.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import config
import db
import domain
import reports

log = logging.getLogger(__name__)


# --- Вход -----------------------------------------------------------------

def configured() -> bool:
    """Админка включена только если задан пароль (открытый или его sha256)."""
    return bool(config.ADMIN_PASSWORD or config.ADMIN_PASSWORD_SHA256)


def allowed(user_id: int) -> bool:
    """Может ли этот пользователь хотя бы пробовать войти."""
    return not config.ADMIN_IDS or user_id in config.ADMIN_IDS


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def password_ok(raw: str) -> bool:
    raw = raw.strip()
    if not raw or not configured():
        return False
    # Сравниваем байты, а не строки: compare_digest на str падает на не-ASCII пароле.
    if config.ADMIN_PASSWORD_SHA256:
        return hmac.compare_digest(_sha256(raw).encode(), config.ADMIN_PASSWORD_SHA256.encode())
    return hmac.compare_digest(raw.encode("utf-8"), config.ADMIN_PASSWORD.encode("utf-8"))


@dataclass
class _Attempts:
    """Счётчик неудачных попыток. Живёт в памяти: перезапуск бота его обнуляет."""
    count: int = 0
    blocked_until: datetime | None = None


_attempts: dict[int, _Attempts] = {}


def lock_left(user_id: int) -> timedelta | None:
    """Сколько ещё ждать после серии неверных паролей. None — можно пробовать."""
    a = _attempts.get(user_id)
    if a is None or a.blocked_until is None:
        return None
    left = a.blocked_until - domain.now()
    if left.total_seconds() <= 0:
        _attempts.pop(user_id, None)
        return None
    return left


def note_failure(user_id: int) -> int:
    """Записать промах. Вернёт, сколько попыток осталось (0 — включилась блокировка)."""
    a = _attempts.setdefault(user_id, _Attempts())
    a.count += 1
    left = config.ADMIN_MAX_ATTEMPTS - a.count
    if left <= 0:
        a.count = 0
        a.blocked_until = domain.now() + config.ADMIN_LOCKOUT
        log.warning("Админка: пользователь %s заблокирован после серии неверных паролей", user_id)
        return 0
    log.warning("Админка: неверный пароль от пользователя %s (осталось %s)", user_id, left)
    return left


def login(user_id: int) -> None:
    _attempts.pop(user_id, None)
    db.grant_admin(user_id)
    log.info("Админка: вход пользователя %s", user_id)


def logout(user_id: int) -> None:
    db.revoke_admin(user_id)


def is_admin(user_id: int) -> bool:
    """Есть ли живая сессия. Просроченную сразу убираем."""
    if not configured() or not allowed(user_id):
        return False
    granted = db.admin_granted_at(user_id)
    if granted is None:
        return False
    if domain.now() - granted > config.ADMIN_SESSION:
        db.revoke_admin(user_id)
        return False
    return True


def session_left(user_id: int) -> timedelta:
    granted = db.admin_granted_at(user_id)
    if granted is None:
        return timedelta(0)
    return max(timedelta(0), config.ADMIN_SESSION - (domain.now() - granted))


# --- Статистика -----------------------------------------------------------

@dataclass(frozen=True)
class UserStat:
    """Всё, что админка знает о пользователе: собрано из смен, штрафов и событий."""
    user_id: int
    created_at: datetime | None
    weekly_ask: bool
    toxic: bool
    achievements: int
    last_seen: datetime | None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    shifts: list[db.Shift] = field(default_factory=list)
    penalties: list[db.Penalty] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p)

    @property
    def name(self) -> str:
        """Коротко и узнаваемо: @ник, иначе имя, иначе просто id."""
        if self.username:
            return f"@{self.username}"
        return self.full_name or f"id {self.user_id}"

    @property
    def title(self) -> str:
        """Развёрнуто для карточки: «Имя Фамилия (@ник)», если известно и то и другое."""
        if self.username and self.full_name:
            return f"{self.full_name} (@{self.username})"
        return self.name

    @property
    def html_name(self) -> str:
        """Имя приходит от пользователя, а сообщения уходят с parse_mode=HTML."""
        return html.escape(self.name)

    @property
    def html_title(self) -> str:
        return html.escape(self.title)

    def _by_status(self, status: str) -> list[db.Shift]:
        return [s for s in self.shifts if s.status == status]

    @property
    def done(self) -> list[db.Shift]:
        return self._by_status("done")

    @property
    def absent(self) -> list[db.Shift]:
        return self._by_status("absent")

    @property
    def cancelled(self) -> list[db.Shift]:
        return self._by_status("cancelled")

    @property
    def unconfirmed(self) -> list[db.Shift]:
        """Смена прошла, но пользователь ещё не сказал, сколько часов зачли."""
        now = domain.now()
        return [
            s for s in self._by_status("planned")
            if domain.shift_end(s.work_date, s.shift_num) <= now
        ]

    @property
    def planned(self) -> list[db.Shift]:
        now = domain.now()
        return [
            s for s in self._by_status("planned")
            if domain.shift_end(s.work_date, s.shift_num) > now
        ]

    @property
    def worked(self) -> list[db.Shift]:
        """Смены, за которые идут деньги: как в /money — с неподтверждёнными по 8 ч."""
        return self.done + self.unconfirmed

    @property
    def hours(self) -> Decimal:
        return sum((s.hours for s in self.worked), Decimal(0))

    @property
    def earned(self) -> Decimal:
        return sum((s.pay for s in self.worked), Decimal(0))

    @property
    def penalty_total(self) -> Decimal:
        return sum((p.amount for p in self.penalties), Decimal(0))

    @property
    def activity(self) -> datetime:
        """Ключ сортировки: последний след, а если его нет — дата регистрации."""
        return self.last_seen or self.created_at or datetime.min.replace(tzinfo=config.TZ)


def collect() -> list[UserStat]:
    """Собрать статистику по всем пользователям — свежие сверху."""
    shifts = db.shifts_by_user()
    penalties = db.penalties_by_user()
    achievements = db.achievement_counts()
    seen = db.last_seen_by_user()
    stats = [
        UserStat(
            user_id=u.user_id,
            created_at=u.created_at,
            weekly_ask=u.weekly_ask,
            toxic=u.toxic,
            achievements=achievements.get(u.user_id, 0),
            last_seen=seen.get(u.user_id),
            username=u.username,
            first_name=u.first_name,
            last_name=u.last_name,
            shifts=shifts.get(u.user_id, []),
            penalties=penalties.get(u.user_id, []),
        )
        for u in db.list_users()
    ]
    return sorted(stats, key=lambda s: s.activity, reverse=True)


def find(users: list[UserStat], user_id: int) -> UserStat | None:
    return next((u for u in users if u.user_id == user_id), None)


def _when(moment: datetime | None) -> str:
    if moment is None:
        return "—"
    delta = domain.now() - moment
    if delta < timedelta(minutes=5):
        return "только что"
    if delta < timedelta(days=1):
        return f"{moment:%H:%M}, сегодня"
    if delta < timedelta(days=30):
        return f"{moment:%d.%m %H:%M} ({delta.days} дн. назад)"
    return f"{moment:%d.%m.%Y}"


def _active(users: list[UserStat], days: int) -> int:
    edge = domain.now() - timedelta(days=days)
    return sum(1 for u in users if u.last_seen and u.last_seen >= edge)


# --- Отчёты ---------------------------------------------------------------

def overview_report(users: list[UserStat] | None = None) -> str:
    """Сводка по всей базе: люди, смены, часы, деньги, штрафы."""
    users = collect() if users is None else users
    lines = ["<b>📊 Статистика по всем</b>", ""]
    if not users:
        lines.append("Пользователей пока нет — бота ещё никто не запускал.")
        return "\n".join(lines)

    done = sum(len(u.done) for u in users)
    unconfirmed = sum(len(u.unconfirmed) for u in users)
    planned = sum(len(u.planned) for u in users)
    absent = sum(len(u.absent) for u in users)
    cancelled = sum(len(u.cancelled) for u in users)
    total = done + unconfirmed + planned + absent + cancelled
    hours = sum((u.hours for u in users), Decimal(0))
    earned = sum((u.earned for u in users), Decimal(0))
    pen_count = sum(len(u.penalties) for u in users)
    pen_sum = sum((u.penalty_total for u in users), Decimal(0))
    achievements = sum(u.achievements for u in users)

    lines.append(f"👥 Пользователей: <b>{len(users)}</b>")
    lines.append(
        f"    активны за 7 дней: {_active(users, 7)} · за 30 дней: {_active(users, 30)}"
    )
    lines.append("")
    lines.append(f"🗓 Смен всего: <b>{total}</b>")
    if total:
        lines.append(f"    ✅ подтверждено: {done} · ⏳ без подтверждения: {unconfirmed}")
        lines.append(f"    🕒 впереди: {planned} · 🚫 прогулов: {absent} · ❌ отменено: {cancelled}")
    lines.append("")
    lines.append(f"⏱ Отработано часов: <b>{domain.fmt_hours(hours)}</b>")
    lines.append(f"💰 Заработано: <b>{domain.money(earned)}</b>")
    if unconfirmed:
        lines.append("    <i>неподтверждённые смены считаю по 8 ч, как в /money</i>")
    lines.append(
        f"⚖️ Штрафов: {pen_count} {domain.penalties_word(pen_count)}"
        f" — <b>{domain.money(pen_sum)}</b>"
    )
    lines.append(f"🏅 Достижений выдано: {achievements}")
    lines.append("")
    lines.append(f"<i>Собрано {domain.now():%d.%m %H:%M}</i>")
    return "\n".join(lines)


def users_report(users: list[UserStat] | None = None) -> str:
    """Список пользователей: кто, сколько смен и когда последний раз заходил."""
    users = collect() if users is None else users
    if not users:
        return "<b>👥 Пользователи</b>\n\nПока никого."
    lines = [f"<b>👥 Пользователи: {len(users)}</b>", ""]
    for i, u in enumerate(users[: config.ADMIN_USER_BUTTONS], start=1):
        n = len(u.shifts)
        lines.append(
            f"{i}. <b>{u.html_name}</b> — {n} {domain.shifts_word(n)},"
            f" {domain.money(u.earned)}"
        )
        lines.append(f"    <code>{u.user_id}</code> · был(а): {_when(u.last_seen)}")
    if len(users) > config.ADMIN_USER_BUTTONS:
        hidden = len(users) - config.ADMIN_USER_BUTTONS
        lines.append(f"\n<i>Ещё {hidden} — не поместились, сортировка по активности.</i>")
    return "\n".join(lines)


def user_card(user_id: int, users: list[UserStat] | None = None) -> str:
    """Карточка одного пользователя, включая деньги за текущий период."""
    users = collect() if users is None else users
    u = find(users, user_id)
    if u is None:
        return "Пользователь не найден."

    year, month = domain.period_anchor(domain.today())
    period = reports.period_data(user_id, year, month)

    lines = [
        f"<b>👤 {u.html_title}</b>",
        f"id <code>{u.user_id}</code>",
        f"Регистрация: {domain.fmt_date_long(u.created_at.date()) if u.created_at else '—'}",
        f"Последняя активность: {_when(u.last_seen)}",
        "",
        f"🗓 Смен всего: <b>{len(u.shifts)}</b>",
    ]
    if u.shifts:
        lines.append(
            f"    ✅ подтверждено: {len(u.done)} · ⏳ без подтверждения: {len(u.unconfirmed)}"
        )
        lines.append(
            f"    🕒 впереди: {len(u.planned)} · 🚫 прогулов: {len(u.absent)}"
            f" · ❌ отменено: {len(u.cancelled)}"
        )
    lines.append(f"⏱ Часы: <b>{domain.fmt_hours(u.hours)}</b>")
    lines.append(f"💰 Заработано за всё время: <b>{domain.money(u.earned)}</b>")
    n = len(u.penalties)
    lines.append(
        f"⚖️ Штрафы: {n} {domain.penalties_word(n)} — <b>{domain.money(u.penalty_total)}</b>"
    )
    lines.append(f"🏅 Достижения: {u.achievements}")
    lines.append("")
    lines.append(f"<b>Период {domain.period_title(year, month)}</b>")
    lines.append(
        f"    смен: {len(period.counted)} · начислено: {domain.money(period.earned)}"
    )
    if period.deductions:
        lines.append(f"    удержания: −{domain.money(period.deductions)}")
    lines.append(f"    на руки: <b>{domain.money(period.net)}</b>")
    lines.append("")
    lines.append(
        "<i>Настройки: еженедельный вопрос — "
        f"{'вкл' if u.weekly_ask else 'выкл'}, тон — "
        f"{'стёб' if u.toxic else 'по-доброму'}.</i>"
    )
    return "\n".join(lines)
