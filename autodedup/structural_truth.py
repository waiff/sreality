"""Labels for pairs that come from STRUCTURE in the adverts — not from a model, not from the operator.

W6 could not measure any judge's false-merge rate because the human negatives it had were 18
band negatives over 16 listings, 14 of them from one card. A rate measured on one development
is not a rate. This module manufactures the missing axis: pairs whose truth a *fact printed in
the adverts* settles, over as many address blocks as the cohort holds.

Two classes, both conservative — a rule that is merely likely is not here.

POSITIVE (certainly ONE unit). Czech agencies stamp their order number into the body text
("evidenční číslo zakázky N115423", "Ev. číslo: 657349") and the SAME string travels onto every
portal the order is syndicated to, and onto the re-post when the advert expires. The code is the
agency's key for one order, so two adverts carrying it are two views of one order. `pos_unit_in_project`
adds the case with no code: one address block, both adverts naming the same unit number, on the
same stated floor, at the same area.

NEGATIVE (certainly TWO units). Co-liveness alone proves nothing — a broker posting one unit twice
on one portal is a duplicate by the operator's ruling — so every negative rule demands an EXPLICIT
conflicting unit fact: two different unit numbers, two different order codes running side by side,
or a different floor together with a different area.

Nothing here reads an engine feature, a judge verdict or an operator label: the labels must be
independent of the thing they measure. The only shared code is `normalize`'s text folding, which is
parsing, not deciding.

Evidence travels as short strings so a contradiction can be adjudicated by reading it. They are run
through `export.scrub_description` (E28) on the way out, because the context window around a code
can clip a broker's signature.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Sequence

from autodedup.dataset import Listing, live_end_stamp
from autodedup.export import scrub_description
from autodedup.text_facts import (
    MAX_CODE_POPULATION,
    MIN_CODE_LEN,
    CODE_MASK,
    address_block_key,
    fold as _fold,
    iter_reference_matches,
    mask_codes,
    reference_codes,
    stated_areas,
    unit_designators,
)

# Re-exported so the benchmark keeps ONE import surface while the parsing lives in
# `text_facts`, where the engine can read the same strings (E60/E61).
unit_numbers = unit_designators
__all__ = [
    "MAX_CODE_POPULATION",
    "MIN_CODE_LEN",
    "CODE_MASK",
    "address_block_key",
    "mask_codes",
    "reference_codes",
    "reference_context",
    "stated_areas",
    "unit_designators",
    "unit_numbers",
    "build_index",
    "label_pair",
    "label_pairs",
    "overlap_days",
    "stratify",
]

LABEL_SAME: str = "same"
LABEL_DIFFERENT: str = "different"

POSITIVE_RULES: tuple[str, ...] = ("pos_ref_cross", "pos_ref_relist", "pos_unit_in_project")
NEGATIVE_RULES: tuple[str, ...] = ("neg_unit_number_conflict", "neg_stated_area_conflict")
RULES: tuple[str, ...] = POSITIVE_RULES + NEGATIVE_RULES

# Two adverts that ran side by side this long are not one advert replaced by its re-post.
MIN_OVERLAP_DAYS: float = 21.0
UNIT_AREA_MAX_REL_DIFF: float = 0.02
# Two areas a Czech advert PRINTS are the same area when they are this close: 47,6 vs 46,6 m²
# is one flat measured twice (adjudicated, pair 16438/92824 — identical text, identical price),
# 50,7 vs 43,3 is two flats. Below this the rule abstains rather than guess.
#
# W8 raises it from 3 % to the engine's own `area_reject_pct`: a printed area is a per-portal
# reading of one tape measure, and 3 % is finer than the measurement is. 464483 × 509660 prints
# 80 and 75 m² for one 13th-floor 2+kk with 9 of 10 photos byte-identical (6.2 % apart), and
# 355436 × 518123 cleared the old bar by 0.3 points. A structural NEGATIVE may not be stricter
# than the guard that would have rejected the pair anyway.
STATED_AREA_MIN_REL_DIFF: float = 0.08


def reference_context(text: str | None, code: str, width: int = 44) -> str:
    """The scrubbed window around `code`'s first mention — what a human adjudicates from."""
    if not text:
        return ""
    for found, match in iter_reference_matches(_fold(text)):
        if found != code:
            continue
        start = max(0, match.start() - width)
        window = text[start : match.end() + 4]
        return " ".join((scrub_description(window) or "").split())
    return ""


def areas_disjoint(a: Iterable[float], b: Iterable[float]) -> bool:
    """No area either text prints is within `STATED_AREA_MIN_REL_DIFF` of one the other prints."""
    left, right = list(a), list(b)
    if not left or not right:
        return False
    return not any(
        abs(x - y) <= STATED_AREA_MIN_REL_DIFF * max(x, y) for x in left for y in right
    )


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_FAR_FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)


def live_end(listing: Listing) -> datetime | None:
    """When an advert was last known to be live — the SIGHTING, never the detection stamp.

    W8 correction. `inactive_at` is when the delisting was DETECTED: rule #3 flips a row only
    after a near-complete index walk plus a cadence-scaled staleness window, so the stamp lands
    hours to weeks after the advert actually went. Listing 412650 was last seen 2026-08-12 and
    stamped inactive 2026-09-08 — a 1-day co-live window scored as 27.4 days, enough to sail
    past the 21-day re-post guard written for exactly that case.

    The rule itself lives on `dataset.live_end_stamp`, so the engine's three readers and this
    benchmark cannot drift apart."""
    return _parse(live_end_stamp(listing)) or (_FAR_FUTURE if listing.is_active else None)


def overlap_days(a: Listing, b: Listing) -> float:
    """Days both adverts were live at once; a live advert ends at +infinity."""
    start_a, start_b = _parse(a.first_seen_at), _parse(b.first_seen_at)
    if start_a is None or start_b is None:
        return 0.0
    end_a, end_b = live_end(a), live_end(b)
    if end_a is None or end_b is None:
        return 0.0
    span = min(end_a, end_b) - max(start_a, start_b)
    return max(0.0, span / timedelta(days=1))


def _rel_diff(x: float | None, y: float | None) -> float | None:
    if x is None or y is None:
        return None
    scale = max(abs(x), abs(y))
    return abs(x - y) / scale if scale else 0.0


@dataclass(slots=True, frozen=True)
class StructuralLabel:
    lo: int
    hi: int
    label: str
    rule: str
    block: str
    evidence: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "lo": self.lo,
            "hi": self.hi,
            "label": self.label,
            "rule": self.rule,
            "block": self.block,
            "evidence": dict(self.evidence),
        }


@dataclass(slots=True)
class Facts:
    """Everything the rules read off ONE listing, computed once."""

    listing_id: int
    codes: frozenset[str]
    units: frozenset[str]
    areas: frozenset[float]
    block: str


@dataclass(slots=True)
class Index:
    facts: dict[int, Facts]
    code_population: dict[str, int]

    def rare_codes(self, listing_id: int) -> set[str]:
        codes = self.facts[listing_id].codes
        return {c for c in codes if self.code_population.get(c, 0) <= MAX_CODE_POPULATION}


def build_index(listings: Mapping[int, Listing]) -> Index:
    facts: dict[int, Facts] = {}
    population: dict[str, int] = defaultdict(int)
    for listing_id, listing in listings.items():
        codes = reference_codes(listing.description)
        facts[listing_id] = Facts(
            listing_id=listing_id,
            codes=frozenset(codes),
            units=frozenset(unit_designators(listing.description)),
            areas=frozenset(stated_areas(listing.description, listing.area_m2)),
            block=address_block_key(listing),
        )
        for code in codes:
            population[code] += 1
    return Index(facts=facts, code_population=dict(population))


def _comparable(a: Listing, b: Listing) -> bool:
    """The standing ruling: never across deal types, never byt with komerční."""
    return a.category_main == b.category_main and a.category_type == b.category_type


def _one_project(a: Listing, b: Listing, index: Index) -> bool:
    """Two adverts a conflicting unit fact can be read across: one address block, or one
    agency's two orders in one municipality (a code conflict needs the agency, not the house)."""
    if index.facts[a.id].block == index.facts[b.id].block:
        return True
    same_place = a.location.obec_kod is not None and a.location.obec_kod == b.location.obec_kod
    if not same_place:
        return False
    if a.broker_identity_id is not None and a.broker_identity_id == b.broker_identity_id:
        return True
    return a.source == b.source and a.broker_key is not None and a.broker_key == b.broker_key


def _code_family(code: str) -> str:
    """`N115423` -> `N999999`: the shape an agency numbers its orders in.

    A code CONFLICT only means something inside one numbering scheme — two agencies selling
    one unit carry two unrelated codes, and calling that a conflict would be a false negative."""
    return re.sub(r"\d", "9", code)


def _positive(a: Listing, b: Listing, index: Index) -> tuple[str, dict[str, str]] | None:
    shared = index.rare_codes(a.id) & index.rare_codes(b.id)
    if shared:
        code = sorted(shared)[0]
        evidence = {
            "code": code,
            "code_population": str(index.code_population.get(code, 0)),
            "context_lo": reference_context(a.description, code),
            "context_hi": reference_context(b.description, code),
        }
        if a.source != b.source:
            return "pos_ref_cross", evidence | {"sources": f"{a.source}|{b.source}"}
        if a.source_id_native != b.source_id_native:
            return "pos_ref_relist", evidence | {
                "source": str(a.source),
                "native": f"{a.source_id_native}|{b.source_id_native}",
                "overlap_days": f"{overlap_days(a, b):.1f}",
            }
        return None
    units_a, units_b = index.facts[a.id].units, index.facts[b.id].units
    if len(units_a) != 1 or len(units_b) != 1 or units_a != units_b:
        return None
    if index.facts[a.id].block != index.facts[b.id].block:
        return None
    area = _rel_diff(a.area_m2, b.area_m2)
    floors_agree = a.floor is not None and a.floor == b.floor
    if not floors_agree and (area is None or area > UNIT_AREA_MAX_REL_DIFF):
        return None
    return "pos_unit_in_project", {
        "unit": sorted(units_a)[0],
        "floor": f"{a.floor}|{b.floor}",
        "area_m2": f"{a.area_m2}|{b.area_m2}",
        "block": index.facts[a.id].block,
    }


def _negative(a: Listing, b: Listing, index: Index) -> tuple[str, dict[str, str]] | None:
    """The two facts a Czech advert prints that settle "different unit" — and nothing softer.

    `neg_ref_code_conflict` was BUILT and DROPPED here: two different order codes in one
    building are not two units, because an agency re-numbers per portal and sometimes per
    re-post. Adjudicated 395722/486034 and 395722/496635 (identical text, identical price
    7 974 910 Kč, codes N115815 vs N118731) — both one flat. Two of the nine band pairs the
    rule produced were false, so it is not ground truth. A shared code stays a POSITIVE."""
    facts_a, facts_b = index.facts[a.id], index.facts[b.id]
    if facts_a.block != facts_b.block:
        return None
    units_a, units_b = facts_a.units, facts_b.units
    if len(units_a) == 1 and len(units_b) == 1 and units_a != units_b:
        return "neg_unit_number_conflict", {
            "unit_lo": sorted(units_a)[0],
            "unit_hi": sorted(units_b)[0],
            "block": facts_a.block,
            "area_m2": f"{a.area_m2}|{b.area_m2}",
        }
    overlap = overlap_days(a, b)
    # An identical asking price, to the koruna, is the developer pricing one flat twice far
    # more often than two flats alike: the K Botiči block prints 27,2 m² and 28,6 m² both at
    # 5 499 000 Kč and the cohort cannot say which is the typo, so the rule abstains there.
    price_identical = a.price is not None and a.price == b.price
    if (
        overlap >= MIN_OVERLAP_DAYS
        and not price_identical
        and areas_disjoint(facts_a.areas, facts_b.areas)
    ):
        return "neg_stated_area_conflict", {
            "areas_lo": ",".join(f"{v:g}" for v in sorted(facts_a.areas)[:6]),
            "areas_hi": ",".join(f"{v:g}" for v in sorted(facts_b.areas)[:6]),
            "min_rel_gap": f"{min(abs(x - y) / max(x, y) for x in facts_a.areas for y in facts_b.areas):.3f}",
            "overlap_days": f"{overlap:.1f}",
            "price": f"{a.price}|{b.price}",
            "block": facts_a.block,
        }
    return None


def label_pair(a: Listing, b: Listing, index: Index) -> StructuralLabel | None:
    """The structural verdict on one pair, or None when no rule fires.

    A pair a positive AND a negative rule both claim is returned as None: the two facts
    contradict each other, and a benchmark may not guess which one lied."""
    if a.id > b.id:
        a, b = b, a
    if not _comparable(a, b):
        return None
    positive = _positive(a, b, index)
    negative = _negative(a, b, index)
    if positive is not None and negative is not None:
        return None
    block = index.facts[a.id].block
    if positive is not None:
        return StructuralLabel(a.id, b.id, LABEL_SAME, positive[0], block, positive[1])
    if negative is not None:
        return StructuralLabel(a.id, b.id, LABEL_DIFFERENT, negative[0], block, negative[1])
    return None


def label_pairs(
    listings: Mapping[int, Listing],
    pairs: Iterable[tuple[int, int]],
    index: Index | None = None,
) -> list[StructuralLabel]:
    index = index or build_index(listings)
    out: list[StructuralLabel] = []
    for lo, hi in pairs:
        a, b = listings.get(lo), listings.get(hi)
        if a is None or b is None:
            continue
        label = label_pair(a, b, index)
        if label is not None:
            out.append(label)
    return out


def stratify(
    labels: Sequence[StructuralLabel], cap_per_block: int, limit: int
) -> list[StructuralLabel]:
    """Round-robin over blocks so no development can dominate the benchmark.

    Blocks are visited in a rotation ordered by `(block, lo, hi)`, taking one pair from each
    before any block gets a second — deterministic, and it maximises block coverage at every
    prefix, so a cap that bites still spreads the pairs it keeps."""
    by_block: dict[str, list[StructuralLabel]] = defaultdict(list)
    for label in sorted(labels, key=lambda item: (item.block, item.lo, item.hi)):
        by_block[label.block].append(label)
    out: list[StructuralLabel] = []
    for round_index in range(cap_per_block):
        for block in sorted(by_block):
            bucket = by_block[block]
            if round_index < len(bucket):
                out.append(bucket[round_index])
                if len(out) >= limit:
                    return out
    return out
