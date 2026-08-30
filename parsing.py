"""Разбор ручного ввода: смены («5.08 1») и штрафы («15.08 опоздание»)."""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

# «5», «5.08», «05.08.2026», допускается диапазон «10-12.08»
TOKEN_RE = re.compile(
    r"""^\s*
    (?P<d1>\d{1,2})(?:\.(?P<m1>\d{1,2}))?(?:\.(?P<y1>\d{2,4}))?
    (?:\s*[-–—]\s*(?P<d2>\d{1,2})(?:\.(?P<m2>\d{1,2}))?(?:\.(?P<y2>\d{2,4}))?)?
    [\s:=]+
    (?P<shift>[123])(?:\s*-?\s*я)?\s*
    (?:смена)?\s*$""",
    re.VERBOSE | re.IGNORECASE,
)


def _mk_date(day: str, month: str | None, year: str | None, dy: int, dm: int) -> date:
    m = int(month) if month else dm
    y = int(year) if year else dy
    if y < 100:
        y += 2000
    d = int(day)
    last = calendar.monthrange(y, m)[1]
    if not 1 <= m <= 12 or not 1 <= d <= last:
        raise ValueError("некорректная дата")
    return date(y, m, d)


def parse_entries(
    text: str, default_year: int, default_month: int
) -> tuple[list[tuple[date, int]], list[str]]:
    """Возвращает (список (дата, номер смены) без дублей, список нераспознанных строк)."""
    entries: dict[tuple[date, int], None] = {}
    errors: list[str] = []
    chunks = [c.strip() for c in re.split(r"[,;\n]+", text) if c.strip()]
    for chunk in chunks:
        m = TOKEN_RE.match(chunk)
        if not m:
            errors.append(chunk)
            continue
        num = int(m.group("shift"))
        try:
            end_month = int(m.group("m2")) if m.group("m2") else default_month
            end_year = int(m.group("y2")) if m.group("y2") else default_year
            if m.group("d2"):
                # в диапазоне «10-12.08» месяц/год берём из правой части
                d_from = _mk_date(m.group("d1"), m.group("m1") or str(end_month),
                                  m.group("y1") or str(end_year), end_year, end_month)
                d_to = _mk_date(m.group("d2"), m.group("m2"), m.group("y2"),
                                default_year, default_month)
            else:
                d_from = d_to = _mk_date(m.group("d1"), m.group("m1"), m.group("y1"),
                                         default_year, default_month)
        except ValueError:
            errors.append(chunk)
            continue
        if d_to < d_from or (d_to - d_from).days > 62:
            errors.append(chunk)
            continue
        cur = d_from
        while cur <= d_to:
            entries[(cur, num)] = None
            cur += timedelta(days=1)
    return sorted(entries.keys()), errors


# --- Штрафы ---------------------------------------------------------------

# Слова, по которым узнаём вид нарушения. Хватает начала слова.
PENALTY_WORDS: dict[str, tuple[str, ...]] = {
    "absence": ("прогул", "не был", "небыл", "неявка", "невыход", "пропуск"),
    "late": ("опозда", "запозда", "поздно"),
    "break": ("перерыв", "перекур"),
    "notice": ("доступност", "диспозиц", "не предупред", "не сообщ"),
    "other": ("друг", "проч", "нарушен", "штраф"),
}

# «15.08», «15» — необязательная дата в начале строки.
PENALTY_DATE_RE = re.compile(
    r"^\s*(?P<d>\d{1,2})(?:\.(?P<m>\d{1,2}))?(?:\.(?P<y>\d{2,4}))?(?=\s|$)"
)
# Сумма: отдельное число, возможно с копейками через запятую или точку.
PENALTY_AMOUNT_RE = re.compile(r"(?<![\d.,])(?P<n>\d{1,6}(?:[.,]\d{1,2})?)(?![\d.,])")


def parse_penalty(
    text: str, default_year: int, default_month: int, today: date
) -> tuple[date, str, Decimal | None, str | None] | None:
    """Разобрать строку штрафа: «15.08 опоздание 200 склад».

    Без даты в начале штраф относится к `today` — дате в часовом поясе бота.

    Возвращает (дата, код вида, сумма или None, заметка или None);
    None — если вид нарушения распознать не удалось.
    """
    rest = text.strip()
    if not rest:
        return None

    at_date = None
    m = PENALTY_DATE_RE.match(rest)
    if m:
        try:
            at_date = _mk_date(m.group("d"), m.group("m"), m.group("y"),
                               default_year, default_month)
        except ValueError:
            return None
        rest = rest[m.end():].strip()

    low = rest.lower()
    kind = None
    best = len(low)
    for code, words in PENALTY_WORDS.items():
        for word in words:
            pos = low.find(word)
            if pos != -1 and pos < best:   # берём тот вид, что упомянут раньше
                kind, best = code, pos
    if kind is None:
        return None

    amount = None
    am = PENALTY_AMOUNT_RE.search(rest)
    if am:
        try:
            amount = Decimal(am.group("n").replace(",", "."))
        except InvalidOperation:
            amount = None
        else:
            rest = (rest[:am.start()] + rest[am.end():])

    # Заметка — всё, что осталось помимо слова-вида.
    note = rest.strip(" .,;-—")
    for word in PENALTY_WORDS[kind]:
        idx = note.lower().find(word)
        if idx != -1:
            end = idx + len(word)
            while end < len(note) and note[end].isalpha():
                end += 1
            note = (note[:idx] + note[end:]).strip(" .,;-—")
            break
    return at_date or today, kind, amount, note or None
