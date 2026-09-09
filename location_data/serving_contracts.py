"""Per-feature minimum-granularity contracts — 05 §5.5.2, verbatim, as code.

Every downstream feature that will read the location projection declares here the least it
consumes: the grain it reads at (listing or property), the coarsest `granularity` rung it
accepts and the lowest `match_confidence` it accepts. A consumer that reads below its own
declared floor is a bug this table lets a test catch. §5.5.2's rule for the table itself:
"every flag named here must exist and be NOT NULL — a missing flag reads as 'no gate' and
fails open", so an undeclared feature raises here rather than defaulting.

Ordinal comparisons never use Python string order or the enum's ordinality: granularity
goes through `GranularityRank` (the `location_granularity_rank` table, 01 §0.4) and
confidence through `MATCH_CONFIDENCE_ORDER`, both from `resolver.types`. `"any"` is the
table's own spelling for "no confidence floor" and is not a `match_confidence` label.

Two §5.5.2 rows are deliberately NOT floors and so are not in `FEATURE_FLOORS`:
* "dedup verification" declares `—` for both columns — its gate is §5.4.5's acceptance
  criterion, a verification rule, not a granularity contract;
* "property-grain aggregate" declares "as the listing rule for its grain" — it resolves to
  whichever listing-grain row it is aggregating (§5.5.3 rule 4), so it has no floor of its own.

Nothing reads this table yet. It exists so that the first consumer flip (W6, gated by
`serving_flags`) has a declared floor to assert against instead of inventing one inline.
"""

from __future__ import annotations

from dataclasses import dataclass

from location_data.resolver.types import (
    DEFAULT_GRANULARITY_RANK,
    MATCH_CONFIDENCE_ORDER,
    GranularityRank,
)

ANY_CONFIDENCE = "any"

GRAINS: tuple[str, ...] = ("listing", "property")

# Operator action A5 (MASTER.md §8.2), DECIDED 2026-09-09: a location filter's default is
# `certain ∪ possible` with the possible rows badged — 05 §5.3.3's two predicates, both
# shown, the second labelled. The alternative (strict by default, an "include approximate"
# toggle) stays a legal value so a consumer can offer it as the toggle's other state; it is
# not the default of any filter. Due "before the first filter flips": no filter has.
FILTER_SEMANTICS: tuple[str, ...] = ("include_and_badge", "strict_with_toggle")
FILTER_DEFAULT_SEMANTICS = "include_and_badge"


@dataclass(frozen=True, slots=True)
class FeatureFloor:
    """One §5.5.2 row. `extra_gates` is the row's prose gate, carried for the reader and for
    the consumer that wires the feature — it is not evaluated by `meets_floor`."""

    grain: str
    min_granularity: str
    min_confidence: str
    extra_gates: str


FEATURE_FLOORS: dict[str, FeatureFloor] = {
    "map_pin": FeatureFloor(
        "listing", "building", "high",
        "renderable_as_point (which already carries pin_collision_ok + not disputed)",
    ),
    "map_circle": FeatureFloor("listing", "obec", ANY_CONFIDENCE, "—"),
    "radius_search": FeatureFloor(
        "listing", "street_segment", "medium", "scalar test §5.3.3(B); `certain` predicate",
    ),
    "drawn_polygon_search": FeatureFloor(
        "listing", "street_segment", "medium", "scalar test §5.3.3(B); `certain` predicate",
    ),
    "admin_filter": FeatureFloor(
        "listing", "obec", ANY_CONFIDENCE,
        "obec/okres/kraj; membership by admin_assignment_method §5.3.3(A)",
    ),
    "cast_obce_filter": FeatureFloor(
        "listing", "cast_obce_or_quarter", "medium", "address-point-set membership",
    ),
    "street_filter": FeatureFloor("listing", "street", "medium", "ulice_kod present"),
    "dedup_rung_0a": FeatureFloor("listing", "address_point", "high", "kod_adm key present"),
    "dedup_rung_0b": FeatureFloor(
        "listing", "building", "high",
        "stavební objekt key present + coverage denominator published (§5.4.3)",
    ),
    "dedup_rung_0c": FeatureFloor(
        "listing", "parcel", "high", "parcela key present; pozemek/auction/cadastral",
    ),
    "dedup_tier_1": FeatureFloor(
        "listing", "street", "medium", "textual; ≥1 side portal-claimed house number",
    ),
    "dedup_tier_2": FeatureFloor("listing", "street_segment", "medium", "geo; geo_blockable"),
    "property_map_pin": FeatureFloor(
        "property", "building", "high",
        "disagreement_flags = '{}' and member_spread_m ≤ f(r,r)",
    ),
    "property_card_location": FeatureFloor(
        "property", "obec", ANY_CONFIDENCE, "shows winner_rule + spread when flagged",
    ),
    "comparables_estimation": FeatureFloor(
        "listing", "street", "medium", "precision recorded in the estimation trace",
    ),
    "per_street_price_stats": FeatureFloor(
        "property", "street", "high", "n ≥ threshold, `certain` only, §5.5.3",
    ),
    "per_obec_price_stats": FeatureFloor(
        "property", "obec", ANY_CONFIDENCE, "`certain` only + stratified disclosure, §5.5.3",
    ),
    "watchdog_geo_rule": FeatureFloor(
        "listing", "obec", ANY_CONFIDENCE, "registry codes only, never portal ids",
    ),
    "chrome_extension_overlay": FeatureFloor(
        "listing", "building", "high", 'else shows "approximate location"',
    ),
}


def floor_for(feature: str) -> FeatureFloor:
    """The declared floor, or KeyError — never a permissive default (§5.5.2)."""
    try:
        return FEATURE_FLOORS[feature]
    except KeyError as exc:
        raise KeyError(f"no §5.5.2 floor declared for feature {feature!r}") from exc


def confidence_at_least(confidence: str, floor: str) -> bool:
    """`match_confidence` ordinal test; `floor == "any"` is always satisfied."""
    if floor == ANY_CONFIDENCE:
        return True
    return MATCH_CONFIDENCE_ORDER.index(confidence) >= MATCH_CONFIDENCE_ORDER.index(floor)


def meets_floor(
    feature: str,
    *,
    granularity: str,
    match_confidence: str,
    rank: GranularityRank | None = None,
) -> bool:
    """Does a projection row's `(granularity, match_confidence)` satisfy `feature`'s floor?

    `rank` is the loaded `location_granularity_rank` mapping when a caller has one; the
    offline default (`DEFAULT_GRANULARITY_RANK`) otherwise. `extra_gates` is NOT checked
    here — that is the wiring consumer's job, because each gate names a different column.
    """
    floor = floor_for(feature)
    ranks = rank or GranularityRank(DEFAULT_GRANULARITY_RANK)
    if not ranks.at_least(granularity, floor.min_granularity):
        return False
    return confidence_at_least(match_confidence, floor.min_confidence)
