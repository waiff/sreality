"""E150: align two near-identical bodies and read the positions where they differ.

Every reader before this one knows a FORM — `parcelní číslo`, `stání č.`, `označením B36`. A
new cohort has produced a new form every time: `B1.2.2` against `B1.2.3`, `F2.103` against
`F3.103`, a plot `s označením 7` against `8`, a garage block `G3` against `G4`, `58,90 m²`
against `58,70 m²`. Knowing the form is not a strategy, because the developer chooses it.

What does NOT change is the shape of the hazard: two adverts for two units of one project are
written from ONE template, so they are word-identical everywhere except at the few positions
that name the unit. So this module knows no form at all. It aligns the two token streams, and
where the alignment says "here one says X and the other says Y", with agreeing context on both
sides, it reads X and Y. A position is a fact only when what stands there can NAME a unit — a
code compared whole, or the numbers a token carries compared by the rounding rule. The
alignment says WHERE to look and the token says whether what is written there can be a name.

THE FOUR WAYS THIS COULD FIRE ON ONE UNIT, and what stops each:

  * truncation — a portal cuts the body at 500 characters. That is an INSERT/DELETE in the
    alignment, never a replace, and a one-sided token is never read (E12 again: a number
    present on one side only has contradicted nothing).
  * a rewritten paragraph — two brokers paraphrasing one flat. A replace longer than
    `MAX_SEGMENT` tokens is prose, not a field, and is skipped; and the whole reading needs
    the two bodies to align above `min_ratio` before any position is read at all.
  * the price, the date, the phone, the order code — every one of them legitimately differs
    between two postings of one unit. They are MASKED to a sentinel before alignment, so they
    align as equal and can never be a position.
  * rounding — `50` against `50,5` is one area printed twice. Two numbers are equal when they
    agree within the ROUNDING UNIT of the coarser of them (half of its last printed digit), so
    50 == 50,5 and 58,90 != 58,70. That is the arithmetic a reader does, not a tolerance.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache

from autodedup.text_facts import BODY_CACHE, mask_codes

# Below this many tokens a body is a headline, and two headlines align on nothing meaningful.
MIN_TOKENS: int = 40
# A replace longer than this is a rewritten paragraph, not a template field.
MAX_SEGMENT: int = 6
# Equal tokens that must flank a replace on BOTH sides before it counts as a position. Without
# it the first and last opcode of a truncated body reads as a difference.
CONTEXT_TOKENS: int = 2

_ACCENTS = re.compile(r"[̀-ͯ]")
_SPACED_THOUSANDS = re.compile(r"(?<=\d)[  ](?=\d{3}(?!\d))")

# Everything that legitimately moves between two postings of ONE unit, masked before alignment.
_MASKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("[DEN]", re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.\s?(?:\d{2}|\d{4})\b")),
    ("[TEL]", re.compile(r"(?:\+\s?420\s?)?\b\d{3}\s?\d{3}\s?\d{3}\b")),
    ("[CENA]", re.compile(r"\b\d[\d  .]*\d\s*(?:kc|czk|,-|kč)")),
    ("[CENA]", re.compile(r"\b\d{6,}\b")),
    # `k dispozici od 06/2026` against `od 07/2026` is one flat available a month apart, not two
    # flats. The month/year form has to go before [ROK] eats the year out from under it.
    ("[DEN]", re.compile(r"\b\d{1,2}\s*/\s*(?:19|20)\d{2}\b")),
    ("[ROK]", re.compile(r"\b(?:19|20)\d{2}\b")),
    ("[PCT]", re.compile(r"\b\d{1,3}(?:[.,]\d+)?\s?%")),
    # `8 km od Olomouce` against `5 min autem` is one house two brokers placed two ways.
    ("[VZDAL]", re.compile(r"\b\d{1,3}(?:[.,]\d+)?\s*(?:km|min|minut\w*|hodin\w*)\b")),
    ("[FOTO]", re.compile(r"\b\d{1,3}\s*(?:fotek|fotografi\w*|obrazk\w*)\b")),
)

# E163: what the shipped mask set still let through, every class taken from a certain duplicate
# the reader split on the two dev cohorts. None of these names a unit; all of them move between
# two postings of one, which is why they concentrate on SAME-PORTAL re-posts (remax 25 %,
# bezrealitky 12.5 %, realitymix 10.9 %, sreality 10.6 %, idnes 7.7 % of certain pairs).
#
#   * a charge — `Náklady na bydlení: 5500` against `4000`, `Provize RK: 12.000.- Kč` against
#     `13.200.-` (the shipped `[CENA]` wants `,-` and this broker writes `.-`), `19tis.kč`
#     against `20tis.kč`, `nájem 3750 inkaso zálohy` against `4800 1200 záloha`.
#   * the TERM of the contract — `minimální délka nájmu činí 3 měsíce` against `12 měsíců`,
#     `anuita … nastavena na 35 let` against `30 let`.
#   * a date with no year — `k nastěhování od 1.7.` against `od 1.10.`, `prohlídky v termínu
#     23.6` against `25.9`. Anchored on the date word, because a bare `5.13` is a unit code.
#   * an order code the population cap is too short to mask — `ev.č. 0831` against `0883`.
#   * an inventory multiplier — `6x pokoj` against `5x`, which counts a whole house's rooms.
#   * a length in metres — `objekt je dlouhý 79 m` against `80 m`, a garage door `2,2 m` wide
#     against `2,3 m`. `m2` is untouched: `m\b` cannot match where a `2` follows.
#   * a RANGE — `cca 150-180 m²` against the same advert on a portal that dropped the hyphen.
#     Masking ONE side is enough: a one-sided token is never read (E12).
_CHARGE_WORD: str = (
    r"(?:naklad\w*|inkaso|zaloh\w*|sluzb\w*|poplat\w*|kauc\w*|jistot\w*|provi\w*"
    r"|najemn\w*|najem|energi\w*|elektrin\w*|vodn\w*|stocn\w*|topen\w*|odpad\w*)"
)
_CODE_WORD: str = (
    r"(?:ev\.?\s*c\w*|evidencn\w*|zakazk\w*|nabidk\w*|referenc\w*|ref\.?\s*c\w*|id\s*c\w*)"
)
_DATE_WORD: str = (
    r"(?:od|do|dne|termin\w*|prohlidk\w*|nastehovani|dispozici|volny|volna|volne|uvolnen\w*)"
)
_HEAL_MASKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("[DEN]", re.compile(_DATE_WORD + r"\s+\d{1,2}\.\s?\d{1,2}\.?(?!\d)")),
    ("[KOD]", re.compile(_CODE_WORD + r"\W{0,4}(?:\[kod\]\W{0,4})?[a-z]{0,3}\s?\d[\d/-]{0,12}")),
    ("[CENA]", re.compile(r"\b\d[\d.]*\d\s*[.,]\s?-")),
    ("[CENA]", re.compile(r"\b\d+(?:[.,]\d+)?\s*tis\.?\s*(?:kc|czk)?")),
    ("[CENA]", re.compile(_CHARGE_WORD + r"\W{0,4}\d[\d.,]*")),
    ("[DOBA]", re.compile(r"\b\d{1,3}\s*(?:mesic\w*|let\b|lety\b|rok\w*)")),
    ("[POCET]", re.compile(r"\b\d{1,2}\s?x\b")),
    ("[ROZSAH]", re.compile(r"\b\d{1,5}(?:[.,]\d+)?\s*[-–]\s*\d{1,5}(?:[.,]\d+)?")),
    ("[ROZMER]", re.compile(r"\b\d{1,4}(?:[.,]\d+)?\s*m\b(?!2)")),
)
# One storey, two vocabularies. `2. NP` and `1. patro` are the same floor of one flat — the
# Czech ground floor is `1. NP` and `přízemí`, and the first `patro` stands above it. Normalised
# to one sentinel so the pair reads as equal while `3. NP` against `4. NP` stays two flats.
_NP = re.compile(r"\b(\d{1,2})\.?\s*(?:np\b|nadzemnim?\s+podlazi\w*)")
_PATRO = re.compile(r"\b(\d{1,2})\.?\s*patr\w*")


def _storey(level: int) -> str:
    """The storey as a LETTER sentinel: `[np]` carrying a digit would read as a quantity."""
    return f" [np{chr(ord('a') + min(max(level, 0), 25))}] "

_TOKEN = re.compile(r"[a-z0-9]+(?:[./,-][a-z0-9]+)*|\[[a-z]+\]")
_DIGIT = re.compile(r"\d")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?$")
# The STREET is read by E151 (`text_facts.prose_streets`) and not here. This module works on a
# folded stream with no capitals, so "the two tokens after `ulice`" was an adjective as often
# as a name — `mimořádně` against `velmi` split one 120 m² flat two portals both carried.


@dataclass(frozen=True, slots=True)
class Token:
    text: str
    digit: bool


def _fold(text: str) -> str:
    return _ACCENTS.sub("", unicodedata.normalize("NFKD", text)).lower()


def mask(text: str, heal: bool = False) -> str:
    """The body with every field that legitimately moves between two postings blanked."""
    out = _SPACED_THOUSANDS.sub("", _fold(mask_codes(text) or ""))
    out = out.replace("[kod]", " [kod] ")
    if heal:
        out = _NP.sub(lambda m: _storey(int(m.group(1))), out)
        out = _PATRO.sub(lambda m: _storey(int(m.group(1)) + 1), out)
    for sentinel, pattern in _MASKS:
        out = pattern.sub(sentinel, out)
    if heal:
        for sentinel, pattern in _HEAL_MASKS:
            out = pattern.sub(sentinel, out)
    return out


@lru_cache(maxsize=BODY_CACHE)
def tokens(text: str, heal: bool = False) -> tuple[Token, ...]:
    """The masked body as tokens, each carrying whether it states a quantity or a code."""
    return tuple(
        Token(word, bool(_DIGIT.search(word)) and not word.startswith("["))
        for word in (match.group(0) for match in _TOKEN.finditer(mask(text, heal)))
    )


# A token that NAMES a unit: a building-and-flat code (`B1.2.2`, `F2.103`), a land-register
# parcel (`934/11`), or a short block label (`G3`, `C2`). These are compared as whole strings,
# because `B1.2.2` and `B2.2.2` state the same three numbers in a different order.
_CODE = re.compile(r"^(?:[a-z]{1,3}\d{1,4}(?:[./-][a-z0-9]{1,4})*|\d{1,5}(?:[./-]\d{1,4})+)$")
# The unit symbols the tokeniser cannot help carrying a digit: `m2`, `m³`, an HTML `<sup>2</sup>`
# that survived as `sup2`. They state no quantity of their own.
_UNIT_SYMBOL = re.compile(r"(?:sup\d|m\d|km\d)")
_NUMBER_IN = re.compile(r"\d+(?:[.,]\d+)?")
# `m2` and `sup2` have the shape of a block label and are a unit symbol. The letters decide.
_NOT_CODE_PREFIX = frozenset((
    "m", "km", "cm", "mm", "dm", "sup", "kc", "czk", "ks", "np", "pp", "kw", "kwh", "ha",
    "tel", "www", "id", "cca", "tj", "obr",
))
_CODE_PREFIX = re.compile(r"^[a-z]+")


def _decimals(text: str) -> int:
    body = text.replace(",", ".")
    return len(body.split(".", 1)[1]) if "." in body else 0


def _is_code(text: str) -> bool:
    if not _CODE.match(text):
        return False
    prefix = _CODE_PREFIX.match(text)
    return not (prefix and prefix.group(0) in _NOT_CODE_PREFIX)


def _numbers_of(segment: list[Token]) -> list[tuple[str, float, int]]:
    out: list[tuple[str, float, int]] = []
    for token in segment:
        if not token.digit or _is_code(token.text):
            continue
        for match in _NUMBER_IN.finditer(_UNIT_SYMBOL.sub(" ", token.text)):
            raw = match.group(0)
            try:
                out.append((raw, float(raw.replace(",", ".")), _decimals(raw)))
            except ValueError:
                continue
    return out


def _reading(segment: list[Token]) -> tuple[frozenset[str], list[tuple[float, int]]]:
    codes = frozenset(token.text for token in segment
                      if token.digit and _is_code(token.text))
    return codes, [(value, decimals) for _, value, decimals in _numbers_of(segment)]


def rounding_equal_values(left: float, left_decimals: int,
                          right: float, right_decimals: int) -> bool:
    """Two printed numbers that agree within the ROUNDING UNIT of the coarser of them.

    `50` and `50,5` are one area printed to two precisions; `58,90` and `58,70` are two areas.
    Half of the last printed digit is what "equal within rounding" means when the only thing
    known about a number is how it was written — and it is arithmetic, not a tolerance: no
    percentage separates a 1 % rounding of 50,5 from a 0,34 % difference between two flats."""
    slack = max(0.5 * 10.0 ** -left_decimals, 0.5 * 10.0 ** -right_decimals)
    return abs(left - right) <= slack


def rounding_equal(left: str, right: str) -> bool:
    """`rounding_equal_values` read off the two printed strings."""
    if not (_NUMBER.match(left) and _NUMBER.match(right)):
        return False
    try:
        a, b = float(left.replace(",", ".")), float(right.replace(",", "."))
    except ValueError:
        return False
    return rounding_equal_values(a, _decimals(left), b, _decimals(right))


@lru_cache(maxsize=BODY_CACHE)
def body_numbers(text: str, heal: bool = False) -> tuple[tuple[float, int], ...]:
    """Every number the whole body states, whatever position it sits in."""
    return tuple(sorted({(value, decimals)
                         for _, value, decimals in _numbers_of(list(tokens(text, heal)))}))


def _stated_elsewhere(values: list[tuple[float, int]],
                      elsewhere: tuple[tuple[float, int], ...]) -> bool:
    """Does the OTHER advert print this number somewhere else of its own?

    `48,4 m² podlahová` against `52,4 m² užitná` is one flat measured two ways, and the second
    advert prints both figures — so the position where they differ is a difference of BASIS,
    not of unit. A number the other side never writes at all is the real thing."""
    return any(rounding_equal_values(value, decimals, other, other_decimals)
               for value, decimals in values
               for other, other_decimals in elsewhere)


def _differ_digit(left: list[Token], right: list[Token],
                  left_body: tuple[tuple[float, int], ...] = (),
                  right_body: tuple[tuple[float, int], ...] = ()) -> tuple[str, str] | None:
    """What the two sides SAY at one position, when it is not the same thing.

    A token is not its spelling. `140m2` and `140m` are one area, `2.nadzemní` and `2` are one
    storey, `pronajmu.2` and `2` are one number the tokeniser glued to different neighbours —
    every one of these was a false split before this function read through the spelling. So a
    token is reduced to what it states: a unit CODE (`B1.2.2`, `F2.103`, `G3`, `934/11`), or
    the numbers it contains with the unit symbols removed. Codes are compared exactly and
    numbers by the rounding rule, and each comparison is a conflict only when BOTH sides state
    something and the two states share nothing."""
    left_codes, left_numbers = _reading(left)
    right_codes, right_numbers = _reading(right)
    if left_codes and right_codes:
        if left_codes & right_codes:
            return None
        return (" ".join(sorted(left_codes)), " ".join(sorted(right_codes)))
    if left_codes or right_codes:
        return None
    if not left_numbers or not right_numbers:
        return None
    if any(rounding_equal_values(a, da, b, db)
           for a, da in left_numbers for b, db in right_numbers):
        return None
    if (_stated_elsewhere(left_numbers, right_body)
            or _stated_elsewhere(right_numbers, left_body)):
        return None
    return (" ".join(sorted(text for text, _, _ in _numbers_of(left))),
            " ".join(sorted(text for text, _, _ in _numbers_of(right))))


def aligned_difference(
    left_text: str | None,
    right_text: str | None,
    min_ratio: float = 0.75,
    heal: bool = False,
) -> tuple[str, str] | None:
    """The first position two near-identical bodies fill differently, or None.

    Returns `(left, right)` naming only the significant tokens, so the reason a pair was
    refused is a sentence an operator can check against the two adverts."""
    if not left_text or not right_text:
        return None
    left, right = tokens(left_text, heal), tokens(right_text, heal)
    if len(left) < MIN_TOKENS or len(right) < MIN_TOKENS:
        return None
    left_words = [token.text for token in left]
    right_words = [token.text for token in right]
    matcher = SequenceMatcher(None, left_words, right_words, autojunk=False)
    blocks = matcher.get_matching_blocks()
    matched = sum(block.size for block in blocks)
    if 2.0 * matched / (len(left) + len(right)) < min_ratio:
        return None
    opcodes = matcher.get_opcodes()
    for index, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag != "replace":
            continue
        if i2 - i1 > MAX_SEGMENT or j2 - j1 > MAX_SEGMENT:
            continue
        before = opcodes[index - 1] if index else None
        after = opcodes[index + 1] if index + 1 < len(opcodes) else None
        if before is None or before[0] != "equal" or before[2] - before[1] < CONTEXT_TOKENS:
            continue
        if after is None or after[0] != "equal" or after[2] - after[1] < CONTEXT_TOKENS:
            continue
        segment_left, segment_right = list(left[i1:i2]), list(right[j1:j2])
        found = _differ_digit(segment_left, segment_right,
                              body_numbers(left_text, heal), body_numbers(right_text, heal))
        if found is not None:
            return found
    return None
