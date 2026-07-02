"""Portal publish-date parsing for listings.published_at (migration 266).

One home for every portal's date format so the malformed-input discipline
(return None, never raise — the forgiving-parser convention) isn't
re-implemented per parser. All sources are day-granular except bezrealitky's
ISO timestamp; the timestamptz column stores a bare date as midnight UTC.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from unicodedata import combining, normalize

# bazos renders the posting date as "[D.M. YYYY]", optionally preceded by a
# promotion marker ("- TOP - [9.6. 2026]").
_BAZOS_DATE_RE = re.compile(r"\[\s*(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{4})\s*\]")

# "10. února 2026" — the ceskereality "Datum vložení" format. Month names are
# matched diacritics-folded; genitive is what the portal renders, nominative
# kept as a cheap safety net.
_CZECH_DATE_RE = re.compile(r"(\d{1,2})\.\s*([^\W\d_]+)\s+(\d{4})", re.UNICODE)
_CZECH_MONTHS: dict[str, int] = {
    "ledna": 1, "leden": 1,
    "unora": 2, "unor": 2,
    "brezna": 3, "brezen": 3,
    "dubna": 4, "duben": 4,
    "kvetna": 5, "kveten": 5,
    "cervna": 6, "cerven": 6,
    "cervence": 7, "cervenec": 7,
    "srpna": 8, "srpen": 8,
    "zari": 9,
    "rijna": 10, "rijen": 10,
    "listopadu": 11, "listopad": 11,
    "prosince": 12, "prosinec": 12,
}


def _fold(text: str) -> str:
    return "".join(c for c in normalize("NFD", text) if not combining(c)).lower()


def _date_or_none(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def bazos_posted_date(text: str | None) -> date | None:
    """The bracketed "[D.M. YYYY]" date off a bazos card/detail. NOTE: bazos
    re-stamps this on every bump / TOP renewal — it is a LAST-BUMP date, not
    first publication; still the tightest publish bound the portal exposes."""
    if not text:
        return None
    m = _BAZOS_DATE_RE.search(text)
    if not m:
        return None
    day, month, year = (int(g) for g in m.groups())
    return _date_or_none(year, month, day)


def czech_date(text: str | None) -> date | None:
    """A Czech long-form date ("10. února 2026") to a date, else None."""
    if not text:
        return None
    m = _CZECH_DATE_RE.search(text)
    if not m:
        return None
    month = _CZECH_MONTHS.get(_fold(m.group(2)))
    if month is None:
        return None
    return _date_or_none(int(m.group(3)), month, int(m.group(1)))


def iso_date(value: Any) -> date | None:
    """A strict "YYYY-MM-DD" string (sreality's `edited`) to a date, else None."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def iso_datetime(value: Any) -> datetime | None:
    """An ISO-8601 timestamp string (bezrealitky's timeActivated) to a
    datetime, else None. Timezone offset preserved when present."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
