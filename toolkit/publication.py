"""Dedup-eligibility SQL predicates — the single source for every surface that asks
"can the dedup engine reach this listing?": the publication gate's ineligible-publish
sweep (migration 273), the engine's own cell loaders, and the real-time dedup-dirty
enqueue gate (scraper.db).

A property is hidden until the dedup engine has evaluated it (properties.published_at).
The engine only ever scans listings that are eligible for one of its THREE passes
(street, geo, byt-geo), so a listing eligible for NONE can never be dedup-checked and
must not be hidden forever. `scripts.recompute_property_stats._publish_sweep` publishes
(reason 'ineligible') any unpublished active property whose repr listing matches none of
the predicates below.

The alias-`l` constants MIRROR the engine's eligibility (`scripts.dedup_engine
._ELIGIBILITY` / `_GEO_ELIGIBLE_SQL` / `_BYT_GEO_ELIGIBLE_SQL`) VERBATIM — a parity unit
test asserts each is a substring of the engine SQL, so the two can never drift.
`eligible_predicate` renders the three-arm union for ANY alias, so a consumer under a
different table alias (the dedup-dirty enqueue gate's subquery alias `le`) embeds the
same text instead of hand-copying it.
"""

from __future__ import annotations

# scripts.dedup_engine._ELIGIBILITY (street + disposition pass eligibility).
_STREET_TEMPLATE = (
    "{a}.street IS NOT NULL AND {a}.street <> '' AND {a}.disposition IS NOT NULL"
)

# The geo-pass category families — the single PYTHON source for "which categories the
# geo pass covers". MatchProfile derives geo_blocked from membership here; the SQL twin
# lives in migration 276's listing_geo_cell_key() (SQL can't import Python — a unit test
# pins the two lists to each other). byt is deliberately NOT here: it keeps the street
# pass, and its cell rung below is candidate-generation only.
GEO_FAMILIES: tuple[str, ...] = ("dum", "pozemek", "komercni", "ostatni")

# The CELL-STAMPED families — everything the stored listings.geo_cell_key is defined
# for: the geo families PLUS byt (its own 'byt' bucket, the byt geo rung — migration
# 290 redefined listing_geo_cell_key() with exactly this list; a unit test pins the
# migration body to this tuple, the same twin-pinning 276 has against GEO_FAMILIES).
CELL_FAMILIES: tuple[str, ...] = GEO_FAMILIES + ("byt",)

_GEO_IN_LIST = ", ".join(f"'{f}'" for f in GEO_FAMILIES)

# The eligibility conjunction inside scripts.dedup_engine._GEO_ELIGIBLE_SQL (the
# geo-proximity pass for single-dwelling house/land/commercial). Rendered with the
# engine's exact whitespace so the parity test's substring check holds; the newlines
# are harmless wherever it is embedded.
_GEO_TEMPLATE = (
    "{a}.is_active = true\n"
    "      AND {a}.category_main IN (" + _GEO_IN_LIST + ")\n"
    "      AND {a}.geom IS NOT NULL\n"
    "      AND {a}.obec_id IS NOT NULL\n"
    "      AND coalesce({a}.area_m2, {a}.estate_area, {a}.usable_area) IS NOT NULL"
)

# The byt geo rung's eligibility (scripts.dedup_engine._BYT_GEO_ELIGIBLE_SQL): a
# street-less byt still needs a coordinate + municipality + area AND a disposition —
# the cell is sharded by disposition class at load, so a disposition-less byt has no
# shard to join (89% of the street-invisible byt carry one; the rest stay with the
# ineligible publish sweep). Same whitespace discipline as the geo template.
_BYT_GEO_TEMPLATE = (
    "{a}.is_active = true\n"
    "      AND {a}.category_main = 'byt'\n"
    "      AND {a}.geom IS NOT NULL\n"
    "      AND {a}.obec_id IS NOT NULL\n"
    "      AND coalesce({a}.area_m2, {a}.estate_area, {a}.usable_area) IS NOT NULL\n"
    "      AND {a}.disposition IS NOT NULL"
)

STREET_ELIGIBLE_PREDICATE: str = _STREET_TEMPLATE.format(a="l")
GEO_ELIGIBLE_PREDICATE: str = _GEO_TEMPLATE.format(a="l")
BYT_GEO_ELIGIBLE_PREDICATE: str = _BYT_GEO_TEMPLATE.format(a="l")


def eligible_predicate(alias: str) -> str:
    """Street-OR-geo-OR-byt-geo dedup eligibility rendered for `alias` — a listing the
    engine can reach through ANY of its three passes. The dedup-dirty enqueue gate's
    property-grain EXISTS embeds this (alias 'le'), so geo-family AND street-less-byt
    properties ride the real-time lane too."""
    return (
        f"({_STREET_TEMPLATE.format(a=alias)}) OR "
        f"({_GEO_TEMPLATE.format(a=alias)}) OR "
        f"({_BYT_GEO_TEMPLATE.format(a=alias)})"
    )
