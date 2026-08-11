"""The psycopg side of the resolver: load the registry/policy inputs, call the pure core,
write the resolution + candidates + projection rows.

Connection mode is the drain's `scraper.db.connect_session()` — the SESSION-mode pooler, a
dedicated backend, psycopg3's default `prepare_threshold` — so the ~40 statements below are
server-side PREPARED once and reused for every listing in the run instead of being re-parsed
and re-planned on each of tens of thousands of listings. It stays autocommit ("callers manage
transactions explicitly"), so every atomic unit is still an explicit `with conn.transaction():`
block, and there is still not a session advisory lock anywhere near this file — the lease is a
row-CAS, which is correct on either pooler mode. Nothing here assumes a session: on a
`SUPABASE_DB_SESSION_URL`-less environment `connect_session()` falls back to the transaction
pooler and every statement below still runs, just unprepared.

The registry view here answers exactly the questions `types.RegistryView` declares, so the
pure core cannot reach past it into SQL. `purpose IN ('pip','authoritative')` is deliberate:
04 C4.3 wants the `ST_Subdivide`d `pip` geometries for containment (migration 381 admits the
purpose and indexes it), and preferring `pip` when rows exist degrades to the authoritative
polygon when the boundary loader has not yet populated them.

**Round trips are the drain's cost model**, not CPU — and round 2 measured the constant.
Production run 31480587021 (after round 1) resolved 2,750 listings in 3,366 s with
`core=1385.7s registry_q=17763 registry_wait=1373.5s`: 77 ms per registry MISS, while the
same run's un-instrumented per-listing statements (SAVEPOINT, RELEASE SAVEPOINT, one DELETE
by primary key — statements with no server-side work at all) account for 225 ms per listing,
i.e. **~75 ms of pure network round trip** from a GitHub-hosted runner to the Frankfurt
pooler. Re-measured server-side on the live mirror, the same registry questions cost
0.02-0.5 ms each. So the wall clock is ~99 % latency and ~1 % query: the lever is the NUMBER
of round trips, never the plan — with two measured exceptions, fixed below.

Levers, all strictly I/O-layer (the pure core's protocol and its answers are untouched, so
deterministic replay stays bit-for-bit):

* `RunCache` + `CachedRegistryView` / `CachedCollisionEvidence` — the mirror is IMMUTABLE at a
  pinned `registry_version_id` and the clusters are immutable at a pinned epoch, so a repeated
  question inside one run has one answer. The resolver asks the same handful over and over
  (`streets_in_obec(Praha)` twice per Prague listing plus once from the reconciler,
  `admin_units_by_name('praha')` three times), and across listings the reuse is corpus-scale.
* `warm_points` — the memo PRE-FILLED for a whole slice. The ~62 % hit rate that survives the
  cache is not a granularity bug: the misses are the five questions keyed on the listing's own
  coordinate, which is unique per listing by construction. Those cannot be shared BETWEEN
  listings, but they can all be asked in ONE round trip FOR the whole slice, which is what the
  `*_BULK` shapes below do. Warming writes the same key the `Cached*` view would compute, so
  the core still sees one answer per question.
* the `*_bulk` loaders — one query per SLICE instead of one per listing, grouped in Python.
* `executemany` for every per-listing writer, batched ACROSS the slice by `drain._write_slice`,
  which psycopg pipelines into a single round trip.

The two genuine plan offenders (measured on the live mirror, EXPLAIN ANALYZE):

* `cast_obce_for_point` — a KNN `<->` scan whose only bound was a `::geography` ST_DWithin
  FILTER, so a point with no address point inside 250 m walked the whole 3 M-row index:
  2,944 ms/point averaged over a sample, 23 ms even on a dense urban hit (the plan also
  seq-scanned + materialised all 18,627 `ruian_admin_units` rows per call). With a `&&`
  bbox that the GiST index takes as an Index Cond, plus the nearest-point subquery resolved
  BEFORE the unit join (safe: `cast_obce_unit_id` is a FOREIGN KEY, so the first row always
  joins): **0.15 ms/point, ~20,000x**.
* `nearest_obec_within` — `ST_DWithin(g.geom::geography, …)` cannot use the geometry GiST
  index, so every call cast all 127 k boundary rows (state and kraj polygons included) to
  geography: **6,752 ms/point**. With the `&&` bbox and the subdivided `pip` pieces preferred
  the same way `containing_obec` prefers them: **2.95 ms/point, ~2,300x**. It is reached only
  when containment misses (~1 % of listings), but at 6.7 s it was also a live timeout hazard
  against the batch's 30 s budget.

Neither needed an index — both are query shapes over indexes that already exist (migration
389's included). No migration 390.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

import psycopg

from location_data.resolver.types import (
    AdminUnit,
    AddressPoint,
    Claim,
    ClusterEvidence,
    CollisionPolicyRow,
    FieldPolicyRow,
    GranularityRank,
    LocationConstants,
    Parcel,
    Resolution,
    Street,
    UncertaintyPolicyRow,
)

_CONSTANTS_SQL = """
SELECT name,
       value_num,
       CASE WHEN value_geom IS NULL THEN NULL ELSE ST_XMin(value_geom) END,
       CASE WHEN value_geom IS NULL THEN NULL ELSE ST_YMin(value_geom) END,
       CASE WHEN value_geom IS NULL THEN NULL ELSE ST_XMax(value_geom) END,
       CASE WHEN value_geom IS NULL THEN NULL ELSE ST_YMax(value_geom) END
  FROM location_constants
"""

_GRANULARITY_RANK_SQL = "SELECT granularity::text, rank FROM location_granularity_rank"

_FIELD_POLICY_SQL = """
SELECT policy_version, field::text, source_pattern, method_pattern, rank,
       min_granularity::text, min_confidence::text, max_age_days,
       may_fill_null, may_overwrite_non_null, requires_independent_agreement, tie_breaker
  FROM location_field_policy
 WHERE policy_version = %s
 ORDER BY rank, field, source_pattern, method_pattern
"""

_UNCERTAINTY_POLICY_SQL = """
SELECT policy_version, position_source::text, granularity::text, source, r95_m,
       radius_semantics::text, derivation
  FROM location_uncertainty_policy
 WHERE policy_version = %s
 ORDER BY position_source, granularity, source
"""

_COLLISION_POLICY_SQL = """
SELECT policy_version, source, obec_kod, threshold_n, radius_m, min_distinct_streets,
       pin_collision_semantics
  FROM location_collision_policy
 WHERE policy_version = %s
 ORDER BY source, obec_key
"""

_CURRENT_REGISTRY_SQL = "SELECT id, label FROM registry_versions WHERE is_current LIMIT 1"

_CURRENT_EPOCH_SQL = "SELECT id FROM pin_cluster_epochs ORDER BY computed_at DESC, id DESC LIMIT 1"

# ONE projection, two predicates — the row unpacking in `_claim` is positional, so a second
# hand-written column list is a silent mis-mapping waiting to happen.
_CLAIMS_SELECT = """
SELECT id, listing_id, source, claim_type::text, surface::text, extraction_method::text,
       extractor_id, licence_class::text, first_observed_at, value_text, value_num,
       CASE WHEN value_geom IS NULL THEN NULL ELSE ST_Y(value_geom) END,
       CASE WHEN value_geom IS NULL THEN NULL ELSE ST_X(value_geom) END,
       value_jsonb, declared_precision_label, declared_confidence, declared_radius_m,
       blur_evidence::text, claim_confidence::text, subject_scoped, page_kind::text,
       snapshot_id, distance_m, target_text
  FROM location_claims_live
"""

_CLAIMS_SQL = _CLAIMS_SELECT + " WHERE listing_id = %s\n ORDER BY id"

# The whole SLICE in one query instead of one per listing; the per-listing order is still
# `id`, which is what `core.resolve` re-sorts on anyway.
_CLAIMS_BULK_SQL = (
    _CLAIMS_SELECT + " WHERE listing_id = ANY(%s::bigint[])\n ORDER BY listing_id, id"
)

_ADDRESS_POINT_SQL = """
SELECT ap.kod_adm, ap.obec_unit_id, ap.obec_kod, ap.psc,
       ST_Y(ap.geom), ST_X(ap.geom), ap.street_id, ap.ulice_kod, s.name_norm,
       ap.cislo_domovni, ap.cislo_orientacni, ap.znak_orientacniho,
       ap.stavebni_objekt_code, ap.cast_obce_unit_id, ap.cast_obce_kod, ap.momc_unit_id
  FROM ruian_address_points ap
  LEFT JOIN ruian_streets s ON s.id = ap.street_id
 WHERE ap.kod_adm = %s AND ap.valid_to IS NULL
"""

# The obec is addressed by UNIT ID, not by `obec_kod`. Both columns exist on every row and
# are 1:1 (6,256 distinct each, no NULLs, no retired rows), but only `obec_unit_id` is
# indexed: `ruian_ap_obec_hn (obec_unit_id, cislo_domovni)` is exactly this lookup's index
# and `obec_kod` has none at all. Measured on the live 3,020,222-row mirror, house number
# only, no street: `obec_kod` planned a parallel scan of the whole PK — 21,494 ms,
# 2,954,453 buffers, 1.5 M rows discarded per worker. The form below touches 424 buffers
# (6,969x fewer) for the same answer; wall clock was 25-185 ms across runs, all of it page
# reads, because buffer count is the honest number on an instance under IO pressure.
# `u.level = %s::ruian_level` casts the PARAMETER (not the column), so
# `ruian_admin_units_code (level, code)` keeps both of its columns — `u.level::text = %s`
# would cast the column and throw the leading one away.
#
# `IN (...)` over EVERY unit row carrying that code, not `= (SELECT ... valid_to IS NULL
# LIMIT 1)`: `ruian_admin_units` is SCD-2, so a code can have a closed row and an open one,
# and an address point still points at the row that was current when it was loaded.
# Restricting the hop to the open row would silently return nothing for exactly those rows
# — a narrower answer than `obec_kod = %s` gave. The IN form is the same index scan
# (verified: `Index Cond: (level = 'obec' AND code = ...)` then
# `(obec_unit_id = u.id AND cislo_domovni = ...)`).
_OBEC_UNIT_ID_SUBQUERY = """
    (SELECT u.id FROM ruian_admin_units u
      WHERE u.level = 'obec'::ruian_level AND u.code = %s)
"""

_ADDRESS_POINTS_BY_NUMBER_SQL = f"""
SELECT ap.kod_adm, ap.obec_unit_id, ap.obec_kod, ap.psc,
       ST_Y(ap.geom), ST_X(ap.geom), ap.street_id, ap.ulice_kod, s.name_norm,
       ap.cislo_domovni, ap.cislo_orientacni, ap.znak_orientacniho,
       ap.stavebni_objekt_code, ap.cast_obce_unit_id, ap.cast_obce_kod, ap.momc_unit_id
  FROM ruian_address_points ap
  LEFT JOIN ruian_streets s ON s.id = ap.street_id
 WHERE ap.obec_unit_id IN {_OBEC_UNIT_ID_SUBQUERY.strip()}
   AND ap.valid_to IS NULL
   AND (%s::text IS NULL OR s.name_norm = %s::text)
   AND (%s::integer IS NULL OR ap.cislo_domovni = %s::integer)
   AND (%s::integer IS NULL OR ap.cislo_orientacni = %s::integer)
 ORDER BY ap.kod_adm
 LIMIT 50
"""

_STREETS_IN_OBEC_SQL = """
SELECT s.id, s.code, s.name, s.name_norm, s.obec_unit_id, u.code
  FROM ruian_streets s
  JOIN ruian_admin_units u ON u.id = s.obec_unit_id
 WHERE u.code = %s AND u.level = 'obec' AND s.valid_to IS NULL
 ORDER BY s.code
"""

_ADMIN_BY_NAME_SQL = """
SELECT u.id, u.level::text, u.code, u.name, u.name_norm, u.path::text, u.display_path,
       u.parent_id,
       CASE WHEN u.definition_point IS NULL THEN NULL ELSE ST_Y(u.definition_point) END,
       CASE WHEN u.definition_point IS NULL THEN NULL ELSE ST_X(u.definition_point) END,
       n.qualifier, n.homonym_count, n.psc_set, g.containment_radius_m
  FROM ruian_name_index n
  JOIN ruian_admin_units u ON u.id = n.entity_id AND u.level = n.entity_kind
  LEFT JOIN ruian_admin_unit_geometries g
         ON g.unit_id = u.id AND g.registry_version_id = %s AND g.purpose = 'authoritative'
 WHERE n.registry_version_id = %s
   AND n.name_norm = %s
   AND (cardinality(%s::text[]) = 0 OR u.level::text = ANY(%s::text[]))
   AND u.valid_to IS NULL
 ORDER BY u.level, u.code
"""

_ADMIN_BY_CODE_SQL = """
SELECT u.id, u.level::text, u.code, u.name, u.name_norm, u.path::text, u.display_path,
       u.parent_id,
       CASE WHEN u.definition_point IS NULL THEN NULL ELSE ST_Y(u.definition_point) END,
       CASE WHEN u.definition_point IS NULL THEN NULL ELSE ST_X(u.definition_point) END,
       NULL::text, 1, NULL::char(5)[], g.containment_radius_m
  FROM ruian_admin_units u
  LEFT JOIN ruian_admin_unit_geometries g
         ON g.unit_id = u.id AND g.registry_version_id = %s AND g.purpose = 'authoritative'
 WHERE u.level = %s::ruian_level AND u.code = %s AND u.valid_to IS NULL
 ORDER BY u.valid_from DESC
 LIMIT 1
"""

_ADMIN_BY_ID_SQL = """
SELECT u.id, u.level::text, u.code, u.name, u.name_norm, u.path::text, u.display_path,
       u.parent_id,
       CASE WHEN u.definition_point IS NULL THEN NULL ELSE ST_Y(u.definition_point) END,
       CASE WHEN u.definition_point IS NULL THEN NULL ELSE ST_X(u.definition_point) END,
       NULL::text, 1, NULL::char(5)[], g.containment_radius_m
  FROM ruian_admin_units u
  LEFT JOIN ruian_admin_unit_geometries g
         ON g.unit_id = u.id AND g.registry_version_id = %s AND g.purpose = 'authoritative'
 WHERE u.id = %s
"""

_ADMIN_CHAIN_SQL = """
WITH RECURSIVE chain AS (
  SELECT u.id, u.parent_id FROM ruian_admin_units u WHERE u.id = %s
  UNION ALL
  SELECT p.id, p.parent_id FROM ruian_admin_units p JOIN chain c ON p.id = c.parent_id
)
SELECT u.id, u.level::text, u.code, u.name, u.name_norm, u.path::text, u.display_path,
       u.parent_id,
       CASE WHEN u.definition_point IS NULL THEN NULL ELSE ST_Y(u.definition_point) END,
       CASE WHEN u.definition_point IS NULL THEN NULL ELSE ST_X(u.definition_point) END,
       NULL::text, 1, NULL::char(5)[], g.containment_radius_m
  FROM chain c
  JOIN ruian_admin_units u ON u.id = c.id
  LEFT JOIN ruian_admin_unit_geometries g
         ON g.unit_id = u.id AND g.registry_version_id = %s AND g.purpose = 'authoritative'
 WHERE u.id <> %s
 ORDER BY u.id
"""

_PSC_OBEC_SQL = """
SELECT DISTINCT obec_kod FROM ruian_address_points
 WHERE psc = %s AND valid_to IS NULL ORDER BY obec_kod
"""

_PARCELS_SQL = """
SELECT p.id, p.code, p.katuz_unit_id, p.parcel_label_norm,
       CASE WHEN p.definition_point IS NULL THEN NULL ELSE ST_Y(p.definition_point) END,
       CASE WHEN p.definition_point IS NULL THEN NULL ELSE ST_X(p.definition_point) END
  FROM ruian_parcels p
  JOIN ruian_admin_units k ON k.id = p.katuz_unit_id
 WHERE k.name_norm = %s AND p.parcel_label_norm = %s AND p.valid_to IS NULL
 ORDER BY p.code
 LIMIT 20
"""

# ------------------------------------------------------- the five point-keyed questions
#
# Every question below is keyed on the LISTING'S OWN coordinate, so the run cache can never
# share an answer between two listings — and at ~75 ms of round trip each they were 5 of the
# drain's ~16 trips per listing. Each therefore has exactly ONE shape, taking parallel
# `(idx[], lat[], lon[])` arrays: `warm_points` calls it with the whole slice's points (one
# trip for 250 listings) and `SqlRegistryView` calls the SAME text with a one-element array
# when a point was not warmed. One shape, so the lazy path can never drift from the warmed
# one — which is the property replay depends on.
#
# `%(pt)s` expands to the point built from the lateral row, so a branch reads like the
# single-point form it replaced.
_PT = "ST_SetSRID(ST_MakePoint(p.lon, p.lat), 4326)"

# The unit column list `_admin_unit` unpacks positionally, as a lateral's output.
_ADMIN_COLUMNS = """
       u.id, u.level::text AS lvl, u.code, u.name, u.name_norm, u.path::text AS pth,
       u.display_path, u.parent_id,
       CASE WHEN u.definition_point IS NULL THEN NULL ELSE ST_Y(u.definition_point) END AS dlat,
       CASE WHEN u.definition_point IS NULL THEN NULL ELSE ST_X(u.definition_point) END AS dlon,
       NULL::text AS qualifier, 1 AS homonym_count, NULL::char(5)[] AS psc_set"""

_POINTS = "unnest(%s::int[], %s::double precision[], %s::double precision[]) AS p(idx, lat, lon)"

# `purpose IN ('pip','authoritative')` cannot use `ruian_aug_pip_gist (geom) WHERE purpose
# = 'pip'` — an IN-list does not imply the partial index's predicate — so the planner fell
# back to the unpartitioned `ruian_aug_geom_gist` and ran ST_Covers against RAW obec
# polygons: 194 ms and 1,225 buffers on the live mirror, nearly all of it detoasting
# geometry the pip pieces exist precisely to avoid.
#
# Split into two branches under one LIMIT instead. `Limit -> Append` stops at the first
# row, so the authoritative branch is `never executed` whenever a pip piece covers the
# point — which is the same preference `ORDER BY (purpose = 'pip') DESC LIMIT 1` expressed,
# just without paying for the losing side first. Measured: 0.24 ms/point across a 50-point
# batch. The authoritative branch stays because boundaries load per unit and a partially
# loaded pack has units with no pip rows yet.
_CONTAINING_OBEC_BRANCH = f"""
    SELECT {_ADMIN_COLUMNS}, g.containment_radius_m AS crad
      FROM ruian_admin_unit_geometries g
      JOIN ruian_admin_units u ON u.id = g.unit_id
     WHERE g.registry_version_id = %s
       AND g.purpose = '{{purpose}}'
       AND u.level = 'obec'
       AND ST_Covers(g.geom, {_PT})
     LIMIT 1
"""

_CONTAINING_OBEC_SQL = f"""
SELECT p.idx, c.*
  FROM {_POINTS}
  CROSS JOIN LATERAL (
    ({_CONTAINING_OBEC_BRANCH.format(purpose="pip")})
    UNION ALL
    ({_CONTAINING_OBEC_BRANCH.format(purpose="authoritative")})
    LIMIT 1
  ) c
"""

# `ST_DWithin(g.geom::geography, …)` cannot use `ruian_aug_geom_gist (geom)` — the cast is
# not the indexed expression — so this used to read and cast EVERY boundary row whose bbox
# reached the point, the state and kraj polygons included: 6,752 ms per point on the live
# mirror. Two changes, both semantics-preserving:
#
# * `g.geom && ST_Expand(pt, %s / 60000.0)` is an Index Cond on that GiST index. 60,000 is a
#   deliberate under-estimate of metres per degree of longitude (69,900 at CZ's northernmost
#   51.1°), so the box strictly CONTAINS the geodesic circle and cannot hide a row the exact
#   `ST_DWithin` would have kept.
# * the subdivided `pip` pieces preferred over the raw polygon, same two-branch form and same
#   reason as `containing_obec`: the pieces TILE the polygon, so the minimum distance over
#   them is the distance to the polygon — with the small pieces the geography cast is cheap.
#   All 6,258 obce carry pip rows today (verified), so the authoritative branch is the
#   partially-loaded-pack fallback only. Measured: 2.95 ms/point.
_NEAREST_OBEC_BRANCH = f"""
    SELECT {_ADMIN_COLUMNS}, g.containment_radius_m AS crad,
           ST_Distance(g.geom::geography, {_PT}::geography) AS d
      FROM ruian_admin_unit_geometries g
      JOIN ruian_admin_units u ON u.id = g.unit_id
     WHERE g.registry_version_id = %s
       AND g.purpose = '{{purpose}}'
       AND u.level = 'obec'
       AND g.geom && ST_Expand({_PT}, %s / 60000.0)
       AND ST_DWithin(g.geom::geography, {_PT}::geography, %s)
     ORDER BY 15
     LIMIT 1
"""

_NEAREST_OBEC_SQL = f"""
SELECT p.idx, c.*
  FROM {_POINTS}
  CROSS JOIN LATERAL (
    ({_NEAREST_OBEC_BRANCH.format(purpose="pip")})
    UNION ALL
    ({_NEAREST_OBEC_BRANCH.format(purpose="authoritative")})
    LIMIT 1
  ) c
"""

# Keyed on (unit_id, lat, lon), so its point array carries the unit too.
_BOUNDARY_DISTANCE_SQL = """
SELECT p.idx,
       ST_Distance(ST_Boundary(g.geom)::geography,
                   ST_SetSRID(ST_MakePoint(p.lon, p.lat), 4326)::geography)
  FROM unnest(%s::int[], %s::bigint[], %s::double precision[], %s::double precision[])
       AS p(idx, unit_id, lat, lon)
  CROSS JOIN LATERAL (
    SELECT g.geom FROM ruian_admin_unit_geometries g
     WHERE g.unit_id = p.unit_id AND g.registry_version_id = %s
       AND g.purpose = 'authoritative'
     LIMIT 1) g
"""

# The KNN `<->` scan had no index-usable bound at all: `ST_DWithin(ap.geom::geography, …)`
# is a FILTER, so a point with no address point inside 250 m walked the entire 3,020,222-row
# geometry index before answering NULL — 2,944 ms per point averaged over a live sample. The
# `&&` box (see `_NEAREST_OBEC_BRANCH` for why 60,000) bounds the scan; resolving the nearest
# point in a subquery BEFORE joining `ruian_admin_units` drops the old plan's seq scan +
# Materialize of all 18,627 unit rows, and is exact because `cast_obce_unit_id` is a FOREIGN
# KEY — the first row out of the subquery always finds its unit. Measured: 0.15 ms/point.
_CAST_OBCE_FOR_POINT_SQL = f"""
SELECT p.idx, {_ADMIN_COLUMNS}, NULL::double precision
  FROM {_POINTS}
  CROSS JOIN LATERAL (
    SELECT ap.cast_obce_unit_id AS unit_id
      FROM ruian_address_points ap
     WHERE ap.cast_obce_unit_id IS NOT NULL
       AND ap.valid_to IS NULL
       AND ap.geom && ST_Expand({_PT}, %s / 60000.0)
       AND ST_DWithin(ap.geom::geography, {_PT}::geography, %s)
     ORDER BY ap.geom <-> {_PT}
     LIMIT 1) nearest
  JOIN ruian_admin_units u ON u.id = nearest.unit_id
"""

# Same shape, same reason: `cast_obce_kod` is unindexed while `ruian_ap_cast_obce
# (cast_obce_unit_id) WHERE cast_obce_unit_id IS NOT NULL` is right there. Measured live:
# 5,059 ms / 79,905 buffers as a parallel seq scan of all 3 M rows, versus 35 ms / 167
# buffers through the index. The remaining cost is the ST_Collect over the part's own
# points, which is the work the question actually asks for.
_CAST_OBCE_EXTENT_SQL = """
SELECT ST_MaxDistance(ST_Collect(ap.geom), ST_Centroid(ST_Collect(ap.geom))) * 111320.0
  FROM ruian_address_points ap
 WHERE ap.cast_obce_unit_id IN (
         SELECT u.id FROM ruian_admin_units u
          WHERE u.level = 'cast_obce'::ruian_level AND u.code = %s)
   AND ap.valid_to IS NULL AND ap.geom IS NOT NULL
"""

_IN_CZ_SQL = f"""
SELECT p.idx, ST_Covers(cz.geom, {_PT})
  FROM {_POINTS}
  CROSS JOIN LATERAL (
    SELECT g.geom FROM ruian_admin_unit_geometries g
      JOIN ruian_admin_units u ON u.id = g.unit_id
     WHERE g.registry_version_id = %s AND g.purpose = 'authoritative' AND u.level = 'stat'
     LIMIT 1) cz
"""

# The cluster row and BOTH neighbourhood sums in one statement — the old three-statement
# form cost three round trips for every distinct pin, and the two sums are scalar
# subqueries over the same nine cells. The `JOIN` (not LEFT JOIN) reproduces
# `if row is None: return None`: a point whose own cell has no cluster yields no row.
#
# The 3x3 expansion is `collision.neighbourhood`, i.e. PYTHON — passed in as a flat
# (idx, cell) pair array rather than reimplemented in SQL, so there is still exactly one
# definition of a cell.
_CLUSTER_SQL = """
WITH pts AS (
  SELECT * FROM unnest(%s::int[], %s::text[], %s::text[],
                       %s::double precision[], %s::double precision[])
              AS t(idx, source, cell, lat, lon)
), nbc AS (
  SELECT * FROM unnest(%s::int[], %s::text[]) AS t(idx, cell)
)
SELECT p.idx, c.id, c.source, c.cell_key, c.listing_count, c.distinct_streets,
       c.distinct_obec_kods, c.classification, c.distance_to_admin_centroid_m,
       c.declared_blur_share,
       coalesce((SELECT sum(n.listing_count) FROM nbc
                   JOIN pin_clusters n ON n.epoch_id = %s AND n.source = p.source
                                      AND n.cell_key = nbc.cell
                  WHERE nbc.idx = p.idx
                    AND ST_DWithin(n.geom::geography,
                                   ST_SetSRID(ST_MakePoint(p.lon, p.lat), 4326)::geography,
                                   25.0)), 0),
       coalesce((SELECT sum(n.listing_count) FROM nbc
                   JOIN pin_clusters n ON n.epoch_id = %s AND n.source = p.source
                                      AND n.cell_key = nbc.cell
                  WHERE nbc.idx = p.idx
                    AND ST_DWithin(n.geom::geography,
                                   ST_SetSRID(ST_MakePoint(p.lon, p.lat), 4326)::geography,
                                   100.0)), 0)
  FROM pts p
  JOIN pin_clusters c ON c.epoch_id = %s AND c.source = p.source AND c.cell_key = p.cell
"""


def load_constants(conn: psycopg.Connection) -> LocationConstants:
    values: dict[str, float] = {}
    bbox: tuple[float, float, float, float] | None = None
    with conn.cursor() as cur:
        cur.execute(_CONSTANTS_SQL)
        for name, num, xmin, ymin, xmax, ymax in cur.fetchall():
            if name == "cz_bbox" and xmin is not None:
                bbox = (float(xmin), float(ymin), float(xmax), float(ymax))
            elif num is not None:
                values[name] = float(num)
    if bbox is None:
        raise LookupError("location_constants has no cz_bbox row — the ONE canonical bbox")
    return LocationConstants(
        cz_bbox=bbox,
        cz_bbox_trigger_buffer_deg=values.get("cz_bbox_trigger_buffer_deg", 0.05),
        pip_sliver_tolerance_m=values.get("pip_sliver_tolerance_m", 250.0),
        registry_pin_conflict_m=values.get("registry_pin_conflict_m", 300.0),
        precise_r95_m=values.get("precise_r95_m", 30.0),
        approx_r95_m=values.get("approx_r95_m", 300.0),
    )


def load_granularity_rank(conn: psycopg.Connection) -> GranularityRank:
    with conn.cursor() as cur:
        cur.execute(_GRANULARITY_RANK_SQL)
        rows = cur.fetchall()
    return GranularityRank({name: int(rank) for name, rank in rows})


def load_field_policy(conn: psycopg.Connection, policy_version: str) -> tuple[FieldPolicyRow, ...]:
    with conn.cursor() as cur:
        cur.execute(_FIELD_POLICY_SQL, (policy_version,))
        return tuple(FieldPolicyRow(*row) for row in cur.fetchall())


def load_uncertainty_policy(
    conn: psycopg.Connection, policy_version: str
) -> tuple[UncertaintyPolicyRow, ...]:
    with conn.cursor() as cur:
        cur.execute(_UNCERTAINTY_POLICY_SQL, (policy_version,))
        return tuple(
            UncertaintyPolicyRow(
                pv, ps, gr, src, None if r95 is None else float(r95), sem, deriv
            )
            for pv, ps, gr, src, r95, sem, deriv in cur.fetchall()
        )


def load_collision_policy(
    conn: psycopg.Connection, policy_version: str
) -> tuple[CollisionPolicyRow, ...]:
    with conn.cursor() as cur:
        cur.execute(_COLLISION_POLICY_SQL, (policy_version,))
        return tuple(CollisionPolicyRow(*row) for row in cur.fetchall())


def current_registry_version(conn: psycopg.Connection) -> tuple[int, str]:
    with conn.cursor() as cur:
        cur.execute(_CURRENT_REGISTRY_SQL)
        row = cur.fetchone()
    if row is None:
        raise LookupError("no registry_versions row is is_current — load the mirror first")
    return int(row[0]), str(row[1])


def current_epoch(conn: psycopg.Connection) -> int | None:
    with conn.cursor() as cur:
        cur.execute(_CURRENT_EPOCH_SQL)
        row = cur.fetchone()
    return None if row is None else int(row[0])


def _claim(row: Sequence[Any]) -> Claim:
    return Claim(
        id=row[0], listing_id=row[1], source=row[2], claim_type=row[3], surface=row[4],
        extraction_method=row[5], extractor_id=row[6], licence_class=row[7],
        observed_at=row[8], value_text=row[9],
        value_num=None if row[10] is None else float(row[10]),
        lat=None if row[11] is None else float(row[11]),
        lon=None if row[12] is None else float(row[12]),
        value_jsonb=row[13] or {}, declared_precision_label=row[14],
        declared_confidence=row[15],
        declared_radius_m=None if row[16] is None else float(row[16]),
        blur_evidence=row[17], claim_confidence=row[18], subject_scoped=row[19],
        page_kind=row[20], snapshot_id=row[21], distance_m=row[22], target_text=row[23],
    )


def load_claims(conn: psycopg.Connection, listing_id: int) -> list[Claim]:
    with conn.cursor() as cur:
        cur.execute(_CLAIMS_SQL, (listing_id,))
        return [_claim(row) for row in cur.fetchall()]


def load_claims_bulk(
    conn: psycopg.Connection, listing_ids: Sequence[int]
) -> dict[int, list[Claim]]:
    """Every claim for a whole slice, grouped by listing. Listings with no claims are absent
    from the mapping, exactly as `load_claims` returns an empty list for them."""
    out: dict[int, list[Claim]] = {}
    if not listing_ids:
        return out
    with conn.cursor() as cur:
        cur.execute(_CLAIMS_BULK_SQL, (list(listing_ids),))
        for row in cur.fetchall():
            out.setdefault(int(row[1]), []).append(_claim(row))
    return out


def _admin_unit(row: Sequence[Any]) -> AdminUnit:
    path = str(row[5] or "")
    return AdminUnit(
        unit_id=int(row[0]), level=str(row[1]), code=int(row[2]), name=str(row[3]),
        name_norm=str(row[4]), path=path, display_path=str(row[6] or ""),
        parent_id=row[7], lat=None if row[8] is None else float(row[8]),
        lon=None if row[9] is None else float(row[9]),
        qualifier=row[10], homonym_count=int(row[11] or 1),
        psc_set=tuple(str(p).strip() for p in (row[12] or ())),
        containment_radius_m=None if row[13] is None else float(row[13]),
        obec_kod=_path_code(path, "b"), okres_kod=_path_code(path, "o"),
        kraj_kod=_path_code(path, "k"),
    )


def _path_code(path: str, prefix: str) -> int | None:
    """The ltree path is `k{kraj}.o{okres}.b{obec}.c{cast_obce}` (01 §3.2.1) — level-prefixed
    NUMERIC codes, which is what makes this parse safe (a name label would not even insert)."""
    for label in path.split("."):
        if label.startswith(prefix) and label[1:].isdigit():
            return int(label[1:])
    return None


def _address_point(row: Sequence[Any]) -> AddressPoint:
    return AddressPoint(
        kod_adm=int(row[0]), obec_unit_id=int(row[1]), obec_kod=int(row[2]), psc=str(row[3]),
        lat=None if row[4] is None else float(row[4]),
        lon=None if row[5] is None else float(row[5]),
        street_id=row[6], ulice_kod=row[7], street_name_norm=row[8], cislo_domovni=row[9],
        cislo_orientacni=row[10], znak_orientacniho=row[11], stavebni_objekt_code=row[12],
        cast_obce_unit_id=row[13], cast_obce_kod=row[14], momc_unit_id=row[15],
    )


class QueryStats:
    """Per-query-KIND round trips and wall clock for one run.

    Round 1's instrumentation logged aggregates only (`registry_q`, `registry_hit`), which
    said the registry cost 77 ms a question but not WHICH question — so round 2 had to
    re-derive the offenders by hand against production. This names them in the run's own log.
    """

    __slots__ = ("queries", "seconds", "rows")

    def __init__(self) -> None:
        self.queries: dict[str, int] = {}
        self.seconds: dict[str, float] = {}
        self.rows: dict[str, int] = {}

    def record(self, kind: str, elapsed: float, rows: int) -> None:
        self.queries[kind] = self.queries.get(kind, 0) + 1
        self.seconds[kind] = self.seconds.get(kind, 0.0) + elapsed
        self.rows[kind] = self.rows.get(kind, 0) + rows

    def report(self, limit: int = 8) -> str:
        """The slowest kinds first — total seconds is what a budget is spent in, not the
        per-call average (a 3 ms question asked 20,000 times outranks a 700 ms one asked
        twice)."""
        ranked = sorted(self.seconds.items(), key=lambda kv: -kv[1])[:limit]
        return " ".join(
            f"{kind}(q={self.queries[kind]},{total:.1f}s,"
            f"{1000.0 * total / max(self.queries[kind], 1):.0f}ms)"
            for kind, total in ranked
        )


class SqlRegistryView:
    """`types.RegistryView` over the mirror, pinned to one `registry_version_id`."""

    def __init__(
        self,
        conn: psycopg.Connection,
        registry_version_id: int,
        stats: QueryStats | None = None,
    ) -> None:
        self._conn = conn
        self._version = registry_version_id
        self.stats = stats if stats is not None else QueryStats()

    def _rows(self, kind: str, sql: str, params: tuple) -> list[tuple]:
        started = time.perf_counter()
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        self.stats.record(kind, time.perf_counter() - started, len(rows))
        return rows

    def address_point(self, kod_adm: int) -> AddressPoint | None:
        rows = self._rows("address_point", _ADDRESS_POINT_SQL, (kod_adm,))
        return _address_point(rows[0]) if rows else None

    def address_points_by_number(
        self, *, obec_kod: int, street_name_norm: str | None,
        cislo_domovni: int | None, cislo_orientacni: int | None,
    ) -> list[AddressPoint]:
        rows = self._rows(
            "address_points_by_number",
            _ADDRESS_POINTS_BY_NUMBER_SQL,
            (obec_kod, street_name_norm, street_name_norm, cislo_domovni, cislo_domovni,
             cislo_orientacni, cislo_orientacni),
        )
        return [_address_point(r) for r in rows]

    def streets_in_obec(self, obec_kod: int) -> list[Street]:
        return [
            Street(street_id=int(r[0]), code=int(r[1]), name=str(r[2]), name_norm=str(r[3]),
                   obec_unit_id=int(r[4]), obec_kod=int(r[5]))
            for r in self._rows("streets_in_obec", _STREETS_IN_OBEC_SQL, (obec_kod,))
        ]

    def admin_units_by_name(self, name_norm: str, *, levels: Sequence[str] = ()) -> list[AdminUnit]:
        wanted = list(levels)
        return [
            _admin_unit(r)
            for r in self._rows(
                "admin_units_by_name",
                _ADMIN_BY_NAME_SQL, (self._version, self._version, name_norm, wanted, wanted)
            )
        ]

    def admin_unit_by_code(self, level: str, code: int) -> AdminUnit | None:
        rows = self._rows("admin_unit_by_code", _ADMIN_BY_CODE_SQL, (self._version, level, code))
        return _admin_unit(rows[0]) if rows else None

    def admin_unit(self, unit_id: int) -> AdminUnit | None:
        rows = self._rows("admin_unit", _ADMIN_BY_ID_SQL, (self._version, unit_id))
        return _admin_unit(rows[0]) if rows else None

    def admin_chain(self, unit_id: int) -> list[AdminUnit]:
        return [
            _admin_unit(r)
            for r in self._rows(
                "admin_chain", _ADMIN_CHAIN_SQL, (unit_id, self._version, unit_id)
            )
        ]

    def obec_codes_for_psc(self, psc: str) -> list[int]:
        return [int(r[0]) for r in self._rows("obec_codes_for_psc", _PSC_OBEC_SQL, (psc,))]

    def parcels(self, *, katuz_name_norm: str, parcel_label_norm: str) -> list[Parcel]:
        return [
            Parcel(
                parcel_id=int(r[0]), code=int(r[1]), katuz_unit_id=int(r[2]),
                parcel_label_norm=str(r[3]),
                lat=None if r[4] is None else float(r[4]),
                lon=None if r[5] is None else float(r[5]),
            )
            for r in self._rows("parcels", _PARCELS_SQL, (katuz_name_norm, parcel_label_norm))
        ]

    # ---- the five point-keyed questions: ONE array-shaped statement, called here with one
    # point and by `warm_points` with the whole slice. `_bulk` returns {idx: answer}.

    def containing_obec_bulk(
        self, points: Sequence[tuple[float, float]]
    ) -> dict[int, AdminUnit]:
        if not points:
            return {}
        idx, lats, lons = _point_arrays(points)
        rows = self._rows(
            "containing_obec", _CONTAINING_OBEC_SQL,
            (idx, lats, lons, self._version, self._version),
        )
        return {int(r[0]): _admin_unit(r[1:]) for r in rows}

    def containing_obec(self, lat: float, lon: float) -> AdminUnit | None:
        return self.containing_obec_bulk([(lat, lon)]).get(0)

    def nearest_obec_within(
        self, lat: float, lon: float, max_m: float
    ) -> tuple[AdminUnit, float] | None:
        idx, lats, lons = _point_arrays([(lat, lon)])
        rows = self._rows(
            "nearest_obec_within", _NEAREST_OBEC_SQL,
            (idx, lats, lons,
             self._version, max_m, max_m, self._version, max_m, max_m),
        )
        if not rows:
            return None
        return _admin_unit(rows[0][1:]), float(rows[0][15])

    def distance_to_admin_boundary_m_bulk(
        self, keys: Sequence[tuple[int, float, float]]
    ) -> dict[int, float]:
        if not keys:
            return {}
        rows = self._rows(
            "distance_to_admin_boundary_m", _BOUNDARY_DISTANCE_SQL,
            (list(range(len(keys))), [int(k[0]) for k in keys],
             [float(k[1]) for k in keys], [float(k[2]) for k in keys], self._version),
        )
        return {int(r[0]): float(r[1]) for r in rows if r[1] is not None}

    def distance_to_admin_boundary_m(self, unit_id: int, lat: float, lon: float) -> float | None:
        return self.distance_to_admin_boundary_m_bulk([(unit_id, lat, lon)]).get(0)

    def cast_obce_for_point_bulk(
        self, points: Sequence[tuple[float, float]]
    ) -> dict[int, AdminUnit]:
        if not points:
            return {}
        idx, lats, lons = _point_arrays(points)
        rows = self._rows(
            "cast_obce_for_point", _CAST_OBCE_FOR_POINT_SQL,
            (idx, lats, lons, CAST_OBCE_RADIUS_M, CAST_OBCE_RADIUS_M),
        )
        return {int(r[0]): _admin_unit(r[1:]) for r in rows}

    def cast_obce_for_point(self, lat: float, lon: float) -> AdminUnit | None:
        return self.cast_obce_for_point_bulk([(lat, lon)]).get(0)

    def cast_obce_extent_m(self, cast_obce_kod: int) -> float | None:
        rows = self._rows("cast_obce_extent_m", _CAST_OBCE_EXTENT_SQL, (cast_obce_kod,))
        return None if not rows or rows[0][0] is None else float(rows[0][0])

    def in_czechia_polygon_bulk(
        self, points: Sequence[tuple[float, float]]
    ) -> dict[int, bool]:
        if not points:
            return {}
        idx, lats, lons = _point_arrays(points)
        rows = self._rows("in_czechia_polygon", _IN_CZ_SQL, (idx, lats, lons, self._version))
        return {int(r[0]): bool(r[1]) for r in rows if r[1] is not None}

    def in_czechia_polygon(self, lat: float, lon: float) -> bool | None:
        return self.in_czechia_polygon_bulk([(lat, lon)]).get(0)


CAST_OBCE_RADIUS_M = 250.0


def _point_arrays(
    points: Sequence[tuple[float, float]]
) -> tuple[list[int], list[float], list[float]]:
    return (
        list(range(len(points))),
        [float(p[0]) for p in points],
        [float(p[1]) for p in points],
    )


class SqlCollisionEvidence:
    """`pin_clusters` at ONE stamped epoch — the corpus-wide input that makes
    `collision_epoch_id` the fifth version in the resolution's identity."""

    def __init__(
        self,
        conn: psycopg.Connection,
        epoch_id: int | None,
        stats: QueryStats | None = None,
    ) -> None:
        self._conn = conn
        self._epoch = epoch_id
        self.stats = stats if stats is not None else QueryStats()

    def for_point_bulk(
        self, points: Sequence[tuple[str, float, float]]
    ) -> dict[int, ClusterEvidence]:
        if self._epoch is None or not points:
            return {}
        from location_data.resolver.collision import cell_of, neighbourhood

        idx: list[int] = []
        sources: list[str] = []
        cells: list[str] = []
        lats: list[float] = []
        lons: list[float] = []
        nb_idx: list[int] = []
        nb_cells: list[str] = []
        for i, (source, lat, lon) in enumerate(points):
            cell = cell_of(lat, lon)
            idx.append(i)
            sources.append(source)
            cells.append(cell)
            lats.append(float(lat))
            lons.append(float(lon))
            for neighbour in neighbourhood(cell):
                nb_idx.append(i)
                nb_cells.append(neighbour)
        started = time.perf_counter()
        with self._conn.cursor() as cur:
            cur.execute(
                _CLUSTER_SQL,
                (idx, sources, cells, lats, lons, nb_idx, nb_cells,
                 self._epoch, self._epoch, self._epoch),
            )
            rows = cur.fetchall()
        self.stats.record("collision_for_point", time.perf_counter() - started, len(rows))
        out: dict[int, ClusterEvidence] = {}
        for row in rows:
            count = int(row[4])
            out[int(row[0])] = ClusterEvidence(
                cluster_id=int(row[1]), source=str(row[2]), cell_key=str(row[3]),
                listing_count=count, distinct_streets=int(row[5]),
                distinct_obec_kods=int(row[6]), classification=str(row[7]),
                n_25m=max(int(row[10] or count), count),
                n_100m=max(int(row[11] or count), count),
                distance_to_admin_centroid_m=None if row[8] is None else float(row[8]),
                declared_blur_share=None if row[9] is None else float(row[9]),
            )
        return out

    def for_point(self, source: str, lat: float, lon: float) -> ClusterEvidence | None:
        return self.for_point_bulk([(source, lat, lon)]).get(0)


class RunCache:
    """Memo for the corpus-constant questions of ONE run, plus its own instrumentation.

    Safe because BOTH mirrors it fronts are immutable for the run's lifetime: the RÚIAN
    registry is pinned to a `registry_version_id` (a load mints a NEW version rather than
    editing one) and `pin_clusters` is pinned to a `collision_epoch_id` (an epoch is minted,
    never updated). Same question, same answer — so the pure core sees exactly what an
    uncached run would and replay stays bit-for-bit.

    `max_entries` is a memory rail, not a hit-rate policy: at the cap the whole memo is
    dropped and refills. Correctness cannot depend on what is resident.
    """

    __slots__ = ("_values", "_max", "hits", "misses", "seconds", "asked_by_kind",
                 "missed_by_kind")

    def __init__(self, max_entries: int = 250_000) -> None:
        self._values: dict[Any, Any] = {}
        self._max = max_entries
        self.hits = 0
        self.misses = 0
        self.seconds = 0.0
        # Every key is a tuple whose head is the question's name, so the per-KIND hit rate
        # comes for free — and it is the number that says whether a key is too narrow.
        self.asked_by_kind: dict[str, int] = {}
        self.missed_by_kind: dict[str, int] = {}

    def _count(self, key: Any, missed: bool) -> None:
        kind = key[0] if isinstance(key, tuple) and key else str(key)
        self.asked_by_kind[kind] = self.asked_by_kind.get(kind, 0) + 1
        if missed:
            self.missed_by_kind[kind] = self.missed_by_kind.get(kind, 0) + 1

    def get(self, key: Any, compute: Callable[[], Any]) -> Any:
        try:
            value = self._values[key]
        except KeyError:
            pass
        else:
            self.hits += 1
            self._count(key, missed=False)
            return value
        self.misses += 1
        self._count(key, missed=True)
        started = time.perf_counter()
        value = compute()
        self.seconds += time.perf_counter() - started
        self.put(key, value)
        return value

    def put(self, key: Any, value: Any) -> None:
        """Pre-seed an answer (`warm_points`). Identical to what `get` would have stored, so
        a warmed run and a cold one hand the core the same bytes."""
        if len(self._values) >= self._max:
            self._values.clear()
        self._values[key] = value

    @property
    def hit_rate(self) -> float:
        asked = self.hits + self.misses
        return 0.0 if asked == 0 else self.hits / asked

    def report(self, limit: int = 8) -> str:
        """The kinds that MISS most — a kind with a low hit rate is either keyed on something
        per-listing (warm it per slice) or keyed too narrowly (widen it)."""
        ranked = sorted(self.missed_by_kind.items(), key=lambda kv: -kv[1])[:limit]
        return " ".join(
            f"{kind}(asked={self.asked_by_kind.get(kind, 0)},miss={missed})"
            for kind, missed in ranked
        )


class CachedRegistryView:
    """`types.RegistryView` over a `SqlRegistryView`, memoized for one run (see `RunCache`).

    Every list-returning method hands back a TUPLE, not the cached list: the protocol asks
    for a `Sequence`, and an immutable one cannot be mutated by a caller into poisoning the
    next listing's answer.
    """

    __slots__ = ("_inner", "_cache")

    def __init__(self, inner: Any, cache: RunCache) -> None:
        self._inner = inner
        self._cache = cache

    def address_point(self, kod_adm: int) -> AddressPoint | None:
        return self._cache.get(
            ("address_point", kod_adm), lambda: self._inner.address_point(kod_adm)
        )

    def address_points_by_number(
        self, *, obec_kod: int, street_name_norm: str | None,
        cislo_domovni: int | None, cislo_orientacni: int | None,
    ) -> Sequence[AddressPoint]:
        key = ("address_points_by_number", obec_kod, street_name_norm, cislo_domovni,
               cislo_orientacni)
        return self._cache.get(
            key,
            lambda: tuple(
                self._inner.address_points_by_number(
                    obec_kod=obec_kod, street_name_norm=street_name_norm,
                    cislo_domovni=cislo_domovni, cislo_orientacni=cislo_orientacni,
                )
            ),
        )

    def streets_in_obec(self, obec_kod: int) -> Sequence[Street]:
        return self._cache.get(
            ("streets_in_obec", obec_kod), lambda: tuple(self._inner.streets_in_obec(obec_kod))
        )

    def admin_units_by_name(
        self, name_norm: str, *, levels: Sequence[str] = ()
    ) -> Sequence[AdminUnit]:
        """Keyed on the NAME only, with the level narrowing applied in Python.

        The level tuple was part of the key, so one name asked four different ways — S3 asks
        for `('obec',)`, then `('obec','cast_obce','momc','zsj')`, S9 for
        `('katastralni_uzemi',)`, `_is_place_name` for all levels — was four cache entries and
        four round trips for one immutable answer. The unnarrowed query is the SUPERSET of
        every narrowing (`cardinality(levels) = 0 OR level = ANY(levels)`) and its
        `ORDER BY u.level, u.code` survives a stable filter, so the narrowed list is
        element-for-element what the narrowed query returned.
        """
        wanted = frozenset(levels)
        every = self._cache.get(
            ("admin_units_by_name", name_norm),
            lambda: tuple(self._inner.admin_units_by_name(name_norm)),
        )
        if not wanted:
            return every
        return tuple(unit for unit in every if unit.level in wanted)

    def admin_unit_by_code(self, level: str, code: int) -> AdminUnit | None:
        return self._cache.get(
            ("admin_unit_by_code", level, code),
            lambda: self._inner.admin_unit_by_code(level, code),
        )

    def admin_unit(self, unit_id: int) -> AdminUnit | None:
        return self._cache.get(("admin_unit", unit_id), lambda: self._inner.admin_unit(unit_id))

    def admin_chain(self, unit_id: int) -> Sequence[AdminUnit]:
        return self._cache.get(
            ("admin_chain", unit_id), lambda: tuple(self._inner.admin_chain(unit_id))
        )

    def obec_codes_for_psc(self, psc: str) -> Sequence[int]:
        return self._cache.get(
            ("obec_codes_for_psc", psc), lambda: tuple(self._inner.obec_codes_for_psc(psc))
        )

    def parcels(self, *, katuz_name_norm: str, parcel_label_norm: str) -> Sequence[Parcel]:
        return self._cache.get(
            ("parcels", katuz_name_norm, parcel_label_norm),
            lambda: tuple(
                self._inner.parcels(
                    katuz_name_norm=katuz_name_norm, parcel_label_norm=parcel_label_norm
                )
            ),
        )

    def containing_obec(self, lat: float, lon: float) -> AdminUnit | None:
        return self._cache.get(
            ("containing_obec", lat, lon), lambda: self._inner.containing_obec(lat, lon)
        )

    def nearest_obec_within(
        self, lat: float, lon: float, max_m: float
    ) -> tuple[AdminUnit, float] | None:
        return self._cache.get(
            ("nearest_obec_within", lat, lon, max_m),
            lambda: self._inner.nearest_obec_within(lat, lon, max_m),
        )

    def distance_to_admin_boundary_m(self, unit_id: int, lat: float, lon: float) -> float | None:
        return self._cache.get(
            ("distance_to_admin_boundary_m", unit_id, lat, lon),
            lambda: self._inner.distance_to_admin_boundary_m(unit_id, lat, lon),
        )

    def cast_obce_for_point(self, lat: float, lon: float) -> AdminUnit | None:
        return self._cache.get(
            ("cast_obce_for_point", lat, lon), lambda: self._inner.cast_obce_for_point(lat, lon)
        )

    def cast_obce_extent_m(self, cast_obce_kod: int) -> float | None:
        return self._cache.get(
            ("cast_obce_extent_m", cast_obce_kod),
            lambda: self._inner.cast_obce_extent_m(cast_obce_kod),
        )

    def in_czechia_polygon(self, lat: float, lon: float) -> bool | None:
        return self._cache.get(
            ("in_czechia_polygon", lat, lon), lambda: self._inner.in_czechia_polygon(lat, lon)
        )


class CachedCollisionEvidence:
    """`SqlCollisionEvidence` memoized on the EXACT pin, which is three queries per miss.

    Collapsed portal pins are the reason the epoch exists at all, so a run resolving a bazos
    cohort asks for the same coordinate hundreds of times.
    """

    __slots__ = ("_inner", "_cache")

    def __init__(self, inner: Any, cache: RunCache) -> None:
        self._inner = inner
        self._cache = cache

    def for_point(self, source: str, lat: float, lon: float) -> ClusterEvidence | None:
        return self._cache.get(
            ("collision", source, lat, lon), lambda: self._inner.for_point(source, lat, lon)
        )


def warm_points(
    registry: SqlRegistryView,
    collision: SqlCollisionEvidence,
    cache: RunCache,
    points: Sequence[tuple[str, float, float]],
) -> None:
    """Answer the five point-keyed questions for a WHOLE SLICE in five round trips.

    These are the questions the run cache can never share between listings — the key IS the
    listing's coordinate — and they were 5 of the drain's ~16 round trips per listing. Asking
    them ahead of the slice does not change a single answer: `warm_points` writes exactly the
    key/value `CachedRegistryView` would have computed lazily, from the SAME statement text
    (the `*_bulk` methods are the single-point methods with a longer array), so the pure core
    is handed identical bytes and replay is unaffected. A point that was NOT warmed — a
    position that came from a registry match rather than from the portal's pin — simply misses
    and asks for itself, exactly as before.

    A NULL answer is an answer: it is cached too, or every rural point would re-ask per call.
    """
    if not points:
        return
    coords = sorted({(lat, lon) for _, lat, lon in points})
    covering = registry.containing_obec_bulk(coords)
    for i, (lat, lon) in enumerate(coords):
        cache.put(("containing_obec", lat, lon), covering.get(i))
    in_cz = registry.in_czechia_polygon_bulk(coords)
    for i, (lat, lon) in enumerate(coords):
        cache.put(("in_czechia_polygon", lat, lon), in_cz.get(i))
    cast_obce = registry.cast_obce_for_point_bulk(coords)
    for i, (lat, lon) in enumerate(coords):
        cache.put(("cast_obce_for_point", lat, lon), cast_obce.get(i))
    # The boundary distance is keyed on (unit, point) and the unit is the one PIP just
    # returned — admin.py asks for exactly that pair right after `containing_obec`.
    pairs = [
        (covering[i].unit_id, lat, lon)
        for i, (lat, lon) in enumerate(coords)
        if i in covering
    ]
    distances = registry.distance_to_admin_boundary_m_bulk(pairs)
    for i, (unit_id, lat, lon) in enumerate(pairs):
        cache.put(("distance_to_admin_boundary_m", unit_id, lat, lon), distances.get(i))
    triples = sorted(set(points))
    clusters = collision.for_point_bulk(triples)
    for i, (source, lat, lon) in enumerate(triples):
        cache.put(("collision", source, lat, lon), clusters.get(i))


# --------------------------------------------------------------------------- writers

_INSERT_RESOLUTION_SQL = """
INSERT INTO location_resolutions
       (listing_id, claim_set_hash, resolver_version, registry_version_id, policy_version,
        collision_epoch_id, status, chosen_rule, candidate_count, runner_up_score_gap,
        country_code, country_status, country_method, country_confidence,
        country_driving_claim_ids, country_conflicting,
        granularity, position_source, blur_evidence, match_confidence,
        uncertainty_radius_m, radius_semantics, geom, position_licence_class, input_claim_ids)
VALUES (%s, decode(%s, 'hex'), %s, %s, %s,
        %s, %s::resolution_status, %s, %s, %s,
        %s, %s::country_status, %s::country_determination_method, %s::match_confidence,
        %s, %s::jsonb,
        %s::location_granularity, %s::position_source, %s::blur_evidence, %s::match_confidence,
        %s, %s::radius_semantics,
        CASE WHEN %s::double precision IS NULL THEN NULL
             ELSE ST_SetSRID(ST_MakePoint(%s, %s), 4326) END,
        %s::licence_class, %s)
ON CONFLICT (listing_id, claim_set_hash, resolver_version, registry_version_id,
             policy_version, collision_epoch_id) DO NOTHING
"""

# The slice's ids read back on the SAME six-column identity, one row per listing. The whole
# tuple travels per row rather than as run constants: they ARE constant for one run today,
# and a join that quietly assumed so would return the wrong row the day they are not.
_SELECT_RESOLUTIONS_BULK_SQL = """
SELECT w.listing_id, r.id
  FROM unnest(%s::bigint[], %s::text[], %s::text[], %s::bigint[], %s::text[], %s::bigint[])
       AS w(listing_id, hash_hex, resolver_version, registry_version_id, policy_version,
            collision_epoch_id)
  JOIN location_resolutions r
    ON r.listing_id = w.listing_id
   AND r.claim_set_hash = decode(w.hash_hex, 'hex')
   AND r.resolver_version = w.resolver_version
   AND r.registry_version_id = w.registry_version_id
   AND r.policy_version = w.policy_version
   AND r.collision_epoch_id = w.collision_epoch_id
"""

_INSERT_CANDIDATE_SQL = """
INSERT INTO location_resolution_candidates
       (resolution_id, rank, score, target_kind, ruian_adm_kod, stavebni_objekt_kod,
        parcela_id, ulice_id, admin_unit_id, geom, granularity, position_source,
        blur_evidence, match_confidence, uncertainty_radius_m, radius_semantics,
        licence_class, component_match, distance_to_pin_m, rejected_reason)
VALUES (%s, %s, %s, %s, %s, %s,
        %s, %s, %s,
        CASE WHEN %s::double precision IS NULL THEN NULL
             ELSE ST_SetSRID(ST_MakePoint(%s, %s), 4326) END,
        %s::location_granularity, %s::position_source,
        %s::blur_evidence, %s::match_confidence, %s, %s::radius_semantics,
        %s::licence_class, %s::jsonb, %s, %s)
ON CONFLICT (resolution_id, rank) DO NOTHING
"""

# Stamp the winner by its RANK rather than by an id read back from the INSERT: the insert is
# ON CONFLICT DO NOTHING, so a re-run returns no id at all and the old read-back left
# `chosen_candidate_id` un-restamped. `(resolution_id, rank)` is the candidate's unique key,
# so this resolves to the same row either way — and it is one statement instead of one
# RETURNING read per candidate.
_SET_CHOSEN_SQL = """
UPDATE location_resolutions r
   SET chosen_candidate_id = c.id
  FROM location_resolution_candidates c
 WHERE r.id = %s AND c.resolution_id = %s AND c.rank = %s
"""

_UPSERT_LISTING_PROJECTION_SQL = """
INSERT INTO listing_location_current AS llc (
    listing_id, property_id, source, resolution_id, registry_version_id, registry_version,
    resolver_version, policy_version, built_at,
    country_code, country_status, country_method, country_confidence,
    country_driving_claim_ids, is_cz,
    geom, granularity, position_source, blur_evidence, match_confidence, match_components,
    uncertainty_radius_m, radius_semantics, position_licence_class,
    ruian_adm_kod, stavebni_objekt_kod, parcela_id, ulice_kod, obec_kod, cast_obce_kod,
    momc_kod, ku_kod, pou_kod, orp_kod, okres_kod, kraj_kod,
    obec_unit_id, cast_obce_unit_id, okres_unit_id, kraj_unit_id, admin_path,
    admin_assignment_method, admin_position_source, admin_sliver_distance_m,
    display_label, display_path, street_name, house_number_cp, house_number_co, evidencni,
    psc, postal_town, cast_obce_name, obec_name, okres_name, kraj_name, development_name,
    place_search_text,
    pin_shared_by_n, pin_shared_by_n_25m, pin_shared_by_n_100m, pin_cluster_id,
    pin_collision_class, cluster_heterogeneity_ok, render_as, renderable_as_point,
    is_low_precision, geo_blockable, location_disputed, distance_to_nearest_boundary_m,
    history_completeness, field_provenance, geom_claim_id, street_claim_id,
    addr_block_key, building_block_key, street_block_key, geo_cell_key, h3_r10,
    position_quality_class, collision_epoch_id)
VALUES (
    %(listing_id)s, %(property_id)s, %(source)s, %(resolution_id)s, %(registry_version_id)s,
    %(registry_version)s, %(resolver_version)s, %(policy_version)s, now(),
    %(country_code)s, %(country_status)s::country_status,
    %(country_method)s::country_determination_method,
    %(country_confidence)s::match_confidence, %(country_driving_claim_ids)s, %(is_cz)s,
    CASE WHEN %(lat)s::double precision IS NULL THEN NULL
         ELSE ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) END,
    %(granularity)s::location_granularity, %(position_source)s::position_source,
    %(blur_evidence)s::blur_evidence, %(match_confidence)s::match_confidence,
    %(match_components)s::jsonb, %(uncertainty_radius_m)s, %(radius_semantics)s::radius_semantics,
    %(position_licence_class)s::licence_class,
    %(ruian_adm_kod)s, %(stavebni_objekt_kod)s, %(parcela_id)s, %(ulice_kod)s, %(obec_kod)s,
    %(cast_obce_kod)s, %(momc_kod)s, %(ku_kod)s, %(pou_kod)s, %(orp_kod)s, %(okres_kod)s,
    %(kraj_kod)s, %(obec_unit_id)s, %(cast_obce_unit_id)s, %(okres_unit_id)s,
    %(kraj_unit_id)s, %(admin_path)s::ltree,
    %(admin_assignment_method)s::admin_assignment_method,
    %(admin_position_source)s::position_source, %(admin_sliver_distance_m)s,
    %(display_label)s, %(display_path)s, %(street_name)s, %(house_number_cp)s,
    %(house_number_co)s, %(evidencni)s, %(psc)s, %(postal_town)s, %(cast_obce_name)s,
    %(obec_name)s, %(okres_name)s, %(kraj_name)s, %(development_name)s, %(place_search_text)s,
    %(pin_shared_by_n)s, %(pin_shared_by_n_25m)s, %(pin_shared_by_n_100m)s, %(pin_cluster_id)s,
    %(pin_collision_class)s, %(cluster_heterogeneity_ok)s, %(render_as)s,
    %(renderable_as_point)s, %(is_low_precision)s, %(geo_blockable)s, %(location_disputed)s,
    %(distance_to_nearest_boundary_m)s, %(history_completeness)s, %(field_provenance)s::jsonb,
    %(geom_claim_id)s, %(street_claim_id)s, %(addr_block_key)s, %(building_block_key)s,
    %(street_block_key)s, %(geo_cell_key)s, %(h3_r10)s,
    %(position_quality_class)s, %(collision_epoch_id)s)
ON CONFLICT (listing_id) DO UPDATE SET
    property_id = EXCLUDED.property_id, source = EXCLUDED.source,
    resolution_id = EXCLUDED.resolution_id,
    registry_version_id = EXCLUDED.registry_version_id,
    registry_version = EXCLUDED.registry_version,
    resolver_version = EXCLUDED.resolver_version, policy_version = EXCLUDED.policy_version,
    built_at = now(), country_code = EXCLUDED.country_code,
    country_status = EXCLUDED.country_status, country_method = EXCLUDED.country_method,
    country_confidence = EXCLUDED.country_confidence,
    country_driving_claim_ids = EXCLUDED.country_driving_claim_ids, is_cz = EXCLUDED.is_cz,
    geom = EXCLUDED.geom, granularity = EXCLUDED.granularity,
    position_source = EXCLUDED.position_source, blur_evidence = EXCLUDED.blur_evidence,
    match_confidence = EXCLUDED.match_confidence, match_components = EXCLUDED.match_components,
    uncertainty_radius_m = EXCLUDED.uncertainty_radius_m,
    radius_semantics = EXCLUDED.radius_semantics,
    position_licence_class = EXCLUDED.position_licence_class,
    ruian_adm_kod = EXCLUDED.ruian_adm_kod,
    stavebni_objekt_kod = EXCLUDED.stavebni_objekt_kod, parcela_id = EXCLUDED.parcela_id,
    ulice_kod = EXCLUDED.ulice_kod, obec_kod = EXCLUDED.obec_kod,
    cast_obce_kod = EXCLUDED.cast_obce_kod, momc_kod = EXCLUDED.momc_kod,
    ku_kod = EXCLUDED.ku_kod, pou_kod = EXCLUDED.pou_kod, orp_kod = EXCLUDED.orp_kod,
    okres_kod = EXCLUDED.okres_kod, kraj_kod = EXCLUDED.kraj_kod,
    obec_unit_id = EXCLUDED.obec_unit_id, cast_obce_unit_id = EXCLUDED.cast_obce_unit_id,
    okres_unit_id = EXCLUDED.okres_unit_id, kraj_unit_id = EXCLUDED.kraj_unit_id,
    admin_path = EXCLUDED.admin_path,
    admin_assignment_method = EXCLUDED.admin_assignment_method,
    admin_position_source = EXCLUDED.admin_position_source,
    admin_sliver_distance_m = EXCLUDED.admin_sliver_distance_m,
    display_label = EXCLUDED.display_label, display_path = EXCLUDED.display_path,
    street_name = EXCLUDED.street_name, house_number_cp = EXCLUDED.house_number_cp,
    house_number_co = EXCLUDED.house_number_co, evidencni = EXCLUDED.evidencni,
    psc = EXCLUDED.psc, postal_town = EXCLUDED.postal_town,
    cast_obce_name = EXCLUDED.cast_obce_name, obec_name = EXCLUDED.obec_name,
    okres_name = EXCLUDED.okres_name, kraj_name = EXCLUDED.kraj_name,
    development_name = EXCLUDED.development_name,
    place_search_text = EXCLUDED.place_search_text,
    pin_shared_by_n = EXCLUDED.pin_shared_by_n,
    pin_shared_by_n_25m = EXCLUDED.pin_shared_by_n_25m,
    pin_shared_by_n_100m = EXCLUDED.pin_shared_by_n_100m,
    pin_cluster_id = EXCLUDED.pin_cluster_id,
    pin_collision_class = EXCLUDED.pin_collision_class,
    cluster_heterogeneity_ok = EXCLUDED.cluster_heterogeneity_ok,
    render_as = EXCLUDED.render_as, renderable_as_point = EXCLUDED.renderable_as_point,
    is_low_precision = EXCLUDED.is_low_precision, geo_blockable = EXCLUDED.geo_blockable,
    location_disputed = EXCLUDED.location_disputed,
    distance_to_nearest_boundary_m = EXCLUDED.distance_to_nearest_boundary_m,
    history_completeness = EXCLUDED.history_completeness,
    field_provenance = EXCLUDED.field_provenance, geom_claim_id = EXCLUDED.geom_claim_id,
    street_claim_id = EXCLUDED.street_claim_id, addr_block_key = EXCLUDED.addr_block_key,
    building_block_key = EXCLUDED.building_block_key,
    street_block_key = EXCLUDED.street_block_key, geo_cell_key = EXCLUDED.geo_cell_key,
    h3_r10 = EXCLUDED.h3_r10,
    position_quality_class = EXCLUDED.position_quality_class,
    collision_epoch_id = EXCLUDED.collision_epoch_id
WHERE llc.listing_id = EXCLUDED.listing_id
"""

_UPSERT_PROPERTY_PROJECTION_SQL = """
INSERT INTO property_location_current AS plc (
    property_id, built_at, member_count, winner_listing_id, winner_rule, winner_source,
    geom, granularity, position_source, blur_evidence, match_confidence,
    uncertainty_radius_m, radius_semantics, position_licence_class,
    ruian_adm_kod, stavebni_objekt_kod, obec_kod, cast_obce_kod, okres_kod, kraj_kod,
    admin_path, admin_assignment_method, street_name, psc, display_label, place_search_text,
    country_code, country_status, member_spread_m, members_with_geom, distinct_street_names,
    distinct_obec_kods, disagreement_flags, pin_shared_by_n, geo_blockable, render_as)
VALUES (
    %(property_id)s, now(), %(member_count)s, %(winner_listing_id)s, %(winner_rule)s,
    %(winner_source)s,
    CASE WHEN %(lat)s::double precision IS NULL THEN NULL
         ELSE ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) END,
    %(granularity)s::location_granularity, %(position_source)s::position_source,
    %(blur_evidence)s::blur_evidence, %(match_confidence)s::match_confidence,
    %(uncertainty_radius_m)s, %(radius_semantics)s::radius_semantics,
    %(position_licence_class)s::licence_class,
    %(ruian_adm_kod)s, %(stavebni_objekt_kod)s, %(obec_kod)s, %(cast_obce_kod)s,
    %(okres_kod)s, %(kraj_kod)s, %(admin_path)s::ltree,
    %(admin_assignment_method)s::admin_assignment_method, %(street_name)s, %(psc)s,
    %(display_label)s, %(place_search_text)s, %(country_code)s,
    %(country_status)s::country_status, %(member_spread_m)s, %(members_with_geom)s,
    %(distinct_street_names)s, %(distinct_obec_kods)s, %(disagreement_flags)s,
    %(pin_shared_by_n)s, %(geo_blockable)s, %(render_as)s)
ON CONFLICT (property_id) DO UPDATE SET
    built_at = now(), member_count = EXCLUDED.member_count,
    winner_listing_id = EXCLUDED.winner_listing_id, winner_rule = EXCLUDED.winner_rule,
    winner_source = EXCLUDED.winner_source, geom = EXCLUDED.geom,
    granularity = EXCLUDED.granularity, position_source = EXCLUDED.position_source,
    blur_evidence = EXCLUDED.blur_evidence, match_confidence = EXCLUDED.match_confidence,
    uncertainty_radius_m = EXCLUDED.uncertainty_radius_m,
    radius_semantics = EXCLUDED.radius_semantics,
    position_licence_class = EXCLUDED.position_licence_class,
    ruian_adm_kod = EXCLUDED.ruian_adm_kod,
    stavebni_objekt_kod = EXCLUDED.stavebni_objekt_kod, obec_kod = EXCLUDED.obec_kod,
    cast_obce_kod = EXCLUDED.cast_obce_kod, okres_kod = EXCLUDED.okres_kod,
    kraj_kod = EXCLUDED.kraj_kod, admin_path = EXCLUDED.admin_path,
    admin_assignment_method = EXCLUDED.admin_assignment_method,
    street_name = EXCLUDED.street_name, psc = EXCLUDED.psc,
    display_label = EXCLUDED.display_label, place_search_text = EXCLUDED.place_search_text,
    country_code = EXCLUDED.country_code, country_status = EXCLUDED.country_status,
    member_spread_m = EXCLUDED.member_spread_m,
    members_with_geom = EXCLUDED.members_with_geom,
    distinct_street_names = EXCLUDED.distinct_street_names,
    distinct_obec_kods = EXCLUDED.distinct_obec_kods,
    disagreement_flags = EXCLUDED.disagreement_flags,
    pin_shared_by_n = EXCLUDED.pin_shared_by_n, geo_blockable = EXCLUDED.geo_blockable,
    render_as = EXCLUDED.render_as
WHERE plc.property_id = EXCLUDED.property_id
"""

_INSERT_CONTRADICTION_SQL = """
INSERT INTO location_contradictions
       (listing_id, property_id, reconciler_version, resolver_version, registry_version_id,
        field, rule, severity, stored, claimed, evidence_claim_ids, distance_m,
        evidence_quote, auto_action, dedupe_key)
VALUES (%s, %s, %s, %s, %s,
        %s::location_claim_type, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s,
        decode(%s, 'hex'))
ON CONFLICT (dedupe_key, listing_id, reconciler_version, registry_version_id) DO NOTHING
"""

_APPEND_DISPOSITION_SQL = """
INSERT INTO location_contradiction_dispositions
       (dedupe_key, status, disposition, decided_by, auto_closed_reason)
VALUES (decode(%s, 'hex'), %s, NULL, %s, %s)
ON CONFLICT (dedupe_key) DO UPDATE SET
    status = EXCLUDED.status, decided_by = EXCLUDED.decided_by,
    auto_closed_reason = EXCLUDED.auto_closed_reason, decided_at = now()
"""

_LOG_DISPOSITION_SQL = """
INSERT INTO location_contradiction_disposition_log
       (dedupe_key, status, disposition, decided_by, auto_closed_reason)
VALUES (decode(%s, 'hex'), %s, NULL, %s, %s)
"""

_OPEN_KEYS_SQL = """
SELECT encode(c.dedupe_key, 'hex')
  FROM location_contradictions_open c
 WHERE c.listing_id = %s
   AND (cardinality(%s::text[]) = 0 OR c.rule = ANY(%s::text[]))
"""

# The slice's open findings in one query. The rule narrowing stays per listing (each listing
# evaluated its own rule set) but moves to Python — the same filter, one round trip.
_OPEN_KEYS_BULK_SQL = """
SELECT c.listing_id, c.rule, encode(c.dedupe_key, 'hex')
  FROM location_contradictions_open c
 WHERE c.listing_id = ANY(%s::bigint[])
"""

_PROPERTY_IDS_BULK_SQL = "SELECT id, property_id FROM listings WHERE id = ANY(%s::bigint[])"

# What the PREVIOUS projection consumed. `inputs_changed` (00 §8.2) is a comparison against
# these four, never an assumption — auto-close must not fire on a re-run of the same inputs.
_PREVIOUS_INPUTS_SQL = """
SELECT encode(r.claim_set_hash, 'hex'), r.registry_version_id, r.policy_version,
       r.collision_epoch_id
  FROM listing_location_current p
  JOIN location_resolutions r ON r.id = p.resolution_id
 WHERE p.listing_id = %s
"""

# Prefetched for the whole slice, which is still strictly BEFORE this run rewrites any of
# those projections: a listing is claimed once per slice, and no listing's write can change
# another listing's row.
_PREVIOUS_INPUTS_BULK_SQL = """
SELECT p.listing_id, encode(r.claim_set_hash, 'hex'), r.registry_version_id, r.policy_version,
       r.collision_epoch_id
  FROM listing_location_current p
  JOIN location_resolutions r ON r.id = p.resolution_id
 WHERE p.listing_id = ANY(%s::bigint[])
"""

# Same read, one round trip for the slice. It is STILL a read-your-writes read — the caller
# runs it after the slice's contradictions and auto-closes are written, never at slice start.
_DISPUTED_BULK_SQL = """
SELECT DISTINCT c.listing_id
  FROM location_contradictions_open c
 WHERE c.listing_id = ANY(%s::bigint[]) AND c.severity = 'major'
"""


def _resolution_params(resolution: Resolution) -> tuple:
    import json

    position = resolution.position
    precision = resolution.precision
    return (
        resolution.listing_id, resolution.claim_set_hash, resolution.resolver_version,
        resolution.registry_version_id, resolution.policy_version,
        resolution.collision_epoch_id, resolution.status, resolution.chosen_rule,
        len(resolution.candidates), resolution.runner_up_score_gap,
        resolution.country.country_code, resolution.country.status,
        resolution.country.method, resolution.country.confidence,
        list(resolution.country.driving_claim_ids),
        json.dumps(list(resolution.country.conflicting)) if resolution.country.conflicting else None,
        precision.granularity, precision.position_source, precision.blur_evidence,
        precision.match_confidence, precision.uncertainty_radius_m, precision.radius_semantics,
        position.lat, position.lon, position.lat,
        resolution.position_licence_class, list(resolution.input_claim_ids),
    )


def write_resolutions_bulk(
    conn: psycopg.Connection, resolutions: Sequence[Resolution]
) -> dict[int, int]:
    """One INSERT (pipelined by `executemany`) plus ONE read-back for the whole slice.

    The read-back is a join on the resolver's own UNIQUE key rather than a `RETURNING`
    harvest: the insert is `ON CONFLICT DO NOTHING`, so a listing whose inputs are unchanged
    returns no id at all, and the id it needs is the EXISTING row's — which is precisely what
    the per-listing writer's fallback SELECT did, once per listing.
    """
    if not resolutions:
        return {}
    with conn.cursor() as cur:
        cur.executemany(_INSERT_RESOLUTION_SQL, [_resolution_params(r) for r in resolutions])
        cur.execute(
            _SELECT_RESOLUTIONS_BULK_SQL,
            ([r.listing_id for r in resolutions],
             [r.claim_set_hash for r in resolutions],
             [r.resolver_version for r in resolutions],
             [r.registry_version_id for r in resolutions],
             [r.policy_version for r in resolutions],
             [r.collision_epoch_id for r in resolutions]),
        )
        found = {int(listing_id): int(row_id) for listing_id, row_id in cur.fetchall()}
    missing = [r.listing_id for r in resolutions if r.listing_id not in found]
    if missing:  # pragma: no cover - only reachable on a concurrent delete
        raise RuntimeError(f"resolutions vanished between INSERT and SELECT: {missing[:5]}")
    return found


def _candidate_params(resolution_id: int, resolution: Resolution) -> list[tuple]:
    import json

    return [
        (resolution_id, c.rank, c.score, c.target_kind, c.ruian_adm_kod,
         c.stavebni_objekt_kod, c.parcela_id, c.ulice_id, c.admin_unit_id,
         c.lat, c.lon, c.lat, c.granularity, c.position_source, c.blur_evidence,
         c.match_confidence, c.uncertainty_radius_m, c.radius_semantics, c.licence_class,
         json.dumps(c.component_match), c.distance_to_pin_m, c.rejected_reason)
        for c in resolution.candidates
    ]


def write_candidates_bulk(
    conn: psycopg.Connection, items: Sequence[tuple[int, Resolution]]
) -> None:
    """The same two statements for a whole SLICE's candidates."""
    params: list[tuple] = []
    chosen: list[tuple] = []
    for resolution_id, resolution in items:
        params.extend(_candidate_params(resolution_id, resolution))
        if resolution.chosen_rank is not None:
            chosen.append((resolution_id, resolution_id, resolution.chosen_rank))
    if not params:
        return
    with conn.cursor() as cur:
        cur.executemany(_INSERT_CANDIDATE_SQL, params)
        if chosen:
            cur.executemany(_SET_CHOSEN_SQL, chosen)


def _listing_projection_params(row: dict[str, Any]) -> dict[str, Any]:
    import json

    params = dict(row)
    params["match_components"] = json.dumps(params.get("match_components") or {})
    params["field_provenance"] = json.dumps(params.get("field_provenance") or {})
    return params


def upsert_listing_projections_bulk(
    conn: psycopg.Connection, rows: Sequence[dict[str, Any]]
) -> None:
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            _UPSERT_LISTING_PROJECTION_SQL, [_listing_projection_params(r) for r in rows]
        )


def upsert_property_projections_bulk(
    conn: psycopg.Connection, rows: Sequence[dict[str, Any]]
) -> None:
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_PROPERTY_PROJECTION_SQL, list(rows))


def write_contradictions_bulk(
    conn: psycopg.Connection,
    items: Sequence[tuple[Sequence[Any], int | None]],
    *,
    reconciler_version: str,
    resolver_version: str,
    registry_version_id: int,
) -> None:
    """Every listing's detections in one `executemany`. The property_id travels WITH each
    listing's detections — it is a per-listing column, not a run constant."""
    import json

    params = [
        (detection.listing_id, property_id, reconciler_version, resolver_version,
         registry_version_id, detection.field, detection.rule, detection.severity,
         json.dumps(detection.stored, default=str),
         json.dumps(detection.claimed, default=str),
         list(detection.evidence_claim_ids), detection.distance_m,
         detection.evidence_quote, detection.auto_action, detection.dedupe_key)
        for detections, property_id in items
        for detection in detections
    ]
    if not params:
        return
    with conn.cursor() as cur:
        cur.executemany(_INSERT_CONTRADICTION_SQL, params)


def open_dedupe_keys(
    conn: psycopg.Connection, listing_id: int, *, rules: Sequence[str] = ()
) -> list[str]:
    """Open findings for this listing, optionally narrowed to the rules a run EVALUATED —
    a rule that was never asked cannot have "stopped firing"."""
    wanted = sorted(set(rules))
    with conn.cursor() as cur:
        cur.execute(_OPEN_KEYS_SQL, (listing_id, wanted, wanted))
        return [str(r[0]) for r in cur.fetchall()]


def open_dedupe_keys_bulk(
    conn: psycopg.Connection, listing_ids: Sequence[int]
) -> dict[int, tuple[tuple[str, str], ...]]:
    """(rule, dedupe_key) pairs per listing, UNNARROWED — the caller applies each listing's
    own evaluated-rule filter (`filter_open_keys`), because that set differs per listing."""
    out: dict[int, list[tuple[str, str]]] = {}
    if not listing_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(_OPEN_KEYS_BULK_SQL, (list(listing_ids),))
        for listing_id, rule, key in cur.fetchall():
            out.setdefault(int(listing_id), []).append((str(rule), str(key)))
    return {k: tuple(v) for k, v in out.items()}


def filter_open_keys(
    pairs: Sequence[tuple[str, str]], *, rules: Sequence[str] = ()
) -> list[str]:
    """`_OPEN_KEYS_SQL`'s `cardinality(...) = 0 OR rule = ANY(...)` predicate, in Python."""
    wanted = set(rules)
    return [key for rule, key in pairs if not wanted or rule in wanted]


def previous_consumed_inputs(
    conn: psycopg.Connection, listing_id: int
) -> tuple[str, int, str, int] | None:
    """(claim_set_hash, registry_version_id, policy_version, collision_epoch_id) of the
    resolution the CURRENT projection was built from, or None if there is none."""
    with conn.cursor() as cur:
        cur.execute(_PREVIOUS_INPUTS_SQL, (listing_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return (str(row[0]), int(row[1]), str(row[2]), int(row[3]))


def previous_consumed_inputs_bulk(
    conn: psycopg.Connection, listing_ids: Sequence[int]
) -> dict[int, tuple[str, int, str, int]]:
    if not listing_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(_PREVIOUS_INPUTS_BULK_SQL, (list(listing_ids),))
        return {
            int(r[0]): (str(r[1]), int(r[2]), str(r[3]), int(r[4])) for r in cur.fetchall()
        }


def property_ids_bulk(
    conn: psycopg.Connection, listing_ids: Sequence[int]
) -> dict[int, int | None]:
    """`listings.property_id` for a whole slice. Not written by this drain, so prefetching it
    cannot race the batch's own writes."""
    if not listing_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(_PROPERTY_IDS_BULK_SQL, (list(listing_ids),))
        return {
            int(r[0]): (None if r[1] is None else int(r[1])) for r in cur.fetchall()
        }


def append_auto_close(conn: psycopg.Connection, closes: Sequence[Any]) -> None:
    params = [
        (close.dedupe_key, close.status, close.decided_by, close.reason) for close in closes
    ]
    if not params:
        return
    with conn.cursor() as cur:
        cur.executemany(_APPEND_DISPOSITION_SQL, params)
        cur.executemany(_LOG_DISPOSITION_SQL, params)


def location_disputed_bulk(
    conn: psycopg.Connection, listing_ids: Sequence[int]
) -> set[int]:
    """The listings of the slice that carry an OPEN major finding. Must be called AFTER the
    slice's contradictions and auto-closes are written — it is a read of what this run just
    wrote, which is why it is not part of `drain._prefetch`."""
    if not listing_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(_DISPUTED_BULK_SQL, (list(listing_ids),))
        return {int(row[0]) for row in cur.fetchall()}
