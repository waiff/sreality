"""Old-vs-new location comparison reads (location W6, dark cutover review).

The platform has TWO location paths and this module is the only place that puts
them side by side:

  OLD  browse_list (property grain, what Browse reads today) - geom-derived
       admin ids region_id / okres_id / obec_id, a lat/lng pin, and the
       `place_search_text` the locality chip ILIKEs against.
  NEW  property_location_current p JOIN listing_location_current w ON
       w.listing_id = p.winner_listing_id - w carries the membership scalars
       (admin_assignment_method, uncertainty_radius_m,
       distance_to_nearest_boundary_m) that p does not.

Membership is decided by the ASSIGNMENT, never by geometry (05 5.3.3 A): two
stored scalars on the winner row, looked up. Radius search is the 05 5.3.3 (B)
pair of predicates on a single ST_DWithin prefilter - never ST_Buffer per row.
Render state is READ off the row (`render_as` / `renderable_as_point`), never
re-derived (05 5.2.2). Every aggregate publishes its denominator tuple (05
5.5.3): the operator sees what was counted and what was excluded, both sides.

A5 is decided: interactive filters return `certain UNION possible`, every
`possible` row badged - so `n_new_certain + n_new_possible` is the new side's
cohort and `verdict` is what the badge would say.

Read-only, service-role, admin-gated at the router: the location tables are
RLS-on with anon/authenticated revoked.

SQL SHAPE. Every module-level `*_SQL` constant is complete, PREPARE-able SQL
with only %(name)s placeholders - CI PREPAREs them against the replayed schema.
The unit level is a BOUND PARAMETER inside CASE expressions, not string
interpolation, so there is no dynamic SQL anywhere in this module; the Python
allowlist exists to 400 junk before it reaches the database, not to build SQL.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

STATEMENT_TIMEOUT_S = 30

DEFAULT_KRAJE = (19, 27)

LEVELS = ("kraj", "okres", "obec", "cast_obce", "street")
UNIT_LIST_LEVELS = ("obec", "cast_obce")

# Legacy Browse converts centre+radius to a bounding box on lat/lng rather than
# a circle (frontend/src/lib/queries.ts centerRadiusBbox); the old side of the
# radius comparison has to reproduce that, not approximate it.
EARTH_RADIUS_M = 6_371_000.0

MIN_RADIUS_M = 50
MAX_RADIUS_M = 50_000


class CompareInputError(ValueError):
    """Caller input the API layer must turn into a 400."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def center_radius_bbox(lat: float, lng: float, radius_m: float) -> dict[str, float]:
    """Port of frontend/src/lib/queries.ts centerRadiusBbox - the LEGACY radius.

    Pinned by test_location_compare so the two can never drift: the old side of
    the radius comparison is only honest if it is the same square Browse sends.
    """
    d_lat = (radius_m / EARTH_RADIUS_M) * (180.0 / math.pi)
    d_lng = (radius_m / (EARTH_RADIUS_M * math.cos(lat * math.pi / 180.0))) * (
        180.0 / math.pi
    )
    return {
        "south": lat - d_lat,
        "north": lat + d_lat,
        "west": lng - d_lng,
        "east": lng + d_lng,
    }


def parse_kraje(raw: str | None) -> list[int]:
    if raw is None or not raw.strip():
        return list(DEFAULT_KRAJE)
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError as exc:
            raise CompareInputError(f"kraje: {part!r} is not an integer") from exc
    if not out:
        raise CompareInputError("kraje must name at least one kraj_kod")
    return out


def check_level(level: str, *, allowed: tuple[str, ...] = LEVELS) -> str:
    if level not in allowed:
        raise CompareInputError(f"level must be one of {', '.join(allowed)}")
    return level


# --------------------------------------------------------------------------
# SQL fragments. None of these names end in _SQL: they are not runnable on
# their own, and the SQL corpus must only ever PREPARE the assembled constants.
# --------------------------------------------------------------------------

# 05 5.3.3 (A): the verdict is a lookup on two stored scalars, and it applies to
# the assigned obec AND every ancestor - distance_to_nearest_boundary_m is
# measured against the OBEC boundary only, which is exactly why an okres/kraj
# question inherits the obec answer instead of asking its own geometry.
def _admin_verdict(alias: str) -> str:
    return f"""
             CASE
               WHEN {alias}.admin_assignment_method IN ('registry', 'claimed')
                 THEN 'certain'
               WHEN {alias}.admin_assignment_method = 'pip_containment'
                 THEN CASE
                        WHEN {alias}.distance_to_nearest_boundary_m IS NOT NULL
                             AND {alias}.uncertainty_radius_m
                                 <= {alias}.distance_to_nearest_boundary_m
                        THEN 'certain' ELSE 'possible'
                      END
               WHEN {alias}.admin_assignment_method = 'pip_nearest_within_n_m'
                 THEN 'possible'
               ELSE 'no'
             END"""


_CURRENT_UNITS_CTE = """
    cur_units AS (
      SELECT DISTINCT ON (u.level, u.code)
             u.id, u.level, u.code, u.name, u.parent_id
      FROM ruian_admin_units u
      WHERE u.retired_at IS NULL
      ORDER BY u.level, u.code, (u.valid_to IS NULL) DESC, u.valid_from DESC
    )"""

# The cohort is the UNION of the two sides' opinions about the selected kraje,
# so a property one side places inside and the other outside stays visible as a
# disagreement instead of silently leaving the denominator.
_COHORT_CTE = """
    cohort AS (
      SELECT b.property_id,
             b.listing_id,
             b.source,
             b.lat AS old_lat,
             b.lng AS old_lng,
             b.region_id AS old_region_id,
             b.okres_id AS old_okres_id,
             b.obec_id AS old_obec_id,
             coalesce(b.place_search_text, concat_ws(', ', b.obec, b.okres)) AS old_label,
             (w.listing_id IS NOT NULL) AS has_row,
             w.kraj_kod AS new_kraj_kod,
             w.okres_kod AS new_okres_kod,
             w.obec_kod AS new_obec_kod,
             w.cast_obce_kod AS new_cast_obce_kod,
             w.ulice_kod AS new_ulice_kod,
             w.display_label AS new_label,
             w.geom AS new_geom,
             w.granularity::text AS granularity,
             w.match_confidence::text AS match_confidence,
             w.admin_assignment_method::text AS admin_assignment_method,
             w.position_source::text AS position_source,
             w.uncertainty_radius_m::double precision AS uncertainty_radius_m,
             w.distance_to_nearest_boundary_m::double precision
               AS distance_to_nearest_boundary_m,
             w.renderable_as_point,
             w.render_as,
             w.pin_collision_class,
             w.location_disputed,
             gr.rank AS granularity_rank,
             p.member_spread_m::double precision AS member_spread_m,
             p.disagreement_flags,
             p.member_count
      FROM browse_list b
      LEFT JOIN property_location_current p ON p.property_id = b.property_id
      LEFT JOIN listing_location_current w ON w.listing_id = p.winner_listing_id
      LEFT JOIN location_granularity_rank gr ON gr.granularity = w.granularity
      WHERE b.is_active
        AND (b.region_id = ANY(%(kraje)s::bigint[])
             OR p.kraj_kod = ANY(%(kraje)s::bigint[]))
    )"""

# The method verdict, level-independent: 'no_row' when the new engine has no
# opinion at all, otherwise the (A) lookup on the assignment.
_METHOD_VERDICT_CTE = f"""
    mv AS (
      SELECT c.*,
             CASE WHEN NOT c.has_row THEN 'no_row'
                  ELSE {_admin_verdict('c')}
             END AS mverdict
      FROM cohort c
    )"""

# The unit-scoped view. `level` is a BOUND PARAM, never interpolated. The
# cast_obce / street old sides have no legacy id column at all, so they use
# Browse's own locality-chip semantics: the containing obec id AND an ILIKE on
# place_search_text (queries.ts districtsFilterClause, the `locality` branch).
_SCOPED_CTE = """
    scoped AS (
      SELECT c.*,
             coalesce(CASE %(level)s::text
               WHEN 'kraj'      THEN c.old_region_id = %(code)s::bigint
               WHEN 'okres'     THEN c.old_okres_id = %(code)s::bigint
               WHEN 'obec'      THEN c.old_obec_id = %(code)s::bigint
               WHEN 'cast_obce' THEN c.old_obec_id = %(parent_obec_kod)s::bigint
                                     AND c.old_label ILIKE %(name_pat)s
               WHEN 'street'    THEN c.old_obec_id = %(parent_obec_kod)s::bigint
                                     AND c.old_label ILIKE %(name_pat)s
               ELSE false
             END, false) AS old_in,
             coalesce(CASE %(level)s::text
               WHEN 'kraj'      THEN c.new_kraj_kod = %(code)s::bigint
               WHEN 'okres'     THEN c.new_okres_kod = %(code)s::bigint
               WHEN 'obec'      THEN c.new_obec_kod = %(code)s::bigint
               WHEN 'cast_obce' THEN c.new_cast_obce_kod = %(code)s::bigint
               WHEN 'street'    THEN c.new_ulice_kod = %(code)s::bigint
               ELSE false
             END, false) AS new_in
      FROM cohort c
    )"""

# cast obce is certain only for registry/claimed (05 5.3.3 A); street ranks the
# granularity through the location_granularity_rank TABLE, never enum order.
_VERDICT_CTE = f"""
    verdicts AS (
      SELECT s.*,
             CASE
               WHEN NOT s.has_row THEN 'no_row'
               WHEN NOT s.new_in THEN 'no'
               WHEN %(level)s::text = 'street' THEN
                 CASE WHEN s.granularity_rank >= (SELECT rank
                                                  FROM location_granularity_rank
                                                  WHERE granularity = 'street')
                           AND s.match_confidence IN ('medium', 'high', 'exact')
                      THEN 'certain'
                      WHEN s.match_confidence = 'low' THEN 'possible'
                      ELSE 'no' END
               WHEN %(level)s::text = 'cast_obce' THEN
                 CASE
                   WHEN s.admin_assignment_method IN ('registry', 'claimed')
                     THEN 'certain'
                   WHEN s.admin_assignment_method
                        IN ('pip_containment', 'pip_nearest_within_n_m')
                     THEN 'possible'
                   ELSE 'no'
                 END
               ELSE {_admin_verdict('s')}
             END AS verdict
      FROM scoped s
    )"""

_REASON = """
             CASE
               WHEN v.verdict = 'no_row' THEN 'no_projection_row'
               WHEN v.admin_assignment_method = 'outside_country'
                 THEN 'outside_country'
               WHEN v.admin_assignment_method
                    IN ('unresolved', 'unresolved_sliver') THEN 'unresolved'
               WHEN v.verdict = 'possible' THEN 'possible_only'
               WHEN NOT v.old_in THEN 'moved_in'
               ELSE 'assigned_elsewhere'
             END"""

_ROW_COLUMNS = """
             v.property_id, v.listing_id, v.source,
             v.old_label, v.new_label,
             v.granularity, v.match_confidence, v.admin_assignment_method,
             v.uncertainty_radius_m, v.distance_to_nearest_boundary_m,
             v.verdict"""


# --------------------------------------------------------------------------
# Assembled, PREPARE-able statements.
# --------------------------------------------------------------------------

_SCOPE_KRAJE_SQL = f"""
WITH{_COHORT_CTE},{_METHOD_VERDICT_CTE},
     k AS (SELECT unnest(%(kraje)s::bigint[]) AS kod)
SELECT k.kod AS kraj_kod,
       u.name AS name,
       count(*) FILTER (WHERE m.old_region_id = k.kod) AS n_old,
       count(*) FILTER (WHERE m.new_kraj_kod = k.kod
                          AND m.mverdict = 'certain') AS n_new_certain,
       count(*) FILTER (WHERE m.new_kraj_kod = k.kod
                          AND m.mverdict = 'possible') AS n_new_possible,
       count(*) FILTER (WHERE m.old_region_id = k.kod AND m.has_row
                          AND (m.new_kraj_kod IS DISTINCT FROM k.kod
                               OR m.mverdict = 'no')) AS n_new_no,
       count(*) FILTER (WHERE m.old_region_id = k.kod
                          AND NOT m.has_row) AS n_no_row,
       count(*) FILTER (WHERE m.old_region_id = k.kod
                          AND (NOT m.has_row
                               OR m.new_kraj_kod IS DISTINCT FROM k.kod
                               OR m.mverdict = 'no')) AS n_only_old,
       count(*) FILTER (WHERE m.new_kraj_kod = k.kod
                          AND m.mverdict IN ('certain', 'possible')
                          AND m.old_region_id IS DISTINCT FROM k.kod) AS n_only_new,
       count(*) FILTER (WHERE m.old_region_id = k.kod
                          AND m.new_kraj_kod = k.kod
                          AND m.mverdict IN ('certain', 'possible')) AS n_agree
FROM k
LEFT JOIN mv m ON m.old_region_id = k.kod OR m.new_kraj_kod = k.kod
LEFT JOIN LATERAL (
    SELECT ru.name FROM ruian_admin_units ru
    WHERE ru.level = 'kraj' AND ru.code = k.kod AND ru.retired_at IS NULL
    ORDER BY (ru.valid_to IS NULL) DESC, ru.valid_from DESC LIMIT 1
) u ON true
GROUP BY k.kod, u.name
ORDER BY k.kod
"""

_SCOPE_OKRESY_SQL = f"""
WITH{_CURRENT_UNITS_CTE},{_COHORT_CTE},{_METHOD_VERDICT_CTE},
     o AS (
       SELECT DISTINCT kod FROM (
         SELECT old_okres_id AS kod FROM cohort WHERE old_okres_id IS NOT NULL
         UNION
         SELECT new_okres_kod FROM cohort WHERE new_okres_kod IS NOT NULL
       ) t
     )
SELECT o.kod AS okres_kod,
       ku.code AS kraj_kod,
       ou.name AS name,
       count(*) FILTER (WHERE m.old_okres_id = o.kod) AS n_old,
       count(*) FILTER (WHERE m.new_okres_kod = o.kod
                          AND m.mverdict = 'certain') AS n_new_certain,
       count(*) FILTER (WHERE m.new_okres_kod = o.kod
                          AND m.mverdict = 'possible') AS n_new_possible,
       count(*) FILTER (WHERE m.old_okres_id = o.kod AND m.has_row
                          AND (m.new_okres_kod IS DISTINCT FROM o.kod
                               OR m.mverdict = 'no')) AS n_new_no,
       count(*) FILTER (WHERE m.old_okres_id = o.kod
                          AND NOT m.has_row) AS n_no_row,
       count(*) FILTER (WHERE m.old_okres_id = o.kod
                          AND (NOT m.has_row
                               OR m.new_okres_kod IS DISTINCT FROM o.kod
                               OR m.mverdict = 'no')) AS n_only_old,
       count(*) FILTER (WHERE m.new_okres_kod = o.kod
                          AND m.mverdict IN ('certain', 'possible')
                          AND m.old_okres_id IS DISTINCT FROM o.kod) AS n_only_new,
       count(*) FILTER (WHERE m.old_okres_id = o.kod
                          AND m.new_okres_kod = o.kod
                          AND m.mverdict IN ('certain', 'possible')) AS n_agree
FROM o
LEFT JOIN mv m ON m.old_okres_id = o.kod OR m.new_okres_kod = o.kod
LEFT JOIN cur_units ou ON ou.level = 'okres' AND ou.code = o.kod
LEFT JOIN ruian_admin_units ku ON ku.id = ou.parent_id
GROUP BY o.kod, ku.code, ou.name
ORDER BY n_old DESC, o.kod
"""

_SCOPE_BY_METHOD_SQL = f"""
WITH{_COHORT_CTE}
SELECT coalesce(c.admin_assignment_method, 'no_row') AS admin_assignment_method,
       count(*) AS n
FROM cohort c
GROUP BY 1
ORDER BY n DESC
"""

_SCOPE_BY_SOURCE_SQL = f"""
WITH{_COHORT_CTE},{_METHOD_VERDICT_CTE}
SELECT m.source AS source,
       count(*) FILTER (WHERE m.old_region_id = ANY(%(kraje)s::bigint[]))
         AS n_old,
       count(*) FILTER (WHERE m.new_kraj_kod = ANY(%(kraje)s::bigint[])
                          AND m.mverdict = 'certain') AS n_new_certain,
       count(*) FILTER (WHERE m.new_kraj_kod = ANY(%(kraje)s::bigint[])
                          AND m.mverdict = 'possible') AS n_new_possible,
       count(*) FILTER (WHERE NOT m.has_row) AS n_no_row
FROM mv m
GROUP BY m.source
ORDER BY n_old DESC, m.source
"""

_UNITS_OBEC_SQL = f"""
WITH{_CURRENT_UNITS_CTE},{_COHORT_CTE},{_METHOD_VERDICT_CTE},
     codes AS (
       SELECT DISTINCT kod FROM (
         SELECT old_obec_id AS kod FROM mv
         WHERE old_okres_id = %(parent_kod)s::bigint AND old_obec_id IS NOT NULL
         UNION
         SELECT new_obec_kod FROM mv
         WHERE new_okres_kod = %(parent_kod)s::bigint AND new_obec_kod IS NOT NULL
       ) t
     )
SELECT codes.kod AS code,
       u.name AS name,
       count(*) FILTER (WHERE m.old_obec_id = codes.kod) AS n_old,
       count(*) FILTER (WHERE m.new_obec_kod = codes.kod
                          AND m.mverdict = 'certain') AS n_new_certain,
       count(*) FILTER (WHERE m.new_obec_kod = codes.kod
                          AND m.mverdict = 'possible') AS n_new_possible,
       count(*) FILTER (WHERE m.old_obec_id = codes.kod
                          AND (NOT m.has_row
                               OR m.new_obec_kod IS DISTINCT FROM codes.kod
                               OR m.mverdict = 'no')) AS n_only_old,
       count(*) FILTER (WHERE m.new_obec_kod = codes.kod
                          AND m.mverdict IN ('certain', 'possible')
                          AND m.old_obec_id IS DISTINCT FROM codes.kod) AS n_only_new
FROM codes
LEFT JOIN mv m ON m.old_obec_id = codes.kod OR m.new_obec_kod = codes.kod
LEFT JOIN cur_units u ON u.level = 'obec' AND u.code = codes.kod
GROUP BY codes.kod, u.name
ORDER BY n_old DESC, codes.kod
"""

# The legacy read model has no cast-obce id at all, so the parts are enumerated
# from the registry and the old side is the locality-chip ILIKE.
#
# The ILIKE runs against DISTINCT (old_label, new state) GROUPS, never against
# every cohort row: Praha is ~110 parts x tens of thousands of rows, and pairing
# those directly is millions of ILIKEs inside a 30s statement timeout, while the
# grouped relation collapses to a few thousand rows before the parts join.
_UNITS_CAST_OBCE_SQL = f"""
WITH{_CURRENT_UNITS_CTE},{_COHORT_CTE},{_METHOD_VERDICT_CTE},
     parent AS (
       SELECT a.id FROM ruian_admin_units a
       WHERE a.level = 'obec' AND a.code = %(parent_kod)s::bigint
     ),
     parts AS (
       SELECT DISTINCT cu.code, cu.name
       FROM cur_units cu
       WHERE cu.level = 'cast_obce'
         AND cu.parent_id IN (SELECT id FROM parent)
     ),
     grp AS (
       SELECT CASE WHEN mv.old_obec_id = %(parent_kod)s::bigint
                   THEN mv.old_label END AS old_label,
              mv.new_cast_obce_kod,
              mv.mverdict,
              mv.admin_assignment_method,
              count(*) AS n
       FROM mv
       WHERE mv.old_obec_id = %(parent_kod)s::bigint
          OR mv.new_obec_kod = %(parent_kod)s::bigint
       GROUP BY 1, 2, 3, 4
     )
SELECT parts.code AS code,
       parts.name AS name,
       coalesce(sum(m.n) FILTER (WHERE m.old_match), 0)::bigint AS n_old,
       coalesce(sum(m.n) FILTER (WHERE m.new_cast_obce_kod = parts.code
                          AND m.mverdict = 'certain'
                          AND m.admin_assignment_method
                              IN ('registry', 'claimed')), 0)::bigint AS n_new_certain,
       coalesce(sum(m.n) FILTER (WHERE m.new_cast_obce_kod = parts.code
                          AND (m.mverdict = 'possible'
                               OR (m.mverdict = 'certain'
                                   AND m.admin_assignment_method
                                       NOT IN ('registry', 'claimed')))), 0)::bigint
         AS n_new_possible,
       coalesce(sum(m.n) FILTER (WHERE m.old_match
                          AND (m.mverdict = 'no_row'
                               OR m.new_cast_obce_kod IS DISTINCT FROM parts.code
                               OR m.mverdict = 'no')), 0)::bigint AS n_only_old,
       coalesce(sum(m.n) FILTER (WHERE m.new_cast_obce_kod = parts.code
                          AND m.mverdict IN ('certain', 'possible')
                          AND NOT m.old_match), 0)::bigint AS n_only_new
FROM parts
LEFT JOIN LATERAL (
    SELECT grp.n, grp.new_cast_obce_kod, grp.mverdict, grp.admin_assignment_method,
           coalesce(grp.old_label ILIKE '%%' || parts.name || '%%', false) AS old_match
    FROM grp
    WHERE grp.new_cast_obce_kod = parts.code
       OR grp.old_label ILIKE '%%' || parts.name || '%%'
) m ON true
GROUP BY parts.code, parts.name
ORDER BY n_old DESC, parts.code
"""

_UNIT_COUNTS_SQL = f"""
WITH{_COHORT_CTE},{_SCOPED_CTE},{_VERDICT_CTE}
SELECT
  (SELECT count(*) FROM verdicts WHERE old_in) AS n_old,
  (SELECT count(*) FROM verdicts WHERE new_in AND verdict = 'certain')
    AS n_new_certain,
  (SELECT count(*) FROM verdicts WHERE new_in AND verdict = 'possible')
    AS n_new_possible,
  (SELECT count(*) FROM verdicts WHERE old_in AND has_row AND verdict = 'no')
    AS n_new_no,
  (SELECT count(*) FROM verdicts WHERE old_in AND NOT has_row) AS n_no_row,
  (SELECT count(*) FROM verdicts WHERE old_in AND verdict IN ('no', 'no_row'))
    AS n_only_old,
  (SELECT count(*) FROM verdicts
    WHERE NOT old_in AND verdict IN ('certain', 'possible')) AS n_only_new,
  (SELECT count(*) FROM verdicts
    WHERE (old_in OR new_in) AND admin_assignment_method = 'claimed') AS n_claimed,
  (SELECT coalesce(jsonb_agg(jsonb_build_object(
             'admin_assignment_method', q.mm, 'n', q.n) ORDER BY q.n DESC),
           '[]'::jsonb)
     FROM (SELECT coalesce(admin_assignment_method, 'no_row') AS mm, count(*) AS n
             FROM verdicts WHERE old_in OR new_in GROUP BY 1) q) AS by_method,
  (SELECT coalesce(jsonb_agg(jsonb_build_object(
             'source', q.source, 'n_old', q.n_old,
             'n_new_certain', q.n_new_certain,
             'n_new_possible', q.n_new_possible,
             'n_no_row', q.n_no_row) ORDER BY q.n_old DESC), '[]'::jsonb)
     FROM (SELECT source,
                  count(*) FILTER (WHERE old_in) AS n_old,
                  count(*) FILTER (WHERE new_in AND verdict = 'certain')
                    AS n_new_certain,
                  count(*) FILTER (WHERE new_in AND verdict = 'possible')
                    AS n_new_possible,
                  count(*) FILTER (WHERE old_in AND NOT has_row) AS n_no_row
             FROM verdicts WHERE old_in OR new_in GROUP BY source) q) AS by_source
"""

_UNIT_ROWS_SQL = f"""
WITH{_COHORT_CTE},{_SCOPED_CTE},{_VERDICT_CTE}
(SELECT 'only_old' AS side,{_ROW_COLUMNS},{_REASON} AS reason
   FROM verdicts v
  WHERE v.old_in AND v.verdict IN ('no', 'no_row')
  ORDER BY v.property_id LIMIT %(limit)s)
UNION ALL
(SELECT 'only_new' AS side,{_ROW_COLUMNS},{_REASON} AS reason
   FROM verdicts v
  WHERE NOT v.old_in AND v.verdict IN ('certain', 'possible')
  ORDER BY v.property_id LIMIT %(limit)s)
"""

_UNIT_NAME_SQL = """
WITH u AS (
  SELECT DISTINCT ON (a.level, a.code) a.id, a.code, a.name, a.parent_id
  FROM ruian_admin_units a
  WHERE a.level = %(level)s::ruian_level AND a.code = %(code)s::bigint
    AND a.retired_at IS NULL
  ORDER BY a.level, a.code, (a.valid_to IS NULL) DESC, a.valid_from DESC
)
SELECT u.code, u.name, pu.code AS parent_code, pu.level::text AS parent_level
FROM u LEFT JOIN ruian_admin_units pu ON pu.id = u.parent_id
"""

# The CZ bounding box has ONE canonical definition (migration 380
# location_constants.cz_bbox); restating the numbers here would make a seventh
# copy, and a narrower one would 400 legitimate points near the border.
_CZ_BBOX_SQL = """
SELECT ST_XMin(value_geom) AS west, ST_YMin(value_geom) AS south,
       ST_XMax(value_geom) AS east, ST_YMax(value_geom) AS north
FROM location_constants
WHERE name = %(name)s::text AND value_geom IS NOT NULL
"""

CZ_BBOX_CONSTANT = "cz_bbox"

_STREET_LOOKUP_SQL = """
SELECT DISTINCT ON (s.code) s.code AS ulice_kod, s.name, u.code AS obec_kod
FROM ruian_streets s
JOIN ruian_admin_units u ON u.id = s.obec_unit_id
WHERE s.code = %(code)s::bigint
ORDER BY s.code, (s.valid_to IS NULL) DESC, s.valid_from DESC
"""

# name_norm is written by ruian_name_norm() (migration 381); normalizing the
# needle with the same function is the only way the two can't drift.
_STREETS_SQL = """
WITH obec AS (
       SELECT a.id FROM ruian_admin_units a
       WHERE a.level = 'obec' AND a.code = %(obec_kod)s::bigint
     ),
     cur_streets AS (
       SELECT DISTINCT ON (s.code) s.code, s.name, s.name_norm
       FROM ruian_streets s
       WHERE s.obec_unit_id IN (SELECT id FROM obec)
       ORDER BY s.code, (s.valid_to IS NULL) DESC, s.valid_from DESC
     )
SELECT s.code AS ulice_kod, s.name, %(obec_kod)s::bigint AS obec_kod
FROM cur_streets s
WHERE %(q)s::text IS NULL
   OR s.name_norm LIKE '%%' || ruian_name_norm(%(q)s::text) || '%%'
ORDER BY s.name
LIMIT %(limit)s
"""

_MAP_BOX_CTE = """
    box AS (
      SELECT c.*,
             CASE WHEN c.old_lat IS NOT NULL AND c.old_lng IS NOT NULL
                       AND c.new_geom IS NOT NULL
                  THEN ST_Distance(
                         ST_SetSRID(ST_MakePoint(c.old_lng, c.old_lat), 4326)::geography,
                         c.new_geom::geography)
             END AS delta_m,
             (c.old_lat IS NOT NULL AND c.old_lng IS NOT NULL) AS has_old_geom,
             (c.new_geom IS NOT NULL) AS has_new_geom
      FROM cohort c
      WHERE (c.old_lat BETWEEN %(south)s::double precision AND %(north)s::double precision
             AND c.old_lng BETWEEN %(west)s::double precision AND %(east)s::double precision)
         OR (c.new_geom IS NOT NULL
             AND ST_Intersects(c.new_geom,
                   ST_MakeEnvelope(%(west)s::double precision, %(south)s::double precision, %(east)s::double precision, %(north)s::double precision, 4326)))
    )"""

_MAP_ROWS_SQL = f"""
WITH{_COHORT_CTE},{_MAP_BOX_CTE}
SELECT box.property_id, box.listing_id, box.source,
       box.old_lat, box.old_lng,
       ST_Y(box.new_geom) AS new_lat, ST_X(box.new_geom) AS new_lng,
       box.render_as, box.renderable_as_point, box.uncertainty_radius_m,
       box.granularity, box.match_confidence, box.admin_assignment_method,
       box.pin_collision_class, box.location_disputed, box.disagreement_flags,
       box.member_spread_m, box.delta_m
FROM box
ORDER BY box.delta_m DESC NULLS LAST, box.property_id
LIMIT %(limit)s
"""

_MAP_COUNTS_SQL = f"""
WITH{_COHORT_CTE},{_MAP_BOX_CTE}
SELECT count(*) AS n_total,
       count(*) FILTER (WHERE has_old_geom AND has_new_geom) AS both,
       count(*) FILTER (WHERE has_old_geom AND NOT has_new_geom) AS only_old_geom,
       count(*) FILTER (WHERE has_new_geom AND NOT has_old_geom) AS only_new_geom,
       count(*) FILTER (WHERE delta_m > 100) AS moved_gt_100m,
       count(*) FILTER (WHERE has_old_geom AND has_row
                          AND NOT renderable_as_point) AS demoted_to_circle,
       -- Cohort-wide, NOT box-wide: a row with no position on either side can
       -- never satisfy the bbox test, so counting it inside `box` would publish
       -- a constant zero for the one 05 5.5.3 bucket that matters most here.
       (SELECT count(*) FROM cohort c
         WHERE (c.old_lat IS NULL OR c.old_lng IS NULL)
           AND c.new_geom IS NULL) AS no_geom_either
FROM box
"""

_MAX_UNCERTAINTY_SQL = f"""
WITH{_COHORT_CTE}
SELECT coalesce(max(c.uncertainty_radius_m), 0)::double precision AS max_u
FROM cohort c
"""

# 05 5.3.3 (B) verbatim: ONE ST_DWithin prefilter, then the two arithmetic
# predicates on the same distance. Never ST_Buffer per row.
_RADIUS_CTE = """
    r AS (
      SELECT c.*,
             coalesce(c.old_lat BETWEEN %(south)s::double precision AND %(north)s::double precision
                      AND c.old_lng BETWEEN %(west)s::double precision AND %(east)s::double precision, false)
               AS old_in_bbox,
             CASE WHEN c.new_geom IS NULL THEN NULL
                  ELSE ST_Distance(c.new_geom::geography,
                         ST_SetSRID(ST_MakePoint(%(lng)s::double precision, %(lat)s::double precision), 4326)::geography)
             END AS dist_m
      FROM cohort c
      WHERE (c.old_lat BETWEEN %(south)s::double precision AND %(north)s::double precision
             AND c.old_lng BETWEEN %(west)s::double precision AND %(east)s::double precision)
         OR (c.new_geom IS NOT NULL
             AND ST_DWithin(c.new_geom::geography,
                   ST_SetSRID(ST_MakePoint(%(lng)s::double precision, %(lat)s::double precision), 4326)::geography,
                   %(prefilter_m)s::double precision))
    ),
    rv AS (
      SELECT r.*,
             CASE
               WHEN r.dist_m IS NULL THEN 'no'
               WHEN r.dist_m + coalesce(r.uncertainty_radius_m, 0) <= %(radius_m)s::double precision
                 THEN 'certain'
               WHEN r.dist_m - coalesce(r.uncertainty_radius_m, 0) <= %(radius_m)s::double precision
                 THEN 'possible'
               ELSE 'no'
             END AS verdict,
             CASE WHEN r.old_lat IS NOT NULL AND r.old_lng IS NOT NULL
                       AND r.new_geom IS NOT NULL
                  THEN ST_Distance(
                         ST_SetSRID(ST_MakePoint(r.old_lng, r.old_lat), 4326)::geography,
                         r.new_geom::geography)
             END AS delta_m
      FROM r
    )"""

_RADIUS_COUNTS_SQL = f"""
WITH{_COHORT_CTE},{_RADIUS_CTE}
SELECT count(*) FILTER (WHERE rv.old_in_bbox) AS old_bbox_count,
       count(*) FILTER (WHERE rv.verdict = 'certain') AS new_certain,
       count(*) FILTER (WHERE rv.verdict = 'possible') AS new_possible
FROM rv
"""

_RADIUS_ROWS_SQL = f"""
WITH{_COHORT_CTE},{_RADIUS_CTE}
(SELECT 'only_old' AS side, rv.property_id, rv.listing_id, rv.source,
        rv.delta_m, NULL::text AS verdict,
        NULL::double precision AS uncertainty_radius_m
   FROM rv
  WHERE rv.old_in_bbox AND rv.verdict NOT IN ('certain', 'possible')
  ORDER BY rv.property_id LIMIT %(limit)s)
UNION ALL
(SELECT 'only_new' AS side, rv.property_id, rv.listing_id, rv.source,
        rv.delta_m, rv.verdict, rv.uncertainty_radius_m
   FROM rv
  WHERE rv.verdict IN ('certain', 'possible') AND NOT rv.old_in_bbox
  ORDER BY rv.property_id LIMIT %(limit)s)
"""


# --------------------------------------------------------------------------
# Reads.
# --------------------------------------------------------------------------


def _envelope(kraje: list[int], **payload: Any) -> dict[str, Any]:
    return {"generated_at": _utcnow(), "kraje": kraje, **payload}


def _pct(agree: int, n_old: int) -> float | None:
    return round(100.0 * agree / n_old, 2) if n_old else None


def _timeout(conn: psycopg.Connection) -> None:
    conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_S}s'")


def _query(
    conn: psycopg.Connection, sql: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def cz_bbox(conn: psycopg.Connection) -> dict[str, float]:
    rows = _query(conn, _CZ_BBOX_SQL, {"name": CZ_BBOX_CONSTANT})
    if not rows:
        raise RuntimeError(
            f"location_constants has no {CZ_BBOX_CONSTANT} geometry (migration 380)"
        )
    return {k: float(v) for k, v in rows[0].items()}


def scope(conn: psycopg.Connection, kraje: list[int]) -> dict[str, Any]:
    params = {"kraje": list(kraje)}
    with conn.transaction():
        _timeout(conn)
        kraj_rows = _query(conn, _SCOPE_KRAJE_SQL, params)
        okres_rows = _query(conn, _SCOPE_OKRESY_SQL, params)
        by_method = _query(conn, _SCOPE_BY_METHOD_SQL, params)
        by_source = _query(conn, _SCOPE_BY_SOURCE_SQL, params)

    for row in (*kraj_rows, *okres_rows):
        row["agreement_pct"] = _pct(row.pop("n_agree"), row["n_old"])

    return _envelope(
        list(kraje),
        kraje_rows=kraj_rows,
        okresy=okres_rows,
        by_method=by_method,
        by_source=by_source,
    )


def units(
    conn: psycopg.Connection, *, level: str, parent_kod: int, kraje: list[int]
) -> dict[str, Any]:
    check_level(level, allowed=UNIT_LIST_LEVELS)
    params = {"kraje": list(kraje), "parent_kod": parent_kod}
    sql = _UNITS_OBEC_SQL if level == "obec" else _UNITS_CAST_OBCE_SQL
    with conn.transaction():
        _timeout(conn)
        rows = _query(conn, sql, params)
    return _envelope(list(kraje), level=level, parent_kod=parent_kod, rows=rows)


def _resolve_unit(
    conn: psycopg.Connection, *, level: str, code: int
) -> tuple[str | None, int | None, str | None]:
    """Return (name, parent_obec_kod, name_pat) for the unit under comparison.

    parent_obec_kod / name_pat are the legacy locality-chip inputs and are only
    populated for the two levels the legacy read model cannot address by id.
    """
    if level == "street":
        rows = _query(conn, _STREET_LOOKUP_SQL, {"code": code})
        if not rows:
            raise CompareInputError(f"no current ruian_streets row for code {code}")
        row = rows[0]
        return row["name"], row["obec_kod"], f"%{row['name']}%"
    rows = _query(conn, _UNIT_NAME_SQL, {"level": level, "code": code})
    # The two levels the legacy side can only address by NAME cannot fall back to
    # an all-false old side: n_old = 0 would read as "legacy has nothing here"
    # rather than as the bad code it is.
    if not rows:
        if level == "cast_obce":
            raise CompareInputError(f"no current cast_obce unit for code {code}")
        return None, None, None
    row = rows[0]
    if level == "cast_obce":
        parent = row["parent_code"] if row["parent_level"] == "obec" else None
        if parent is None:
            raise CompareInputError(
                f"cast_obce {code} has no current obec parent to scope the old side"
            )
        return row["name"], parent, f"%{row['name']}%"
    return row["name"], None, None


def unit_detail(
    conn: psycopg.Connection, *, level: str, code: int, kraje: list[int], limit: int
) -> dict[str, Any]:
    check_level(level)
    with conn.transaction():
        _timeout(conn)
        name, parent_obec_kod, name_pat = _resolve_unit(conn, level=level, code=code)
        params = {
            "kraje": list(kraje),
            "level": level,
            "code": code,
            "parent_obec_kod": parent_obec_kod,
            "name_pat": name_pat,
            "limit": limit,
        }
        counts = _query(conn, _UNIT_COUNTS_SQL, params)[0]
        rows = _query(conn, _UNIT_ROWS_SQL, params)

    by_method = counts.pop("by_method")
    by_source = counts.pop("by_source")
    only_old = [r for r in rows if r["side"] == "only_old"]
    only_new = [r for r in rows if r["side"] == "only_new"]
    for row in rows:
        row.pop("side")

    return _envelope(
        list(kraje),
        level=level,
        code=code,
        name=name,
        counts=counts,
        by_method=by_method,
        by_source=by_source,
        only_old=only_old,
        only_new=only_new,
    )


def streets(
    conn: psycopg.Connection, *, obec_kod: int, q: str | None, limit: int
) -> dict[str, Any]:
    with conn.transaction():
        _timeout(conn)
        rows = _query(
            conn, _STREETS_SQL, {"obec_kod": obec_kod, "q": q or None, "limit": limit}
        )
    return {"generated_at": _utcnow(), "obec_kod": obec_kod, "rows": rows}


def map_rows(
    conn: psycopg.Connection,
    *,
    west: float,
    south: float,
    east: float,
    north: float,
    kraje: list[int],
    limit: int,
) -> dict[str, Any]:
    params = {
        "kraje": list(kraje),
        "west": west,
        "south": south,
        "east": east,
        "north": north,
        "limit": limit,
    }
    with conn.transaction():
        _timeout(conn)
        counts = _query(conn, _MAP_COUNTS_SQL, params)[0]
        rows = _query(conn, _MAP_ROWS_SQL, params)
    n_total = counts.pop("n_total")
    return _envelope(
        list(kraje),
        rows=rows,
        truncated=n_total > len(rows),
        counts=counts,
        bbox={"west": west, "south": south, "east": east, "north": north},
    )


def radius(
    conn: psycopg.Connection,
    *,
    lat: float,
    lng: float,
    radius_m: float,
    kraje: list[int],
    limit: int,
) -> dict[str, Any]:
    if not MIN_RADIUS_M <= radius_m <= MAX_RADIUS_M:
        raise CompareInputError(
            f"radius_m must be between {MIN_RADIUS_M} and {MAX_RADIUS_M}"
        )

    bbox = center_radius_bbox(lat, lng, radius_m)
    with conn.transaction():
        _timeout(conn)
        cz = cz_bbox(conn)
        if not (cz["south"] <= lat <= cz["north"]):
            raise CompareInputError("lat is outside the CZ bounding box")
        if not (cz["west"] <= lng <= cz["east"]):
            raise CompareInputError("lng is outside the CZ bounding box")
        max_u = _query(conn, _MAX_UNCERTAINTY_SQL, {"kraje": list(kraje)})[0]["max_u"]
        params = {
            "kraje": list(kraje),
            "lat": lat,
            "lng": lng,
            "radius_m": radius_m,
            "prefilter_m": float(radius_m) + float(max_u or 0),
            "limit": limit,
            **bbox,
        }
        counts = _query(conn, _RADIUS_COUNTS_SQL, params)[0]
        rows = _query(conn, _RADIUS_ROWS_SQL, params)

    only_old = [r for r in rows if r["side"] == "only_old"]
    only_new = [r for r in rows if r["side"] == "only_new"]
    for row in rows:
        row.pop("side")

    return _envelope(
        list(kraje),
        centre={"lat": lat, "lng": lng, "radius_m": radius_m},
        old_bbox=bbox,
        max_uncertainty_radius_m=float(max_u or 0),
        old_bbox_count=counts["old_bbox_count"],
        new_certain=counts["new_certain"],
        new_possible=counts["new_possible"],
        only_old=only_old,
        only_new=only_new,
    )
