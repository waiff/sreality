"""Street + disposition keyed dedup engine — pure decision logic.

Replaces the old geo-proximity matcher (20m/price/area spatial probe). The new
engine keys on the two identifiers the operator trusts — STREET and DISPOSITION
— and confirms ambiguous pairs with room-aware vision. The rules (A-E):

  A. Eligibility. A listing matches only with BOTH a street and a disposition;
     otherwise it's flagged (location_unclear / disposition_unclear) and never
     compared. (Enforced as listings.dedup_eligibility, migration 127.)
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
    street_key: str          # street_id as text, or normalized street name
    disposition: str
    house_number: str | None
    floor: int | None
    area_m2: float | None


@dataclass(frozen=True)
class PairDecision:
    action: str               # 'auto_merge' | 'candidate' | 'reject'
    reason: str | None        # 'address_exact' | 'area_guard' | None ...
    # for 'reject': which disqualifier fired
    detail: str | None = None


def normalize_street(street: str | None, street_id: int | None) -> str | None:
    """The street grouping key: prefer the portal's canonical street_id, else a
    diacritics-stripped lowercase street name. None when there is no street."""
    if street_id is not None and street_id > 0:
        return f"id:{street_id}"
    if street and street.strip():
        decomposed = unicodedata.normalize("NFKD", street.strip().lower())
        ascii_name = "".join(c for c in decomposed if not unicodedata.combining(c))
        collapsed = " ".join(ascii_name.split())
        return f"name:{collapsed}" if collapsed else None
    return None


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
