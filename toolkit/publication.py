"""Dedup-eligibility SQL predicates — the single source for every surface that asks
"can the dedup engine reach this listing?": the publication gate's ineligible-publish
sweep (migration 273), the engine's own geo loader, and the real-time dedup-dirty
enqueue gate (scraper.db).

A property is hidden until the dedup engine has evaluated it (properties.published_at).
The engine only ever scans listings that are eligible for one of its two passes, so a
listing eligible for NEITHER can never be dedup-checked and must not be hidden forever.
`scripts.recompute_property_stats._publish_sweep` publishes (reason 'ineligible') any
unpublished active property whose repr listing matches neither predicate below.

The alias-`l` constants MIRROR the engine's eligibility (`scripts.dedup_engine
._ELIGIBILITY` / `_GEO_ELIGIBLE_SQL`) VERBATIM — a parity unit test asserts each is a
substring of the engine SQL, so the two can never drift. `eligible_predicate` renders
the street-OR-geo union for ANY alias, so a consumer under a different table alias
(the dedup-dirty enqueue gate's subquery alias `le`) embeds the same text instead of
hand-copying it.
"""

from __future__ import annotations

# scripts.dedup_engine._ELIGIBILITY (street + disposition pass eligibility).
_STREET_TEMPLATE = (
    "{a}.street IS NOT NULL AND {a}.street <> '' AND {a}.disposition IS NOT NULL"
)

# The eligibility conjunction inside scripts.dedup_engine._GEO_ELIGIBLE_SQL (the
# geo-proximity pass for single-dwelling house/land/commercial). Rendered with the
# engine's exact whitespace so the parity test's substring check holds; the newlines
# are harmless wherever it is embedded.
_GEO_TEMPLATE = (
    "{a}.is_active = true\n"
    "      AND {a}.category_main IN ('dum', 'pozemek', 'komercni', 'ostatni')\n"
    "      AND {a}.geom IS NOT NULL\n"
    "      AND {a}.obec_id IS NOT NULL\n"
    "      AND coalesce({a}.area_m2, {a}.estate_area, {a}.usable_area) IS NOT NULL"
)

STREET_ELIGIBLE_PREDICATE: str = _STREET_TEMPLATE.format(a="l")
GEO_ELIGIBLE_PREDICATE: str = _GEO_TEMPLATE.format(a="l")


def eligible_predicate(alias: str) -> str:
    """Street-OR-geo dedup eligibility rendered for `alias` — a listing the engine can
    reach through EITHER pass. The dedup-dirty enqueue gate's property-grain EXISTS
    embeds this (alias 'le'), so geo-family properties ride the real-time lane too."""
    return (
        f"({_STREET_TEMPLATE.format(a=alias)}) OR "
        f"({_GEO_TEMPLATE.format(a=alias)})"
    )
