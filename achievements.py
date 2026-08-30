"""Достижения: каталог, подсчёт статистики, выдача и снятие.

Верхняя половина файла — чистая логика (`Stats`, `CATALOG`, `build_stats`, `evaluate`):
она ничего не знает про базу и покрыта тестами. Нижняя — `sync()`, которая ходит
в `db` и решает, что показать пользователю.

Каждая ачивка — это метрика из `Stats` и порог. Открыта, когда метрика доросла до
порога; отсюда же бесплатно берётся прогресс («63/100») для тех, что ещё закрыты.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

import config
import domain

# Тон текстов: токсичный (по умолчанию) и мягкий — переключается /toxic_off.
TOXIC = "toxic"
SOFT = "soft"


@dataclass(frozen=True)
class Achievement:
    code: str
    emoji: str
    metric: str          # имя поля в Stats
    goal: int            # порог, с которого ачивка считается открытой
    title: str
    note: str
    soft_title: str = ""
    soft_note: str = ""
    soft_emoji: str = ""
    unit: str = ""       # что считаем в прогрессе: «смен», «ночных», ...
    hidden: bool = False    # условие не показываем, пока не откроется
    losable: bool = False   # можно потерять, если метрика упала обратно
    impossible: bool = False  # издевательская: не открывается никогда

    def names(self, tone: str) -> tuple[str, str]:
        if tone == SOFT and self.soft_title:
            return self.soft_title, self.soft_note
        return self.title, self.note

    def icon(self, tone: str) -> str:
        if tone == SOFT and self.soft_emoji:
            return self.soft_emoji
        return self.emoji

    def label(self, tone: str) -> str:
        return f"{self.icon(tone)} {self.names(tone)[0]}"


def A(*args, **kwargs) -> Achievement:  # noqa: N802 — короткий конструктор для таблицы ниже
    return Achievement(*args, **kwargs)


# --- Каталог --------------------------------------------------------------
# Порядок здесь — порядок отображения в /achievements.

CATALOG: tuple[Achievement, ...] = (
    # Стаж
    A("first", "🥚", "worked", 1, "Первая смена",
      "Началось. Обратной дороги нет.", unit="смен"),
    A("ten", "🔩", "worked", 10, "Свой человек",
      "Десять смен. Тебя уже узнают на проходной.", unit="смен"),
    A("fifty", "⚙️", "worked", 50, "Полтинник",
      "Пятьдесят смен. Ты уже часть механизма.", unit="смен"),
    A("hundred", "🍭", "worked", 100, "Сладкая жизнь",
      "Сто смен. От запаха карамели уже подташнивает.", unit="смен"),
    A("veteran", "🗿", "worked", 250, "Экспонат",
      "На этом месте раньше был человек.",
      soft_emoji="🏅", soft_title="Ветеран", soft_note="250 смен. Это очень много. Ты большой молодец.",
      unit="смен"),
    A("caries", "🦷", "worked", 500, "Кариес",
      "Пятьсот смен. Завод победил: сладкое ты теперь ненавидишь.",
      soft_emoji="🍬", soft_title="Пятьсот", soft_note="500 смен. Ты знаешь это производство наизусть.",
      unit="смен"),

    # Позорные
    A("loh", "💀", "cancel_streak", 3, "Капец ты лох",
      "Три отмены подряд. Завод даже видеть тебя не хочет.",
      soft_emoji="🌧", soft_title="Не твой период", soft_note="Три отмены подряд — и ни одна не по твоей вине.",
      unit="отмен подряд"),
    A("clown", "🤡", "unconfirmed", 10, "Клоун цеха",
      "Десять неподтверждённых смен. Ты работаешь. Наверное. Данных нет.",
      soft_emoji="🗂", soft_title="Руки не дошли", soft_note="Десять смен ждут подтверждения: /confirm",
      unit="смен"),
    A("corpse", "🪫", "night_streak", 5, "Труп",
      "Пять ночных подряд. Биоритмы уничтожены. Соболезную.",
      soft_emoji="🛌", soft_title="Ночной марафон", soft_note="Пять ночных подряд. Отоспись как следует.",
      unit="ночных подряд"),
    A("slowpoke", "🐌", "slow_confirms", 1, "Вспомнил",
      "Подтвердил смену спустя семь дней. Спасибо, что вспомнил.",
      soft_emoji="🕰", soft_title="Лучше поздно", soft_note="Смена подтверждена спустя неделю. Всё равно засчитано."),
    A("melted", "🫠", "idle_now", 21, "Растёкся",
      "Три недели без смен. Всё нормально? Просто спрашиваю.",
      soft_emoji="⏸", soft_title="Долгая пауза", soft_note="Три недели без смен. Надеюсь, по хорошему поводу.",
      unit="дней"),
    A("ghost", "👻", "absent", 1, "Тебя не было",
      "Смена была. Тебя — нет. Никто не заметил. Наверное.",
      soft_emoji="🤍", soft_title="Пропуск", soft_note="Одну смену пропустил. Бывает у всех."),
    A("six_am", "🥱", "morning_shifts", 10, "Кто придумал 6 утра",
      "Десять первых смен. Этого человека нужно найти.", unit="смен"),

    # Штрафы
    A("fined", "⚖️", "penalties", 1, "Мелкий шрифт",
      "Первый штраф. Оказывается, там был мелкий шрифт.",
      soft_emoji="📄", soft_title="Первый штраф",
      soft_note="Бывает у всех. Записал, чтобы не забыть про удержание."),
    A("hurry", "🏃‍♂️", "late_penalties", 3, "Вечно бежишь",
      "Три опоздания. Будильник — это тоже часть работы.",
      soft_emoji="⏱", soft_title="Не успеваешь",
      soft_note="Три опоздания. Может, стоит выходить пораньше?", unit="раз"),
    A("donor", "🩸", "penalty_money", 1000, "Спонсор завода",
      "Тысяча злотых штрафов. Ты уже доплачиваешь за право работать.",
      soft_emoji="💸", soft_title="Тысяча штрафов",
      soft_note="Тысяча злотых удержаний. Дальше будет аккуратнее.", unit="zł"),

    # Мета: стёб над самим ботом
    A("accountant", "🧮", "money_views_7d", 20, "Бухгалтер отчаяния",
      "Двадцать раз за неделю открыл /money. Деньги от этого не растут. Проверено.",
      unit="раз"),
    A("narcissus", "🪞", "ach_views", 10, "Нарцисс",
      "Десятый раз смотришь этот список. Достижений больше не стало.", unit="раз"),
    A("void", "🕳️", "empty_views", 1, "Пустота",
      "Открыл /shifts, а там ничего. Красиво."),
    A("oracle", "🔮", "planned_ahead", 21, "Ясновидящий",
      "Смены записаны на три недели вперёд. Тебя ждёт… завод.", unit="дней"),

    # Ночные
    A("owl", "🌙", "nights_60d", 10, "Сова",
      "Десять ночных за два месяца. Солнце переоценено.",
      unit="ночных", losable=True),
    A("bat", "🦇", "nights", 50, "Нетопырь",
      "Пятьдесят ночных. Рассвет ты видишь чаще, чем закат.", unit="ночных"),
    A("enough", "🤢", "nights", 100, "Больше не могу",
      "Сто ночных. Ты больше никогда добровольно не купишь мармелад.", unit="ночных"),
    A("newyear", "🌌", "newyear_shifts", 1, "Под ёлочкой",
      "Работал в новогоднюю ночь. Куранты били где-то там.", hidden=True),

    # Ритм
    A("week5", "🔥", "max_week", 5, "Неделя в огне",
      "Пять смен за неделю. Полный комплект.", unit="смен"),
    A("circle", "♻️", "circle_weeks", 1, "Карусель",
      "Первая, вторая и ночная — всё за одну неделю. Организм в недоумении."),
    A("streak4", "📅", "week_streak", 4, "Месяц на ногах",
      "Четыре недели подряд со сменами. Ни одной пустой.", unit="недель"),
    A("clean", "🍫", "clean_periods", 1, "В глазури",
      "Период закрыт без единой неподтверждённой смены. Ровно, гладко, без подтёков."),

    # Парные — приходят вместе
    A("wanted", "😇", "ambitious_weeks", 1, "Хотел как лучше",
      "Записал пять смен на неделю. Амбициозно."),
    A("always", "🤦", "failed_ambitions", 1, "Получилось как всегда",
      "Записал пять — вышел на две. Классика."),

    # Дисциплина учёта
    A("honest", "✅", "confirmed", 20, "Всё по чесноку",
      "Двадцать смен подтверждены честно, по фактическим часам.", unit="смен"),
    A("fast", "⚡", "fast_confirms", 5, "Шустрый",
      "Пять раз подтвердил смену сразу, не отходя от проходной.", unit="раз"),

    # Конфетный цех: продукция и сезоны
    A("layers", "🧇", "work_streak", 5, "Слоёный",
      "Пять смен подряд. Как вафля: слой работы, слой сна, слой работы.", unit="смен"),
    A("caramel", "🌡", "work_streak", 10, "Карамелизация",
      "Десять смен подряд без единого пропуска. Загустел.", unit="смен"),
    A("pretzel", "🥨", "pretzel", 1, "Скрутило",
      "Первая, вторая и ночная — три дня подряд. График скрутился кренделем."),
    A("bird_milk", "🕊", "bird_milk", 1, "Суфле",
      "С ночной отпустили раньше. Лёгкая, воздушная, будто и не было.", hidden=True),
    A("advent", "🎄", "advent_season", 15, "Адвентовый ад",
      "Пятнадцать смен за октябрь–декабрь. Кто-то откроет дверцу и не узнает, что ты не спал.",
      unit="смен"),
    A("door24", "🗓", "dec24", 1, "Дверца №24",
      "Смена 24 декабря. Последняя дверца — и ты внутри.", hidden=True),
    A("mikolajki", "🎅", "mikolajki", 1, "Mikołajki",
      "Смена на Микołajki. Подарки собирал лично.", hidden=True),
    A("easter", "🐰", "easter_season", 15, "Пасхальный аврал",
      "Пятнадцать смен за март–апрель. Кролики сами себя не сделают.", unit="смен"),
    A("valentine", "💘", "valentine", 1, "Сердечки",
      "Смена в первой половине февраля. Кто-то дарит. Ты производишь.", hidden=True),

    # Редкие
    A("friday13", "🖤", "friday13", 1, "Пятница, 13-е",
      "Вышел на смену в пятницу 13-го. И вернулся.", hidden=True),
    A("summer", "☀️", "summer_shifts", 20, "Лето прошло",
      "Двадцать смен за июль и август. Лето прошло, ты не заметил.",
      unit="смен", hidden=True),

    # Деньги
    A("beer", "🍬", "money", 1000, "Первая тысяча",
      "Тысяча злотых. В мармеладе это целый поддон.", unit="zł"),
    A("rich", "💸", "money", 10000, "Богач",
      "Десять тысяч. Теперь ты можешь купить… ну, что-то. Это Польша.", unit="zł"),
    A("tycoon", "🏦", "money", 100000, "Магнат",
      "Сто тысяч злотых заработано. Сто. Тысяч.", unit="zł"),

    # Утешительные
    A("lucky", "🍀", "cancelled", 5, "Халява",
      "Пять отменённых смен. Завод сам выдал тебе выходные.", unit="смен"),
    A("runner", "🏃", "early", 10, "Отпустили",
      "Десять раз отпустили раньше. Умеешь ты выбирать дни.", unit="раз"),
    A("rest", "😴", "max_idle", 7, "Простой",
      "Целая неделя без единой смены. Так и надо.", unit="дней"),

    A("spotless", "🛡", "penalty_free_periods", 3, "Чистый лист",
      "Три расчётных периода подряд без единого штрафа. Договор доволен.", unit="периодов"),

    # Мета-мета
    A("collector", "🥇", "unlocked_count", 10, "Достигатор",
      "Десять достижений. Ты играешь в бота вместо того, чтобы работать.", unit="шт"),
    A("locked", "🚫", "never", 1, "Недоступно",
      "Условия неизвестны. Открыть нельзя. Просто висит.", impossible=True),
)

BY_CODE = {a.code: a for a in CATALOG}

# «Достигатор» считается после всех остальных — он смотрит на их количество.
LAST_PASS = ("collector",)


# --- Статистика -----------------------------------------------------------

@dataclass
class Stats:
    """Всё, на что смотрят ачивки. Поля названы так же, как `metric` в каталоге."""
    worked: int = 0             # прошедшие неотменённые смены
    confirmed: int = 0          # из них подтверждённые вручную
    unconfirmed: int = 0        # прошедшие, но так и не подтверждённые
    nights: int = 0
    nights_60d: int = 0
    morning_shifts: int = 0     # 1я смена, 06:00
    hours: Decimal = Decimal(0)
    money: int = 0
    absent: int = 0
    cancelled: int = 0
    penalties: int = 0          # сколько штрафов записано за всё время
    late_penalties: int = 0     # из них за опоздания
    penalty_money: int = 0      # сумма штрафов, zł
    early: int = 0              # отпустили раньше
    cancel_streak: int = 0
    night_streak: int = 0
    max_week: int = 0
    circle_weeks: int = 0
    week_streak: int = 0
    clean_periods: int = 0
    ambitious_weeks: int = 0
    failed_ambitions: int = 0
    planned_ahead: int = 0      # на сколько дней вперёд записаны смены
    idle_now: int = 0           # дней с последней смены
    max_idle: int = 0           # самый длинный перерыв за всю историю
    slow_confirms: int = 0
    fast_confirms: int = 0
    friday13: int = 0
    newyear_shifts: int = 0
    work_streak: int = 0        # смен подряд по календарным дням
    pretzel: int = 0            # 1я, 2я и ночная три дня подряд
    advent_season: int = 0      # смен за октябрь–декабрь (лучший год)
    easter_season: int = 0      # смен за март–апрель (лучший год)
    dec24: int = 0
    mikolajki: int = 0
    valentine: int = 0
    bird_milk: int = 0          # с ночной отпустили раньше
    penalty_free_periods: int = 0
    summer_shifts: int = 0
    money_views_7d: int = 0
    ach_views: int = 0
    empty_views: int = 0
    unlocked_count: int = 0
    never: int = 0              # всегда 0 — для 🚫 «Недоступно»


# Подтверждение считается быстрым, если пришло вскоре после вопроса бота.
FAST_CONFIRM = timedelta(hours=1)
SLOW_CONFIRM = timedelta(days=7)
RECENT_NIGHTS = timedelta(days=60)


def work_week(work_date: date, shift_num: int) -> date:
    """Понедельник рабочей недели смены. Ночная с воскресенья — уже следующая неделя."""
    if domain.shift(shift_num).is_night and work_date.weekday() == 6:
        return work_date + timedelta(days=1)
    return domain.monday_of(work_date)


def _max_run(days: list[date]) -> int:
    """Самая длинная серия идущих подряд календарных дней."""
    if not days:
        return 0
    best = run = 1
    for prev, cur in zip(days, days[1:]):
        run = run + 1 if cur - prev == timedelta(days=1) else 1
        best = max(best, run)
    return best


def build_stats(shifts, events: dict[str, int], now: datetime, penalties=()) -> Stats:
    """Собрать статистику по всем сменам пользователя (включая отменённые).

    `shifts` — объекты со свойствами db.Shift; `events` — счётчики команд;
    `penalties` — штрафы из договора, если они уже заведены.
    """
    st = Stats(**events)
    today = now.date()

    past, planned = [], []
    for s in shifts:
        if s.status in ("cancelled", "absent"):
            continue
        (past if domain.shift_end(s.work_date, s.shift_num) <= now else planned).append(s)

    st.worked = len(past)
    st.hours = sum((s.hours for s in past), Decimal(0))
    st.money = int(sum((s.pay for s in past), Decimal(0)))
    st.confirmed = sum(1 for s in past if s.status == "done")
    st.unconfirmed = sum(1 for s in past if s.status != "done")
    st.nights = sum(1 for s in past if s.kind.is_night)
    st.morning_shifts = sum(1 for s in past if s.shift_num == 1)
    st.nights_60d = sum(
        1 for s in past if s.kind.is_night and now - domain.shift_end(s.work_date, s.shift_num) <= RECENT_NIGHTS
    )
    st.early = sum(
        1 for s in past if s.status == "done" and s.hours < config.SHIFT_HOURS
    )
    st.absent = sum(1 for s in shifts if s.status == "absent")
    st.cancelled = sum(1 for s in shifts if s.status == "cancelled")

    # Календарные редкости
    st.friday13 = sum(1 for s in past if s.work_date.weekday() == 4 and s.work_date.day == 13)
    st.newyear_shifts = sum(
        1 for s in past
        if (s.work_date.month, s.work_date.day) in ((12, 31), (1, 1))
    )
    st.summer_shifts = _best_season(past, (7, 8))

    # Конфетный календарь: у завода год расписан по праздникам
    st.advent_season = _best_season(past, (10, 11, 12))
    st.easter_season = _best_season(past, (3, 4))
    st.dec24 = sum(1 for s in past if (s.work_date.month, s.work_date.day) == (12, 24))
    st.mikolajki = sum(
        1 for s in past if s.work_date.month == 12 and s.work_date.day in (5, 6)
    )
    st.valentine = sum(
        1 for s in past if s.work_date.month == 2 and s.work_date.day <= 14
    )
    # 🕊 «Птичье молоко»: с ночной смены отпустили раньше — лёгкая, воздушная
    st.bird_milk = sum(
        1 for s in past
        if s.kind.is_night and s.status == "done" and s.hours < config.SHIFT_HOURS
    )

    # Смены подряд и «крендель» — три разные смены три дня подряд
    work_days = sorted({s.work_date for s in past})
    st.work_streak = _max_run(work_days)
    st.pretzel = _pretzel_days(past)

    # Периоды без штрафов
    st.penalty_free_periods = _penalty_free_periods(shifts, penalties, now.date())

    # Ночные подряд
    st.night_streak = _max_run(sorted({s.work_date for s in past if s.kind.is_night}))

    # Отмены подряд: смотрим только на «решённые» смены в хронологии
    decided = sorted(
        (s for s in shifts if s.status in ("cancelled", "absent", "done")
         or domain.shift_end(s.work_date, s.shift_num) <= now),
        key=lambda s: (s.work_date, s.shift_num),
    )
    run = 0
    for s in decided:
        run = run + 1 if s.status in ("cancelled", "absent") else 0
        st.cancel_streak = max(st.cancel_streak, run)

    # Недели
    weeks: dict[date, list] = {}
    for s in past:
        weeks.setdefault(work_week(s.work_date, s.shift_num), []).append(s)
    st.max_week = max((len(v) for v in weeks.values()), default=0)
    st.circle_weeks = sum(1 for v in weeks.values() if {s.shift_num for s in v} == {1, 2, 3})
    st.week_streak = _week_streak(sorted(weeks))

    # Амбиции: неделя, на которую записали 5+ смен, но вышло 2 или меньше
    all_weeks: dict[date, list] = {}
    for s in shifts:
        all_weeks.setdefault(work_week(s.work_date, s.shift_num), []).append(s)
    for monday, items in all_weeks.items():
        if len(items) < 5 or monday + timedelta(days=7) > today:
            continue
        st.ambitious_weeks += 1
        survived = sum(1 for s in items if s.status not in ("cancelled", "absent"))
        if survived <= 2:
            st.failed_ambitions += 1

    # Перерывы и планы
    if work_days:
        st.idle_now = (today - work_days[-1]).days
        st.max_idle = max(
            ((b - a).days - 1 for a, b in zip(work_days, work_days[1:])), default=0
        )
        st.max_idle = max(st.max_idle, st.idle_now)
    st.planned_ahead = max(((s.work_date - today).days for s in planned), default=0)

    # Скорость подтверждения
    for s in past:
        at = getattr(s, "confirmed_at", None)
        if at is None:
            continue
        delay = at - domain.shift_end(s.work_date, s.shift_num)
        if delay >= SLOW_CONFIRM:
            st.slow_confirms += 1
        elif delay <= FAST_CONFIRM:
            st.fast_confirms += 1

    st.clean_periods = _clean_periods(shifts, today)
    return st


def _best_season(shifts, months: tuple[int, ...]) -> int:
    """Смены за сезон (набор месяцев) в самом плотном году."""
    by_year: dict[int, int] = {}
    for s in shifts:
        if s.work_date.month in months:
            by_year[s.work_date.year] = by_year.get(s.work_date.year, 0) + 1
    return max(by_year.values(), default=0)


def _pretzel_days(shifts) -> int:
    """Сколько раз график скручивался кренделем: 1я, 2я и ночная три дня подряд."""
    by_day: dict[date, set[int]] = {}
    for s in shifts:
        by_day.setdefault(s.work_date, set()).add(s.shift_num)
    found = 0
    for d in sorted(by_day):
        trio = (d, d + timedelta(days=1), d + timedelta(days=2))
        if all(x in by_day for x in trio) and set().union(*(by_day[x] for x in trio)) == {1, 2, 3}:
            found += 1
    return found


def _penalty_free_periods(shifts, penalties, today: date) -> int:
    """Закрытые расчётные периоды, в которых были смены и не было ни одного штрафа."""
    fined = {domain.period_anchor(p.at_date) for p in penalties}
    worked: set[tuple[int, int]] = set()
    for s in shifts:
        if s.status not in ("cancelled", "absent"):
            worked.add(domain.period_anchor(s.work_date))
    clean = 0
    for anchor in worked - fined:
        if domain.period_bounds(*anchor)[1] < today:
            clean += 1
    return clean


def _week_streak(mondays: list[date]) -> int:
    """Самая длинная серия недель подряд, в каждой из которых была смена."""
    if not mondays:
        return 0
    best = run = 1
    for prev, cur in zip(mondays, mondays[1:]):
        run = run + 1 if cur - prev == timedelta(days=7) else 1
        best = max(best, run)
    return best


def _clean_periods(shifts, today: date) -> int:
    """Полностью прошедшие расчётные периоды, где каждая смена подтверждена."""
    periods: dict[tuple[int, int], list] = {}
    for s in shifts:
        periods.setdefault(domain.period_anchor(s.work_date), []).append(s)
    clean = 0
    for anchor, items in periods.items():
        _, end = domain.period_bounds(*anchor)
        if end >= today:
            continue
        if items and all(s.status in ("done", "cancelled", "absent") for s in items):
            clean += 1
    return clean


# --- Оценка ---------------------------------------------------------------

def evaluate(stats: Stats, already: set[str]) -> set[str]:
    """Коды ачивок, условия которых выполнены прямо сейчас."""
    earned = set()
    for a in CATALOG:
        if a.impossible or a.code in LAST_PASS:
            continue
        if getattr(stats, a.metric) >= a.goal:
            earned.add(a.code)
    # «Достигатор» смотрит на то, сколько всего уже набралось
    stats.unlocked_count = len(earned | (already - {"collector"}))
    for code in LAST_PASS:
        a = BY_CODE[code]
        if getattr(stats, a.metric) >= a.goal:
            earned.add(code)
    return earned


def progress(stats: Stats, a: Achievement) -> tuple[int, int]:
    return min(int(getattr(stats, a.metric)), a.goal), a.goal


# Формы единиц для строки прогресса: «0/1 смена», «0/50 смен».
UNIT_FORMS = {
    "смен": ("смена", "смены", "смен"),
    "ночных": ("ночная", "ночные", "ночных"),
    "раз": ("раз", "раза", "раз"),
    "дней": ("день", "дня", "дней"),
    "недель": ("неделя", "недели", "недель"),
    "периодов": ("период", "периода", "периодов"),
    "отмен подряд": ("отмена подряд", "отмены подряд", "отмен подряд"),
    "ночных подряд": ("ночная подряд", "ночные подряд", "ночных подряд"),
}


def unit_label(a: Achievement, n: int) -> str:
    """Единица измерения в правильной форме — для строки «63/100 смен»."""
    if not a.unit:
        return ""
    forms = UNIT_FORMS.get(a.unit)
    return domain.plural(n, *forms) if forms else a.unit


# --- Ранги и эквиваленты --------------------------------------------------

# Звания — по продукции: чем выше, тем сложнее изделие.
RANKS = (
    (0, "🫥 Крошка"),
    (8, "🍬 Мармеладка"),
    (40, "🧇 Вафля"),
    (100, "👷 Умпа-лумпа"),
    (200, "🥨 Крендель"),
    (400, "🐿 Белочка"),
    (700, "🍒 Пьяная вишня"),
    (1000, "🎩 Вилли Вонка"),
)


def rank(hours: Decimal) -> tuple[str, str | None, int]:
    """Текущее звание, следующее и сколько часов до него."""
    h = int(hours)
    title = RANKS[0][1]
    nxt = None
    for need, name in RANKS:
        if h >= need:
            title = name
        else:
            nxt = (name, need - h)
            break
    return (title, nxt[0], nxt[1]) if nxt else (title, None, 0)


# Зарплата в собственной продукции: единица меняется по сезону завода.
# (месяцы, эмодзи, цена штуки, формы слова, приписка)
PRODUCTS = (
    ((10, 11, 12), "🎄", Decimal("50"),
     ("адвент-календарь", "адвент-календаря", "адвент-календарей"), "Ты их и собирал."),
    ((2,), "💘", Decimal("25"),
     ("коробка конфет", "коробки конфет", "коробок конфет"), "Кто-то дарит. Ты производишь."),
    ((3, 4), "🐰", Decimal("30"),
     ("пасхальный набор", "пасхальных набора", "пасхальных наборов"), "Кролики с твоей линии."),
)
DEFAULT_PRODUCT = ("🍬", Decimal("8"),
                   ("пачка мармелада", "пачки мармелада", "пачек мармелада"), "Свежий, с линии.")


def fun_equivalent(amount: Decimal, when: date | None = None) -> str:
    """Деньги в том, что завод как раз производит — строка для /money."""
    month = (when or domain.today()).month
    icon, price, forms, tail = DEFAULT_PRODUCT
    for months, i, p, f, t in PRODUCTS:
        if month in months:
            icon, price, forms, tail = i, p, f, t
            break
    n = int(amount / price)
    if n < 1:
        return f"{icon} Даже на одну {forms[0]} не хватает."
    return f"{icon} Это {n} {domain.plural(n, *forms)}. {tail}"


# --- Связь с базой --------------------------------------------------------

EVENT_KEYS = ("money_view", "ach_view", "empty_view")


def _events(user_id: int, penalties) -> dict[str, int]:
    import db

    week_ago = domain.now() - timedelta(days=7)
    return {
        "money_views_7d": db.count_events(user_id, "money_view", since=week_ago),
        "ach_views": db.count_events(user_id, "ach_view"),
        "empty_views": db.count_events(user_id, "empty_view"),
        "penalties": len(penalties),
        "late_penalties": sum(1 for p in penalties if p.kind == "late"),
        "penalty_money": int(sum((p.amount for p in penalties), Decimal(0))),
    }


def stats_for(user_id: int) -> Stats:
    import db

    penalties = db.all_penalties(user_id)
    return build_stats(
        db.all_shifts(user_id), _events(user_id, penalties), domain.now(), penalties
    )


def sync(user_id: int) -> tuple[list[Achievement], list[Achievement], Stats]:
    """Пересчитать ачивки: вернуть (новые, потерянные, статистику)."""
    import db

    stats = stats_for(user_id)
    already = db.unlocked_codes(user_id)
    earned = evaluate(stats, already)

    fresh = sorted(earned - already, key=lambda c: [a.code for a in CATALOG].index(c))
    # Потерять можно только то, что явно помечено losable — остальное навсегда.
    lost = {c for c in already - earned if BY_CODE[c].losable}

    if fresh:
        db.unlock_achievements(user_id, fresh)
    if lost:
        db.lock_achievements(user_id, lost)
    return [BY_CODE[c] for c in fresh], [BY_CODE[c] for c in sorted(lost)], stats
