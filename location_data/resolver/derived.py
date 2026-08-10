"""The builder-side twins of migration 384's derived-value functions and the two honesty
predicates (01 §7.1.1, 00 §7.3/§7.4).

Neither projection has a generated column: `ST_Transform`, `unaccent()` and `to_char()` are
all non-IMMUTABLE, and Postgres does not recompute a stored generated column when an enum
gains a value. So the BUILDER writes every derived value, and the SQL functions are the
single definition it must agree with — which is why `tests/location_data/
test_projection_parity.py` re-implements each SQL body straight from the migration text and
runs a battery of rows through both.

Rendering notes that are the parity contract, not implementation detail:

* `round(numeric, 4)::text` in Postgres pads to exactly four decimals (`50.1` renders
  `50.1000`) and rounds half AWAY FROM ZERO. `Decimal.quantize(ROUND_HALF_UP)` is the same
  rule; `float8::numeric` takes the shortest round-trip representation, which is `repr()`.
  The `to_char()` spelling is forbidden upstream because its output depends on
  `LC_NUMERIC` — this module must never grow one either.
* `lower(unaccent(street))` folds diacritics but keeps spaces and punctuation. It is NOT
  `normalize_match_key`, which additionally folds punctuation to single spaces: the block
  key and the gazetteer key are different keys and must not be quietly unified.

THE CANONICAL PREDICATES. `pin_collision_class` is carried verbatim from
`pin_clusters.classification` and is **never NULL** — an unclustered listing is `'normal'`.
The retired form that tested that column for NULL was never-true (the vocabulary has no
NULL member) and additionally excluded the two classes that ARE fine; 01 §A.2 check 8
forbids that spelling anywhere in the tree and `test_projection_predicates.py` scans this
package for it.
"""

from __future__ import annotations

import unicodedata
from decimal import Decimal, ROUND_HALF_UP

from location_data.resolver.types import GranularityRank

PIN_COLLISION_OK_CLASSES = frozenset({"normal", "building_1_to_many"})
GEO_BLOCKABLE_EXCLUDED_SOURCES = frozenset(
    {"admin_centroid", "portal_pin_blurred", "carried_forward", "none"}
)
RENDERABLE_SOURCES = frozenset({"registry_point", "portal_pin"})
GEO_BLOCKABLE_FLOOR = "street_segment"
RENDERABLE_FLOOR = "building"
LOW_PRECISION_FLOOR = "street"

_QUANT_4 = Decimal("0.0001")


def _numeric_4(value: float) -> str:
    """`round(x::numeric, 4)::text` — four decimals, padded, half away from zero."""
    rounded = Decimal(repr(float(value))).quantize(_QUANT_4, rounding=ROUND_HALF_UP)
    if rounded == 0:  # Postgres numeric has no signed zero; Decimal does.
        rounded = Decimal("0").quantize(_QUANT_4)
    return str(rounded)


def geo_cell_key(lat: float | None, lon: float | None) -> str | None:
    """`location_geo_cell_key(geom)`. The h3-pg fallback: a rounded 4-dp cell, so cell
    equality MUST be expanded to the 3×3 neighbourhood at query time."""
    if lat is None or lon is None:
        return None
    return f"c:{_numeric_4(lat)}:{_numeric_4(lon)}"


def street_block_key(obec_kod: int | None, street: str | None, hn: str | None) -> str | None:
    """`location_street_block_key(obec_kod, street, hn)`."""
    if obec_kod is None or street is None:
        return None
    return f"{obec_kod}:{_unaccent(street).lower()}:{hn or ''}"


def addr_block_key(ruian_adm_kod: int | None) -> str | None:
    return None if ruian_adm_kod is None else f"a:{ruian_adm_kod}"


def building_block_key(stavebni_objekt_kod: int | None) -> str | None:
    return None if stavebni_objekt_kod is None else f"b:{stavebni_objekt_kod}"


def _unaccent(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )


def pin_collision_ok(
    *,
    pin_collision_class: str,
    cluster_heterogeneity_ok: bool,
    pin_shared_by_n: int,
    threshold_n: int,
) -> bool:
    """00 §7.3, canonical:

        pin_collision_class IN ('normal','building_1_to_many')
        AND cluster_heterogeneity_ok
        AND pin_shared_by_n <= threshold_from(location_collision_policy, source, obec_kod)
    """
    return (
        pin_collision_class in PIN_COLLISION_OK_CLASSES
        and cluster_heterogeneity_ok
        and pin_shared_by_n <= threshold_n
    )


def geo_blockable(
    *,
    granularity: str,
    position_source: str,
    collision_ok: bool,
    rank: GranularityRank,
) -> bool:
    """`rank(granularity) >= rank('street_segment')` — the RANK, never the enum ordinality —
    AND `position_source NOT IN (admin_centroid, portal_pin_blurred, carried_forward, none)`
    AND `pin_collision_ok`."""
    return (
        rank.at_least(granularity, GEO_BLOCKABLE_FLOOR)
        and position_source not in GEO_BLOCKABLE_EXCLUDED_SOURCES
        and collision_ok
    )


def renderable_as_point(
    *,
    granularity: str,
    position_source: str,
    collision_ok: bool,
    location_disputed: bool,
    rank: GranularityRank,
) -> bool:
    return (
        rank.at_least(granularity, RENDERABLE_FLOOR)
        and position_source in RENDERABLE_SOURCES
        and collision_ok
        and not location_disputed
    )


def is_low_precision(*, granularity: str, rank: GranularityRank) -> bool:
    """`rank(granularity) < rank('street')` — a DIFFERENT question from renderability (a
    coarse-but-honest row is low precision and still renderable as an area) and never a
    render gate."""
    return not rank.at_least(granularity, LOW_PRECISION_FLOOR)


def render_as(
    *,
    renderable: bool,
    granularity: str,
    position_source: str,
    has_geom: bool,
    rank: GranularityRank,
) -> str:
    """The 3-valued API rendering of the SAME decision. `llc_render` CHECKs
    `renderable_as_point = (render_as = 'point')`, so this must never disagree."""
    if renderable:
        return "point"
    if not has_geom:
        return "area"
    if position_source == "admin_centroid" or not rank.at_least(granularity, "street"):
        return "area"
    return "circle"
