"""The set-based SQL of path C candidate generation — one statement per (town block, id
range, rung), composed from shared fragments so the INSERT and the COUNT (estimate mode) can
never drift from each other, plus the funnel and audit aggregates a generation records.

Every statement is a module-level `*_SQL` string with named psycopg placeholders, which is
what tests/sql_corpus.py discovers and CI's schema-replay job PREPAREs against a freshly
migrated database — the SQL is compiled against the real schema on every push.

How the rule of toolkit/dedup_candidates.py (the oracle) maps onto SQL, once:

* `base` is the projection row joined to the listing's attributes — the ONE location read
  (docs/design/location-serving-contract.md §7): `obec_kod` as the block key, the granularity
  floor `dedup_path_c` applied by RANK through `location_granularity_rank` (never by enum
  order). Nothing legacy — no `listings.geom`, `obec_id`, `street`.
* "Not available": disposition = `nullif(btrim(disposition), '')`; area = the category's
  area column when > 0 (`estate_area` for pozemek, else `usable_area`), else NULL.
* Rung C1 joins on equal disposition (both present, by construction of the join); rung C3
  takes only pairs with a disposition missing on at least one side and compares areas.
* The category guards and the byt floor rule are the same predicates on both rungs.
* The pair is ordered `a.listing_id < b.listing_id`, so `a` is always the `lo` side and each
  real pair is produced exactly once per statement (ON CONFLICT DO UPDATE cannot tolerate a
  duplicate inside one statement).

THE AREA BAND. A town-wide "areas within N pct" join is a range join, which Postgres can only
do as a nested loop — Praha's ~30k disposition-less listings against its ~110k rows would be
3 × 10⁹ comparisons. Instead every row gets `band = floor(ln(area) / w)` with w = −ln(1 − t),
t the WIDEST tolerance as a fraction. Two areas within t of each other have ln-values at most
w apart, so their bands differ by at most 1: joining on `b.band = a.band + d` for d ∈ {−1, 0,
+1} is three EQUALITY hash joins, and the exact per-pair tolerance is re-checked in the WHERE.
Each ordered pair matches exactly one d, so no duplicates arise.
"""

from __future__ import annotations

import math
from typing import Any

# --------------------------------------------------------------------------- fragments

# Path C's floor (dedup_path_c: granularity ≥ obec, any confidence, obec_kod present),
# compared by rank. `active_only` narrows to listings active on BOTH sides (scope 'active').
_BASE_CTE = (
    "WITH base AS ("
    " SELECT x.id AS listing_id, x.category_type, x.category_main,"
    " NULLIF(BTRIM(x.disposition), '') AS disposition, x.floor,"
    " CASE WHEN x.category_main = 'pozemek'"
    "      THEN CASE WHEN x.estate_area > 0 THEN x.estate_area END"
    "      ELSE CASE WHEN x.usable_area > 0 THEN x.usable_area END END AS area"
    " FROM listing_location_current l"
    " JOIN location_granularity_rank gr ON gr.granularity = l.granularity"
    " JOIN listings x ON x.id = l.listing_id"
    " WHERE l.obec_kod = %(block_key)s::text"
    " AND gr.rank >= (SELECT r.rank FROM location_granularity_rank r WHERE r.granularity = 'obec')"
    " AND (NOT %(active_only)s::boolean OR x.is_active)"
    ")"
)

_BAND_CTE = (
    ", band AS ("
    " SELECT base.*, FLOOR(LN(area) / %(band_width)s::float8)::int AS band"
    " FROM base WHERE area IS NOT NULL"
    ")"
)

# The merge chokepoint's two guards (toolkit/property_identity.py): sale ≠ rent with NULL =
# unknown = not a conflict; category_main equal, NULL, or the sanctioned dům ↔ komerční.
_CATEGORY_GUARD = (
    " AND (a.category_type IS NULL OR b.category_type IS NULL OR a.category_type = b.category_type)"
    " AND (a.category_main IS NULL OR b.category_main IS NULL OR a.category_main = b.category_main"
    "      OR (a.category_main = 'dum' AND b.category_main = 'komercni')"
    "      OR (a.category_main = 'komercni' AND b.category_main = 'dum'))"
)

_FLOOR_CHECKED = (
    "(a.category_main = 'byt' AND b.category_main = 'byt'"
    " AND a.floor IS NOT NULL AND b.floor IS NOT NULL)"
)

_FLOOR_GUARD = (
    f" AND (NOT {_FLOOR_CHECKED} OR ABS(a.floor - b.floor) <= %(floor_tolerance)s::int)"
)

_ID_RANGE = " AND a.listing_id >= %(id_from)s::bigint AND a.listing_id < %(id_to)s::bigint"

_PAIR_COLUMNS = (
    "inputs_id, listing_id_lo, listing_id_hi, rung, generation_id, block_key,"
    " category_type, category_main_lo, category_main_hi, disposition,"
    " area_lo, area_hi, area_diff_pct, floor_lo, floor_hi, floor_checked"
)

_C1_SELECT = (
    " SELECT %(inputs_id)s::bigint, a.listing_id, b.listing_id, 'C1', %(generation_id)s::bigint,"
    " %(block_key)s::text, COALESCE(a.category_type, b.category_type),"
    " a.category_main, b.category_main, a.disposition,"
    " NULL::numeric, NULL::numeric, NULL::numeric, a.floor, b.floor, " + _FLOOR_CHECKED +
    " FROM base a"
    " JOIN base b ON b.disposition = a.disposition AND b.listing_id > a.listing_id"
    " WHERE a.disposition IS NOT NULL" + _ID_RANGE + _CATEGORY_GUARD + _FLOOR_GUARD
)

_AREA_DIFF_PCT = "ABS(a.area - b.area) / GREATEST(a.area, b.area) * 100"
_AREA_TOLERANCE = (
    "CASE WHEN 'pozemek' IN (a.category_main, b.category_main)"
    " THEN %(area_pct_pozemek)s::numeric ELSE %(area_pct_general)s::numeric END"
)

_C3_SELECT = (
    " SELECT %(inputs_id)s::bigint, a.listing_id, b.listing_id, 'C3', %(generation_id)s::bigint,"
    " %(block_key)s::text, COALESCE(a.category_type, b.category_type),"
    " a.category_main, b.category_main, NULL::text,"
    " a.area, b.area, " + _AREA_DIFF_PCT + ", a.floor, b.floor, " + _FLOOR_CHECKED +
    " FROM band a"
    " JOIN (VALUES (-1), (0), (1)) d(off) ON TRUE"
    " JOIN band b ON b.band = a.band + d.off AND b.listing_id > a.listing_id"
    " WHERE (a.disposition IS NULL OR b.disposition IS NULL)" + _ID_RANGE + _CATEGORY_GUARD +
    _FLOOR_GUARD +
    " AND " + _AREA_DIFF_PCT + " <= " + _AREA_TOLERANCE
)

_ON_CONFLICT = (
    " ON CONFLICT (inputs_id, listing_id_lo, listing_id_hi) DO UPDATE SET"
    " rung = excluded.rung, generation_id = excluded.generation_id, block_key = excluded.block_key,"
    " category_type = excluded.category_type, category_main_lo = excluded.category_main_lo,"
    " category_main_hi = excluded.category_main_hi, disposition = excluded.disposition,"
    " area_lo = excluded.area_lo, area_hi = excluded.area_hi, area_diff_pct = excluded.area_diff_pct,"
    " floor_lo = excluded.floor_lo, floor_hi = excluded.floor_hi, floor_checked = excluded.floor_checked,"
    " last_seen_at = now()"
)

# --------------------------------------------------------------------------- statements

C1_INSERT_SQL = (
    _BASE_CTE + " INSERT INTO dedup_sim.candidate_pairs (" + _PAIR_COLUMNS + ")" + _C1_SELECT + _ON_CONFLICT
)
C3_INSERT_SQL = (
    _BASE_CTE + _BAND_CTE + " INSERT INTO dedup_sim.candidate_pairs (" + _PAIR_COLUMNS + ")" + _C3_SELECT + _ON_CONFLICT
)
C1_COUNT_SQL = _BASE_CTE + " SELECT count(*) FROM (" + _C1_SELECT + ") t"
C3_COUNT_SQL = _BASE_CTE + _BAND_CTE + " SELECT count(*) FROM (" + _C3_SELECT + ") t"
# The rows themselves, no write — verify mode compares them with the Python oracle.
C1_ROWS_SQL = _BASE_CTE + _C1_SELECT
C3_ROWS_SQL = _BASE_CTE + _BAND_CTE + _C3_SELECT

RUNG_SQL: dict[str, dict[str, str]] = {
    "C1": {"insert": C1_INSERT_SQL, "count": C1_COUNT_SQL, "rows": C1_ROWS_SQL},
    "C3": {"insert": C3_INSERT_SQL, "count": C3_COUNT_SQL, "rows": C3_ROWS_SQL},
}

# Column order of every rung statement's SELECT list (= the INSERT column list).
PAIR_COLUMN_NAMES: tuple[str, ...] = tuple(c.strip() for c in _PAIR_COLUMNS.split(","))

# One block's listings with their RAW attributes, for the oracle (it applies the "not
# available" definitions itself — the point of verify mode is that SQL and Python agree).
BLOCK_ATTRS_SQL = (
    "SELECT x.id AS listing_id, x.category_type, x.category_main, x.disposition, x.floor,"
    " x.usable_area, x.estate_area"
    " FROM listing_location_current l"
    " JOIN location_granularity_rank gr ON gr.granularity = l.granularity"
    " JOIN listings x ON x.id = l.listing_id"
    " WHERE l.obec_kod = %(block_key)s::text"
    " AND gr.rank >= (SELECT r.rank FROM location_granularity_rank r WHERE r.granularity = 'obec')"
    " AND (NOT %(active_only)s::boolean OR x.is_active)"
    " ORDER BY x.id"
)

# The towns, in a fixed order, with how many listings path C sees in each.
BLOCKS_SQL = (
    "SELECT l.obec_kod, count(*) AS listings"
    " FROM listing_location_current l"
    " JOIN location_granularity_rank gr ON gr.granularity = l.granularity"
    " JOIN listings x ON x.id = l.listing_id"
    " WHERE l.obec_kod IS NOT NULL"
    " AND gr.rank >= (SELECT r.rank FROM location_granularity_rank r WHERE r.granularity = 'obec')"
    " AND (NOT %(active_only)s::boolean OR x.is_active)"
    " GROUP BY l.obec_kod ORDER BY l.obec_kod"
)

# The listing ids of one block, sorted — the lane cuts these into id ranges.
BLOCK_IDS_SQL = _BASE_CTE + " SELECT listing_id FROM base ORDER BY listing_id"

# After a COMPLETE run: rows of this parameter set that this generation did not re-produce
# (a listing's attributes or town changed since). Never run after a partial (block-limited) run.
STALE_SWEEP_SQL = (
    "DELETE FROM dedup_sim.candidate_pairs"
    " WHERE inputs_id = %(inputs_id)s::bigint AND generation_id <> %(generation_id)s::bigint"
)

# --------------------------------------------------------------------------- statistics

# The funnel from the listing side, per portal and property type: how many listings there
# are, how many the projection knows, how many have a town, and which attributes each rung
# would need. `town_no_attribute` is the data-quality hole path C cannot see past: a town but
# neither a disposition nor an area. Runs in estimate mode too — it needs no pair row.
FUNNEL_SQL = (
    "WITH r AS (SELECT rank FROM location_granularity_rank WHERE granularity = 'obec'),"
    " j AS ("
    " SELECT x.source, x.category_main, x.category_type, x.is_active,"
    " (l.listing_id IS NOT NULL) AS has_projection,"
    " (l.obec_kod IS NOT NULL AND gr.rank >= (SELECT rank FROM r)) AS has_town,"
    " (NULLIF(BTRIM(x.disposition), '') IS NOT NULL) AS has_disposition,"
    " (CASE WHEN x.category_main = 'pozemek' THEN x.estate_area ELSE x.usable_area END > 0) AS has_area,"
    " (x.floor IS NOT NULL) AS has_floor"
    " FROM listings x"
    " LEFT JOIN listing_location_current l ON l.listing_id = x.id"
    " LEFT JOIN location_granularity_rank gr ON gr.granularity = l.granularity"
    " WHERE (NOT %(active_only)s::boolean OR x.is_active))"
    " SELECT source, category_main, category_type,"
    " count(*) AS listings,"
    " count(*) FILTER (WHERE is_active) AS active,"
    " count(*) FILTER (WHERE has_projection) AS with_projection,"
    " count(*) FILTER (WHERE has_town) AS with_town,"
    " count(*) FILTER (WHERE has_disposition) AS with_disposition,"
    " count(*) FILTER (WHERE has_area) AS with_area,"
    " count(*) FILTER (WHERE category_main = 'byt') AS byt,"
    " count(*) FILTER (WHERE category_main = 'byt' AND has_floor) AS byt_with_floor,"
    " count(*) FILTER (WHERE has_town AND has_disposition) AS c1_eligible,"
    " count(*) FILTER (WHERE has_town AND NOT has_disposition AND COALESCE(has_area, false)) AS c3_eligible,"
    " count(*) FILTER (WHERE has_town AND NOT has_disposition AND NOT COALESCE(has_area, false)) AS town_no_attribute"
    " FROM j GROUP BY source, category_main, category_type"
    " ORDER BY source, category_main, category_type"
)

# How the town of each path-C-eligible listing was assigned (point-in-polygon, claimed,
# centroid …) — a breakdown for the audit, never a gate.
TOWN_ASSIGNMENT_SQL = (
    "SELECT l.admin_assignment_method::text AS method, count(*) AS listings"
    " FROM listing_location_current l"
    " JOIN location_granularity_rank gr ON gr.granularity = l.granularity"
    " WHERE l.obec_kod IS NOT NULL"
    " AND gr.rank >= (SELECT r.rank FROM location_granularity_rank r WHERE r.granularity = 'obec')"
    " GROUP BY 1 ORDER BY 2 DESC"
)

# The largest (town, disposition) buckets — the C1 "candidate storm" view, from the listing
# side (cheap; no pair rows needed). The pin/clique analogue for a path with no pins.
TOP_BUCKETS_SQL = (
    "SELECT l.obec_kod, l.obec_name, NULLIF(BTRIM(x.disposition), '') AS disposition,"
    " count(*) AS listings, count(*) FILTER (WHERE x.is_active) AS active"
    " FROM listing_location_current l"
    " JOIN location_granularity_rank gr ON gr.granularity = l.granularity"
    " JOIN listings x ON x.id = l.listing_id"
    " WHERE l.obec_kod IS NOT NULL"
    " AND gr.rank >= (SELECT r.rank FROM location_granularity_rank r WHERE r.granularity = 'obec')"
    " AND (NOT %(active_only)s::boolean OR x.is_active)"
    " GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT %(limit)s::int"
)

# --- over the pair table, one full pass each; only after a generation ---

PAIR_MATRIX_SQL = (
    "SELECT rung, category_main_lo, category_main_hi, category_type,"
    " count(*) AS pairs, count(*) FILTER (WHERE floor_checked) AS floor_checked"
    " FROM dedup_sim.candidate_pairs WHERE inputs_id = %(inputs_id)s::bigint"
    " GROUP BY 1, 2, 3, 4"
)

# GROUP BY, not DISTINCT: a hash aggregate over ≤ 10⁶ listing ids streams; a DISTINCT would
# sort the whole pair table on disk.
LISTINGS_WITH_CANDIDATES_SQL = (
    "SELECT category_main, count(*) AS listings FROM ("
    " SELECT id, category_main FROM ("
    "  SELECT listing_id_lo AS id, category_main_lo AS category_main"
    "  FROM dedup_sim.candidate_pairs WHERE inputs_id = %(inputs_id)s::bigint"
    "  UNION ALL"
    "  SELECT listing_id_hi, category_main_hi"
    "  FROM dedup_sim.candidate_pairs WHERE inputs_id = %(inputs_id)s::bigint"
    " ) u GROUP BY id, category_main"
    ") g GROUP BY category_main"
)

PAIRS_PER_BLOCK_SQL = (
    "SELECT block_key, rung, count(*) AS pairs"
    " FROM dedup_sim.candidate_pairs WHERE inputs_id = %(inputs_id)s::bigint"
    " GROUP BY 1, 2"
)

BLOCK_NAMES_SQL = (
    "SELECT obec_kod, min(obec_name) AS obec_name FROM listing_location_current"
    " WHERE obec_kod = ANY(%(keys)s::text[]) GROUP BY obec_kod"
)


# --------------------------------------------------------------------------- parameters


def band_width(area_pct_general: float, area_pct_pozemek: float) -> float:
    """w = −ln(1 − t) for the WIDEST tolerance t (as a fraction): two areas within t have
    ln-values at most w apart, so their bands differ by at most one. A tolerance of 100 pct
    or more would make every pair a match; then one band holds everything."""
    t = max(float(area_pct_general), float(area_pct_pozemek)) / 100.0
    if t >= 1.0:
        return 1e9
    if t <= 0.0:
        return 1e-9
    return -math.log(1.0 - t)


def rung_params(inputs: dict[str, Any]) -> dict[str, Any]:
    """The placeholder values every rung statement needs from one parameter set."""
    return {
        "floor_tolerance": int(inputs["l0_floor_tolerance"]),
        "area_pct_general": float(inputs["l0_area_tolerance_pct_general"]),
        "area_pct_pozemek": float(inputs["l0_area_tolerance_pct_pozemek"]),
        "band_width": band_width(
            inputs["l0_area_tolerance_pct_general"], inputs["l0_area_tolerance_pct_pozemek"]
        ),
        "active_only": inputs.get("l0_candidate_scope", "all") == "active",
    }
