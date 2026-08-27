"""Portal publish-date parsing for listings.published_at (migration 266).

One home for every portal's date format so the malformed-input discipline
(return None, never raise — the forgiving-parser convention) isn't
re-implemented per parser. All sources are day-granular except bezrealitky's
ISO timestamp; the timestamptz column stores a bare date as midnight UTC.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from unicodedata import combining, normalize
from zoneinfo import ZoneInfo

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

# Czech portals render a fresh listing's date RELATIVELY and only fall back to a
# long-form date once it ages out. ceskereality's "Datum vložení" reads "včera"
# for a listing posted yesterday — so the absolute-date regex above missed
# exactly the rows where the publish date matters most (a brand-new listing),
# leaving published_at NULL on every freshly-inserted one.
#
# Resolved against Europe/Prague, not UTC: the portals are Czech and render
# "dnes" in local time, so a walk running at 23:30 UTC (01:30 CEST) would
# otherwise resolve "dnes" to the previous day.
_PRAGUE = ZoneInfo("Europe/Prague")
_RELATIVE_DAYS: dict[str, int] = {
    "dnes": 0,
    "vcera": 1,
    "predevcirem": 2,
}
# "před 3 dny" / "před 1 dnem" / "před týdnem".
_PRED_N_DNY_RE = re.compile(r"\bpred\s+(\d{1,3})\s+dn", re.UNICODE)
_PRED_TYDNEM_RE = re.compile(r"\bpred\s+tydnem\b", re.UNICODE)


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


def czech_date(text: str | None, today: date | None = None) -> date | None:
    """A Czech date to a date, else None.

    Handles the long form ("10. února 2026") and the RELATIVE forms portals use
    for recent listings ("dnes", "včera", "předevčírem", "před 3 dny", "před
    týdnem"). `today` is injectable so the relative arms are testable without
    freezing the clock; it defaults to today in Europe/Prague.
    """
    if not text:
        return None
    folded = _fold(text)
    anchor = today or datetime.now(_PRAGUE).date()
    for word, delta in _RELATIVE_DAYS.items():
        if word in folded:
            return anchor - timedelta(days=delta)
    m_rel = _PRED_N_DNY_RE.search(folded)
    if m_rel:
        return anchor - timedelta(days=int(m_rel.group(1)))
    if _PRED_TYDNEM_RE.search(folded):
        return anchor - timedelta(days=7)
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
