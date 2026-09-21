"""E150: align two near-identical bodies and read the positions where they differ.

Every reader before this one knows a FORM — `parcelní číslo`, `stání č.`, `označením B36`. A
new cohort has produced a new form every time: `B1.2.2` against `B1.2.3`, `F2.103` against
`F3.103`, a plot `s označením 7` against `8`, a garage block `G3` against `G4`, `58,90 m²`
against `58,70 m²`. Knowing the form is not a strategy, because the developer chooses it.

What does NOT change is the shape of the hazard: two adverts for two units of one project are
written from ONE template, so they are word-identical everywhere except at the few positions
that name the unit. So this module knows no form at all. It aligns the two token streams, and
where the alignment says "here one says X and the other says Y", with agreeing context on both
sides, it reads X and Y. A position is a fact only when at least one side carries a DIGIT or a
code-like token, or is a street name — the alignment tells us WHERE to look and the token
tells us whether what is written there can name a unit.

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
    ("[ROK]", re.compile(r"\b(?:19|20)\d{2}\b")),
    ("[PCT]", re.compile(r"\b\d{1,3}(?:[.,]\d+)?\s?%")),
    ("[FOTO]", re.compile(r"\b\d{1,3}\s*(?:fotek|fotografi\w*|obrazk\w*)\b")),
)

_TOKEN = re.compile(r"[a-z0-9]+(?:[./,-][a-z0-9]+)*|\[[a-z]+\]")
_DIGIT = re.compile(r"\d")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?$")
# The street keyword, read on the FOLDED stream: the token that follows it is a name.
_STREET_KEYWORD = re.compile(r"^(?:ulice|ulici|ulicich|ul|tride|trida|tr|namesti|nam)$")
STREET_LOOKAHEAD: int = 2


@dataclass(frozen=True, slots=True)
class Token:
    text: str
    digit: bool
    street: bool


def _fold(text: str) -> str:
    return _ACCENTS.sub("", unicodedata.normalize("NFKD", text)).lower()


def mask(text: str) -> str:
    """The body with every field that legitimately moves between two postings blanked."""
    out = _SPACED_THOUSANDS.sub("", _fold(mask_codes(text) or ""))
    out = out.replace("[kod]", " [kod] ")
    for sentinel, pattern in _MASKS:
        out = pattern.sub(sentinel, out)
    return out


@lru_cache(maxsize=BODY_CACHE)
def tokens(text: str) -> tuple[Token, ...]:
    """The masked body as tokens, each carrying whether it can NAME a unit."""
    raw = [match.group(0) for match in _TOKEN.finditer(mask(text))]
    street_at: set[int] = set()
    for index, word in enumerate(raw):
        if _STREET_KEYWORD.match(word):
            for offset in range(1, STREET_LOOKAHEAD + 1):
                if index + offset < len(raw):
                    street_at.add(index + offset)
    return tuple(
        Token(word, bool(_DIGIT.search(word)) and not word.startswith("["),
              index in street_at and not _DIGIT.search(word) and len(word) > 2)
        for index, word in enumerate(raw)
    )


def _decimals(text: str) -> int:
    body = text.replace(",", ".")
    return len(body.split(".", 1)[1]) if "." in body else 0


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


def _differ(left: list[Token], right: list[Token], attribute: str) -> tuple[str, str] | None:
    """The two sides' significant tokens at one position, when they are not the same set."""
    lefts = [token.text for token in left if getattr(token, attribute)]
    rights = [token.text for token in right if getattr(token, attribute)]
    if not lefts or not rights:
        return None
    if len(lefts) == 1 and len(rights) == 1 and rounding_equal(lefts[0], rights[0]):
        return None
    if set(lefts) == set(rights):
        return None
    return (" ".join(lefts), " ".join(rights))


def aligned_difference(
    left_text: str | None,
    right_text: str | None,
    min_ratio: float = 0.75,
) -> tuple[str, str] | None:
    """The first position two near-identical bodies fill differently, or None.

    Returns `(left, right)` naming only the significant tokens, so the reason a pair was
    refused is a sentence an operator can check against the two adverts."""
    if not left_text or not right_text:
        return None
    left, right = tokens(left_text), tokens(right_text)
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
        for attribute in ("digit", "street"):
            found = _differ(segment_left, segment_right, attribute)
            if found is not None:
                return found
    return None
