"""The psycopg side of the resolver: load the claims and the registry inputs, call the pure
core, write the one `listing_location` row.

Connection mode is the drain's `scraper.db.connect_session()` — the SESSION-mode pooler, a
dedicated backend, psycopg3's default `prepare_threshold` — so the statements below are
server-side PREPARED once and reused for every listing in the run. It stays autocommit
("callers manage transactions explicitly"), so every atomic unit is an explicit
`with conn.transaction():` block, and there is still not a session advisory lock anywhere
near this file — the lease is a row-CAS, which is correct on either pooler mode.

**Round trips are the drain's cost model**, not CPU, and the constant is measured: ~75 ms of
network round trip from a GitHub-hosted runner to the Frankfurt pooler, against 0.02-0.5 ms
of server-side work per registry question. So the wall clock is ~99 % latency and ~1 % query,
and the lever is the NUMBER of round trips.

W2-a cuts that number by deleting questions rather than by tuning them. Fifteen registry
query kinds become nine:

* parcels went with the rung that could never reach them;
* `distance_to_admin_boundary_m`, `cast_obce_for_point` and `cast_obce_extent_m` went with
  the rule-2 boundary comparison and the ČástObce point lookup — FILL takes the quarter off
  the bound entity's own chain instead;
* `pin_clusters` went with the collision epoch;
* `admin_unit_by_code` and `admin_unit` FOLDED into `admin_chain`, which now returns the
  unit itself ahead of its ancestors, so "this unit and its chain" is one trip and not three.
  W2-a3 folds the unit's POSITION into the same answer rather than adding a tenth question:
  FILL places a row whose pin was inadmissible at the finest bound unit's own point, and the
  chain is the read it already makes.

`nearest_obec_within` stays: it is BIND's sliver rung and the thing that keeps a border pin
from having no town at all, which rule 25 does not allow.

The five policy/constant loaders went with the tables they read. What is left is four
point-free questions the run cache shares corpus-wide (name, code, street, address point),
two point-keyed ones (`containing_obec`, `in_czechia_polygon`) that `warm_points` asks once
per SLICE, and the sliver fallback, asked lazily because it is reached on ~1 % of listings.
The ~62 % cache plateau was those point-keyed questions; with the rest gone, the surviving
misses are two per distinct pin.

The registry view here answers exactly the questions `types.RegistryView` declares, so the
pure core cannot reach past it into SQL. Containment reads the `ST_Subdivide`d `pip` pieces
only: they tile the authoritative polygon and every unit of every loaded version carries both
(checked 2026-09-26), so there is no "partially loaded pack" left to fall back from.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

import psycopg

from location_data.resolver import composite
from location_data.resolver.types import (
    AddressPoint,
    AdminUnit,
    Claim,
    Street,
    StreetPoint,
)

_CURRENT_REGISTRY_SQL = "SELECT id, label FROM registry_versions WHERE is_current LIMIT 1"

# THE LICENCE RAIL LIVES HERE (W2-a). Migration 384 enforced it with three CHECK constraints
# on the stores of record — a `position_licence_class` column on each projection and on the
# resolution, each constrained `<> 'ephemeral_display_only'`. `listing_location` has no such
# column, because the guard moved one step upstream and became stronger: a Mapy-class
# coordinate is not refused at write time, it is never READ.
_ADMISSIBLE_LICENCE_CLASSES = "('portal', 'operator')"

# AND SO DOES THE CONTRACT-VERSION RAIL — BUT A BUMP MUST NEVER BLACK A LISTING OUT (W11),
# AND A HALF-FINISHED RE-MINE MUST NEVER BLANK ONE (W18-b).
# `location_claims` is append-only and its fingerprint hashes `extractor_version`, so a
# contract BUMP does not supersede the old version's rows — it inserts new ones beside them.
# The superseded row has the LOWER id, so it would win every "first admissible claim of this
# type" tie in BIND and FILL: the version rail is what stops the retired contract's answer
# being served forever.
#
# W1-c spelled that rail `pc.is_active`, and on 2026-09-14 that spelling cost the corpus its
# location. W9 bumped eight contracts at 05:58Z and a full-resolve sweep ran at 06:42Z, hours
# before the intake lanes had re-mined the pages under the new versions: 595,816 listings were
# re-judged with NO admissible claim at all and came out `unknown/undetermined/low`, and under
# W5 (consumers serve only resolved locations) Browse fell from ~350 k rows to 45,810.
#
# So the rail reads a listing's NEWEST EVIDENCE instead of only the newest CONTRACT: the
# resolver takes the claims of the highest contract version present that is `<= the active
# version` — the active version's claims the moment they exist, the most recent earlier
# version's until then. A bump is then what it always should have been: invisible until the
# evidence arrives.
#
# W11 SPELLED "NEWEST EVIDENCE" PER (LISTING, PORTAL), AND ON 2026-09-17 THAT COST 29,545
# LISTINGS THEIR LOCATION (W18-b). W18 bumped the bazos contract 6 -> 7 for ONE payload-lane
# entry (`street_name` off `raw_json` /title). The payload lane re-mined that entry across
# all 147 k bazos listings in a single hop, while the same run's BODIES pass — which re-mines
# the four PAGE entries (`obec_name`, `psc`, `precision_declaration`, `coordinate`) — is
# bounded and reached 56,905 of ~155 k bodies before the chain yielded. For ~90 k listings
# the newest version present was therefore 7 and carried the STREET alone: the per-portal
# partition read that one claim and hid the town, the PSČ and the pin, which were sitting
# right there at version 6. 29,545 of 50,598 live bazos listings went `undetermined` with no
# geom, 61,396 rows to granularity `unknown`.
#
# So the rail is PER CLAIM TYPE (v5.3): for each claim type the portal's ACTIVE contract
# declares an entry for, the listing's claims come from the newest version `<= active` that
# carries a claim OF THAT TYPE. A partial re-mine now costs a listing nothing — each type
# keeps its best evidence and the new version's types upgrade one at a time — while the
# supersession the version rail exists for is unchanged: a type re-mined under the new
# version never reads the old version's answer beside it.
#
# A claim type the ACTIVE contract no longer declares is not read AT ALL (the `EXISTS` over
# the active contract's entries): a deliberately dropped entry stops being evidence the
# moment it is dropped, instead of lingering forever at its last version, and
# `scripts/location_claims_retire.py` stays the only thing that DELETES those rows. It is an
# `EXISTS` and not a join because the active contract may declare the same claim type on
# several surfaces, and a join would then multiply the claim rows.
#
# `contract_version` is NULL for an operator claim (no entry by construction), which `max()`
# ignores and the outer filter keeps — operator corrections are unchanged.
#
# Operator claims carry NO contract entry
# (`location_data/operator_corrections.py` writes `contract_entry_id NULL` +
# `licence_class 'operator'`), so they are named explicitly rather than let through by the
# LEFT JOIN — a portal claim that somehow lost its entry id must NOT be admitted.
_ADMISSIBLE_CONTRACT = """
       (c.contract_entry_id IS NULL AND c.licence_class = 'operator'
        OR pc.id IS NOT NULL AND pc.version <= act.version
           AND EXISTS (SELECT 1 FROM portal_contract_entries ace
                        WHERE ace.contract_id = act.id
                          AND ace.claim_type = c.claim_type))"""

# ONE projection, ONE admissibility predicate — the row unpacking in `_claim` is positional,
# so a second hand-written column list is a silent mis-mapping waiting to happen
# (`test_resolver_jobs.test_the_claims_select_maps_onto_claim_positionally` pins it).
_CLAIM_COLUMNS = """id, listing_id, source, claim_type::text, surface::text,
       extraction_method::text, licence_class::text, first_observed_at, value_text,
       value_num,
       CASE WHEN value_geom IS NULL THEN NULL ELSE ST_Y(value_geom) END,
       CASE WHEN value_geom IS NULL THEN NULL ELSE ST_X(value_geom) END,
       value_jsonb, declared_precision_label, declared_radius_m,
       blur_evidence::text, claim_confidence::text, subject_scoped"""


def _claims_sql(listing_filter: str, order_by: str) -> str:
    """The claim read, with the caller's listing filter INSIDE the window's scope.

    The filter cannot be appended to a finished statement any more: `max(pc.version) OVER
    (PARTITION BY ...)` would then be computed over the whole table before the listing was
    picked. Measured on prod over a 250-listing slice, the shape below is 11 ms / 1.2 k
    buffers against 152 ms / 75 k for the same rule written as a correlated anti-join.

    The partition carries `c.claim_type` (W18-b): the newest version is asked PER TYPE, so a
    re-mine that has reached one entry and not the others upgrades that entry alone instead
    of blanking every type the new version has not reached yet.
    """
    return f"""
WITH evidence AS (
SELECT c.*, pc.version AS contract_version,
       max(pc.version) OVER (PARTITION BY c.listing_id, c.source, c.claim_type)
         AS newest_version
  FROM location_claims c
  LEFT JOIN portal_contract_entries pce ON pce.id = c.contract_entry_id
  LEFT JOIN portal_contracts pc ON pc.id = pce.contract_id
  LEFT JOIN portal_contracts act ON act.source = pc.source AND act.is_active
 WHERE c.licence_class IN {_ADMISSIBLE_LICENCE_CLASSES}
   AND{_ADMISSIBLE_CONTRACT}
   AND {listing_filter}
)
SELECT {_CLAIM_COLUMNS}
  FROM evidence
 WHERE contract_version IS NULL OR contract_version = newest_version
 ORDER BY {order_by}
"""


_CLAIMS_SQL = _claims_sql("c.listing_id = %s", "id")

# The whole SLICE in one query instead of one per listing; the per-listing order is still
# `id`, which is what `core.resolve` re-sorts on anyway.
_CLAIMS_BULK_SQL = _claims_sql("c.listing_id = ANY(%s::bigint[])", "listing_id, id")

_SOURCES_BULK_SQL = "SELECT id, source FROM listings WHERE id = ANY(%s::bigint[])"

# ------------------------------------------------------------------ the registry reads

# THE KÚ OF A POINT (PR-B): the katastrální území whose `pip` piece covers it at the view's
# registry version — one GiST probe on `ruian_aug_pip_gist`, the same shape as
# `containing_obec` (0.4 ms/point warm, measured over 639 sampled address points). It is asked
# of REGISTRY points only (an address point, a door), never of a portal pin. `ORDER BY code`
# only makes a door that sits exactly ON a shared border replay to the same KÚ every time, and
# the `OFFSET 0` fence is what keeps the probe the DRIVING side: with a bare `ORDER BY u.code
# LIMIT 1` the planner walked all 13,074 KÚ in code order against the probe — 16 ms a point.
def _katastr_covering(point: str) -> str:
    return f"""
    SELECT kc.id, kc.code
      FROM (SELECT u.id, u.code
              FROM ruian_admin_unit_geometries g
              JOIN ruian_admin_units u ON u.id = g.unit_id
             WHERE g.registry_version_id = %s
               AND g.purpose = 'pip'
               AND u.level = 'katastralni_uzemi'
               AND ST_Covers(g.geom, {point})
            OFFSET 0) kc
     ORDER BY kc.code
     LIMIT 1"""


# THE DOOR RULE (operator Q7, 2026-09-25): a street or část obce is in a KÚ when EVERY one of
# its valid RÚIAN doors is. Asked door by door it is a GiST probe each — 1.7 s for the
# 9,193-door část measured 2026-09-26 — so it is asked ONCE: the KÚ covering the lowest-kód
# door, kept only when that KÚ's authoritative polygon, read by `(unit_id, version)` on
# `ruian_aug_unique_nonpip`, covers the whole door set (22 ms for the same část). The pip
# pieces tile that polygon (pip = authoritative for every unit at every loaded version,
# checked 2026-09-26), so the two answers are the same answer. No door -> no row -> NULL.
def _doors_katastr(doors: str) -> str:
    return f"""
SELECT k.code
  FROM (SELECT d.geom FROM {doors} d ORDER BY d.kod_adm LIMIT 1) f
  CROSS JOIN LATERAL ({_katastr_covering("f.geom")}) k
  JOIN ruian_admin_unit_geometries a
    ON a.unit_id = k.id AND a.registry_version_id = %s AND a.purpose = 'authoritative'
 WHERE ST_Covers(a.geom, (SELECT ST_Collect(d.geom) FROM {doors} d))"""


_ADDRESS_POINT_COLUMNS = """
       ap.kod_adm, ap.obec_unit_id, ap.obec_kod, ap.psc,
       ST_Y(ap.geom), ST_X(ap.geom), ap.ulice_kod, s.name_norm, s.name,
       ap.cislo_domovni, ap.cislo_orientacni, ap.znak_orientacniho,
       ap.cast_obce_unit_id, ap.cast_obce_kod, ku.code"""

# Every address-point read answers its KÚ in the SAME round trip — the drain's cost is round
# trips, and FILL reads the bound point anyway. Binds the registry version FIRST.
_ADDRESS_POINT_FROM = f"""
  FROM ruian_address_points ap
  LEFT JOIN ruian_streets s ON s.id = ap.street_id
  LEFT JOIN LATERAL ({_katastr_covering("ap.geom")}) ku ON true"""

_ADDRESS_POINT_SQL = f"""
SELECT {_ADDRESS_POINT_COLUMNS}{_ADDRESS_POINT_FROM}
 WHERE ap.kod_adm = %s AND ap.valid_to IS NULL
"""

# The obec is addressed by UNIT ID, not by `obec_kod`. Both columns exist on every row and
# are 1:1, but only `obec_unit_id` is indexed: `ruian_ap_obec_hn (obec_unit_id,
# cislo_domovni)` is exactly this lookup's index and `obec_kod` has none at all. Measured on
# the live 3,020,222-row mirror, house number only, no street: `obec_kod` planned a parallel
# scan of the whole PK — 21,494 ms, 2,954,453 buffers. The form below touches 424 buffers.
# `u.level = %s::ruian_level` casts the PARAMETER (not the column), so
# `ruian_admin_units_code (level, code)` keeps both of its columns.
#
# `IN (...)` over EVERY unit row carrying that code, not `= (SELECT ... valid_to IS NULL
# LIMIT 1)`: `ruian_admin_units` is SCD-2, so a code can have a closed row and an open one,
# and an address point still points at the row that was current when it was loaded.
_OBEC_UNIT_ID_SUBQUERY = """
    (SELECT u.id FROM ruian_admin_units u
      WHERE u.level = 'obec'::ruian_level AND u.code = %s)
"""

_ADDRESS_POINTS_BY_NUMBER_SQL = f"""
SELECT {_ADDRESS_POINT_COLUMNS}{_ADDRESS_POINT_FROM}
 WHERE ap.obec_unit_id IN {_OBEC_UNIT_ID_SUBQUERY.strip()}
   AND ap.valid_to IS NULL
   AND (%s::text IS NULL OR s.name_norm = %s::text)
   AND (%s::integer IS NULL OR ap.cislo_domovni = %s::integer)
   AND (%s::integer IS NULL OR ap.cislo_orientacni = %s::integer)
 ORDER BY ap.kod_adm
 LIMIT 50
"""

_STREETS_IN_OBEC_SQL = """
SELECT s.code, s.name, s.name_norm, u.code, s.id
  FROM ruian_streets s
  JOIN ruian_admin_units u ON u.id = s.obec_unit_id
 WHERE u.code = %s AND u.level = 'obec' AND s.valid_to IS NULL
 ORDER BY s.code
"""

# WHERE A STREET IS (W18). `ruian_streets` carries no geometry, so the answer is derived from
# the street's own valid address points: the CENTROID of the set, and as its extent the
# distance from that centroid to the FARTHEST of them — the radius of the smallest circle
# around the centroid that contains every point of the street.
#
# Half the bounding-box diagonal is the number this first computed and it was wrong in a way
# that mattered: a box's half-diagonal is the radius of a circle around the box's CENTRE, and
# the point published is the centroid, which is not that. On 65 % of streets a real address
# point lies outside it — Jiráskova in obec 535419 is 63 points whose half-diagonal is 864 m
# while its farthest point is 1,288 m away, so three of its own doors read as "off the
# street": an exact pin standing on one of them was moved to the centroid and stamped
# `pin_off_street`, and the published radius understated the street.
#
# Keyed on `ap.street_id`, never on `ap.ulice_kod`: `ruian_ap_street_hn (street_id,
# cislo_domovni, cislo_orientacni)` is exactly this lookup's index and `ulice_kod` has none,
# so the kód form would plan a scan of three million rows for a question asked once per
# listing that binds a street. `::geography` on the distance because the answer is METRES;
# the centroid stays geometry (4326 degrees), which is what `ST_Y`/`ST_X` want.
#
# The same door set answers the street's KÚ (PR-B, the door rule) in the same round trip.
_STREET_POINT_SQL = f"""
WITH p AS (
    SELECT ap.kod_adm, ap.geom
      FROM ruian_address_points ap
     WHERE ap.street_id = %s AND ap.valid_to IS NULL AND ap.geom IS NOT NULL
), c AS (
    SELECT ST_Centroid(ST_Collect(p.geom)) AS g FROM p
)
SELECT ST_Y(c.g), ST_X(c.g),
       MAX(ST_Distance(c.g::geography, p.geom::geography)),
       count(*),
       ({_doors_katastr("p")})
  FROM p, c
 GROUP BY 1, 2
"""

# A část obce's KÚ by the same door rule. `ruian_ap_cast_obce (cast_obce_unit_id)` is its
# index. Asked lazily, for a row bound to a část in a multi-KÚ obec only.
_PART_KATASTR_SQL = f"""
WITH p AS (
    SELECT ap.kod_adm, ap.geom
      FROM ruian_address_points ap
     WHERE ap.cast_obce_unit_id = %s AND ap.valid_to IS NULL AND ap.geom IS NOT NULL
)
{_doors_katastr("p")}
"""

# The unit column list `_admin_unit` unpacks positionally, over ONE point expression: the
# unit's own position is the last two columns of the projection, and only where the query
# can answer it.
def _admin_columns(point: str) -> str:
    return f"""
       u.id, u.level::text, u.code, u.name, u.name_norm, u.path::text, u.parent_id,
       CASE WHEN {point} IS NULL THEN NULL ELSE ST_Y({point}) END,
       CASE WHEN {point} IS NULL THEN NULL ELSE ST_X({point}) END"""


_ADMIN_COLUMNS = _admin_columns("u.definition_point")

# The CHAIN also answers "where is this unit?" (W2-a3), because FILL places a row with no
# admissible pin at the finest bound unit's point and the chain is the read it already
# makes. `definition_point` is RÚIAN's own definiční bod and would be the better answer, but
# `ruian_load` has never written that column — every unit point in the mirror is the
# polygon's, so the coalesce is a preference, not a fallback that fires.
#
# `representative_point` is the boundary loader's stored ST_MaximumInscribedCircle CENTRE:
# always INSIDE the polygon, which `ST_Centroid` is not for a concave unit (a C-shaped obec
# centroids into its own notch, i.e. into a neighbouring town). It is the stored equivalent
# of `ST_PointOnSurface`, better centred and — the reason it is read instead of computed —
# free of the multipolygon detoast that made the containment query 194 ms before it was
# split.
_ADMIN_CHAIN_COLUMNS = _admin_columns("coalesce(u.definition_point, gp.pt)")

_ADMIN_BY_NAME_SQL = f"""
SELECT {_ADMIN_COLUMNS}, n.qualifier, n.homonym_count, n.psc_set
  FROM ruian_name_index n
  JOIN ruian_admin_units u ON u.id = n.entity_id AND u.level = n.entity_kind
 WHERE n.registry_version_id = %s
   AND n.name_norm = %s
   AND (cardinality(%s::text[]) = 0 OR u.level::text = ANY(%s::text[]))
   AND u.valid_to IS NULL
 ORDER BY u.level, u.code
"""

# The chain INCLUDING the unit itself, coarsest last. `depth` is what orders it, and it is
# what lets `admin_unit(id)` and `admin_chain(id)` be the same round trip: FILL reads the
# bound entity's own level off row 0 and its ancestors off the rest.
#
# The lateral is a POINT read, not a geometry read: one index lookup per chain row on
# `ruian_aug_unique_nonpip (unit_id, registry_version_id, purpose) WHERE purpose <> 'pip'`,
# which is unique for a non-pip purpose, so the `LIMIT 1` picks a row rather than one of
# several. No extra round trip — and round trips are this file's cost model — so folding the
# unit point in here is what keeps the protocol at NINE questions instead of ten.
#
# It runs for EVERY chain row rather than for the bound unit alone because the two levels a
# listing most often binds, `cast_obce` and `momc`, have no polygon in RÚIAN at all — the
# loader's layer list (`ruian_boundaries.LAYERS`) is stát → kraj → okres → ORP → POU → obec
# → KÚ — so FILL walks up to the first ancestor that HAS a point, and it can only walk what
# this answer carries.
#
# An OBEC row also answers its one KÚ child (PR-B): every entity inside a one-KÚ obec lies in
# that KÚ by the hierarchy alone, so FILL needs no geometry for 3,942 of the 6,258 obce. An
# index read on `ruian_admin_units_parent`, in the round trip FILL already makes.
_ADMIN_CHAIN_TAIL = f"""
SELECT {_ADMIN_CHAIN_COLUMNS}, NULL::text, 1, NULL::char(5)[], ks.code
  FROM chain c
  JOIN ruian_admin_units u ON u.id = c.id
  LEFT JOIN LATERAL (
    SELECT g.representative_point AS pt
      FROM ruian_admin_unit_geometries g
     WHERE g.unit_id = u.id
       AND g.registry_version_id = %s
       AND g.purpose = 'authoritative'
     LIMIT 1) gp ON true
  LEFT JOIN LATERAL (
    SELECT CASE WHEN count(*) = 1 THEN min(k.code) END AS code
      FROM ruian_admin_units k
     WHERE u.level = 'obec'
       AND k.parent_id = u.id
       AND k.level = 'katastralni_uzemi'
       AND k.valid_to IS NULL) ks ON true
 ORDER BY c.depth
"""

_ADMIN_CHAIN_SQL = f"""
WITH RECURSIVE chain AS (
  SELECT u.id, u.parent_id, 0 AS depth FROM ruian_admin_units u WHERE u.id = %s
  UNION ALL
  SELECT p.id, p.parent_id, c.depth + 1
    FROM ruian_admin_units p JOIN chain c ON p.id = c.parent_id
)
{_ADMIN_CHAIN_TAIL}
"""

_ADMIN_CHAIN_BY_CODE_SQL = f"""
WITH RECURSIVE chain AS (
  SELECT a.id, a.parent_id, 0 AS depth
    FROM (SELECT u.id, u.parent_id FROM ruian_admin_units u
           WHERE u.level = %s::ruian_level AND u.code = %s AND u.valid_to IS NULL
           ORDER BY u.valid_from DESC
           LIMIT 1) a
  UNION ALL
  SELECT p.id, p.parent_id, c.depth + 1
    FROM ruian_admin_units p JOIN chain c ON p.id = c.parent_id
)
{_ADMIN_CHAIN_TAIL}
"""

_PSC_OBEC_SQL = """
SELECT DISTINCT obec_kod FROM ruian_address_points
 WHERE psc = %s AND valid_to IS NULL ORDER BY obec_kod
"""

# ------------------------------------------------------ the two point-keyed questions
#
# Both are keyed on the LISTING'S OWN coordinate, so the run cache can never share an answer
# between two listings. Each therefore has exactly ONE shape, taking parallel
# `(idx[], lat[], lon[])` arrays: `warm_points` calls it with the whole slice's points (one
# trip for 250 listings) and `SqlRegistryView` calls the SAME text with a one-element array
# when a point was not warmed. One shape, so the lazy path can never drift from the warmed
# one — which is the property replay depends on.
_PT = "ST_SetSRID(ST_MakePoint(p.lon, p.lat), 4326)"
_POINTS = "unnest(%s::int[], %s::double precision[], %s::double precision[]) AS p(idx, lat, lon)"

# THE ONE CONTAINING-OBEC STATEMENT. `api/maps.py` runs this same text for a Mapy pick and
# for an estimation subject that has no stored location, so a chip, a listing and an
# estimation can never disagree about which town a point is in.
#
# `purpose = 'pip'` is what lets `ruian_aug_pip_gist (geom) WHERE purpose = 'pip'` serve it:
# the subdivided pieces are small, so ST_Covers never detoasts a raw obec polygon (194 ms and
# 1,225 buffers when an IN-list forced exactly that). Measured: 0.24 ms/point. The
# authoritative fallback branch it used to carry (and the copy in `api/maps.py`) is gone: it
# was reachable only for a version loaded with `--allow-missing-pip`, and pip = authoritative
# for every unit of every loaded version (checked 2026-09-26).
CONTAINING_OBEC_SQL = f"""
SELECT p.idx, c.*
  FROM {_POINTS}
  CROSS JOIN LATERAL (
    SELECT {_ADMIN_COLUMNS}, NULL::text, 1, NULL::char(5)[]
      FROM ruian_admin_unit_geometries g
      JOIN ruian_admin_units u ON u.id = g.unit_id
     WHERE g.registry_version_id = %s
       AND g.purpose = 'pip'
       AND u.level = 'obec'
       AND ST_Covers(g.geom, {_PT})
     LIMIT 1
  ) c
"""

# `ST_DWithin(g.geom::geography, ...)` cannot use `ruian_aug_geom_gist (geom)` — the cast is
# not the indexed expression — so this used to read and cast EVERY boundary row whose bbox
# reached the point, the state and kraj polygons included: 6,752 ms per point on the live
# mirror. Two changes, both semantics-preserving:
#
# * `g.geom && ST_Expand(pt, %s / 60000.0)` is an Index Cond on that GiST index. 60,000 is a
#   deliberate under-estimate of metres per degree of longitude (69,900 at CZ's northernmost
#   51.1°), so the box strictly CONTAINS the geodesic circle and cannot hide a row the exact
#   `ST_DWithin` would have kept.
# * the subdivided `pip` pieces, never the raw polygon, for the same reason as
#   `containing_obec`: the pieces TILE the polygon, so the minimum distance over them is the
#   distance to the polygon — and with the small pieces the geography cast is cheap.
#   Measured: 2.95 ms/point.
#
# It is reached only when `containing_obec` misses (~1 % of listings), which is also why it is
# NOT in `warm_points`: warming it would run this lateral for all 250 of a slice's points to
# answer the two or three that ask.
_NEAREST_OBEC_SQL = f"""
SELECT p.idx, c.*
  FROM {_POINTS}
  CROSS JOIN LATERAL (
    SELECT {_ADMIN_COLUMNS}, NULL::text, 1, NULL::char(5)[],
           ST_Distance(g.geom::geography, {_PT}::geography) AS d
      FROM ruian_admin_unit_geometries g
      JOIN ruian_admin_units u ON u.id = g.unit_id
     WHERE g.registry_version_id = %s
       AND g.purpose = 'pip'
       AND u.level = 'obec'
       AND g.geom && ST_Expand({_PT}, %s / 60000.0)
       AND ST_DWithin(g.geom::geography, {_PT}::geography, %s)
     ORDER BY 13
     LIMIT 1
  ) c
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

# ------------------------------------------------------------------------ the one write

_UPSERT_LISTING_LOCATION_SQL = """
INSERT INTO listing_location AS ll (
    listing_id, geom,
    country_code, kraj_name, okres_name, obec_name, cast_obce_name,
    street_name, house_number_cp, house_number_co, psc,
    kraj_kod, okres_kod, obec_kod, cast_obce_kod, ulice_kod, ruian_adm_kod,
    match_confidence, granularity, uncertainty_radius_m,
    country_status, disputed,
    resolver_version, resolved_at, claim_set_hash, registry_version, katastr_kod)
VALUES (
    %(listing_id)s,
    CASE WHEN %(lat)s::double precision IS NULL THEN NULL
         ELSE ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) END,
    %(country_code)s, %(kraj_name)s, %(okres_name)s, %(obec_name)s, %(cast_obce_name)s,
    %(street_name)s, %(house_number_cp)s, %(house_number_co)s, %(psc)s,
    %(kraj_kod)s, %(okres_kod)s, %(obec_kod)s, %(cast_obce_kod)s, %(ulice_kod)s,
    %(ruian_adm_kod)s,
    %(match_confidence)s::match_confidence, %(granularity)s::location_granularity,
    %(uncertainty_radius_m)s,
    %(country_status)s::country_status, %(disputed)s,
    %(resolver_version)s, now(), decode(%(claim_set_hash)s, 'hex'), %(registry_version)s,
    %(katastr_kod)s)
ON CONFLICT (listing_id) DO UPDATE SET
    geom = EXCLUDED.geom, country_code = EXCLUDED.country_code,
    kraj_name = EXCLUDED.kraj_name, okres_name = EXCLUDED.okres_name,
    obec_name = EXCLUDED.obec_name, cast_obce_name = EXCLUDED.cast_obce_name,
    street_name = EXCLUDED.street_name, house_number_cp = EXCLUDED.house_number_cp,
    house_number_co = EXCLUDED.house_number_co, psc = EXCLUDED.psc,
    kraj_kod = EXCLUDED.kraj_kod, okres_kod = EXCLUDED.okres_kod,
    obec_kod = EXCLUDED.obec_kod, cast_obce_kod = EXCLUDED.cast_obce_kod,
    ulice_kod = EXCLUDED.ulice_kod, ruian_adm_kod = EXCLUDED.ruian_adm_kod,
    match_confidence = EXCLUDED.match_confidence, granularity = EXCLUDED.granularity,
    uncertainty_radius_m = EXCLUDED.uncertainty_radius_m,
    country_status = EXCLUDED.country_status, disputed = EXCLUDED.disputed,
    resolver_version = EXCLUDED.resolver_version, resolved_at = now(),
    claim_set_hash = EXCLUDED.claim_set_hash, registry_version = EXCLUDED.registry_version,
    katastr_kod = EXCLUDED.katastr_kod
WHERE ll.listing_id = EXCLUDED.listing_id
"""


def current_registry_version(conn: psycopg.Connection) -> tuple[int, str]:
    with conn.cursor() as cur:
        cur.execute(_CURRENT_REGISTRY_SQL)
        row = cur.fetchone()
    if row is None:
        raise LookupError("no registry_versions row is is_current — load the mirror first")
    return int(row[0]), str(row[1])


def _claim(row: Sequence[Any]) -> Claim:
    return Claim(
        id=row[0], listing_id=row[1], source=row[2], claim_type=row[3], surface=row[4],
        extraction_method=row[5], licence_class=row[6],
        observed_at=row[7], value_text=row[8],
        value_num=None if row[9] is None else float(row[9]),
        lat=None if row[10] is None else float(row[10]),
        lon=None if row[11] is None else float(row[11]),
        value_jsonb=row[12] or {}, declared_precision_label=row[13],
        declared_radius_m=None if row[14] is None else float(row[14]),
        blur_evidence=row[15], claim_confidence=row[16], subject_scoped=row[17],
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


def sources_bulk(conn: psycopg.Connection, listing_ids: Sequence[int]) -> dict[int, str]:
    """`listings.source` for a whole slice — the one fact a claimless listing cannot read
    off a claim, because it has none."""
    if not listing_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(_SOURCES_BULK_SQL, (list(listing_ids),))
        return {int(r[0]): str(r[1]) for r in cur.fetchall()}


def _admin_unit(row: Sequence[Any], *, sole_katastr_kod: int | None = None) -> AdminUnit:
    path = str(row[5] or "")
    return AdminUnit(
        unit_id=int(row[0]), level=str(row[1]), code=int(row[2]), name=str(row[3]),
        name_norm=str(row[4]), path=path, parent_id=row[6],
        lat=None if row[7] is None else float(row[7]),
        lon=None if row[8] is None else float(row[8]),
        qualifier=row[9], homonym_count=int(row[10] or 1),
        psc_set=tuple(str(p).strip() for p in (row[11] or ())),
        obec_kod=_path_code(path, "b"), okres_kod=_path_code(path, "o"),
        kraj_kod=_path_code(path, "k"), sole_katastr_kod=sole_katastr_kod,
    )


def _chain_unit(row: Sequence[Any]) -> AdminUnit:
    """A chain row: the unit columns, then the obec's one KÚ child (NULL off an obec row)."""
    return _admin_unit(row[:12], sole_katastr_kod=row[12])


def _path_code(path: str, prefix: str) -> int | None:
    """The ltree path is `k{kraj}.o{okres}.b{obec}.c{cast_obce}` — level-prefixed NUMERIC
    codes, which is what makes this parse safe (a name label would not even insert)."""
    for label in path.split("."):
        if label.startswith(prefix) and label[1:].isdigit():
            return int(label[1:])
    return None


def _address_point(row: Sequence[Any]) -> AddressPoint:
    return AddressPoint(
        kod_adm=int(row[0]), obec_unit_id=int(row[1]), obec_kod=int(row[2]), psc=str(row[3]),
        lat=None if row[4] is None else float(row[4]),
        lon=None if row[5] is None else float(row[5]),
        ulice_kod=row[6], street_name_norm=row[7], street_name=row[8],
        cislo_domovni=row[9], cislo_orientacni=row[10], znak_orientacniho=row[11],
        cast_obce_unit_id=row[12], cast_obce_kod=row[13], katastr_kod=row[14],
    )


class QueryStats:
    """Per-query-KIND round trips and wall clock for one run — the run's own log names which
    question spent the budget, which an aggregate-only line could not."""

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
        rows = self._rows("address_point", _ADDRESS_POINT_SQL, (self._version, kod_adm))
        return _address_point(rows[0]) if rows else None

    def address_points_by_number(
        self, *, obec_kod: int, street_name_norm: str | None,
        cislo_domovni: int | None, cislo_orientacni: int | None,
    ) -> list[AddressPoint]:
        rows = self._rows(
            "address_points_by_number",
            _ADDRESS_POINTS_BY_NUMBER_SQL,
            (self._version, obec_kod, street_name_norm, street_name_norm, cislo_domovni,
             cislo_domovni, cislo_orientacni, cislo_orientacni),
        )
        return [_address_point(r) for r in rows]

    def streets_in_obec(self, obec_kod: int) -> list[Street]:
        return [
            Street(code=int(r[0]), name=str(r[1]), name_norm=str(r[2]), obec_kod=int(r[3]),
                   id=None if r[4] is None else int(r[4]))
            for r in self._rows("streets_in_obec", _STREETS_IN_OBEC_SQL, (obec_kod,))
        ]

    def street_point(self, street: Street) -> StreetPoint | None:
        """Asked for the WINNING street only (`bind._street_candidate` carries the row), so
        this is one round trip per listing that binds a street — and the run cache makes it
        one per STREET per run. A street with no valid address point has no position: the
        aggregate comes back with a NULL centroid and a zero count."""
        if street.id is None:
            return None
        rows = self._rows(
            "street_point", _STREET_POINT_SQL, (street.id, self._version, self._version)
        )
        if not rows or rows[0][0] is None or rows[0][1] is None:
            return None
        return StreetPoint(
            lat=float(rows[0][0]), lon=float(rows[0][1]),
            extent_m=float(rows[0][2] or 0.0), point_count=int(rows[0][3] or 0),
            katastr_kod=rows[0][4],
        )

    def part_katastr_kod(self, unit_id: int) -> int | None:
        rows = self._rows(
            "part_katastr_kod", _PART_KATASTR_SQL, (unit_id, self._version, self._version)
        )
        return int(rows[0][0]) if rows else None

    def admin_units_by_name(self, name_norm: str, *, levels: Sequence[str] = ()) -> list[AdminUnit]:
        wanted = list(levels)
        return [
            _admin_unit(r)
            for r in self._rows(
                "admin_units_by_name", _ADMIN_BY_NAME_SQL,
                (self._version, name_norm, wanted, wanted),
            )
        ]

    def admin_chain(self, unit_id: int) -> list[AdminUnit]:
        return [
            _chain_unit(r)
            for r in self._rows("admin_chain", _ADMIN_CHAIN_SQL, (unit_id, self._version))
        ]

    def admin_chain_by_code(self, level: str, code: int) -> list[AdminUnit]:
        return [
            _chain_unit(r)
            for r in self._rows(
                "admin_chain_by_code", _ADMIN_CHAIN_BY_CODE_SQL, (level, code, self._version)
            )
        ]

    def obec_codes_for_psc(self, psc: str) -> list[int]:
        return [int(r[0]) for r in self._rows("obec_codes_for_psc", _PSC_OBEC_SQL, (psc,))]

    # ---- the two point-keyed questions: ONE array-shaped statement, called here with one
    # point and by `warm_points` with the whole slice. `_bulk` returns {idx: answer}.

    def containing_obec_bulk(
        self, points: Sequence[tuple[float, float]]
    ) -> dict[int, AdminUnit]:
        if not points:
            return {}
        idx, lats, lons = _point_arrays(points)
        rows = self._rows(
            "containing_obec", CONTAINING_OBEC_SQL, (idx, lats, lons, self._version)
        )
        return {int(r[0]): _admin_unit(r[1:]) for r in rows}

    def containing_obec(self, lat: float, lon: float) -> AdminUnit | None:
        return self.containing_obec_bulk([(lat, lon)]).get(0)

    def nearest_obec_within(
        self, lat: float, lon: float, max_m: float
    ) -> tuple[AdminUnit, float] | None:
        """BIND's last rung, asked per point rather than per slice: it is reached only when
        `containing_obec` missed, which is ~1 % of listings."""
        idx, lats, lons = _point_arrays([(lat, lon)])
        rows = self._rows(
            "nearest_obec_within", _NEAREST_OBEC_SQL,
            (idx, lats, lons, self._version, max_m, max_m),
        )
        if not rows:
            return None
        return _admin_unit(rows[0][1:]), float(rows[0][13])

    def in_czechia_polygon_bulk(self, points: Sequence[tuple[float, float]]) -> dict[int, bool]:
        if not points:
            return {}
        idx, lats, lons = _point_arrays(points)
        rows = self._rows("in_czechia_polygon", _IN_CZ_SQL, (idx, lats, lons, self._version))
        return {int(r[0]): bool(r[1]) for r in rows if r[1] is not None}

    def in_czechia_polygon(self, lat: float, lon: float) -> bool | None:
        return self.in_czechia_polygon_bulk([(lat, lon)]).get(0)


def _point_arrays(
    points: Sequence[tuple[float, float]]
) -> tuple[list[int], list[float], list[float]]:
    return (
        list(range(len(points))),
        [float(p[0]) for p in points],
        [float(p[1]) for p in points],
    )


class RunCache:
    """Memo for the corpus-constant questions of ONE run, plus its own instrumentation.

    Safe because the mirror it fronts is immutable for the run's lifetime: it is pinned to a
    `registry_version_id`, and a load mints a NEW version rather than editing one. Same
    question, same answer — so the pure core sees exactly what an uncached run would.

    `max_entries` is a memory rail, not a hit-rate policy: at the cap the whole memo is
    dropped and refills. Correctness cannot depend on what is resident.

    SHARED BY THE DRAIN'S WORKER THREADS (W2-a5), so the dict is under a lock — but the lock
    is NEVER held across `compute()`. Holding it there would serialize the very round trips
    the workers exist to overlap; two threads racing the same key simply both ask the mirror
    and store the same immutable answer, which costs one extra trip and changes nothing.
    """

    __slots__ = ("_values", "_max", "_lock", "hits", "misses", "seconds", "asked_by_kind",
                 "missed_by_kind")

    def __init__(self, max_entries: int = 250_000) -> None:
        self._values: dict[Any, Any] = {}
        self._max = max_entries
        self._lock = threading.Lock()
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
        with self._lock:
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
        elapsed = time.perf_counter() - started
        with self._lock:
            self.seconds += elapsed
            self._store(key, value)
        return value

    def put(self, key: Any, value: Any) -> None:
        """Pre-seed an answer (`warm_points`). Identical to what `get` would have stored, so
        a warmed run and a cold one hand the core the same bytes."""
        with self._lock:
            self._store(key, value)

    def _store(self, key: Any, value: Any) -> None:
        """Caller holds the lock."""
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
    next listing's answer. It forwards by EXPLICIT method, not by `__getattr__`, so a
    question added to `RegistryView` and to `SqlRegistryView` but not here raises
    AttributeError mid-drain rather than quietly falling through.
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

    def street_point(self, street: Street) -> StreetPoint | None:
        """Keyed on the street's own id: one listing per street is the common case in a
        slice, but a whole town's worth of listings on one high street is the case that pays
        for the memo."""
        return self._cache.get(
            ("street_point", street.id, street.code),
            lambda: self._inner.street_point(street),
        )

    def street_index(self, obec_kod: int) -> dict[str, list[Street]]:
        """`composite.build_street_index` over this obec's streets, ONCE per run.

        Not a `RegistryView` question — it is a fold of an answer this view already caches,
        and `composite.street_index` falls back to building it for any view that does not
        offer it. It is here because the fold is two regex passes per street and Praha holds
        ~10,000 of them: a four-segment title paid for 40,000 passes without it.
        """
        return self._cache.get(
            ("street_index", obec_kod),
            lambda: composite.build_street_index(self.streets_in_obec(obec_kod)),
        )

    def part_katastr_kod(self, unit_id: int) -> int | None:
        return self._cache.get(
            ("part_katastr_kod", unit_id), lambda: self._inner.part_katastr_kod(unit_id)
        )

    def admin_units_by_name(
        self, name_norm: str, *, levels: Sequence[str] = ()
    ) -> Sequence[AdminUnit]:
        """Keyed on the NAME only, with the level narrowing applied in Python.

        The level tuple was part of the key, so one name asked four different ways was four
        cache entries and four round trips for one immutable answer. The unnarrowed query is
        the SUPERSET of every narrowing and its `ORDER BY u.level, u.code` survives a stable
        filter, so the narrowed list is element-for-element what the narrowed query returned.
        """
        wanted = frozenset(levels)
        every = self._cache.get(
            ("admin_units_by_name", name_norm),
            lambda: tuple(self._inner.admin_units_by_name(name_norm)),
        )
        if not wanted:
            return every
        return tuple(unit for unit in every if unit.level in wanted)

    def admin_chain(self, unit_id: int) -> Sequence[AdminUnit]:
        return self._cache.get(
            ("admin_chain", unit_id), lambda: tuple(self._inner.admin_chain(unit_id))
        )

    def admin_chain_by_code(self, level: str, code: int) -> Sequence[AdminUnit]:
        return self._cache.get(
            ("admin_chain_by_code", level, code),
            lambda: tuple(self._inner.admin_chain_by_code(level, code)),
        )

    def obec_codes_for_psc(self, psc: str) -> Sequence[int]:
        return self._cache.get(
            ("obec_codes_for_psc", psc), lambda: tuple(self._inner.obec_codes_for_psc(psc))
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

    def in_czechia_polygon(self, lat: float, lon: float) -> bool | None:
        return self._cache.get(
            ("in_czechia_polygon", lat, lon), lambda: self._inner.in_czechia_polygon(lat, lon)
        )


def warm_points(
    registry: SqlRegistryView, cache: RunCache, points: Sequence[tuple[float, float]]
) -> None:
    """Answer the TWO point-keyed questions for a WHOLE SLICE in two round trips.

    These are the questions the run cache can never share between listings — the key IS the
    listing's coordinate. Asking them ahead of the slice does not change a single answer:
    `warm_points` writes exactly the key/value `CachedRegistryView` would have computed
    lazily, from the SAME statement text (the `*_bulk` methods are the single-point methods
    with a longer array), so the pure core is handed identical bytes.

    A NULL answer is an answer: it is cached too, or every rural point would re-ask per call.
    """
    if not points:
        return
    coords = sorted(set(points))
    covering = registry.containing_obec_bulk(coords)
    for i, (lat, lon) in enumerate(coords):
        cache.put(("containing_obec", lat, lon), covering.get(i))
    in_cz = registry.in_czechia_polygon_bulk(coords)
    for i, (lat, lon) in enumerate(coords):
        cache.put(("in_czechia_polygon", lat, lon), in_cz.get(i))


def upsert_listing_locations_bulk(
    conn: psycopg.Connection, rows: Sequence[dict[str, Any]]
) -> None:
    """The slice's answer rows in ONE `executemany`, which psycopg pipelines into one round
    trip. It is the drain's ONLY projection write."""
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_LISTING_LOCATION_SQL, list(rows))
