"""Shared Czech floor grammar + free-text floor mining.

One canonical reading of a Czech floor expression so the integer `listings.floor`
means the same thing whatever portal wrote it. The convention is GROUND = 0
(přízemí = 0, 1. patro = 1, 1. NP = 0), matching idnes and the LLM extraction
rubric (`scraper/source_parsers/common.py`). sreality/bezrealitky/remax store the
NP/ground=1 reading raw — the historical clash the dedup engine still tolerates
(CLAUDE.md rule #15); converting those at the source is a separate follow-up.

`normalize_floor` is the canonical expression->int grammar (the one unit any
portal can route an extracted floor token through). `floor_from_text` is the
free-text miner built on it (for bazos, the one portal with no structured floor
field): HIGH-PRECISION only — it fires on explicit NUMERIC unit-floor cues and
defers the ambiguous tail (spelled-out ordinals, mezonet/loft, bare 'suterén' in
prose) to the LLM enrichment. The chief trap is the BUILDING total leaking in as
the unit's floor ("v 6. patře šestipodlažního domu", "z celkových 10 pater"): the
unit floor uses the positional-ordinal NOUN ('6. patře'), the building total the
ADJECTIVAL form ('šestipodlažní') or an explicit 'celkem'/'z celkových' phrase —
lexically separable, so the miner captures the former and reads the latter only
as `total_floors`, then drops any floor that exceeds the stated total.

Text is diacritics-folded before matching (přízemí==prizemi, patře==patre), so a
listing that drops accents reads the same; patterns are written folded/ASCII.
"""

from __future__ import annotations

import re
from unicodedata import combining, normalize

# Canonical ground floor = 0; plausibility window for a residential storey.
_FLOOR_MIN, _FLOOR_MAX = -3, 40


def _fold(text: str) -> str:
    return "".join(c for c in normalize("NFD", text) if not combining(c)).lower()


# N. NP (nadzemní podlaží, 1-indexed from ground): k.NP -> k-1.
_NP_RE = re.compile(r"(\d{1,2})\.\s*(?:np|nadzemni\w*\s+podlazi)\b")
# N. PP (podzemní podlaží, below ground): k.PP -> -k.
_PP_RE = re.compile(r"(\d{1,2})\.\s*(?:pp|podzemni\w*\s+podlazi)\b")
# N. patro/patra/patře/patrem (ground=0 relative): k -> k. Noun forms only — the
# building ADJECTIVE 'patrový' (patr+ovy) is excluded by the trailing \b stopping
# at its 'v', and the genitive-plural building total 'pater' (pat+er) lacks the
# 'patr' stem entirely.
_PATRO_NOUN = r"(?:patr[oau]|patre|patrem)\b"
_PATRO_RE = re.compile(rf"(\d{{1,2}})\.\s*{_PATRO_NOUN}")
# přízemí (ground) -> 0; 'zvýšené/snížené přízemí' is still the ground storey.
_PRIZEMI_RE = re.compile(r"prizem")
# suterén (basement) -> -1. NOT 'sklep' (a cellar the flat HAS, not the storey it
# is ON), so the bare token is only trusted from a structured floor field.
_SUTEREN_RE = re.compile(r"suteren")


def _bounded(value: int) -> int | None:
    return value if _FLOOR_MIN <= value <= _FLOOR_MAX else None


def is_plausible_floor(floor: int | None, total_floors: int | None) -> bool:
    """A residential storey in a sane band and not above the building's total.

    The shared guard for any floor write (this module's miner AND the LLM
    enrichment fill): a floor outside [-3, 40] or exceeding a known total-floor
    count is almost always a misread building number and is rejected.
    """
    if floor is None:
        return False
    if not (_FLOOR_MIN <= floor <= _FLOOR_MAX):
        return False
    if total_floors is not None and floor > total_floors:
        return False
    return True


def normalize_floor(text: str | None) -> int | None:
    """One Czech floor expression -> canonical storey (ground = 0), or None.

    Recognizes N.NP, N.PP, N. patro, přízemí, suterén (diacritics-insensitive).
    Returns None for a bare integer (no keyword = no convention to read) or any
    unrecognized form, so a caller never silently reads an NP value as a patro.
    """
    if not text:
        return None
    folded = _fold(text)
    m = _NP_RE.search(folded)
    if m:
        return _bounded(int(m.group(1)) - 1)
    m = _PP_RE.search(folded)
    if m:
        return _bounded(-int(m.group(1)))
    m = _PATRO_RE.search(folded)
    if m:
        return _bounded(int(m.group(1)))
    if _PRIZEMI_RE.search(folded):
        return 0
    if _SUTEREN_RE.search(folded):
        return -1
    return None


# --- Free-text miner ---------------------------------------------------------

# Building-total cues -> total_floors (never the unit floor). 'Podlaží celkem: 6',
# 'z celkových 10 pater', digit adjectival '6-podlažní' / '6podlažní' / '6patrový'.
# Word-number adjectivals ('šestipodlažní') carry no digit and are intentionally
# left unparsed — they also never match the unit-floor patterns, so the trap is
# closed without reading them.
_TOTAL_LABEL_RE = re.compile(r"podlazi\s+celkem\s*[:\-]?\s*(\d{1,2})")
_TOTAL_CELKEM_RE = re.compile(r"z\s+celkov\w+\s+(\d{1,2})\s+pater")
_TOTAL_ADJ_RE = re.compile(r"(\d{1,2})\s*-?\s*(?:podlazni|patrov)\w*")

# A spec-style 'Podlaží: <expr>' label: the value is read through normalize_floor
# so its embedded NP/patro form ('Podlaží: 4. NP') is converted correctly. A bare
# 'Podlaží: 7' with no NP/patro keyword yields None (the integer's convention is
# unknowable from the label alone) and is left to the LLM. 'Podlaží celkem:' has a
# word between 'podlazi' and the colon, so it never matches this floor label.
_FLOOR_LABEL_RE = re.compile(r"\bpodlazi\s*[:\-]\s*([^\n]{1,30})")
# A numeric unit-floor expression, optionally bound by a leading preposition.
_FLOOR_EXPR = rf"\d{{1,2}}\.\s*(?:np|nadzemni\w*\s+podlazi|pp|podzemni\w*\s+podlazi|{_PATRO_NOUN})"
_FLOOR_PREP_RE = re.compile(rf"\b(?:v|ve|na)\s+({_FLOOR_EXPR})")
_FLOOR_BARE_RE = re.compile(rf"\b({_FLOOR_EXPR})")
_PRIZEMI_TEXT_RE = re.compile(r"\bprizem")


def _parse_total_floors(folded: str) -> int | None:
    for rx in (_TOTAL_LABEL_RE, _TOTAL_CELKEM_RE, _TOTAL_ADJ_RE):
        m = rx.search(folded)
        if m:
            value = int(m.group(1))
            if 1 <= value <= _FLOOR_MAX:
                return value
    return None


def floor_from_text(haystack: str | None) -> tuple[int | None, int | None]:
    """Mine (floor, total_floors) from free text, high-precision only.

    Returns (None, None) when no unambiguous numeric unit-floor cue is present —
    the ambiguous tail (spelled-out ordinals, mezonet/loft, bare 'suterén') is
    left to the LLM enrichment. Reads the building total separately and drops a
    captured floor that exceeds it (most likely a building number we grabbed).
    """
    if not haystack:
        return None, None
    folded = _fold(haystack)
    total = _parse_total_floors(folded)

    floor: int | None = None
    m = _FLOOR_LABEL_RE.search(folded)           # 1) 'Podlaží: 4. NP' label
    if m:
        floor = normalize_floor(m.group(1))
    if floor is None:                             # 2) 've 3. patře' / '6. NP'
        m = _FLOOR_PREP_RE.search(folded)
        if m:
            floor = normalize_floor(m.group(1))
    if floor is None:                             # 3) bare '3. patro' / '4. NP'
        m = _FLOOR_BARE_RE.search(folded)
        if m:
            floor = normalize_floor(m.group(1))
    if floor is None and _PRIZEMI_TEXT_RE.search(folded):  # 4) přízemí -> 0
        floor = 0

    if floor is not None and not is_plausible_floor(floor, total):
        floor = None
    return floor, total
