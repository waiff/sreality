"""The `listing_location` row, and the column list that IS its contract.

27 columns, down from 81. What went and why is in migration 501's header; the short version
is that 54 of them were provably NULL, reachable through a join, derivable at read, or the
output of an engine this wave deletes. There is no property-grain twin any more:
`property_location_current` was a verbatim copy of its winner's row (migration 493 measured
`p.kraj_kod` and `w.kraj_kod` agreeing on 0 of 637,381 rows because the rollup IS the copy),
nothing outside one pg_cron statement read it, and it was the drain's only cross-listing
write — which is what kept the drain single-connection.

This is a CACHE, never truth: truncating it is always legal and the drain is its only
writer.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from location_data.resolver.types import Resolution

# The table, in DDL order. The gate that keeps the builder and migration 501 in step.
LISTING_LOCATION_COLUMNS: tuple[str, ...] = (
    "listing_id",
    "geom",
    "country_code", "kraj_name", "okres_name", "obec_name", "cast_obce_name",
    "street_name", "house_number_cp", "house_number_co", "psc",
    "kraj_kod", "okres_kod", "obec_kod", "cast_obce_kod", "ulice_kod", "ruian_adm_kod",
    "match_confidence", "granularity", "uncertainty_radius_m",
    "country_status", "disputed", "pin_shared_by_n",
    "resolver_version", "resolved_at", "claim_set_hash", "registry_version",
)

# `geom` is built in SQL from the pair; `resolved_at` is the statement's own `now()`.
GEOM_PARAMS: tuple[str, ...] = ("lat", "lon")
SERVER_DEFAULTED: tuple[str, ...] = ("resolved_at",)

ROW_PARAMS: tuple[str, ...] = tuple(
    name
    for name in LISTING_LOCATION_COLUMNS
    if name not in SERVER_DEFAULTED and name != "geom"
) + GEOM_PARAMS

# Fields the resolution carries for the log line and the queue, not for the table.
_NOT_COLUMNS = frozenset({"source"})


def build_listing_row(resolution: Resolution) -> dict[str, Any]:
    """-> the named parameters `resolve_db._UPSERT_LISTING_LOCATION_SQL` binds."""
    row = {k: v for k, v in asdict(resolution).items() if k not in _NOT_COLUMNS}
    missing = set(ROW_PARAMS) - set(row)
    if missing:  # pragma: no cover - a Resolution field renamed without the column list
        raise KeyError(f"the resolution cannot fill {sorted(missing)}")
    return {k: row[k] for k in ROW_PARAMS}
