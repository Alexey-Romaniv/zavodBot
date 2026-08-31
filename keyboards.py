"""Клавиатуры и callback-данные."""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import db
import domain
import texts

# Версия набора кнопок. Клавиатура хранится в клиенте Telegram и меняется только
# когда бот пришлёт сообщение с новой, поэтому после обновления меню каждому
# пользователю нужно один раз её досдать — см. middlewares.FreshMenu.
# Увеличивать при любой правке main_menu().
MENU_VERSION = 3

# Подписи кнопок. Короткие — иначе на телефоне переносятся в две строки
# и клавиатура выглядит рваной. На самой клавиатуре живут только первые
# четыре, остальное открывается кнопкой «Ещё» — см. main_menu().
BTN_WEEK = "📅 Смены на неделю"
BTN_MY = "🗓 Мои смены"
BTN_MONEY = "💰 Деньги"
BTN_MORE = "➕ Ещё"
BTN_MANUAL = "✍️ Вписать смены"
BTN_ACH = "🏅 Достижения"
BTN_PENALTY = "⚖️ Штрафы"
BTN_REPORT = "📄 Отчёт"
BTN_SETTINGS = "⚙️ Настройки"

# Клавиатура живёт в клиенте, пока не пришлём новую, поэтому старые подписи
# продолжают приходить от тех, кто давно не писал боту. Принимаем и их.
TXT_WEEK = frozenset({BTN_WEEK})
TXT_MY = frozenset({BTN_MY})
TXT_MONEY = frozenset({BTN_MONEY})
TXT_MANUAL = frozenset({BTN_MANUAL, "✍️ Вписать вручную"})
TXT_ACH = frozenset({BTN_ACH})
TXT_PENALTY = frozenset({BTN_PENALTY})
TXT_REPORT = frozenset({BTN_REPORT})
TXT_SETTINGS = frozenset({BTN_SETTINGS})
TXT_MORE = frozenset({BTN_MORE})


class WeekCb(CallbackData, prefix="wk"):
    action: str  # week | shift | day | save | more | close
    arg: str = ""


class ShiftCb(CallbackData, prefix="sh"):
    action: str  # cancel | restore
    shift_id: int


class ConfirmCb(CallbackData, prefix="cf"):
    action: str  # full | early | hours | halves | late | late_kind | absent | absent_* | pick
    shift_id: int
    hours: str = ""
    arg: str = ""  # код вида нарушения для late_kind


class MoneyCb(CallbackData, prefix="mn"):
    ym: str          # YYYY-MM месяца-якоря периода
    action: str = "nav"  # nav | payout | check | payout_del | export | list


class PenaltyCb(CallbackData, prefix="pn"):
    action: str      # nav | menu | kind | day | day_set | manual | del | ratecut | close
    ym: str = ""     # YYYY-MM периода, к которому относится действие
    arg: str = ""    # код вида нарушения или id штрафа


class ManualCb(CallbackData, prefix="mu"):
    action: str      # month | shift | day | save | close
    ym: str = ""     # YYYY-MM месяца, который вписываем
    arg: str = ""    # номер смены или день месяца


class SettingsCb(CallbackData, prefix="st"):
    action: str      # menu | toggle | quiet | quiet_set | evening | evening_set
                     # | mute | mute_set | unmute | close
    arg: str = ""    # имя тумблера, «22:00-07:00», час или вид тишины


class MoreCb(CallbackData, prefix="mr"):
    action: str      # penalty | ach | manual | report | settings | help | close


class AdminCb(CallbackData, prefix="ad"):
    action: str      # menu | stats | users | user | cast | cast_go | logout | close
    arg: str = ""    # user_id для карточки пользователя


def main_menu() -> ReplyKeyboardMarkup:
    """Всё, что нужно, — кнопками: команды набирать не требуется.

    Клавиатура постоянная и занимает место под перепиской, поэтому в ней только
    ежедневное: смены, деньги. Редкое (штрафы, достижения, отчёт, настройки,
    ручной ввод) прячем за «Ещё» — оно открывается кнопками в сообщении и
    экран не съедает. Два ряда по две кнопки — подписи не переносятся.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_MY)],
            [KeyboardButton(text=BTN_MONEY), KeyboardButton(text=BTN_MORE)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def more_menu() -> InlineKeyboardMarkup:
    """То, что убрано с клавиатуры: всё остальное, что умеет бот."""
    kb = InlineKeyboardBuilder()
    kb.button(text=BTN_PENALTY, callback_data=MoreCb(action="penalty"))
    kb.button(text=BTN_ACH, callback_data=MoreCb(action="ach"))
    kb.button(text=BTN_MANUAL, callback_data=MoreCb(action="manual"))
    kb.button(text=BTN_REPORT, callback_data=MoreCb(action="report"))
    kb.button(text=BTN_SETTINGS, callback_data=MoreCb(action="settings"))
    kb.button(text="❓ Что умеет бот", callback_data=MoreCb(action="help"))
    kb.adjust(2)
    kb.row(
        InlineKeyboardButton(
            text="✖️ Закрыть", callback_data=MoreCb(action="close").pack()
        )
    )
    return kb.as_markup()


WEEK_NAMES = {-7: "прошлая", 0: "эта", 7: "следующая", 14: "через неделю"}


def week_choice(preferred: date | None = None) -> InlineKeyboardMarkup:
    """Недели с явными датами: самая уместная сейчас — первой кнопкой.

    `preferred` задаёт эту неделю вручную: понедельничное напоминание про
    следующую неделю не должно предлагать первой уже начавшуюся.
    """
    base = domain.monday_of(domain.today())
    plan = preferred or domain.planning_monday()
    offsets = [0, 7, -7] if plan == base else [7, 14, 0]
    kb = InlineKeyboardBuilder()
    for off in offsets:
        monday = base + timedelta(days=off)
        kb.button(
            text=f"📅 {domain.week_label(monday)} ({WEEK_NAMES[off]})",
            callback_data=WeekCb(action="week", arg=monday.isoformat()),
        )
    kb.button(text="✖️ Закрыть", callback_data=WeekCb(action="close"))
    kb.adjust(1)
    return kb.as_markup()


def shift_choice(monday: date) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for num in (1, 2, 3):
        s = domain.shift(num)
        tag = "🌙 ночная" if s.is_night else "☀️ дневная"
        kb.button(
            text=f"{s.title} {s.hours_label} · {tag}",
            callback_data=WeekCb(action="shift", arg=f"{monday.isoformat()}|{num}"),
        )
    kb.button(text="⬅️ Другая неделя", callback_data=WeekCb(action="back_week"))
    kb.button(text="✖️ Закрыть", callback_data=WeekCb(action="close"))
    kb.adjust(1)
    return kb.as_markup()


def days_choice(monday: date, num: int, selected: set[date]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    now = domain.now()
    for d in domain.week_days(monday, num):
        mark = "✅" if d in selected else "▫️"
        past = " ⌛" if domain.shift_end(d, num) <= now else ""
        kb.button(
            text=f"{mark} {domain.fmt_date(d)}{past}",
            callback_data=WeekCb(action="day", arg=d.isoformat()),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text="💾 Сохранить", callback_data=WeekCb(action="save").pack()
        ),
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=WeekCb(action="shift_back", arg=monday.isoformat()).pack()
        ),
    )
    return kb.as_markup()


def after_save(monday: date) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="➕ Добавить ещё смены", callback_data=WeekCb(action="more", arg=monday.isoformat())
    )
    kb.button(text="🗓 Мои смены", callback_data=WeekCb(action="show_my"))
    kb.adjust(1)
    return kb.as_markup()


def upcoming(shifts: list[db.Shift]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in shifts:
        kb.button(
            text=f"❌ Отменить: {domain.fmt_date(s.work_date)} · {s.kind.title}",
            callback_data=ShiftCb(action="cancel", shift_id=s.id),
        )
    kb.adjust(1)
    return kb.as_markup()


def reminder(shift_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Смену отменили", callback_data=ShiftCb(action="cancel", shift_id=shift_id))
    return kb.as_markup()


def restore(shift_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Вернуть смену", callback_data=ShiftCb(action="restore", shift_id=shift_id))
    return kb.as_markup()


def money_nav(user_id: int, year: int, month: int) -> InlineKeyboardMarkup:
    """Месяцы, отметка фактической выплаты и выгрузка периода в файл."""
    ym = f"{year}-{month:02d}"
    py, pm = domain.prev_month(year, month)
    ny, nm = domain.next_month(year, month)
    paid = db.get_payout(user_id, year, month)
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text=f"◀️ {domain.MONTHS_NOM[pm - 1]}", callback_data=MoneyCb(ym=f"{py}-{pm:02d}").pack()
        ),
        InlineKeyboardButton(
            text=f"{domain.MONTHS_NOM[nm - 1]} ▶️", callback_data=MoneyCb(ym=f"{ny}-{nm:02d}").pack()
        ),
    )
    if paid is None:
        kb.row(
            InlineKeyboardButton(
                text="💳 Пришла выплата — сверить",
                callback_data=MoneyCb(ym=ym, action="payout").pack(),
            )
        )
    else:
        kb.row(
            InlineKeyboardButton(
                text=f"💳 Сверка: {domain.money(paid)}",
                callback_data=MoneyCb(ym=ym, action="check").pack(),
            )
        )
        kb.row(
            InlineKeyboardButton(
                text="✏️ Изменить сумму",
                callback_data=MoneyCb(ym=ym, action="payout").pack(),
            ),
            InlineKeyboardButton(
                text="🗑 Убрать",
                callback_data=MoneyCb(ym=ym, action="payout_del").pack(),
            ),
        )
    kb.row(
        InlineKeyboardButton(
            text="📋 Все смены списком",
            callback_data=MoneyCb(ym=ym, action="list").pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="📄 Выгрузить в таблицу",
            callback_data=MoneyCb(ym=ym, action="export").pack(),
        )
    )
    return kb.as_markup()


def export_done(year: int, month: int) -> InlineKeyboardMarkup:
    """Под присланным файлом: вернуться к деньгам или посмотреть то же в чате."""
    ym = f"{year}-{month:02d}"
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Показать в чате", callback_data=MoneyCb(ym=ym, action="list"))
    kb.button(text="💰 Деньги за период", callback_data=MoneyCb(ym=ym))
    kb.adjust(1)
    return kb.as_markup()


def payout_ask(year: int, month: int) -> InlineKeyboardMarkup:
    """Кнопка из напоминания в день выплаты."""
    ym = f"{year}-{month:02d}"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="💳 Ввести фактическую сумму", callback_data=MoneyCb(ym=ym, action="payout")
    )
    kb.button(text="💰 Отчёт за период", callback_data=MoneyCb(ym=ym))
    kb.adjust(1)
    return kb.as_markup()


def confirm(shift_id: int) -> InlineKeyboardMarkup:
    full = domain.fmt_hours(config.SHIFT_HOURS)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Полностью ({full})", callback_data=ConfirmCb(action="full", shift_id=shift_id))
    kb.button(text="🏃 Отпустили раньше", callback_data=ConfirmCb(action="early", shift_id=shift_id))
    kb.button(text="⏰ Опоздал / долгий перерыв", callback_data=ConfirmCb(action="late", shift_id=shift_id))
    kb.button(text="❌ Не был", callback_data=ConfirmCb(action="absent", shift_id=shift_id))
    kb.adjust(1)
    return kb.as_markup()


def confirm_late(shift_id: int) -> InlineKeyboardMarkup:
    """Что именно нарушено: опоздание или затянутый перерыв — штраф одинаковый."""
    kb = InlineKeyboardBuilder()
    for code in ("late", "break"):
        k = domain.penalty(code)
        kb.button(
            text=f"{k.icon} {k.title} — {domain.money(k.amount)}",
            callback_data=ConfirmCb(action="late_kind", shift_id=shift_id, arg=code),
        )
    kb.button(text="⬅️ Назад", callback_data=ConfirmCb(action="pick", shift_id=shift_id))
    kb.adjust(1)
    return kb.as_markup()


def confirm_absent(shift_id: int) -> InlineKeyboardMarkup:
    """Прогул со штрафом или пропуск, за который штрафа не будет."""
    fine = domain.penalty("absence")
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"{fine.icon} Прогул — штраф {domain.money(fine.amount)}",
        callback_data=ConfirmCb(action="absent_fine", shift_id=shift_id),
    )
    kb.button(
        text="🤷 Не был, но без штрафа",
        callback_data=ConfirmCb(action="absent_free", shift_id=shift_id),
    )
    kb.button(text="⬅️ Назад", callback_data=ConfirmCb(action="pick", shift_id=shift_id))
    kb.adjust(1)
    return kb.as_markup()


def confirm_hours(shift_id: int, with_full: bool = False) -> InlineKeyboardMarkup:
    """Сколько часов зачли, если отпустили раньше."""
    kb = InlineKeyboardBuilder()
    for h in ("1", "2", "3", "4", "5", "6", "7"):
        kb.button(text=f"{h} ч", callback_data=ConfirmCb(action="hours", shift_id=shift_id, hours=h))
    kb.adjust(4)
    if with_full:
        kb.row(
            InlineKeyboardButton(
                text=f"✅ Полностью ({domain.fmt_hours(config.SHIFT_HOURS)})",
                callback_data=ConfirmCb(action="full", shift_id=shift_id).pack(),
            )
        )
    kb.row(
        InlineKeyboardButton(
            text="🔢 С половинками",
            callback_data=ConfirmCb(action="halves", shift_id=shift_id).pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=ConfirmCb(action="pick", shift_id=shift_id).pack()
        )
    )
    return kb.as_markup()


def confirm_halves(shift_id: int) -> InlineKeyboardMarkup:
    """Часы с шагом в полчаса — чтобы «6,5» не приходилось писать руками."""
    kb = InlineKeyboardBuilder()
    half = Decimal("0.5")
    value = half
    while value < config.SHIFT_HOURS:
        kb.button(
            text=domain.fmt_hours(value),
            callback_data=ConfirmCb(
                action="hours", shift_id=shift_id, hours=f"{value.normalize():f}"
            ),
        )
        value += half
    kb.adjust(4)
    kb.row(
        InlineKeyboardButton(
            text=f"✅ Полностью ({domain.fmt_hours(config.SHIFT_HOURS)})",
            callback_data=ConfirmCb(action="full", shift_id=shift_id).pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=ConfirmCb(action="pick", shift_id=shift_id).pack()
        )
    )
    return kb.as_markup()


def unconfirmed(shifts: list[db.Shift]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in shifts:
        kb.button(
            text=f"{domain.fmt_date(s.work_date)} · {s.kind.title}",
            callback_data=ConfirmCb(action="pick", shift_id=s.id),
        )
    kb.adjust(1)
    return kb.as_markup()


# --- Штрафы ---------------------------------------------------------------

def penalties_nav(year: int, month: int, penalties, rate_cut: bool) -> InlineKeyboardMarkup:
    """Отчёт по штрафам: месяцы, запись нового, тумблер сниженной ставки, удаление."""
    ym = f"{year}-{month:02d}"
    py, pm = domain.prev_month(year, month)
    ny, nm = domain.next_month(year, month)
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text=f"◀️ {domain.MONTHS_NOM[pm - 1]}",
            callback_data=PenaltyCb(action="nav", ym=f"{py}-{pm:02d}").pack(),
        ),
        InlineKeyboardButton(
            text=f"{domain.MONTHS_NOM[nm - 1]} ▶️",
            callback_data=PenaltyCb(action="nav", ym=f"{ny}-{nm:02d}").pack(),
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="➕ Записать штраф", callback_data=PenaltyCb(action="menu", ym=ym).pack()
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="📈 Убрать снижение ставки" if rate_cut else "📉 Ставка снижена в этом месяце",
            callback_data=PenaltyCb(action="ratecut", ym=ym).pack(),
        )
    )
    for pen in penalties[:domain.PENALTY_BUTTONS]:
        kb.row(
            InlineKeyboardButton(
                text=f"🗑 {domain.fmt_date(pen.at_date)} · {pen.title}"
                     f" · {domain.money(pen.amount)}",
                callback_data=PenaltyCb(action="del", ym=ym, arg=str(pen.id)).pack(),
            )
        )
    return kb.as_markup()


def penalty_kinds(ym: str, at_date: date | None = None) -> InlineKeyboardMarkup:
    """Выбор вида нарушения. Дата уже выбрана кнопкой — писать её не нужно."""
    kb = InlineKeyboardBuilder()
    suffix = f"|{at_date.isoformat()}" if at_date else ""
    for code in domain.PENALTY_ORDER:
        k = domain.penalty(code)
        kb.button(
            text=f"{k.icon} {k.title} — {domain.money(k.amount)}",
            callback_data=PenaltyCb(action="kind", ym=ym, arg=code + suffix),
        )
    if at_date is None:
        kb.button(text="📅 Другой день", callback_data=PenaltyCb(action="day", ym=ym))
    kb.button(
        text="✏️ Своя сумма или заметка", callback_data=PenaltyCb(action="manual", ym=ym)
    )
    kb.button(text="⬅️ Назад", callback_data=PenaltyCb(action="nav", ym=ym))
    kb.adjust(1)
    return kb.as_markup()


def penalty_days(year: int, month: int) -> InlineKeyboardMarkup:
    """Дни расчётного периода кнопками — для штрафа задним числом."""
    ym = f"{year}-{month:02d}"
    start, end = domain.period_bounds(year, month)
    today = domain.today()
    kb = InlineKeyboardBuilder()
    d = start
    while d <= end:
        mark = "•" if d == today else ""
        kb.button(
            text=f"{mark}{d.day} {domain.WEEKDAY_SHORT[d.weekday()]}",
            callback_data=PenaltyCb(action="day_set", ym=ym, arg=d.isoformat()),
        )
        d += timedelta(days=1)
    kb.adjust(4)
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=PenaltyCb(action="menu", ym=ym).pack()
        )
    )
    return kb.as_markup()


# --- Вписать смены за прошлые дни -----------------------------------------

def manual_shift_choice(year: int, month: int) -> InlineKeyboardMarkup:
    """Какую смену вписываем. Месяц листается стрелками — набирать даты не нужно."""
    ym = f"{year}-{month:02d}"
    py, pm = domain.prev_month(year, month)
    ny, nm = domain.next_month(year, month)
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text=f"◀️ {domain.MONTHS_NOM[pm - 1]}",
            callback_data=ManualCb(action="month", ym=f"{py}-{pm:02d}").pack(),
        ),
        InlineKeyboardButton(
            text=f"{domain.MONTHS_NOM[nm - 1]} ▶️",
            callback_data=ManualCb(action="month", ym=f"{ny}-{nm:02d}").pack(),
        ),
    )
    for num in (1, 2, 3):
        shift = domain.shift(num)
        tag = "🌙 ночная" if shift.is_night else "☀️ дневная"
        kb.row(
            InlineKeyboardButton(
                text=f"{shift.title} {shift.hours_label} · {tag}",
                callback_data=ManualCb(action="shift", ym=ym, arg=str(num)).pack(),
            )
        )
    kb.row(
        InlineKeyboardButton(text="✖️ Закрыть", callback_data=ManualCb(action="close").pack())
    )
    return kb.as_markup()


def manual_days(year: int, month: int, num: int, selected: set[date]) -> InlineKeyboardMarkup:
    """Все дни месяца кнопками.

    В отличие от планирования недели тут не фильтруем по дню недели: смены
    случаются и в субботу, а задним числом важно записать то, что было.
    """
    ym = f"{year}-{month:02d}"
    kb = InlineKeyboardBuilder()
    last = calendar.monthrange(year, month)[1]
    for day in range(1, last + 1):
        d = date(year, month, day)
        mark = "✅" if d in selected else ""
        kb.button(
            text=f"{mark}{day} {domain.WEEKDAY_SHORT[d.weekday()]}",
            callback_data=ManualCb(action="day", ym=ym, arg=str(day)),
        )
    kb.adjust(4)
    kb.row(
        InlineKeyboardButton(
            text=f"💾 Сохранить ({len(selected)})",
            callback_data=ManualCb(action="save", ym=ym, arg=str(num)).pack(),
        ),
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=ManualCb(action="month", ym=ym).pack()
        ),
    )
    return kb.as_markup()


# --- Настройки ------------------------------------------------------------

# Варианты тихих часов: «начало-конец» в целых часах либо off — совсем без тишины.
# Двоеточие в callback_data — разделитель самого aiogram, поэтому только часы.
QUIET_PRESETS = ((22, 7), (23, 7), (23, 8), (0, 8))
# Во сколько предупреждать вечером накануне утренней смены.
EVENING_PRESETS = (18, 19, 20, 21, 22)


def settings_menu(prefs) -> InlineKeyboardMarkup:
    """Все настройки одним экраном: нажатие переключает и перерисовывает меню."""
    kb = InlineKeyboardBuilder()
    for flag, icon, title, on, off in texts.PREF_LABELS:
        state = on if getattr(prefs, flag) else off
        mark = "✅" if getattr(prefs, flag) else "❌"
        kb.button(
            text=f"{mark} {icon} {title}: {state}",
            callback_data=SettingsCb(action="toggle", arg=flag),
        )
    kb.button(
        text=f"🌙 Тихие часы: {prefs.quiet_label}",
        callback_data=SettingsCb(action="quiet"),
    )
    kb.button(
        text=f"🌆 Вечером накануне: {prefs.evening_hour:02d}:00",
        callback_data=SettingsCb(action="evening"),
    )
    if prefs.muted_until is not None:
        kb.button(
            text=f"🔔 Снять тишину (до {prefs.muted_until:%H:%M})",
            callback_data=SettingsCb(action="unmute"),
        )
    else:
        kb.button(text="🔇 Тишина до конца дня", callback_data=SettingsCb(action="mute"))
    kb.button(text="✖️ Закрыть", callback_data=SettingsCb(action="close"))
    kb.adjust(1)
    return kb.as_markup()


def quiet_choice(prefs) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for start, end in QUIET_PRESETS:
        current = (
            not prefs.quiet_off
            and (prefs.quiet_from.hour, prefs.quiet_from.minute) == (start, 0)
            and (prefs.quiet_to.hour, prefs.quiet_to.minute) == (end, 0)
        )
        kb.button(
            text=("• " if current else "") + f"{start:02d}:00–{end:02d}:00",
            callback_data=SettingsCb(action="quiet_set", arg=f"{start}-{end}"),
        )
    kb.button(
        text=("• " if prefs.quiet_off else "") + "Без тихих часов",
        callback_data=SettingsCb(action="quiet_set", arg="off"),
    )
    kb.button(text="⬅️ Назад", callback_data=SettingsCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


def evening_choice(prefs) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for hour in EVENING_PRESETS:
        kb.button(
            text=("• " if hour == prefs.evening_hour else "") + f"{hour:02d}:00",
            callback_data=SettingsCb(action="evening_set", arg=str(hour)),
        )
    kb.adjust(3)
    kb.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=SettingsCb(action="menu").pack()
        )
    )
    return kb.as_markup()


def mute_menu(prefs) -> InlineKeyboardMarkup:
    """Насколько замолчать. «Пора выходить» приходит всё равно — это будильник."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🌅 До утра", callback_data=SettingsCb(action="mute_set", arg="morning"))
    kb.button(text="🌘 На сутки", callback_data=SettingsCb(action="mute_set", arg="24h"))
    if prefs.muted_until is not None:
        kb.button(text="🔔 Снять тишину", callback_data=SettingsCb(action="unmute"))
    kb.button(text="⚙️ Все настройки", callback_data=SettingsCb(action="menu"))
    kb.adjust(2)
    return kb.as_markup()


# --- Админка --------------------------------------------------------------

def admin_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Статистика по всем", callback_data=AdminCb(action="stats"))
    kb.button(text="👥 Пользователи", callback_data=AdminCb(action="users"))
    kb.button(text="📣 Рассылка", callback_data=AdminCb(action="cast"))
    kb.button(text="🚪 Выйти", callback_data=AdminCb(action="logout"))
    kb.button(text="✖️ Закрыть", callback_data=AdminCb(action="close"))
    kb.adjust(1)
    return kb.as_markup()


def admin_back() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню админки", callback_data=AdminCb(action="menu"))
    return kb.as_markup()


def admin_users(users) -> InlineKeyboardMarkup:
    """Список пользователей кнопками — по клику открывается карточка."""
    kb = InlineKeyboardBuilder()
    for u in users[:config.ADMIN_USER_BUTTONS]:
        # Текст кнопки Telegram как HTML не разбирает — экранировать тут нечего,
        # но длину имени лучше подрезать, иначе кнопка расползается.
        name = u.name if len(u.name) <= 24 else u.name[:23] + "…"
        kb.button(
            text=f"👤 {name} · {len(u.shifts)} {domain.shifts_word(len(u.shifts))}",
            callback_data=AdminCb(action="user", arg=str(u.user_id)),
        )
    kb.button(text="⬅️ В меню админки", callback_data=AdminCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


def admin_user_card() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К списку", callback_data=AdminCb(action="users"))
    kb.button(text="🛠 В меню админки", callback_data=AdminCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


def admin_broadcast(count: int) -> InlineKeyboardMarkup:
    """Подтверждение рассылки: без этой кнопки ничего никому не уходит."""
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"📣 Отправить — {count} чел.", callback_data=AdminCb(action="cast_go")
    )
    kb.button(text="⬅️ Отмена", callback_data=AdminCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()
