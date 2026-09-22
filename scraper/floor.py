"""Shared Czech floor grammar: ONE storey scale, ground = 0, on all nine portals.

`listings.floor` means the same storey whatever portal wrote it: **přízemí = 0,
1. patro = 1, 1. NP = 0, suterén = −1**. Ground = 0 is not a preference — it is the
only convention anything in this repo ever DECLARED (this module, the LLM parse
prompt, `scraper/source_parsers/common.py`); ground = 1 was undeclared portal
passthrough on six portals until W8, and the column was a ~50/50 mix of the two
(the same flat read 3 from its idnes row and 4 from its sreality row).

**A portal's convention is contract DATA, not a per-parser branch.** Each
`scraper.attribute_contract` floor cell declares which scale its source key carries:

  * `ground0` — the key counts from the ground storey (ceskereality's `patro`).
  * `ground1` — the key counts the STOREY ORDINAL, 1 = ground (every `podlaží`-named
    key: sreality `floor_number`, bezrealitky `etage`, mmreality `floor`, remax
    `cislo podlazi`, realitymix `číslo podlaží v domě`, maxima `podlaží`).
  * `word` — the VALUE states its own convention in Czech ("2. patro (3. NP)"), so the
    number alone is meaningless and the grammar below reads the word.

`floor_from_portal` is the one converter and it REFUSES a bare integer whose cell
declares no convention: reading a number off a key whose Czech meaning nobody wrote
down is exactly how ceskereality's `patro` and realitymix's `podlaží` — one storey
apart — ended up in a single bare-int reader.

`normalize_floor` is the value-side grammar (the word wins wherever a value carries
one, on every portal). `floor_from_text` is the free-text miner built on it (bazos, the
one portal with no structured floor field): HIGH-PRECISION only — it fires on explicit
NUMERIC unit-floor cues and defers the ambiguous tail (spelled-out ordinals,
mezonet/loft, bare 'suterén' in prose). The chief trap is the BUILDING total leaking in
as the unit's floor ("v 6. patře šestipodlažního domu", "z celkových 10 pater"): the
unit floor uses the positional-ordinal NOUN ('6. patře'), the building total the
ADJECTIVAL form ('šestipodlažní') or an explicit 'celkem'/'z celkových' phrase —
lexically separable, so the miner captures the former and reads the latter only as
`total_floors`, then drops any floor that is not below the stated total.

`total_floors` is a PODLAŽÍ COUNT on every portal (ground storey included), so a
building's top storey is `total_floors - 1` under ground = 0 — which is what
`is_plausible_floor` enforces. That count convention is also why the two patra-worded
total cues below are read as `n + 1`: "třípatrový dům" / "z celkových 3 pater" is three
storeys ABOVE the ground one, i.e. four podlaží.

Text is diacritics-folded before matching (přízemí==prizemi, patře==patre), so a
listing that drops accents reads the same; patterns are written folded/ASCII.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from unicodedata import combining, normalize

FloorConvention = Literal["ground0", "ground1", "word"]

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
# 'patr' stem entirely. The sign is captured: idnes spells a basement '-1. patro'.
_PATRO_NOUN = r"(?:patr[oau]|patre|patrem)\b"
# The sign may not be the hyphen of a storey RANGE: in "1.-2. patro" (a maisonette)
# the '-' separates two ordinals, so a bare `-?` read it as -2 = suterén.
_PATRO_RE = re.compile(rf"(?<![\d.])(-?\d{{1,2}})\.\s*{_PATRO_NOUN}")
# přízemí (ground) -> 0; 'zvýšené/snížené přízemí' is still the ground storey.
_PRIZEMI_RE = re.compile(r"prizem")
# suterén (basement) -> -1. NOT 'sklep' (a cellar the flat HAS, not the storey it
# is ON), so the bare token is only trusted from a structured floor field.
_SUTEREN_RE = re.compile(r"suteren")
# The number a bare-int cell carries, sign included: ceskereality writes "2.".
_BARE_INT_RE = re.compile(r"-?\d+")
# Any storey WORD at all. A value carrying one has already spoken for itself, so when
# the grammar reads it and refuses it (out of band) the key's convention must not get a
# second go at the bare number — "45. patro" is not 44 under any reading.
_WORD_CUE_RE = re.compile(
    rf"prizem|suteren|\d{{1,2}}\.\s*(?:np|pp|nadzemni|podzemni|{_PATRO_NOUN})")


def _bounded(value: int) -> int | None:
    return value if _FLOOR_MIN <= value <= _FLOOR_MAX else None


def is_plausible_floor(floor: int | None, total_floors: int | None) -> bool:
    """A residential storey in a sane band and not above the building's top one.

    The shared guard for any floor write. `total_floors` counts PODLAŽÍ including the
    ground storey, so under ground = 0 the top storey is `total_floors - 1`: a floor at
    or above the count itself is a misread building number, not a flat.
    """
    if floor is None:
        return False
    if not (_FLOOR_MIN <= floor <= _FLOOR_MAX):
        return False
    if total_floors is not None and floor > total_floors - 1:
        return False
    return True


def normalize_floor(text: str | None) -> int | None:
    """One Czech floor expression -> canonical storey (ground = 0), or None.

    Recognizes přízemí, N. patro, N.NP, N.PP, suterén (diacritics-insensitive).
    Returns None for a bare integer (no keyword = no convention to read) or any
    unrecognized form, so a caller never silently reads an NP value as a patro.

    přízemí is read FIRST because idnes files 'snížené přízemí' as '1. PP': the half-sunk
    ground storey is the one the advert names, not the basement its parenthetical numbers
    it as, and letting PP win would move those flats to -1 (= suterén).
    """
    if not text:
        return None
    folded = _fold(text)
    if _PRIZEMI_RE.search(folded):
        return 0
    m = _PATRO_RE.search(folded)
    if m:
        return _bounded(int(m.group(1)))
    m = _NP_RE.search(folded)
    if m:
        return _bounded(int(m.group(1)) - 1)
    m = _PP_RE.search(folded)
    if m:
        return _bounded(-int(m.group(1)))
    if _SUTEREN_RE.search(folded):
        return -1
    return None


def floor_from_portal(convention: FloorConvention | None, value: Any) -> int | None:
    """A portal's stated floor -> the canonical storey, under its DECLARED convention.

    A value carrying a Czech storey word is read through `normalize_floor` whatever the
    key's convention is — the word is the portal speaking about that one advert, and the
    convention only says how to read a number with no word on it. `ground1` shifts a
    positive ordinal down by one; 0 and negatives are left alone (sreality emits BOTH 0
    and 1 for the ground storey, and -1 is suterén under either reading, so a blanket
    decrement would invent basements).

    Raises on an undeclared convention rather than guessing: a bare integer whose Czech
    meaning is written down nowhere is the defect this function exists to end.
    """
    if convention is None:
        raise ValueError(
            "floor_from_portal: the contract cell declares no convention — a bare "
            "storey number cannot be read without one (ground0 | ground1 | word)")
    text = value if isinstance(value, str) else None
    if text is not None:
        spelled = normalize_floor(text)
        if spelled is not None:
            return spelled
        if _WORD_CUE_RE.search(_fold(text)):
            return None
    if convention == "word":
        return None
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif text is not None and (m := _BARE_INT_RE.search(text)):
        number = int(m.group(0))
    else:
        return None
    return number - 1 if convention == "ground1" and number >= 1 else number


# --- Free-text miner ---------------------------------------------------------

# Building-total cues -> total_floors, as a PODLAŽÍ count (ground storey included).
# 'Podlaží celkem: 6' and the digit adjectival '6-podlažní' / '6podlažní' already count
# podlaží; the patra-worded cues ('z celkových 10 pater', '6patrový') count the storeys
# ABOVE the ground one, so they are +1. Word-number adjectivals ('šestipodlažní') carry
# no digit and are intentionally left unparsed — they also never match the unit-floor
# patterns, so the trap is closed without reading them.
_TOTAL_LABEL_RE = re.compile(r"podlazi\s+celkem\s*[:\-]?\s*(\d{1,2})")
_TOTAL_CELKEM_PATER_RE = re.compile(r"z\s+celkov\w+\s+(\d{1,2})\s+pater")
_TOTAL_ADJ_PODLAZI_RE = re.compile(r"(\d{1,2})\s*-?\s*podlazni\w*")
_TOTAL_ADJ_PATRO_RE = re.compile(r"(\d{1,2})\s*-?\s*patrov\w*")
_TOTAL_CUES: tuple[tuple[re.Pattern[str], int], ...] = (
    (_TOTAL_LABEL_RE, 0), (_TOTAL_CELKEM_PATER_RE, 1),
    (_TOTAL_ADJ_PODLAZI_RE, 0), (_TOTAL_ADJ_PATRO_RE, 1),
)

# A spec-style 'Podlaží: <expr>' label: the value is read through normalize_floor
# so its embedded NP/patro form ('Podlaží: 4. NP') is converted correctly. A bare
# 'Podlaží: 7' with no NP/patro keyword yields None (the integer's convention is
# unknowable from the label alone) and is left to the text lane. 'Podlaží celkem:' has a
# word between 'podlazi' and the colon, so it never matches this floor label.
# The capture stops at a comma/semicolon: it routinely runs past the value into the next
# clause, and a 'přízemí' three words later would otherwise beat the label's own '3. NP'.
_FLOOR_LABEL_RE = re.compile(r"\bpodlazi\s*[:\-]\s*([^\n,;]{1,30})")
# A numeric unit-floor expression, optionally bound by a leading preposition.
_FLOOR_EXPR = rf"\d{{1,2}}\.\s*(?:np|nadzemni\w*\s+podlazi|pp|podzemni\w*\s+podlazi|{_PATRO_NOUN})"
_FLOOR_PREP_RE = re.compile(rf"\b(?:v|ve|na)\s+({_FLOOR_EXPR})")
_FLOOR_BARE_RE = re.compile(rf"\b({_FLOOR_EXPR})")
_PRIZEMI_TEXT_RE = re.compile(r"\bprizem")


def _parse_total_floors(folded: str) -> int | None:
    for rx, above_ground in _TOTAL_CUES:
        m = rx.search(folded)
        if m:
            value = int(m.group(1)) + above_ground
            if 1 <= value <= _FLOOR_MAX:
                return value
    return None


def floor_from_text(haystack: str | None) -> tuple[int | None, int | None]:
    """Mine (floor, total_floors) from free text, high-precision only.

    Returns (None, None) when no unambiguous numeric unit-floor cue is present —
    the ambiguous tail (spelled-out ordinals, mezonet/loft, bare 'suterén') is
    left to the text lane. Reads the building total separately and drops a
    captured floor that is not below it (most likely a building number we grabbed).
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
