"""Dedup-eligibility SQL predicates — the single source for the publication gate's
ineligible-publish sweep (migration 273).

A property is hidden until the dedup engine has evaluated it (properties.published_at).
The engine only ever scans listings that are eligible for one of its two passes, so a
listing eligible for NEITHER can never be dedup-checked and must not be hidden forever.
`scripts.recompute_property_stats._publish_sweep` publishes (reason 'ineligible') any
unpublished active property whose repr listing matches neither predicate below.

These MIRROR the engine's own eligibility (`scripts.dedup_engine._ELIGIBILITY` /
`_GEO_ELIGIBLE_SQL`) VERBATIM — a parity unit test asserts each constant is a substring
of the engine SQL, so the two can never drift.
"""

from __future__ import annotations

# scripts.dedup_engine._ELIGIBILITY (street + disposition pass eligibility).
STREET_ELIGIBLE_PREDICATE: str = (
    "l.street IS NOT NULL AND l.street <> '' AND l.disposition IS NOT NULL"
)

# The eligibility conjunction inside scripts.dedup_engine._GEO_ELIGIBLE_SQL (the
# geo-proximity pass for single-dwelling house/land/commercial). Copied with the
# engine's exact whitespace so the parity test's substring check holds; the newlines
# are harmless where it is embedded as `AND NOT (...)` in the sweep SQL.
GEO_ELIGIBLE_PREDICATE: str = (
    "l.is_active = true\n"
    "      AND l.category_main IN ('dum', 'pozemek', 'komercni', 'ostatni')\n"
    "      AND l.geom IS NOT NULL\n"
    "      AND l.obec_id IS NOT NULL\n"
    "      AND coalesce(l.area_m2, l.estate_area, l.usable_area) IS NOT NULL"
)
