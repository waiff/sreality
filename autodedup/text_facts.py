"""Facts a Czech advert PRINTS, parsed once for both the benchmark and the engine (E60/E61).

`structural_truth` manufactured these parsers to LABEL pairs. W7 then showed the strongest of
them — a shared agency order code — is evidence the engine itself never sees: there is no
reference-code slot in `FEATURE_ORDER`, and `rare_token_overlap` is 0.0 on 62.8 % of the pairs
one certifies. Moving the parsing here gives the rule floor and the benchmark ONE spelling of
each fact instead of two that can drift apart.

What stays in `structural_truth` is the RULES — which combination of facts settles a pair — so
the benchmark's verdicts are still stated in one place. What lives here is only parsing: no
rule, no threshold that decides a pair, nothing that reads an engine feature.

The measurement consequence is stated once, here, so nobody forgets it: the engine's K-R
certificate and the benchmark's `pos_ref_*` labels now read the SAME string, so scoring K-R
against those labels is scoring a rule against itself. K-R is measured against the operator
and judge labels; `pos_ref_*` coverage is reported, never counted as accuracy.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from html import unescape
from typing import Iterable, Iterator, Mapping, TYPE_CHECKING

from autodedup.normalize import fact_text

if TYPE_CHECKING:  # pragma: no cover
    from autodedup.dataset import Listing

# A code shorter than this, or one carried by a crowd of listings, is a per-broker sequence
# number rather than an order key: it collides across objects and cannot certify anything.
MIN_CODE_LEN: int = 5
MAX_CODE_POPULATION: int = 8

# The cluster invariant asks the same advert's facts once per PAIR, so a 256-member group would
# re-scan one body 255 times. Keyed on the body text, which is what the readers actually parse.
BODY_CACHE: int = 65536

CODE_MASK: str = "[KOD]"

_ACCENTS = re.compile(r"[̀-ͯ]")

# `ev. číslo: 657349`, `evidenční číslo zakázky N115423`, `evidenční číslo / ID zakázky: R2256`.
# The keyword is mandatory: a bare number in a body is a price, a year or a house number.
_REFERENCE = re.compile(
    r"(?:ev\.?\s*c(?:\.|islo)?"
    r"|evidencni\s+cislo(?:\s*/\s*id\s+zakazky)?(?:\s+zakazky)?"
    r"|cislo\s+zakazky|id\s+zakazky|kod\s+zakazky|zakazka\s*c\.?"
    r"|ref\.?\s*c(?:\.|islo)?|referencni\s+cislo"
    r"|cislo\s+nabidky|kod\s+nabidky|nabidka\s*c\.?)"
    r"\s*[:\s]\s*([a-z]{0,3}\s?\d[\d/-]{2,20})(?![\w/-])"
)
# One agency echoes its own key into the bazos headline as `[ID 84553]` rather than a Czech
# keyword; the bracket IS the keyword, so the token is as anchored as the others.
_BRACKET_ID = re.compile(r"\[\s*id\s*(\d{4,9})\s*\]")

# The unit DESIGNATOR — a name for one flat inside one building. `normalize`'s `unit` slot only
# reads `jednotka 12` / `byt č. 12`; the developer adverts this benchmark lives on write
# `byt (č.3)`, `označením B36`, `apartmán č. A311`, which that slot misses entirely. A trailing
# `+` or digit is excluded so `jednotku 4+kk` (a disposition) can never become unit 4.
_UNIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"byt\w*\s*[\(\[]?\s*c\.?\s*(\d{1,4})(?![\d+]|[.,]\d)"),
    re.compile(r"jednotk\w*\s*(?:c\.|cislo)\s*([a-z]?\d{1,4})(?![\d+]|[.,]\d)"),
    re.compile(r"cisl\w*\s+bytu\s*[:\-]?\s*(\d{1,4})(?![\d+]|[.,]\d)"),
    re.compile(r"oznacen\w{0,3}\s+([a-z]\d{1,3})\b"),
    re.compile(r"apartman\w*\s*c\.?\s*([a-z]?\d{1,4})\b"),
)

# --- printed floor areas ------------------------------------------------------------------
# A body text names the balcony, the plot and the cellar too; anything outside this window is
# not a dwelling's floor area and would make two unrelated adverts look like they agree.
STATED_AREA_MIN_M2: float = 5.0
STATED_AREA_MAX_M2: float = 100000.0
# W8: an area more than this many times the listing's OWN stored area (or that many times
# smaller) is not this unit's floor area — it is the building, the plot or a cellar. The stored
# column is used as a SANITY WINDOW only, never as the value: a cross-portal parse that prints
# 70 on one side and 72 on the other must still read as one fact (W7, pair 469862/15290511).
STATED_AREA_STORED_RATIO: float = 3.0
# Mentions this close together are one enumeration — `10 m2, 20 m2, 30 m2, …` is a size MENU of
# what the landlord can offer, not a statement about the advertised unit.
MENU_MAX_GAP_CHARS: int = 12
MENU_MIN_RUN: int = 3

# Spaced and dot-grouped thousands, then the plain form. Without the grouped alternative first,
# `10 500 m2` matches at `500` and fabricates an area the advert never printed (it did, on
# ceskereality 412654) — the same thousands truncation the portal parsers carry. Fixed HERE and
# not in `normalize`, whose `m2` slot feeds scored features that this wave must not move.
_AREA_NUMBER = r"(\d{1,3}(?:[  .]\d{3})+|\d+(?:[.,]\d+)?)"
_M2_MENTION = re.compile(_AREA_NUMBER + r"\s*m2\b")
# `od 55 m2 do 900 m2`, `55 m2 az 900 m2`, `55 - 900 m2`: a RANGE of what is available.
_AREA_RANGE = re.compile(
    r"\bod\s+" + _AREA_NUMBER + r"\s*(?:m2)?\s*(?:do|az|-|–)\s*" + _AREA_NUMBER + r"\s*m2"
    r"|" + _AREA_NUMBER + r"\s*m2\s*(?:az|do|-|–)\s*" + _AREA_NUMBER + r"\s*m2"
)
# The building's own size, named as such: `budova o velikosti 10.500 m2`, `objekt disponuje
# celkovou plochou cca 10 500 m2`. Adjacency is strict on purpose — `cihlové budovy. s užitnou
# plochou 80 m2` (listing 464483) names the UNIT, and a looser window would eat it.
_BUILDING_TOTAL = re.compile(
    r"(?:budov|objekt|areal|komplex)\w*\s+(?:o\s+)?(?:celkov\w+\s+)?"
    r"(?:velikosti|rozloze|ploche|plochou|vymere|vymera)\s+(?:cca\s+)?" + _AREA_NUMBER + r"\s*m2"
    r"|celkov\w+\s+(?:plochou|plocha|vymer\w+)\s+(?:cca\s+)?" + _AREA_NUMBER + r"\s*m2"
)


def fold(text: str) -> str:
    """Deaccented lowercase — the folding every pattern here is written against."""
    return _ACCENTS.sub("", unicodedata.normalize("NFKD", text)).lower()


def reference_codes(text: str | None) -> set[str]:
    """Agency order numbers the body text names explicitly, normalised to one spelling."""
    if not text:
        return set()
    out: set[str] = set()
    folded = fold(text)
    for pattern in (_REFERENCE, _BRACKET_ID):
        for match in pattern.finditer(folded):
            code = re.sub(r"\s+", "", match.group(1)).upper().strip("/-")
            if len(code) >= MIN_CODE_LEN:
                out.add(code)
    return out


def mask_codes(text: str | None) -> str | None:
    r"""Every agency order code this module can find, replaced by one token.

    Masking runs over the ORIGINAL text (the folded copy `reference_codes` matches on is not
    index-aligned with it), joining the code's characters with `\s*` because the capture removed
    the whitespace a body may print inside the number."""
    if not text:
        return text
    out = text
    for code in reference_codes(text):
        pattern = re.compile(r"\s*".join(re.escape(ch) for ch in code), re.IGNORECASE)
        out = pattern.sub(CODE_MASK, out)
    return out


def iter_reference_matches(folded: str) -> Iterator[tuple[str, re.Match[str]]]:
    """Every reference-code match in already-folded text, as `(code, match)` pairs."""
    for pattern in (_REFERENCE, _BRACKET_ID):
        for match in pattern.finditer(folded):
            yield re.sub(r"\s+", "", match.group(1)).upper().strip("/-"), match


def unit_designators(text: str | None) -> set[str]:
    """Unit designators the body text names: `byt (č.3)`, `jednotka č. 12`, `označením B36`."""
    if not text:
        return set()
    folded = fold(text)
    return {
        match.group(1).upper()
        for pattern in _UNIT_PATTERNS
        for match in pattern.finditer(folded)
    }


# --- the printed unit CODE, read whole and in the bare form (E161) --------------------------
# `_UNIT_PATTERNS` reads `oznacen\w*\s+([a-z]\d{1,3})` and stops at the word boundary, so
# `s označením B2.2.1` reads `B2` and `s označením B1.2.2` reads `B1` — and B1 against B1 is no
# conflict at all. The bare form `byt B1.2.1 o dispozici` (no keyword) reads nothing. That is
# how b1.2.1 and b2.2.1 of Slavonínské zahrady ended in one cluster of every W16 arm.
#
# So this reader is anchored on the unit NOUN rather than on a keyword, and the code is kept
# WHOLE. Two segments is the whole point: a bare `B2` is a BUILDING and every flat in it shares
# it, so a single segment is never read — that is the fail-safe direction, because an empty set
# is never a conflict.
_UNIT_NOUN: str = (
    r"(?:byt\w*|jednotk\w*|apartman\w*|mezonet\w*|atelier\w*|studi[ou]"
    r"|dum|domu|domek\w*|domku|vil[aeuy]\w{0,2}|rd|radovk\w*"
    r"|parcel\w*|pozemk\w*|pozemek|garaz\w*|\bstani\b|chat[ay]|chalup\w*)"
)
# `byt 3+kk č. 2.07`: the disposition sits between the noun and the code, and it is the only
# thing that legitimately does — a longer gap is a sentence and the code belongs to something
# else. `pdl\d+` is `body_align`'s storey token and is never a unit's name.
_UNIT_MARKER: str = (
    r"(?:\d\s?\+\s?(?:kk|\d)\s+)?(?:s\s+)?(?:oznacen\w{0,4}\s+)?(?:c\.?\s*|cislo\s+)?"
)
_UNIT_CODE_BODY: str = (
    r"([a-z]{1,2}\s?\d{1,3}(?:\s?[.\-/]\s?\d{1,3}){1,3}|\d{1,3}(?:\.\d{1,3}){1,3})"
)
_PRINTED_UNIT_CODE = re.compile(_UNIT_NOUN + r"\s+" + _UNIT_MARKER + _UNIT_CODE_BODY
                                + r"(?![\d+]|\s*m2)")
# The three forms the segment reader cannot see. Each needs an EXPLICIT marker, because a bare
# letter or numeral after a noun is Czech grammar: `dům i zahrada` is a house and a garden, not
# unit I, and `byt a garáž` is a flat and a garage.
_ROMAN: str = r"(?:ii|iii|iv|vi|vii|viii|ix|xi|xii)"
_PRINTED_UNIT_ROMAN = re.compile(_UNIT_NOUN + r"\s+" + _UNIT_MARKER + r"(" + _ROMAN + r")\b")
_NUMBER_WORDS: dict[str, str] = {
    "jedna": "I", "dva": "II", "dve": "II", "tri": "III", "ctyri": "IV", "pet": "V",
    "sest": "VI", "sedm": "VII", "osm": "VIII", "devet": "IX", "deset": "X",
}
_PRINTED_UNIT_WORD = re.compile(
    _UNIT_NOUN + r"\s+(?:s\s+)?(?:oznacen\w{0,4}\s+)?(?:c\.?\s*|cislo\s+)"
    r"(" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")\b"
)
_PRINTED_UNIT_LETTER = re.compile(
    _UNIT_NOUN + r"\s+(?:s\s+)?(?:oznacen\w{0,4}\s+|c\.?\s*|cislo\s+)([a-z])\b(?!\s*m2)"
)
# More than this many distinct codes in one body is a developer's price list, not this unit's
# identity — and a price list must never refuse anything (an empty set is not a conflict).
UNIT_CODE_MAX_PER_ADVERT: int = 4


def printed_unit_codes(text: str | None, wide: bool = False) -> frozenset[str]:
    """Every unit code the body prints as this unit's, whole and normalised.

    `wide` adds the Roman numeral, the number word and the single letter, each of which needs
    an explicit `označením` / `č.` / `číslo` marker to be read at all."""
    return _printed_unit_codes(text, wide) if text else frozenset()


@lru_cache(maxsize=BODY_CACHE)
def _printed_unit_codes(text: str, wide: bool) -> frozenset[str]:
    folded = fold(text)
    out: set[str] = {
        re.sub(r"\s+", "", match.group(1)).upper().strip(".-/")
        for match in _PRINTED_UNIT_CODE.finditer(folded)
    }
    if wide:
        out |= {match.group(1).upper() for match in _PRINTED_UNIT_ROMAN.finditer(folded)}
        out |= {_NUMBER_WORDS[match.group(1)] for match in _PRINTED_UNIT_WORD.finditer(folded)}
        out |= {match.group(1).upper() for match in _PRINTED_UNIT_LETTER.finditer(folded)}
    out.discard("")
    return frozenset(out) if len(out) <= UNIT_CODE_MAX_PER_ADVERT else frozenset()


# --- the storey the BODY prints (E166) -----------------------------------------------------
# `1. NP` is the Czech ground floor and `1. patro` stands above it, so the two vocabularies are
# one scale with an offset of one. Read from the prose because the stored column is null on one
# side of a third of the corpus — and read as a set, because a body names the building's storeys
# as well as this unit's.
_PROSE_NP = re.compile(r"\b(\d{1,2})\.?\s*(?:np\b|nadzemnim?\s+podlazi)")
_PROSE_PATRO = re.compile(r"\b(\d{1,2})\.?\s*patr")
PROSE_FLOOR_MAX: int = 40

# E201: the storey written as an ORDINAL WORD. `ve třetím patře` carries no digit, so every
# reader above is blind to it — and a Czech letting agent writes the storey that way as often
# as with a numeral (both Ústí `ul. Stará` 1+kk adverts of one broker do, `v přízemí` against
# `ve třetím patře`). The word must sit directly in front of the storey noun; `druhé` on its
# own is one of the commonest words in the language and is never read alone.
_FLOOR_ORDINAL_WORDS: dict[str, int] = {
    "prvnim": 1, "prvni": 1, "druhem": 2, "druhe": 2, "druhy": 2, "tretim": 3, "treti": 3,
    "ctvrtem": 4, "ctvrte": 4, "patem": 5, "pate": 5, "sestem": 6, "seste": 6,
    "sedmem": 7, "sedme": 7, "osmem": 8, "osme": 8, "devatem": 9, "devate": 9,
    "desatem": 10, "desate": 10,
}
_ORDINAL_ALTERNATION: str = "|".join(
    sorted(_FLOOR_ORDINAL_WORDS, key=len, reverse=True))
_WORD_NP = re.compile(
    r"\b(" + _ORDINAL_ALTERNATION + r")\s+(?:nadzemnim\s+)?(?:podlazi|np)\b")
_WORD_PATRO = re.compile(r"\b(" + _ORDINAL_ALTERNATION + r")\s+(?:patre|patro|poschodi)\b")


# Which WORD carried a storey, kept beside the number. `3. patro` is the fourth storey and
# `3. NP` is the third, so two bodies that both write "three" are one storey apart on the NP
# scale for no other reason than which noun their author reached for — and one Bílina agency
# re-writes its own advert from `v šestém patře osmipodlažního objektu` to `ve 6. nadzemním
# podlaží`, same flat, same rent, same house number. That is a vocabulary, not a storey, and
# `_same_feed` cannot see it because it is one feed's two spellings.
FLOOR_FORMS: tuple[str, ...] = ("np", "patro")


def printed_floors(text: str | None, words: bool = False) -> frozenset[int]:
    """Every storey the body prints, on the NP scale (`1` is the ground floor)."""
    return _printed_floors(text, words) if text else frozenset()


def printed_floors_by_form(text: str | None, words: bool = False
                           ) -> dict[str, frozenset[int]]:
    """`printed_floors` split by the noun that carried each storey — `np` or `patro`."""
    return dict(_printed_floors_by_form(text, words)) if text else {}


@lru_cache(maxsize=BODY_CACHE)
def _printed_floors_by_form(text: str, words: bool) -> tuple[tuple[str, frozenset[int]], ...]:
    folded = fold(text)
    np_side = {int(match.group(1)) for match in _PROSE_NP.finditer(folded)}
    patro = {int(match.group(1)) + 1 for match in _PROSE_PATRO.finditer(folded)}
    if words:
        np_side |= {_FLOOR_ORDINAL_WORDS[match.group(1)]
                    for match in _WORD_NP.finditer(folded)}
        patro |= {_FLOOR_ORDINAL_WORDS[match.group(1)] + 1
                  for match in _WORD_PATRO.finditer(folded)}
    return tuple((name, frozenset(v for v in values if 0 < v <= PROSE_FLOOR_MAX))
                 for name, values in (("np", np_side), ("patro", patro)))


def _printed_floors(text: str, words: bool = False) -> frozenset[int]:
    return frozenset().union(*(v for _, v in _printed_floors_by_form(text, words)))


def same_form_floor_gap(left: Mapping[str, frozenset[int]],
                        right: Mapping[str, frozenset[int]]) -> int | None:
    """The smallest storey gap the two bodies state IN ONE NOUN, or None if they share none.

    `patro` and `NP` are two scales and this module has no converter it trusts: `3. patro` is
    the fourth storey, `3. NP` the third, and a writer who means one and types the other is
    commonplace — one Bílina agency re-writes its own advert for č.p. 707 from `v šestém patře
    osmipodlažního objektu` to `ve 6. nadzemním podlaží`, and one Prague flat is `ve druhém
    patře` on sreality and `v prvním podlaží` on bezrealitky at the same 20,553 Kč. Across the
    two nouns the difference is the noun; within one noun it is a storey, and only that is
    read. Returning 0 says the two agree.
    """
    gaps = [abs(value_a - value_b)
            for form, values_a in left.items()
            for value_a in values_a
            for value_b in right.get(form, frozenset())]
    return min(gaps) if gaps else None


# --- the storey the body predicates of the OFFERED unit (E181) ------------------------------
# `printed_floors` is a SET of every storey a body names, and a body names the building's as
# well as the unit's: one Dašice mill advert offers a space `umístěného v 2.NP` and mentions a
# WC `v 1.NP`, so its set {1, 2} meets the ground-floor advert's {1} and the two storeys never
# contradict. What separates them is the PLACEMENT clause — the storey stated of the thing
# being sold. The cue must be a verb of placement and the storey must follow it closely; a
# storey with no cue in front of it is not read here at all, which is why this reader is
# strictly narrower than `printed_floors` and never contradicts it.
_PLACEMENT_CUE = re.compile(
    r"(?:umisten\w*|situovan\w*|nachazi\s+se|se\s+nachazi|lezici\w*|nabizime?\s+\w{0,12}\s*"
    r"(?:byt|prostor|jednotk)\w*|(?:byt|prostor|jednotk|apartman|kancelar)\w*)\s+"
    r"(?:se\s+)?(?:v|ve)\s+"
)
# `umístěného v 1.NP` needs 0; `situovaný ve druhém nadzemním podlaží` is spelled out and is not
# read; the window only has to cover an ordinal and its separator.
PLACEMENT_WINDOW: int = 12


# A worded ordinal is longer than a numeral, so the placement window has to reach past it:
# `nachází se ve třetím patře` needs 12 characters for `tretim patre` alone.
PLACEMENT_WORD_WINDOW: int = 26


def subject_floors(text: str | None, words: bool = False) -> frozenset[int]:
    """The storeys a PLACEMENT clause states of the offered unit, on the NP scale."""
    return _subject_floors(text, words) if text else frozenset()


def subject_floors_by_form(text: str | None, words: bool = False
                           ) -> dict[str, frozenset[int]]:
    """`subject_floors` split by the noun that carried each storey — `np` or `patro`."""
    return dict(_subject_floors_by_form(text, words)) if text else {}


@lru_cache(maxsize=BODY_CACHE)
def _subject_floors_by_form(text: str, words: bool) -> tuple[tuple[str, frozenset[int]], ...]:
    folded = fold(text)
    np_side: set[int] = set()
    patro: set[int] = set()
    for cue in _PLACEMENT_CUE.finditer(folded):
        window = folded[cue.end(): cue.end() + PLACEMENT_WINDOW]
        for match in _PROSE_NP.finditer(window):
            if match.start() == 0:
                np_side.add(int(match.group(1)))
        for match in _PROSE_PATRO.finditer(window):
            if match.start() == 0:
                patro.add(int(match.group(1)) + 1)
        if not words:
            continue
        wide = folded[cue.end(): cue.end() + PLACEMENT_WORD_WINDOW]
        for match in _WORD_NP.finditer(wide):
            if match.start() == 0:
                np_side.add(_FLOOR_ORDINAL_WORDS[match.group(1)])
        for match in _WORD_PATRO.finditer(wide):
            if match.start() == 0:
                patro.add(_FLOOR_ORDINAL_WORDS[match.group(1)] + 1)
    return tuple((name, frozenset(v for v in values if 0 < v <= PROSE_FLOOR_MAX))
                 for name, values in (("np", np_side), ("patro", patro)))


def _subject_floors(text: str, words: bool = False) -> frozenset[int]:
    return frozenset().union(*(v for _, v in _subject_floors_by_form(text, words)))


# --- ground against upper, written without a number (E181) ----------------------------------
# `v přízemí` and `v patře` name a storey as surely as `1.NP` does and neither carries a digit,
# so `printed_floors` is blind to both. One HK-Pouchov 3+kk is advertised `s terasou 15 m2 v
# přízemí novostavby` on one portal and `s balkonem v patře novostavby` on four others at the
# same rent. The words are portal-independent — `přízemí` is the ground floor on every portal —
# so unlike the numbered storeys this reading needs no convention table. Both must be stated of
# the offered unit: the preposition is mandatory, because `v přízemí domu je kočárkárna` is a
# statement about the building.
_GROUND_WORD = re.compile(r"\b(?:v|ve)\s+prizemi\b|\bprizemni\s+(?:byt|jednotk|apartman)\w*")
_UPPER_WORD = re.compile(r"\b(?:v|ve)\s+(?:\d{1,2}\.?\s*)?(?:patre|poschodi)\b")
# E201: the same clause with the storey spelled out. `ve třetím patře` is `v patře` with an
# ordinal in the middle, and without this the worded upper storey reads as no storey at all.
_UPPER_WORD_ORDINAL = re.compile(
    r"\b(?:v|ve)\s+(?:" + _ORDINAL_ALTERNATION + r")\s+(?:patre|poschodi)\b")


def ground_or_upper(text: str | None, words: bool = False) -> frozenset[str]:
    """`{"ground"}`, `{"upper"}`, both, or nothing — the storey named in words, not digits."""
    return _ground_or_upper(text, words) if text else frozenset()


@lru_cache(maxsize=BODY_CACHE)
def _ground_or_upper(text: str, words: bool = False) -> frozenset[str]:
    folded = fold(text)
    out: set[str] = set()
    if _GROUND_WORD.search(folded):
        out.add("ground")
    if _UPPER_WORD.search(folded) or (words and _UPPER_WORD_ORDINAL.search(folded)):
        out.add("upper")
    return frozenset(out)


# --- how many units the advert says the object holds (E183) ---------------------------------
# `Výnosový dům se 2 byty 3+kk` against `Výnosový dům se 4 byty 3+kk`, both live on bazos for
# 7.3 days at 11,100,000 and 21,500,000. A stated count of flats is a fact about the object,
# not a tolerance — and it is the only thing that separates those two adverts, whose bodies are
# otherwise the same seller's template. Instrumental number words are read because that is how
# Czech writes the phrase; the digit form covers the rest.
_COUNT_WORDS: dict[str, int] = {
    "jednim": 1, "dvema": 2, "tremi": 3, "ctyrmi": 4, "peti": 5, "sesti": 6, "sedmi": 7,
    "osmi": 8, "deviti": 9, "deseti": 10,
}
_UNIT_COUNT_DIGIT = re.compile(
    r"\b(?:se|s)\s+(\d{1,2})\s+(?:byt\w*|bytov\w*\s+jednotk\w*|jednotk\w*)\b"
    r"|\b(\d{1,2})\s+bytov\w*\s+jednotk\w*"
)
_UNIT_COUNT_WORD = re.compile(
    r"\b(?:se|s)\s+(" + "|".join(sorted(_COUNT_WORDS)) + r")\s+"
    r"(?:byty|bytov\w*\s+jednotkami|jednotkami)\b"
)
UNIT_COUNT_MAX: int = 40


def stated_unit_counts(text: str | None) -> frozenset[int]:
    """How many dwelling units the body says the offered object holds."""
    return _stated_unit_counts(text) if text else frozenset()


@lru_cache(maxsize=BODY_CACHE)
def _stated_unit_counts(text: str) -> frozenset[int]:
    folded = fact_text(text)
    out: set[int] = set()
    for match in _UNIT_COUNT_DIGIT.finditer(folded):
        raw = match.group(1) or match.group(2)
        if raw is not None:
            out.add(int(raw))
    for match in _UNIT_COUNT_WORD.finditer(folded):
        out.add(_COUNT_WORDS[match.group(1)])
    return frozenset(value for value in out if 0 < value <= UNIT_COUNT_MAX)


# --- printed land-register parcels (E140) --------------------------------------------------
# A Czech land or house advert prints the parcel the object stands on, and that number IS the
# object's identity in the land register: two adverts printing disjoint parcels are two
# objects, whatever else they look like. The keyword is mandatory and a digit must follow it,
# because a bare number in a body is a price, a year or a house number.
_PARCEL_KEYWORD = re.compile(
    r"(?:parceln\w*\s*cisl\w*"
    r"|parcel\w*\s*(?:c\.|cisl\w*)"
    r"|parc\.?\s*(?:c\.|cisl\w*)"
    r"|\b(?:st\.?\s*)?p\.?\s*(?:p\.?\s*)?c\.?)"
    r"\s*:?\s*(?=\d)"
)
# One advert may print several parcels (`parcelní čísla 3217, 3097, 3341`), and the run ends at
# the first token that is not another number: `parcelní číslo 3255, podíl 1/1` prints ONE
# parcel and a share, and reading the share as a second parcel would make every share-advert
# of one seller look alike. The list separator must therefore carry a comma, a `+` or the word
# `a`/`i` — a bare space is not one, or `parc. č. 123, 2 393 m²` would read the AREA as a
# second parcel (idnes 166968, and the spaced-thousands trap the portal parsers carry).
_PARCEL_NUMBER = re.compile(r"\d{1,5}(?:/\d{1,4})?")
_PARCEL_SEPARATOR = re.compile(r"\s*(?:[,;+]|\ba\b|\bi\b)[\s,;+]*")
_AREA_UNIT_AFTER = re.compile(r"\s*m2\b")
# Spaced thousands are collapsed first, so `2 393 m²` is one token the area guard can refuse
# rather than two tokens the list reader accepts.
_SPACED_THOUSANDS = re.compile(r"(?<=\d) (?=\d{3}(?!\d))")
PARCEL_MAX_PER_ADVERT: int = 12

# E155: the forms the narrow keyword misses, counted over the 15,017 region bodies —
# `parcelní číslo: st. 661` (21 of 25 bodies, the stavební-parcela prefix and the biggest
# hole), `pozemek č. N` (23 of 30), `parcela N/M` with no `č.` (15 of 19), `pod číslem
# parcely N` (11 of 11), `na parcele N` (5 of 5), `č. parc. N` (2 of 2). Widening is
# FAIL-SAFE in both directions: an empty set is never a conflict, so the narrow reader's
# silence costs protection rather than precision, and a wider one can only add protection.
_PARCEL_KEYWORD_WIDE = re.compile(
    r"(?:parceln\w*\s*\.?\s*cisl\w*"
    r"|pod\s+cisl\w*\s+parcel\w*"
    r"|na\s+parcel[aeiu]"
    r"|c\.?\s*parc\w*\s*\.?"
    r"|parc\w*\s*\.?\s*(?:c\.|cisl\w*)"
    r"|parcel[aeuy]\b"
    r"|pozemk\w*\s*(?:c\.|cisl\w*)"
    r"|pozemek\s*(?:c\.|cisl\w*)"
    r"|\b(?:st\.?\s*)?p\.?\s*(?:p\.?\s*)?c\.?)"
    r"\s*:?\s*(?:st\.?\s*)?(?=\d)"
)


def parcel_numbers(text: str | None, wide: bool = False) -> set[str]:
    """Every land-register parcel the body prints, as `934/11` / `1633` strings.

    HTML entities are unescaped first: one sreality broker publishes an entity-escaped body and
    its parcel line would otherwise read as prose. Capped per advert — a body listing a whole
    estate's parcels is a seller's inventory, not this object's identity."""
    return set(_parcel_numbers(text, wide)) if text else set()


@lru_cache(maxsize=BODY_CACHE)
def _parcel_numbers(text: str, wide: bool = False) -> frozenset[str]:
    folded = _SPACED_THOUSANDS.sub("", fact_text(unescape(text)))
    out: set[str] = set()
    for keyword in (_PARCEL_KEYWORD_WIDE if wide else _PARCEL_KEYWORD).finditer(folded):
        position = keyword.end()
        while True:
            number = _PARCEL_NUMBER.match(folded, position)
            if number is None or _AREA_UNIT_AFTER.match(folded, number.end()):
                break
            out.add(number.group(0))
            separator = _PARCEL_SEPARATOR.match(folded, number.end())
            if separator is None:
                break
            position = separator.end()
    return frozenset(out) if len(out) <= PARCEL_MAX_PER_ADVERT else frozenset()


# --- the parcel TABLE, and which of its rows is THIS advert's (E182) ------------------------
# A parcelling is advertised twice over: one portal's body names the single parcel it sells
# (`číslo pozemku: 274/9 + 274/14`), another's prints the seller's whole catalogue —
#
#     • 277/2 + 277/3 — 1 465 m² — 3 469 000 Kč
#     • 274/8 + 274/13 — 1 458 m² — 3 459 000 Kč
#
# — and `parcel_numbers` then returns fourteen numbers for an advert that sells one plot, so
# the two sides always share a number and the parcel fact can never fire. The table itself says
# which row is this advert's: the row whose area and price are the advert's OWN. Each row must
# carry all three of parcels, area and price, which is what tells a catalogue from prose.
_PARCEL_TABLE_ROW = re.compile(
    r"(?P<parcels>\d{1,5}(?:/\d{1,4})?(?:\s*(?:\+|,|\ba\b)\s*\d{1,5}(?:/\d{1,4})?){0,5})"
    r"\s*[-–—:]\s*(?P<area>\d{2,7})\s*m2"
    r"\s*[-–—:]\s*(?P<price>\d{4,12})\s*kc"
)
PARCEL_TABLE_MIN_ROWS: int = 2
# The forms the WIDE keyword still misses, and only the table reader needs: the Czech order
# `číslo pozemku` (the keyword AFTER the noun), which is how idnes writes the one parcel it
# sells. Fail-safe like every other widening — an empty set is never a conflict.
_PARCEL_KEYWORD_WIDER = re.compile(
    r"(?:cisl\w*\s+(?:pozemk\w*|parcel\w*)"
    r"|parceln\w*\s*\.?\s*cisl\w*"
    r"|pod\s+cisl\w*\s+parcel\w*"
    r"|na\s+parcel[aeiu]"
    r"|c\.?\s*parc\w*\s*\.?"
    r"|parc\w*\s*\.?\s*(?:c\.|cisl\w*)"
    r"|parcel[aeuy]\b"
    r"|pozemk\w*\s*(?:c\.|cisl\w*)"
    r"|pozemek\s*(?:c\.|cisl\w*)"
    r"|\b(?:st\.?\s*)?p\.?\s*(?:p\.?\s*)?c\.?)"
    r"\s*:?\s*(?:st\.?\s*)?(?=\d)"
)


def parcel_table(text: str | None) -> tuple[tuple[frozenset[str], float, float], ...]:
    """The catalogue rows the body prints, as `(parcels, area m2, price)` — empty when none."""
    return _parcel_table(text) if text else ()


@lru_cache(maxsize=BODY_CACHE)
def _parcel_table(text: str) -> tuple[tuple[frozenset[str], float, float], ...]:
    folded = _SPACED_THOUSANDS.sub("", fact_text(unescape(text)))
    rows: list[tuple[frozenset[str], float, float]] = []
    for match in _PARCEL_TABLE_ROW.finditer(folded):
        parcels = frozenset(_PARCEL_NUMBER.findall(match.group("parcels")))
        if parcels:
            rows.append((parcels, float(match.group("area")), float(match.group("price"))))
    return tuple(rows) if len(rows) >= PARCEL_TABLE_MIN_ROWS else ()


def parcel_numbers_wider(text: str | None) -> set[str]:
    """`parcel_numbers` under the widest keyword set — E182's reading, behind its own field."""
    return set(_parcel_numbers_wider(text)) if text else set()


@lru_cache(maxsize=BODY_CACHE)
def _parcel_numbers_wider(text: str) -> frozenset[str]:
    folded = _SPACED_THOUSANDS.sub("", fact_text(unescape(text)))
    out: set[str] = set()
    for keyword in _PARCEL_KEYWORD_WIDER.finditer(folded):
        position = keyword.end()
        while True:
            number = _PARCEL_NUMBER.match(folded, position)
            if number is None or _AREA_UNIT_AFTER.match(folded, number.end()):
                break
            out.add(number.group(0))
            separator = _PARCEL_SEPARATOR.match(folded, number.end())
            if separator is None:
                break
            position = separator.end()
    return frozenset(out) if len(out) <= PARCEL_MAX_PER_ADVERT else frozenset()


# --- printed accessory designators (E141) --------------------------------------------------
# The parking space, the cellar and the garage a flat comes WITH, named by their number. A
# developer's adverts inside one building are near-identical by construction (E61), and where
# no unit number is printed the accessory number is the next thing the building itself names.
# `stání` is matched as a whole word so `stanice metra` can never become a parking space, and
# `pokoj č. 2` is deliberately not read: that is a room inside one flat, not an accessory.
_ACCESSORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("stani", re.compile(r"\bstani\b\s*(?:pro\s+auto\s*)?(?:c\.|cislo)\s*(\d{1,4})\b")),
    ("sklep", re.compile(r"\bsklep\w*\s*(?:koj\w*\s*)?(?:c\.|cislo)\s*(\d{1,4})\b")),
    ("sklep", re.compile(r"\bkoj\w*\s*(?:c\.|cislo)\s*(\d{1,4})\b")),
    ("garaz", re.compile(r"\bgaraz\w*\s*(?:c\.|cislo)\s*(\d{1,4})\b")),
)
ACCESSORY_KINDS: tuple[str, ...] = ("stani", "sklep", "garaz")
ACCESSORY_MAX_PER_KIND: int = 6


@lru_cache(maxsize=BODY_CACHE)
def _accessory_designators(text: str) -> tuple[tuple[str, frozenset[str]], ...]:
    folded = fact_text(unescape(text))
    found: dict[str, set[str]] = {}
    for kind, pattern in _ACCESSORY_PATTERNS:
        for match in pattern.finditer(folded):
            found.setdefault(kind, set()).add(match.group(1))
    return tuple(sorted((kind, frozenset(values)) for kind, values in found.items()
                        if len(values) <= ACCESSORY_MAX_PER_KIND))


def accessory_designators(text: str | None) -> dict[str, frozenset[str]]:
    """`kind -> the numbers this advert prints for it`: `stání č. 47`, `sklepní kóje č. 25`.

    A kind whose run is longer than `ACCESSORY_MAX_PER_KIND` is a building's price list rather
    than one flat's accessories and is dropped."""
    return dict(_accessory_designators(text)) if text else {}


# --- the offered product tier (E142) -------------------------------------------------------
# A serviced-office operator sells the SAME room as several products — "kancelář pro 1 osobu"
# at 10,890 Kč and "kancelář pro 2 pracovní místa" at 15,590 Kč, both printing the building's
# generic 50 m² and the building's address. The capacity is the only thing that tells the two
# apart, and it is stated only in prose. The office noun is mandatory: a bare `pro 2 osoby` is
# a service-charge line or a flat's suitability on residential adverts and fires on both.
_OFFER_NOUN = re.compile(r"kancelar\w*|coworking\w*|open\s?space|pracovi\w*|zasedac\w*")
_CAPACITY = re.compile(
    r"pro\s+(\d{1,3})\s*(?:osob\w*|pracovni\w*\s+mist\w*|mist\w*|zamestnan\w*|clen\w*)"
)
CAPACITY_WINDOW: int = 60


def capacity_counts(text: str | None) -> set[int]:
    """How many people the offered WORKSPACE is for, when an office noun carries the phrase."""
    return set(_capacity_counts(text)) if text else set()


@lru_cache(maxsize=BODY_CACHE)
def _capacity_counts(text: str) -> frozenset[int]:
    folded = fact_text(text)
    out: set[int] = set()
    for match in _CAPACITY.finditer(folded):
        window = folded[max(0, match.start() - CAPACITY_WINDOW): match.start()]
        if _OFFER_NOUN.search(window):
            out.add(int(match.group(1)))
    return frozenset(out)


# The EXTENT of a room let: "Pronajmu pokoj" against "Pronajmu 2 spojené pokoje" is one room
# against two, in one house, at two prices. The offer verb is mandatory — "v domě je jen 6
# pokojů" describes the house, not what is on offer — and the count window is short so an
# adjective (`2 spojené pokoje`) fits and a sentence does not.
_OFFER_VERB = re.compile(r"\b(?:pronajmu|pronajimam|nabizim|nabizime|pronajem|k\s+pronajmu)\b")
_ROOM_EXTENT = re.compile(r"(?:(\d{1,2})\s+)?(?:\w+\s+){0,1}?pokoj(?:e|u|ich|em)?\b")
EXTENT_WINDOW: int = 34


def offered_room_counts(text: str | None) -> set[int]:
    """How many ROOMS a room let offers, read only where an offer verb carries the phrase."""
    return set(_offered_room_counts(text)) if text else set()


@lru_cache(maxsize=BODY_CACHE)
def _offered_room_counts(text: str) -> frozenset[int]:
    folded = fact_text(text)
    out: set[int] = set()
    for verb in _OFFER_VERB.finditer(folded):
        clause = folded[verb.end(): verb.end() + EXTENT_WINDOW]
        match = _ROOM_EXTENT.match(clause.lstrip())
        if match is None:
            continue
        out.add(int(match.group(1)) if match.group(1) else 1)
    return frozenset(out)


# The compass direction a body PRINTS as this unit's: `Orientace je na východ`, `byt je
# orientovaný na jihozápad`, `situovaný na sever`. Longest stem first, because `jihovýchod`
# contains `východ` and a shorter match would read one flat's south-east as another's east.
# The keyword is mandatory — a body also names the direction of the motorway and of the park.
_COMPASS_STEMS: tuple[str, ...] = (
    "severovychod", "severozapad", "jihovychod", "jihozapad",
    "sever", "jih", "vychod", "zapad",
)
_ORIENTATION_KEYWORD = re.compile(r"orientac\w*|orientovan\w*|situovan\w*")
_ORIENTATION_WINDOW: int = 44
_COMPASS_WORD = re.compile(
    r"\b(severovychod\w*|severozapad\w*|jihovychod\w*|jihozapad\w*"
    r"|sever\w*|jizni|jih|vychod\w*|zapad\w*)\b"
)


def orientations(text: str | None) -> set[str]:
    """Every compass direction the body states as this unit's, normalised to one stem.

    The clause after the keyword is read WHOLE, not to its first direction: `orientaci na jih
    i na sever` is a through-flat naming two, and a rule that stopped at `jih` would read it
    as one. Abbreviations (`na J/Z`) are deliberately not read — a two-letter token is a coin
    flip against street names and room labels, and the rule would rather abstain than guess."""
    if not text:
        return set()
    folded = fold(text)
    out: set[str] = set()
    for keyword in _ORIENTATION_KEYWORD.finditer(folded):
        clause = folded[keyword.end(): keyword.end() + _ORIENTATION_WINDOW]
        clause = re.split(r"[.;!?]", clause, maxsplit=1)[0]
        for match in _COMPASS_WORD.finditer(clause):
            word = match.group(1)
            if word == "jizni":
                out.add("jih")
                continue
            for stem in _COMPASS_STEMS:
                if word.startswith(stem):
                    out.add(stem)
                    break
    return out


def _area_value(raw: str) -> float | None:
    cleaned = raw.replace(" ", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:[.]\d{3})+", cleaned):  # 10.500 = ten and a half thousand
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned.replace(",", "."))
    except ValueError:
        return None


def _menu_spans(folded: str, mentions: list[tuple[int, int, float]]) -> list[tuple[int, int]]:
    """Character spans that hold a size MENU rather than one unit's area."""
    spans: list[tuple[int, int]] = [match.span() for match in _AREA_RANGE.finditer(folded)]
    run: list[tuple[int, int, float]] = []
    for mention in mentions:
        if run and mention[0] - run[-1][1] <= MENU_MAX_GAP_CHARS:
            run.append(mention)
            continue
        if len(run) >= MENU_MIN_RUN:
            spans.append((run[0][0], run[-1][1]))
        run = [mention]
    if len(run) >= MENU_MIN_RUN:
        spans.append((run[0][0], run[-1][1]))
    return spans


def stated_areas(text: str | None, stored_area_m2: float | None = None) -> set[float]:
    """Every floor area the BODY prints AS THIS UNIT'S, in m².

    Deliberately not `listings.area_m2`: that column is a per-portal parse, and one flat can
    carry 70 on one portal and 72 on another off the same sentence. What the two texts SAY is
    the same fact on both sides.

    W8 narrows what counts as "prints": a commercial advert also prints the BUILDING it sits in
    (`Polygon je moderní budova o velikosti 10.500 m²`) and a landlord prints a MENU of what is
    available (`od 55 m² do 900 m²`, `10 m², 20 m², 30 m², …`). Neither is a statement about the
    advertised unit, and comparing one side's building with the other side's unit mislabelled
    three same-unit cross-broker pairs as different (W7 benchmark, root cause AREA SCOPE)."""
    return set(_stated_areas(text, stored_area_m2)) if text else set()


@lru_cache(maxsize=BODY_CACHE)
def _stated_areas(text: str, stored_area_m2: float | None) -> frozenset[float]:
    folded = fact_text(text)
    mentions: list[tuple[int, int, float]] = []
    for match in _M2_MENTION.finditer(folded):
        value = _area_value(match.group(1))
        if value is not None:
            mentions.append((match.start(), match.end(), value))
    if not mentions:
        return frozenset()
    skip = _menu_spans(folded, mentions) + [
        match.span() for match in _BUILDING_TOTAL.finditer(folded)
    ]
    low, high = STATED_AREA_MIN_M2, STATED_AREA_MAX_M2
    if stored_area_m2 is not None and stored_area_m2 > 0.0:
        low = max(low, stored_area_m2 / STATED_AREA_STORED_RATIO)
        high = min(high, stored_area_m2 * STATED_AREA_STORED_RATIO)
    out: set[float] = set()
    for start, end, value in mentions:
        if any(span[0] <= start and end <= span[1] for span in skip):
            continue
        if low <= value <= high:
            out.add(value)
    return frozenset(out)


# A price the advert states as a STARTING price — "ceny od 5 499 000 Kč", "již od 2 750 000".
# It is the printed admission that the number belongs to a CATALOGUE of units rather than to
# this one, which is why a price tie between two such adverts says nothing about identity.
_FROM_PRICE = re.compile(
    r"(?:cen\w*\s+(?:jiz\s+)?od|jiz\s+od|prodej\s+od|pronajem\s+od|k\s+dispozici\s+od)"
    r"\s*(?:cca\s*)?\d[\d\s\u00a0]{3,}"
)


def states_from_price(text: str | None) -> bool:
    """Does the body quote a FROM price — the advert's own word for a price list?"""
    return bool(text) and bool(_FROM_PRICE.search(fold(text)))


# --- the areas the BODY prints, read WITHOUT the stored column (E153) -----------------------
# `stated_areas` clamps every printed m² to `stored/3 .. stored*3`, and 2,390 of the 15,017
# region listings (16 %) print a headline area outside that window — because the portal stored
# the terrace, the cellar or the plot. bazos stores `area_m2 = 10.0` for a 76 m² apartment
# whose body says "terasou o velikosti 10 m²", and every reader that goes through the clamp is
# then blind to the 76. So this reader never looks at the stored column at all. Instead it
# SCOPES each mention by the noun that carries it: the terrace's size is the terrace's, a
# bedroom's is the bedroom's, and what is left is the size the advert is sold BY.
_AREA_SCOPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("accessory", re.compile(
        r"teras\w*|balk\w*|lodzi\w*|sklep\w*|koj\w*|komor\w*|spiz\w*|garaz\w*|\bstani\w*"
        r"|pud\w*|dvur|predzahrad\w*|atrium|zastaven\w*|sklepn\w*")),
    ("room", re.compile(
        r"pokoj\w*|loznic\w*|kuchyn\w*|kuchyns\w*|koupeln\w*|predsin\w*|chodb\w*|jideln\w*"
        r"|satn\w*|zadver\w*|zavetr\w*|pradeln\w*|\bwc\b|toalet\w*|technick\w*|zachod\w*")),
    ("land", re.compile(r"pozemk\w*|pozemek|parcel\w*|zahrad\w*|louk\w*|orn\w*|les\w*")),
)
# How far back the scoping noun may sit. `podlahovou plochou 76,1 m² a terasou o velikosti
# 10 m²` needs ~24 characters; a window much wider starts reading the previous sentence's noun.
AREA_SCOPE_WINDOW: int = 34


def printed_areas(text: str | None) -> frozenset[tuple[float, int, str]]:
    """Every m² the body prints, as `(value, printed decimals, scope)`.

    The decimals are kept because they are the only thing that says what the number MEANS:
    `50` and `50,5` are one area written to two precisions, `58,90` and `58,70` are two areas
    (see `body_align.rounding_equal`). Menu runs and the building's own size are dropped the
    same way `stated_areas` drops them."""
    return _printed_areas(text) if text else frozenset()


@lru_cache(maxsize=BODY_CACHE)
def _printed_areas(text: str) -> frozenset[tuple[float, int, str]]:
    folded = fact_text(text)
    mentions: list[tuple[int, int, float]] = []
    decimals: dict[int, int] = {}
    for match in _M2_MENTION.finditer(folded):
        value = _area_value(match.group(1))
        if value is None:
            continue
        mentions.append((match.start(), match.end(), value))
        raw = match.group(1).replace(",", ".")
        decimals[match.start()] = len(raw.split(".", 1)[1]) if "." in raw else 0
    if not mentions:
        return frozenset()
    skip = _menu_spans(folded, mentions) + [
        match.span() for match in _BUILDING_TOTAL.finditer(folded)
    ]
    out: set[tuple[float, int, str]] = set()
    for start, end, value in mentions:
        if any(span[0] <= start and end <= span[1] for span in skip):
            continue
        if not STATED_AREA_MIN_M2 <= value <= STATED_AREA_MAX_M2:
            continue
        window = folded[max(0, start - AREA_SCOPE_WINDOW): start]
        scope = "unit"
        best = -1
        for name, pattern in _AREA_SCOPES:
            for hit in pattern.finditer(window):
                if hit.start() > best:
                    best, scope = hit.start(), name
        out.add((value, decimals[start], scope))
    return frozenset(out)


def leading_area(text: str | None, scopes: frozenset[str]) -> tuple[float, int] | None:
    """E186: the FIRST area the body states in one of `scopes` — the size it leads with.

    `printed_areas` is a set, and a set cannot tell the offer from the context. One HK-Zámeček
    advert opens `stavební pozemek o výměře 732 m²` and explains four sentences later that the
    plot `vznikne rozdělením parcely o celkové výměře 2 195 m² na tři části`; the advert of the
    WHOLE parcel opens with 2 195. Read as sets the two share 2 195 and never contradict. What
    an advert leads with is what it sells."""
    return _leading_area(text, scopes) if text else None


@lru_cache(maxsize=BODY_CACHE)
def _leading_area(text: str, scopes: frozenset[str]) -> tuple[float, int] | None:
    folded = fact_text(text)
    best: tuple[int, float, int] | None = None
    for value, decimals, scope in _printed_areas(text):
        if scope not in scopes:
            continue
        position = _position_of(folded, value, decimals)
        if position is None:
            continue
        if best is None or position < best[0]:
            best = (position, value, decimals)
    return None if best is None else (best[1], best[2])


def _position_of(folded: str, value: float, decimals: int) -> int | None:
    """Where in the body this exact printed figure first stands."""
    for match in _M2_MENTION.finditer(folded):
        parsed = _area_value(match.group(1))
        if parsed is None or abs(parsed - value) > 1e-9:
            continue
        raw = match.group(1).replace(",", ".")
        printed = len(raw.split(".", 1)[1]) if "." in raw else 0
        if printed == decimals:
            return match.start()
    return None


# --- the street the BODY names (E151) -------------------------------------------------------
# A portal's resolved street key is missing or wrong on exactly the adverts that need it: two
# Olomouc office blocks, one on Litovelská and one on třída 28. října, carry the same obec and
# no street, and the only place either street is written is the prose. The keyword is
# mandatory and the name must be CAPITALISED in the original text, because that is what tells
# a street from the genitive of a common noun.
_PROSE_STREET = re.compile(
    r"(?:ulici|ulice|ul\.|na\s+ulici|v\s+ulici|t[rř][ií]d[aěe]|t[rř]\.|n[aá]m[eě]st[ií]|n[aá]m\.)"
    r"\s+((?:\d{1,2}\.\s+)?[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][\wáčďéěíňóřšťúůýž]{2,}"
    r"(?:\s+[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][\wáčďéěíňóřšťúůýž]{2,}){0,2})"
)
# A street named as a LANDMARK is not this advert's address. Every one of these prefixes was
# taken from a body that names a second street it is merely near.
_STREET_PROXIMITY = re.compile(
    r"(?:nedaleko|bl[ií]zko|pobl[ií]|v\s+bl[ií]zkosti|kousek|sm[eě]rem|zast[aá]vk\w*"
    r"|kone[cč]n\w*|dojezd\w*|dostupnost\w*|roh\w*\s+s|k\s+ulici|na\s+rohu)\s*$"
)
STREET_PROXIMITY_WINDOW: int = 30
# Two spellings of one street ("Krapkova" / "Krapkově") share this much of their stem.
STREET_STEM: int = 5
# A portal truncates the title mid-phrase and the body resumes with a capitalised verb, so the
# keyword's next word is `Nabízíme`, not a street: `...garážového stání ... v Olomouci, ul.
# Nabízíme k pronájmu...` (261802). A capture that STARTS with one of these is not a street;
# one that merely runs into it is cut there.
_STREET_STOPWORD = re.compile(
    r"^(?:nabizim\w*|nabidk\w*|nabizen\w*|prodej\w*|pronaj\w*|prodav\w*|cena|ceny|dum|byt|byty"
    r"|jedna|jde|tento|tato|toto|kontakt\w*|informac\w*|exkluzivn\w*|nove|nova|novy|vice"
    r"|vsechny|vse|dale|velmi|ideal\w*|k|v|ve|na|po|pri|za|do|od|pro|lokalit\w*)$"
)


def prose_streets(text: str | None) -> frozenset[str]:
    """Every street the body names AS THIS ADVERT'S, folded to one spelling.

    Each capture also yields its PREFIXES, because the regex cannot tell where a name ends:
    `ul. Milana Ticháka Nabízíme` is one street and one verb. A prefix makes the reader agree
    with the other side more readily, and agreement is the fail-safe direction — a street fact
    is a REFUSAL to merge, so an over-read name must never be the only thing on its side."""
    return _prose_streets(text) if text else frozenset()


@lru_cache(maxsize=BODY_CACHE)
def _prose_streets(text: str) -> frozenset[str]:
    out: set[str] = set()
    for match in _PROSE_STREET.finditer(text):
        before = text[max(0, match.start() - STREET_PROXIMITY_WINDOW): match.start()]
        if _STREET_PROXIMITY.search(fold(before)):
            continue
        words = re.sub(r"\s+", " ", fold(match.group(1))).strip().split(" ")
        kept: list[str] = []
        for word in words:
            if _STREET_STOPWORD.match(word):
                break
            kept.append(word)
            # `tř. 20. dubna` numbers its street, and `20.` alone names nothing: a prefix is a
            # name only once it carries a word. Without this the numeral becomes the only
            # street on its side and refuses every advert that names a real one.
            if any(len(word) > 2 and word.isalpha() for word in kept):
                out.add(" ".join(kept))
    return frozenset(out)


def streets_agree(left: Iterable[str], right: Iterable[str]) -> bool:
    """Do two sets of street names share one street? A stem match counts, an inflection is not
    a different street."""
    for one in left:
        for other in right:
            if one == other:
                return True
            shared = 0
            for a_char, b_char in zip(one, other):
                if a_char != b_char:
                    break
                shared += 1
            if shared >= STREET_STEM:
                return True
    return False


def address_block_key(listing: "Listing") -> str:
    """The finest place key the listing carries — the stratification unit and the "one project" test.

    RÚIAN address code first (one entrance), then obec+street+house number, then obec+street,
    then a 3-decimal pin (~110 m), then the obec. Never empty, so every pair has a stratum."""
    loc = listing.location
    if loc.ruian_adm_kod:
        return f"ruian:{loc.ruian_adm_kod}"
    obec = loc.obec_kod or 0
    if loc.street_key and loc.house_number:
        return f"addr:{obec}:{loc.street_key}:{loc.house_number}"
    if loc.street_key:
        return f"street:{obec}:{loc.street_key}"
    if loc.lat is not None and loc.lon is not None:
        return f"pin:{obec}:{loc.lat:.3f}:{loc.lon:.3f}"
    return f"obec:{obec}"


def code_population(descriptions: Mapping[int, str | None]) -> dict[str, int]:
    """How many listings of the corpus print each code — the purity cap's denominator."""
    population: dict[str, int] = {}
    for text in descriptions.values():
        for code in reference_codes(text):
            population[code] = population.get(code, 0) + 1
    return population


def rare_codes(codes: Iterable[str], population: Mapping[str, int]) -> set[str]:
    """The codes of one listing that a crowd does not share (E60's population cap)."""
    return {code for code in codes if population.get(code, 0) <= MAX_CODE_POPULATION}


# --- what the advert says the tenant pays BESIDES the rent (E203) ---------------------------
# D49 refused the bare co-live price limb and that refusal stands: two live adverts at two
# prices may be one flat a portal has not re-read yet. What the co-live price gap has never
# had beside it is a SECOND stated number of the same tenancy. One Bílina 2+1 prints
# `nájemné 11200 Kč + zálohy na služby 3800 Kč`; another, live with it on the same portal,
# prints `Nájemné: 9.000 Kč / Zálohy na služby: 4.500 Kč / Vratná kauce: 20.000 Kč`. A landlord
# quotes one service advance per flat, so two advances are two tenancies.
CHARGE_MIN_CZK: float = 500.0
CHARGE_MAX_CZK: float = 5_000_000.0
CHARGE_WINDOW: int = 64
CHARGE_KINDS: tuple[str, ...] = ("services", "deposit")
_CHARGE_KEYWORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("services", re.compile(
        r"zaloh\w*\s+(?:na\s+)?(?:sluzb\w*|energi\w*|topeni|vodu)"
        r"|poplatk\w*\s+za\s+sluzb\w*|sluzby\s+a\s+energie|inkaso\w*")),
    ("deposit", re.compile(r"\bkauc\w*|\bjistin\w*|\bjistot\w*|\bvratn\w+\s+zaloh\w*")),
)
_CHARGE_MONEY = re.compile(_AREA_NUMBER + r"\s*(?:,-)?\s*(?:kc\b|kč\b)?")


def stated_charges(text: str | None) -> dict[str, frozenset[float]]:
    """`{kind: {amounts}}` — the service advance and the deposit the body quotes, in Kč."""
    return dict(_stated_charges(text)) if text else {}


@lru_cache(maxsize=BODY_CACHE)
def _stated_charges(text: str) -> tuple[tuple[str, frozenset[float]], ...]:
    folded = fold(text)
    found: dict[str, set[float]] = {}
    for kind, keyword in _CHARGE_KEYWORDS:
        for match in keyword.finditer(folded):
            window = folded[match.end(): match.end() + CHARGE_WINDOW]
            for money in _CHARGE_MONEY.finditer(window):
                value = _area_value(money.group(1))
                # `kauce ve výši 3 nájmů tj. 15 000 Kč` states the multiplier before the money;
                # a charge is a sum of money, so anything below the floor is not one.
                if value is None or not CHARGE_MIN_CZK <= value <= CHARGE_MAX_CZK:
                    continue
                found.setdefault(kind, set()).add(value)
                break
    return tuple((kind, frozenset(values)) for kind, values in sorted(found.items()))


# --- the plot the BODY says comes with the house (E202) -------------------------------------
# `plot_area` reads a stored column, and bazos has none: one Hrobčice house is offered twice by
# one seller, 25 minutes apart, `+ areál o rozloze 2 830 m²` for 7,999,000 and `+ pozemek o
# rozloze 1 483 m²` for 6,190,000, and the only place either figure exists is the prose and the
# URL slug. The noun set is deliberately narrower than `_AREA_SCOPES`'s `land`: a `zahrada` is
# not the plot the house stands on, and both of those bodies print the same 87 m² garden.
_PROSE_PLOT = re.compile(
    r"\b(?:pozemk\w*|pozemek|parcel\w*|areal\w*|dvur)[^.;:]{0,24}?"
    r"(?:o\s+)?(?:celkov\w+\s+)?(?:rozloze|vymere|vymera|velikosti|ploche|plose)"
    r"\s+(?:cca\s+)?" + _AREA_NUMBER + r"\s*m2")
PROSE_PLOT_MIN_M2: float = 50.0


def prose_plot_areas(text: str | None) -> frozenset[float]:
    """Every plot size the body states as the land sold WITH the object, in m²."""
    return _prose_plot_areas(text) if text else frozenset()


@lru_cache(maxsize=BODY_CACHE)
def _prose_plot_areas(text: str) -> frozenset[float]:
    out: set[float] = set()
    for match in _PROSE_PLOT.finditer(fact_text(text)):
        value = _area_value(next(group for group in match.groups() if group))
        if value is not None and PROSE_PLOT_MIN_M2 <= value <= STATED_AREA_MAX_M2:
            out.add(value)
    return frozenset(out)


# --- the advert that admits it is a PART of a bigger parcel (E204) ---------------------------
# One HK-Zámeček body offers `stavební pozemek o výměře 732 m²` and says four sentences later
# that it `vznikne rozdělením parcely o celkové výměře 2 195 m² na tři části`; the advert of the
# whole parcel offers 2 195 m². Read as SETS the two share 2 195 and never contradict — the
# part names the whole it will be cut from, which is exactly what makes it a part.
_PARCEL_DIVISION = re.compile(
    r"(?:rozdelen\w*|rozdeleni\w*|deleni\w*|oddelen\w*)\s+(?:puvodni\s+)?"
    r"(?:parcely|pozemku|parcele)[^.;:]{0,30}?"
    r"(?:o\s+)?(?:celkov\w+\s+)?(?:vymere|vymera|rozloze|ploche|plose)"
    r"\s+(?:cca\s+)?" + _AREA_NUMBER + r"\s*m2")


def parcel_divisions(text: str | None) -> frozenset[float]:
    """The parcel sizes this advert says its own plot will be CUT FROM, in m²."""
    return _parcel_divisions(text) if text else frozenset()


@lru_cache(maxsize=BODY_CACHE)
def _parcel_divisions(text: str) -> frozenset[float]:
    out: set[float] = set()
    for match in _PARCEL_DIVISION.finditer(fact_text(text)):
        value = _area_value(next(group for group in match.groups() if group))
        if value is not None and PROSE_PLOT_MIN_M2 <= value <= STATED_AREA_MAX_M2:
            out.add(value)
    return frozenset(out)


# --- W21 / S6 --------------------------------------------------------------------------------

def cross_form_floor_agreement(left: Mapping[str, frozenset[int]],
                               right: Mapping[str, frozenset[int]]) -> bool:
    """E210: do the two bodies name ONE storey across the two nouns?

    `same_form_floor_gap` refuses to convert between `patro` and `NP` because a writer who
    means one and types the other is commonplace. That refusal is about a DISAGREEMENT: it
    cannot say which storey `3. patro` is when the other body says `3. NP`. An AGREEMENT needs
    no such judgement — `7. patře` and `8. nadzemním podlaží` are the same storey on the one
    scale this module already converts to, and one Brno Kobližná office is advertised both ways
    at the same 57 m² and the same 7,900 Kč. Where the two bodies meet on that scale, the
    worded reading has nothing left to separate them with.
    """
    return bool(frozenset().union(*left.values()) & frozenset().union(*right.values()))


# --- the storey the OFFER is, for a let of a whole floor (E212) ------------------------------
# `subject_floors` needs a placement verb and reads a short window after it, so it sees neither
# `Pronájem přízemního podlaží` (the offer is the storey, not a unit placed on one) nor `Místo
# o ploše 14 m² se nachází v nejžádanějším přízemí` (an adjective stands in the window) nor
# `se nachází v 3 nadzemním podlaží` (the noun is longer than the window). All three name the
# storey OF THE OFFER as plainly as `v 2.NP` does. Read on the NP scale, where `přízemí` is 1.
# Two cue families, both narrow. An OFFER verb answers "what is being let" directly. A
# PLACEMENT verb answers it only when the clause's own subject is the offered object, so the
# subject noun is named: `vitrínky umístěné v přízemí` is a notice board, not the let.
_OFFER_SUBJECT: str = (
    r"misto|mista|stani|prostor\w*|byt|byty|jednotk\w*|kancelar\w*|hala|haly|halu"
    r"|podlazi|patro|patra|objekt\w*|dum|domu|apartman\w*|atelier\w*|sklad\w*")
_OFFER_CUE = re.compile(
    r"(?:\b(?:pronajem|pronajmu|pronajmy|prodej|prodeji|nabizime|nabizim"
    r"|predmetem\s+pronajmu\s+je|predmetem\s+prodeje\s+je|jedna\s+se\s+o)"
    r"|\b(?:" + _OFFER_SUBJECT + r")[^.;:]{0,24}?(?:se\s+nachazi|nachazi\s+se"
    r"|je\s+situovan\w*|je\s+umisten\w*))"
    r"\s+(?:k\s+pronajmu\s+|k\s+prodeji\s+)?(?:se\s+)?(?:v|ve)?\s*"
)
# Two adjectives is what `samostatného 1. patra` and `nejžádanějším přízemí` need; a third
# starts reading the next clause.
OFFER_STOREY_ADJECTIVES: int = 2
_OFFER_STOREY = re.compile(
    r"(?:\w+\s+){0,%d}?(?:(prizemn\w*\s+(?:podlazi|patr\w*|prostor\w*|cast\w*)|prizemi)"
    r"|(\d{1,2})\.?\s*(?:np\b|nadzemni\w*\s+podlazi)|(\d{1,2})\.?\s*patr\w*)" % (
        OFFER_STOREY_ADJECTIVES,)
)
OFFER_STOREY_WINDOW: int = 44


def offered_storeys(text: str | None) -> frozenset[int]:
    """The storey an OFFER clause names as the thing on offer, on the NP scale."""
    return _offered_storeys(text) if text else frozenset()


@lru_cache(maxsize=BODY_CACHE)
def _offered_storeys(text: str) -> frozenset[int]:
    folded = fact_text(text)
    out: set[int] = set()
    for cue in _OFFER_CUE.finditer(folded):
        match = _OFFER_STOREY.match(folded, cue.end(), cue.end() + OFFER_STOREY_WINDOW)
        if match is None:
            continue
        if match.group(1) is not None:
            out.add(1)
        elif match.group(2) is not None:
            out.add(int(match.group(2)))
        else:
            out.add(int(match.group(3)) + 1)
    return frozenset(v for v in out if 0 < v <= PROSE_FLOOR_MAX)


# --- the unit id printed under its own LABEL (E214) ------------------------------------------
# `printed_unit_codes` requires a digit, because a bare letter is a building and a bare word is
# prose. A LABEL removes that doubt: an advert that writes `ID jednotky: DOUBLE B` has told us
# the value identifies the unit, whatever its shape — and one Brno Dornych co-live residence
# rotates `DOUBLE A`, `DOUBLE B` and `STANDARD` through one idnes slot at one rent.
_LABELLED_UNIT_ID = re.compile(
    r"\b(?:id|kod|oznaceni|cislo|c\.|typ)\s+(?:jednotky|jednotka|bytu|apartmanu|pokoje)\s*"
    r"[:\-]?\s*([a-z0-9][a-z0-9+._/-]{0,19})(?:\s+([a-z0-9+]{1,2})\b)?"
)
LABELLED_UNIT_ID_MAX: int = 3


def labelled_unit_ids(text: str | None) -> frozenset[str]:
    """Every value the body prints under an explicit unit-identity LABEL."""
    return _labelled_unit_ids(text) if text else frozenset()


@lru_cache(maxsize=BODY_CACHE)
def _labelled_unit_ids(text: str) -> frozenset[str]:
    out: set[str] = set()
    for match in _LABELLED_UNIT_ID.finditer(fact_text(unescape(text))):
        head, tail = match.group(1), match.group(2)
        out.add(f"{head} {tail}" if tail else head)
    return frozenset(out) if len(out) <= LABELLED_UNIT_ID_MAX else frozenset()


# --- the size of the accessory, stated (E215) ------------------------------------------------
# `printed_area` scopes a cellar's m² OUT of the headline comparison, which is right — a cellar
# is not what the flat is sold by — and leaves it read by nothing. Two Prague Želivecká 4+kk of
# one house, same 90 m², same 3.NP, same 10,900,000, live together on two portals: one states
# `dva sklepy o celkové ploše 9 m²`, the other `Celkem 10 m² úložného prostoru`.
_ACCESSORY_AREA = re.compile(
    r"\b(?:sklep\w*|sklepn\w*|koj\w*|komor\w*|ulozn\w*)[^.;:]{0,44}?"
    r"(?:o\s+)?(?:celkove\s+)?(?:ploche|plose|vymere|vymera|velikosti|rozloze)"
    r"\s+(?:cca\s+)?" + _AREA_NUMBER + r"\s*m2"
    r"|\bcelkem\s+" + _AREA_NUMBER + r"\s*m2\s+(?:ulozn|sklep|kojn)\w*"
)
ACCESSORY_AREA_MAX_M2: float = 120.0


def accessory_areas(text: str | None) -> frozenset[float]:
    """The cellar/storage size the body states, in m²."""
    return _accessory_areas(text) if text else frozenset()


@lru_cache(maxsize=BODY_CACHE)
def _accessory_areas(text: str) -> frozenset[float]:
    out: set[float] = set()
    for match in _ACCESSORY_AREA.finditer(fact_text(text)):
        value = _area_value(next(group for group in match.groups() if group))
        if value is not None and 0.0 < value <= ACCESSORY_AREA_MAX_M2:
            out.add(value)
    return frozenset(out)


# --- the capacity, written in English (E216) -------------------------------------------------
# `capacity_counts` is Czech-only, and a serviced-office operator publishes the same building's
# products in both languages: Regus Spielberk's `soukromá servisovaná kancelář pro 1 osobu` and
# its `private serviced office space for 2 workstations` both print 50 m² and 8,190 Kč.
_OFFER_NOUN_EN = re.compile(r"office\w*|workspace\w*|coworking\w*|desk\w*|suite\w*")
_CAPACITY_EN = re.compile(
    r"(?:for|suits|suitable\s+for|accommodates|ideal\s+for|up\s+to)\s+(\d{1,3})\s*"
    r"(?:workstation|desk|person|people|employee|staff|colleague)\w*"
)


@lru_cache(maxsize=BODY_CACHE)
def _capacity_counts_en(text: str) -> frozenset[int]:
    folded = fact_text(text)
    out: set[int] = set()
    for match in _CAPACITY_EN.finditer(folded):
        window = folded[max(0, match.start() - CAPACITY_WINDOW): match.start()]
        if _OFFER_NOUN_EN.search(window) or _OFFER_NOUN.search(window):
            out.add(int(match.group(1)))
    return frozenset(out)


def capacity_counts_english(text: str | None) -> set[int]:
    """`capacity_counts` for the English half of a bilingual operator's catalogue."""
    return set(_capacity_counts_en(text)) if text else set()


# E216, the other vocabulary the capacity reader was blind to: a FURNISHED let sells its unit
# types by how many people they sleep, and it says so in its equipment list rather than in an
# office phrase. Two Brno Dornych co-live units of one residence, floor 4 at 14,990 and floor 3
# at 16,690, run the same 1,900-character template and differ under `Vybavení:` — `2 jednolůžka
# s úložným prostorem, 2 pracovní stoly` against `postel s úložným prostorem`. Read ONLY inside
# that section: a `postel` in prose is furniture in a photograph caption, not the offer's size.
_FURNISHING_SECTION = re.compile(r"\b(?:vybaveni|zarizeni|k\s+dispozici\s+je)\b")
_BED_NOUN: str = r"jednoluzk\w*|dvouluzk\w*|luzk\w*|postel\w*|palanda\w*"
_BED_WORDS: dict[str, int] = {"jedno": 1, "dve": 2, "dva": 2, "tri": 3, "ctyri": 4, "pet": 5}
_BED_COUNT = re.compile(
    r"(?:(\d{1,2})|\b(" + "|".join(_BED_WORDS) + r"))\s+(?:\w+\s+){0,1}?(?:" + _BED_NOUN
    + r")\b|\b(?:manzelsk\w+\s+)?(" + _BED_NOUN + r")\b")
FURNISHING_WINDOW: int = 420


def stated_bed_counts(text: str | None) -> frozenset[int]:
    """How many sleeping places the equipment list of a FURNISHED let states."""
    return _stated_bed_counts(text) if text else frozenset()


@lru_cache(maxsize=BODY_CACHE)
def _stated_bed_counts(text: str) -> frozenset[int]:
    folded = fact_text(text)
    out: set[int] = set()
    for section in _FURNISHING_SECTION.finditer(folded):
        window = folded[section.end(): section.end() + FURNISHING_WINDOW]
        for match in _BED_COUNT.finditer(window):
            if match.group(1) is not None:
                out.add(int(match.group(1)))
            elif match.group(2) is not None:
                out.add(_BED_WORDS[match.group(2)])
            else:
                out.add(1)
    return frozenset(v for v in out if 0 < v <= 12)


# --- the place the BODY names (E217) ---------------------------------------------------------
# E135 refuses the raw obec conflict: portals disagree about which municipality a property is
# in, and a village is routinely filed under its town. The BODY is a different witness — one
# Brno developer sells one 5+kk design in TWO municipalities, and the advert whose portal
# locality is Brno-Chrlice is separated from its Újezd u Brna twin only by what the other
# advert's own body says it is. Capitalisation is the cue, so this reader takes the RAW text.
_BODY_PLACE = re.compile(
    r"\b(?:v|ve)\s+([A-ZÁČĎÉĚÍŇÓŘŠŤ"
    r"ÚŮÝŽ][\w]{2,}(?:\s+u\s+[A-Z][\w]{2,})?)"
)
PLACE_STEM: int = 3
BODY_PLACE_MAX: int = 6


def body_localities(text: str | None) -> frozenset[str]:
    """Every capitalised place the body states the object is IN, folded."""
    return _body_localities(text) if text else frozenset()


@lru_cache(maxsize=BODY_CACHE)
def _body_localities(text: str) -> frozenset[str]:
    out = {fact_text(match.group(1)) for match in _BODY_PLACE.finditer(unescape(text))}
    out.discard("")
    return frozenset(out) if len(out) <= BODY_PLACE_MAX else frozenset()


def place_names_match(left: str, right: str) -> bool:
    """Do two place names name one place? Czech declines the ending, so the stem decides.

    `v Brně` against the stored `Brno` shares three letters and `v Rebešovicích` against
    `Rebešovice` shares ten: what a declension changes is the tail, so the shared prefix has to
    reach within two characters of the shorter name. A match only ever SILENCES this rule, so
    the loose direction is the safe one.
    """
    head_a, head_b = left.split()[0], right.split()[0]
    shared = 0
    for char_a, char_b in zip(head_a, head_b):
        if char_a != char_b:
            break
        shared += 1
    return shared >= PLACE_STEM and shared >= min(len(head_a), len(head_b)) - 2


# --- the seller's price list, row by row (E218) ----------------------------------------------
# E183 reads a parcel CATALOGUE keyed on parcel numbers. One Ochoz u Brna seller's bazos body
# has no parcel numbers at all — it prices four plots by name (`Obora (4840 m2) ... Cena za
# pozemek 958.000,- Kč`) — and the portal stores ONE of those prices per advert. The price the
# row carries is what says which plot this advert is.
_PRICED_ROW_AREA = re.compile(_AREA_NUMBER + r"\s*m2")
_PRICED_ROW_PRICE = re.compile(
    r"cena\s+(?:za\s+\w+\s+)?(\d{1,3}(?:[  .]\d{3})+|\d{4,9})")
PRICED_ROW_MAX: int = 12
PRICED_ROW_MIN_M2: float = 20.0
# How far a row's price may stand from the size it prices. One Ochoz body runs 330 characters
# of prose between `Obora (4840 m2)` and `Cena za pozemek 958.000,- Kč`; a following SIZE ends
# the row whatever the distance, which is what keeps the pairing honest.
PRICED_ROW_WINDOW: int = 600


def priced_land_rows(text: str | None) -> tuple[tuple[float, float], ...]:
    """`(area m², price Kč)` for every plot the body prices as its own line."""
    return _priced_land_rows(text) if text else ()


@lru_cache(maxsize=BODY_CACHE)
def _priced_land_rows(text: str) -> tuple[tuple[float, float], ...]:
    folded = fact_text(text)
    areas = [(match.start(), _area_value(match.group(1)))
             for match in _PRICED_ROW_AREA.finditer(folded)]
    prices = [(match.start(), _area_value(match.group(1)))
              for match in _PRICED_ROW_PRICE.finditer(folded)]
    rows: list[tuple[float, float]] = []
    for index, (start, area) in enumerate(areas):
        if area is None or area < PRICED_ROW_MIN_M2:
            continue
        stop = areas[index + 1][0] if index + 1 < len(areas) else len(folded)
        stop = min(stop, start + PRICED_ROW_WINDOW)
        hit = next((value for at, value in prices if start < at < stop and value), None)
        if hit is not None:
            rows.append((area, hit))
    return tuple(sorted(set(rows))) if len(rows) <= PRICED_ROW_MAX else ()


# --- the plot, stated the way the portal does not (E218) -------------------------------------
# `_PROSE_PLOT` wants the measurement word AFTER the noun (`pozemek o výměře 732 m²`). One Brno
# developer writes it the other way round — `Celková plocha pozemku činí 1.288 m²` — and eight
# adverts of two Rebešovice semi-detached halves carry `1.288` and `1.294` in prose while every
# portal stores the same 1,288 in the column.
_PROSE_PLOT_WIDE = re.compile(
    r"(?:celkov\w+\s+)?(?:plocha|vymera|rozloha|velikost)\s+(?:pozemku|parcely|arealu)"
    r"\s*(?:cini|je|:|-)?\s*" + _AREA_NUMBER + r"\s*m2")


def prose_plot_areas_wide(text: str | None) -> frozenset[float]:
    """`prose_plot_areas` plus the measurement-first phrasing."""
    if not text:
        return frozenset()
    return prose_plot_areas(text) | _prose_plot_areas_wide(text)


@lru_cache(maxsize=BODY_CACHE)
def _prose_plot_areas_wide(text: str) -> frozenset[float]:
    out: set[float] = set()
    for match in _PROSE_PLOT_WIDE.finditer(fact_text(text)):
        value = _area_value(match.group(1))
        if value is not None and PROSE_PLOT_MIN_M2 <= value <= STATED_AREA_MAX_M2:
            out.add(value)
    return frozenset(out)


# --- a second plot on offer, and what is built on this one (E219) ----------------------------
# Absence is not a statement, and an attribute one body prints and the other does not is never
# a fact on its own (D63). Two Opatovice adverts make it one: each says a bezprostředně
# sousedící plot of the same 500 m² is ALSO on offer, so the seller has told us there are two —
# and only one of them says the electricity connection is already built with a meter fitted.
_SECOND_PLOT = re.compile(
    r"(?:sousedic\w*|sousedni\w*|vedlejsi|dalsi|druhy|druhe)\s+(?:\w+\s+){0,2}?"
    r"(?:stavebni\s+)?(?:pozemek|pozemku|parcel\w*)"
    r"|\boba\s+pozemky\b|\bobe\s+parcely\b")
_SECOND_PLOT_OFFER = re.compile(r"nabizen\w*|nabizime|na\s+prodej|k\s+prodeji|koupit|prodava\w*")
SECOND_PLOT_WINDOW: int = 90
_BUILT_CONNECTION = re.compile(
    r"(?:jiz\s+)?vybudovan\w*\s+(?:\w+\s+){0,2}?pripojk\w*"
    r"|pripojk\w*\s+(?:\w+\s+){0,3}?(?:je|jsou)\s+(?:jiz\s+)?(?:vybudovan|zrizen|hotov)\w*"
    r"|osazen\w*\s+elektromer\w*|elektromer\w*\s+(?:je\s+)?osazen\w*")


def states_second_plot(text: str | None) -> bool:
    """Does the body say a NEIGHBOURING plot is on offer as well as this one?"""
    if not text:
        return False
    return _states_second_plot(text)


@lru_cache(maxsize=BODY_CACHE)
def _states_second_plot(text: str) -> bool:
    folded = fact_text(text)
    for match in _SECOND_PLOT.finditer(folded):
        window = folded[max(0, match.start() - SECOND_PLOT_WINDOW): match.end()
                        + SECOND_PLOT_WINDOW]
        if _SECOND_PLOT_OFFER.search(window):
            return True
    return False


def built_connection(text: str | None) -> bool:
    """Does the body say a utility connection is already BUILT on this plot?"""
    if not text:
        return False
    return bool(_BUILT_CONNECTION.search(fact_text(text)))
