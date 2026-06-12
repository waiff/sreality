"""Street + disposition keyed dedup engine — pure decision logic.

Replaces the old geo-proximity matcher (20m/price/area spatial probe). The new
engine keys on the two identifiers the operator trusts — STREET and DISPOSITION
— and confirms ambiguous pairs with room-aware vision. The rules (A-E):

  A. Eligibility. A listing matches only with BOTH a street and a disposition;
     otherwise it's flagged (location_unclear / disposition_unclear) and never
     compared. Computed inline (a partial index backs the scan, migration 127).
  B. Exact-address auto-merge. Same street + house number + disposition + floor
     => same property. Guarded: if area is known on both and differs by >5%,
     don't blind-merge — route to visual (a building can have같은-floor units).
  C. Candidate gate. Two listings sharing street + disposition are visual
     candidates UNLESS a hard contradiction rules them out: floors differ (both
     known), areas differ by >20% (both known), or house numbers differ (both
     known). Never compare anything that doesn't share street + disposition.
  D. Visual confirmation (layered): pHash fast-path on INTERIOR photos (>=2
     near-identical pairs => merge), else room-aware forensic comparison in
     priority order, stop at the first High verdict => merge.
  E. Everything left (same street+disposition, no contradiction, but no images
     or a non-High verdict) is queued for the operator.

This module is the PURE half: dataclasses + functions with no DB / LLM / I/O, so
the rules are unit-tested in isolation. scripts/dedup_engine.py is the I/O
orchestrator that feeds these and calls merge_properties / the vision tools.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from toolkit.comparables import _DISPOSITION_LOOSE

# Rule B: exact-address merge is blocked when areas disagree by more than this.
ADDRESS_AREA_GUARD_PCT = 0.05
# Rule C disqualifier: a >20% area gap means "not the same property".
CANDIDATE_AREA_MAX_PCT = 0.20
# Rule D layer 1: an interior image pair this close (Hamming) counts as identical.
PHASH_IDENTICAL_MAX = 6
# Rule D layer 1: need at least this many identical interior pairs to auto-merge
# (one shared photo can be a reused stock/marketing shot; two is a real signal).
PHASH_MIN_IDENTICAL_PAIRS = 2

# Rule D priority order: cheap, distinctive rooms first; bedrooms last (most
# generic / interchangeable). exterior_facade sits late — a shared facade is
# weak evidence for a specific unit. floor_plan / other are never compared.
ROOM_PRIORITY: tuple[str, ...] = (
    "kitchen", "bathroom", "toilet", "living_room", "hallway",
    "balcony_terrace", "garden", "exterior_facade", "bedroom",
)


@dataclass(frozen=True)
class ListingKey:
    """The matchable identity of one eligible listing."""
    sreality_id: int
    property_id: int | None
    source: str
    street_key: str          # one grouping key: 'id:<street_id>' or 'name:<normalized>'
    disposition: str
    house_number: str | None
    floor: int | None
    area_m2: float | None
    description: str | None = None
    # Offering category: category_type is prodej/pronajem/drazba/podil,
    # category_main is byt/dum/… A sale and a rental (or a flat and a house) at
    # one address are categorically different offerings — never the same
    # property — so a mismatch is a hard reject in classify_pair.
    category_type: str | None = None
    category_main: str | None = None
    # Canonical portal street id when known (sreality), regardless of which
    # grouping key this instance carries. Two known-but-different ids inside a
    # name group mean two distinct streets sharing a name — a hard reject.
    street_id: int | None = None


# Unit markers in the description that identify a SPECIFIC unit within one
# development (developer projects list near-identical plots/houses/flats with
# only the unit number differing — "pozemek č. 3" vs "č. 4", "dům 3A" vs "5C",
# "byt 42" vs "45", "budova A" vs "budova B"). When two listings name DIFFERENT
# units under the same keyword they are distinct properties, even though
# street/disposition/photos (shared marketing renders) all match.
#
# Two token shapes:
#  - NUMERIC: "<keyword> [č./č/no/number] <token>" where the token is a number
#    optionally followed by a letter or /number (3, 3A, 12/4). Case-insensitive
#    (run on diacritics-stripped lowercase text).
#  - LETTER: "<keyword> [A-H]" — a single UPPERCASE building/section letter
#    ("Budova A" vs "Budova B"). Matched CASE-SENSITIVELY against the original
#    text so the Czech conjunction "a"/"i" and stray lowercase letters don't
#    masquerade as unit labels. Restricted to A–H (real projects don't label
#    blocks past a handful) to further cut false positives.
#
# Keyword sets are kept apart: numeric keywords (a flat/plot number can follow)
# vs. container keywords that take a letter label (building/block/entrance/…).
_NUMERIC_KEYWORDS = "pozemek|dum|byt|jednotka|parcela|chata|rd|objekt|sekce|vchod"
_CONTAINER_KEYWORDS = "budova|blok|sekce|vchod|etapa|objekt|dum|dom"

_UNIT_NUM_RE = re.compile(
    r"\b(" + _NUMERIC_KEYWORDS + r")\b"
    r"(?:\s*(?:c|cislo|no|number)\.?)?\s*"
    r"(\d+[a-z]?(?:/\d+)?)\b"
)
# Letter labels: keyword must keep its diacritics-stripped form, but the LETTER
# is read from the original text so case is preserved. Built case-insensitively
# on the keyword, case-sensitively on the [A-H] group via an inline flag.
_UNIT_LETTER_RE = re.compile(
    r"(?i:\b(" + _CONTAINER_KEYWORDS + r")\b)\s*"
    r"(?-i:([A-H]))(?![\w])"
)


def _strip_diacritics(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _unit_markers(description: str | None) -> dict[str, set[str]]:
    """Map each unit keyword → the set of unit tokens it names in the text.

    e.g. "...dům, pozemek č.4..." → {'pozemek': {'4'}}; "Budova A" → {'budova':
    {'A'}}. Numeric tokens come from diacritics-stripped lowercase text; letter
    labels from the original text (case preserved) so 'A'/'B' are unit labels but
    the conjunction 'a' is not. Empty when nothing matches.
    """
    if not description:
        return {}
    out: dict[str, set[str]] = {}
    ascii_lower = _strip_diacritics(description).lower()
    for kw, token in _UNIT_NUM_RE.findall(ascii_lower):
        out.setdefault(kw, set()).add(token)
    # Letter labels: run on the diacritics-stripped (but case-preserved) text so
    # 'dům'→'dum' for the keyword while 'A' stays uppercase.
    ascii_cased = _strip_diacritics(description)
    for kw, letter in _UNIT_LETTER_RE.findall(ascii_cased):
        out.setdefault(kw.lower(), set()).add(letter)
    return out


def _unit_markers_contradict(a: str | None, b: str | None) -> bool:
    """True when both descriptions name the SAME unit keyword but DIFFERENT
    tokens for it (e.g. pozemek 3 vs pozemek 4) — distinct units, never merge.

    Conservative: only fires on a shared keyword whose token sets are disjoint.
    A keyword present in only one description, or with overlapping tokens, does
    NOT contradict (avoids false rejections from prose like "byt 2+kk")."""
    ma, mb = _unit_markers(a), _unit_markers(b)
    for kw in ma.keys() & mb.keys():
        if ma[kw].isdisjoint(mb[kw]):
            return True
    return False


@dataclass(frozen=True)
class PairDecision:
    action: str               # 'auto_merge' | 'candidate' | 'reject'
    reason: str | None        # 'address_exact' | 'area_guard' | None ...
    # for 'reject': which disqualifier fired
    detail: str | None = None


# Street words that decorate a name without identifying it ("ul. Hlavní" and
# "Hlavní" are one street; "Vinohradská třída" ~ "Vinohradská"). Diacritics-
# folded forms incl. the inflections bazos' extract_street emits. Compared
# token-wise with a trailing dot stripped, so "Třebízského" is never touched.
_STREET_WORDS = frozenset({
    "ul", "ulice", "ulici",
    "nam", "namesti",
    "tr", "trida", "tride", "tridu", "tridy",
    "nabr", "nabrezi",
    "sidliste", "sidlisti",
})
# A trailing house-number token: 12, 12a, 123/45, 160/26b. Bounded at 4 digits
# like the bazos extractor; "679 61" (PSČ) strips as two successive tokens.
_HOUSE_NO_TOKEN_RE = re.compile(r"\d{1,4}[a-z]?(?:/\d{1,4}[a-z]?)?")


def _street_name_key(street: str | None) -> str | None:
    """Grouping form of a street NAME: diacritics-stripped lowercase with
    street words and trailing house-number tokens removed. Portals disagree on
    decoration (sreality stores the bare canonical name; bazos mines "ul.
    Koterovská 12"-style strings from free text), so the key must not. Falls
    back to the undecorated-stripped form rather than going empty (a street
    literally named "Náměstí" keeps a usable key)."""
    if not street or not street.strip():
        return None
    decomposed = unicodedata.normalize("NFKD", street.strip().lower())
    ascii_name = "".join(c for c in decomposed if not unicodedata.combining(c))
    collapsed = " ".join(ascii_name.split())
    if not collapsed:
        return None
    tokens = collapsed.split()
    changed = True
    while changed and tokens:
        changed = False
        if tokens[0].rstrip(".") in _STREET_WORDS:
            tokens.pop(0)
            changed = True
        if tokens and tokens[-1].rstrip(".") in _STREET_WORDS:
            tokens.pop()
            changed = True
        if tokens and _HOUSE_NO_TOKEN_RE.fullmatch(tokens[-1]):
            tokens.pop()
            changed = True
    stripped = " ".join(tokens)
    return stripped or collapsed


def normalize_street(street: str | None, street_id: int | None) -> str | None:
    """The primary street grouping key: prefer the portal's canonical
    street_id, else the normalized street name. None when there is no street."""
    if street_id is not None and street_id > 0:
        return f"id:{street_id}"
    name = _street_name_key(street)
    return f"name:{name}" if name is not None else None


def street_group_keys(street: str | None, street_id: int | None) -> tuple[str, ...]:
    """ALL grouping keys for one listing, canonical id first. A row carrying
    both a street_id and a street name is dual-keyed — it joins its 'id:' group
    AND the 'name:' group — so an id-keyed sreality row can meet a name-only
    bazos row in one candidate group (id-only grouping made that impossible)."""
    keys: list[str] = []
    if street_id is not None and street_id > 0:
        keys.append(f"id:{street_id}")
    name = _street_name_key(street)
    if name is not None:
        keys.append(f"name:{name}")
    return tuple(keys)


def disposition_compatible(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return a == b or b in _DISPOSITION_LOOSE.get(a, ())


def _area_pct_diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or max(a, b) == 0:
        return None
    return abs(a - b) / max(a, b)


def _house_numbers_contradict(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a.strip().lower() != b.strip().lower()


def classify_pair(a: ListingKey, b: ListingKey) -> PairDecision:
    """Decide what to do with two eligible listings that share a street key.

    Pure: no images yet. Returns 'auto_merge' (rule B), 'reject' (rule C
    disqualifier), or 'candidate' (needs the visual layer). The street-key match
    is a precondition the caller guarantees by only pairing within a street
    group; we re-assert disposition compatibility here.
    """
    if a.sreality_id == b.sreality_id:
        return PairDecision("reject", None, "same_listing")
    if a.property_id is not None and a.property_id == b.property_id:
        return PairDecision("reject", None, "already_merged")
    if a.street_key != b.street_key:
        return PairDecision("reject", None, "street_mismatch")
    # Dual-keying lets same-NAME streets from different towns share one name
    # group; when both portals supplied a canonical street_id and they differ,
    # these are two distinct streets — hard reject (NULL ids don't contradict).
    if (
        a.street_id is not None and b.street_id is not None
        and a.street_id != b.street_id
    ):
        return PairDecision("reject", None, "street_id_contradiction")
    # Offering category is fundamental: a sale and a rental (prodej vs pronajem),
    # or a flat and a house (byt vs dum), at one address are different offerings,
    # never the same property — reject before anything else. NULLs don't
    # contradict (a missing category is unknown, not a conflict).
    if (
        a.category_type is not None and b.category_type is not None
        and a.category_type != b.category_type
    ):
        return PairDecision("reject", None, "category_type_contradiction")
    if (
        a.category_main is not None and b.category_main is not None
        and a.category_main != b.category_main
    ):
        return PairDecision("reject", None, "category_main_contradiction")
    if not disposition_compatible(a.disposition, b.disposition):
        return PairDecision("reject", None, "disposition_mismatch")

    # Rule C hard disqualifiers (apply to BOTH the exact-address and candidate
    # paths — a contradiction means "not the same property", full stop).
    if a.floor is not None and b.floor is not None and a.floor != b.floor:
        return PairDecision("reject", None, "floor_contradiction")
    if _house_numbers_contradict(a.house_number, b.house_number):
        return PairDecision("reject", None, "house_number_contradiction")
    area_diff = _area_pct_diff(a.area_m2, b.area_m2)
    if area_diff is not None and area_diff > CANDIDATE_AREA_MAX_PCT:
        return PairDecision("reject", None, "area_contradiction")
    # Distinct units of one development (pozemek č.3 vs č.4, dům 3A vs 5C, …):
    # the descriptions name the same keyword with different unit tokens. The
    # photos can't disambiguate (shared renders), so this MUST gate before the
    # visual layer — a hard reject, like the other rule-C contradictions.
    if _unit_markers_contradict(a.description, b.description):
        return PairDecision("reject", None, "unit_marker_contradiction")

    # Rule B: exact address (street + house number + disposition + floor), with
    # the 5% area guard. house_number must be present + equal on both, floor
    # present + equal on both, disposition exactly equal (not just loose).
    exact_address = (
        a.house_number is not None and b.house_number is not None
        and a.house_number.strip().lower() == b.house_number.strip().lower()
        and a.floor is not None and b.floor is not None and a.floor == b.floor
        and a.disposition == b.disposition
    )
    if exact_address:
        if area_diff is not None and area_diff > ADDRESS_AREA_GUARD_PCT:
            # Same address+floor+disposition but materially different area: most
            # likely two distinct units — let vision settle it.
            return PairDecision("candidate", "area_guard")
        return PairDecision("auto_merge", "address_exact")

    # Rule C: shares street + disposition, no contradiction — a visual candidate.
    return PairDecision("candidate", None)


@dataclass
class VisualOutcome:
    """The result of the visual layer for one candidate pair."""
    action: str               # 'auto_merge' | 'queue'
    reason: str | None        # 'image_phash' | 'visual_match' | None
    room_type: str | None = None
    verdict: str | None = None      # High|Medium|Low (forensic path)
    rationale: str | None = None
    phash_pairs: int = 0
    rooms_tried: list[str] = field(default_factory=list)


def decide_phash_fastpath(identical_interior_pairs: int) -> bool:
    """Rule D layer 1: >=2 near-identical INTERIOR image pairs => same property."""
    return identical_interior_pairs >= PHASH_MIN_IDENTICAL_PAIRS


def rooms_in_priority(common_rooms: set[str]) -> list[str]:
    """The room types present in BOTH listings, in comparison priority order."""
    return [r for r in ROOM_PRIORITY if r in common_rooms]


def verdict_is_merge(verdict: str | None) -> bool:
    """Rule D / auto gate: only a High forensic verdict auto-merges."""
    return verdict == "High"
