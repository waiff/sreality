"""Location-quality dashboard reads (05 5.5.4) - the FIRST consumer of the
location serving projection.

Everything here reads `listing_location_current` (+ `pin_clusters`,
`registry_versions`, `location_claims_live`) and NOTHING else derives state:
the dashboard is "built entirely from the projection (no new instrumentation)".
Every payload states its grain ('listing') - a precision mix that silently
mixed grains would be upward-biased (05 5.5.4).

Service-role connection only: the location tables are RLS-on with anon /
authenticated revoked, and these queries are served through the admin-gated
API. Aggregates run under a SET LOCAL statement_timeout inside an explicit
transaction (connect() is autocommit, so a bare SET LOCAL would apply to
nothing).

Enum-ordinal comparisons (granularity >= 'street') are legal HERE - D3 forbids
ordinality only in persisted artefacts (index predicates, CHECKs, generated
columns), not in query predicates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

STATEMENT_TIMEOUT_S = 20

_PIN_BUCKETS_SQL = """
    SELECT CASE
             WHEN p.pin_shared_by_n <= 1 THEN '1'
             WHEN p.pin_shared_by_n = 2 THEN '2'
             WHEN p.pin_shared_by_n BETWEEN 3 AND 4 THEN '3-4'
             WHEN p.pin_shared_by_n BETWEEN 5 AND 9 THEN '5-9'
             WHEN p.pin_shared_by_n BETWEEN 10 AND 19 THEN '10-19'
             WHEN p.pin_shared_by_n BETWEEN 20 AND 49 THEN '20-49'
             ELSE '50+'
           END AS bucket,
           coalesce(p.pin_collision_class::text, 'none') AS collision_class,
           count(*) AS n
    FROM listing_location_current p
    JOIN listings l ON l.id = p.listing_id AND l.is_active
    WHERE p.source = %(source)s
    GROUP BY 1, 2
"""

_TOP_CLUSTERS_SQL = """
    SELECT pc.cell_key, pc.listing_count, pc.distinct_streets, pc.distinct_obec_kods,
           pc.classification::text, pc.declared_blur_share,
           u.name AS nearest_admin_unit
    FROM pin_clusters pc
    LEFT JOIN ruian_admin_units u ON u.id = pc.nearest_admin_unit_id
    WHERE pc.epoch_id = (SELECT max(id) FROM pin_cluster_epochs)
      AND pc.source = %(source)s
    ORDER BY pc.listing_count DESC
    LIMIT 12
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mix(conn: psycopg.Connection, source: str, column: str) -> list[dict[str, Any]]:
    # column is interpolated from a fixed allowlist only - never caller input.
    sql = f"""
        SELECT {column}::text AS value, count(*) AS n
        FROM listing_location_current p
        JOIN listings l ON l.id = p.listing_id AND l.is_active
        WHERE p.source = %(source)s
        GROUP BY 1 ORDER BY n DESC
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, {"source": source})
        return cur.fetchall()


_MIX_COLUMNS = (
    "granularity",
    "position_source",
    "match_confidence",
    "admin_assignment_method",
    "pin_collision_class",
    "country_code",
    "registry_version",
)


def source_overview(conn: psycopg.Connection, source: str) -> dict[str, Any]:
    with conn.transaction():
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_S}s'")

        mixes = {col: _mix(conn, source, col) for col in _MIX_COLUMNS}

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT count(*) AS active_rows,
                       count(*) FILTER (WHERE p.granularity >= 'street') AS street_or_better,
                       count(*) FILTER (WHERE p.granularity IN ('address_point','building'))
                           AS building_or_better,
                       count(*) FILTER (WHERE p.geo_blockable) AS geo_blockable,
                       count(*) FILTER (WHERE p.renderable_as_point) AS renderable_as_point,
                       count(*) FILTER (WHERE p.is_low_precision) AS low_precision,
                       count(*) FILTER (WHERE p.location_disputed) AS disputed,
                       count(*) FILTER (WHERE p.ruian_adm_kod IS NOT NULL) AS with_adm_kod,
                       count(*) FILTER (WHERE p.stavebni_objekt_kod IS NOT NULL)
                           AS with_stavebni_objekt,
                       count(*) FILTER (WHERE p.ulice_kod IS NOT NULL) AS with_ulice_kod,
                       count(*) FILTER (WHERE p.parcela_id IS NOT NULL) AS with_parcela
                FROM listing_location_current p
                JOIN listings l ON l.id = p.listing_id AND l.is_active
                WHERE p.source = %(source)s
                """,
                {"source": source},
            )
            totals = cur.fetchone()

            cur.execute(_PIN_BUCKETS_SQL, {"source": source})
            pin_histogram = cur.fetchall()

            cur.execute(_TOP_CLUSTERS_SQL, {"source": source})
            top_clusters = cur.fetchall()

            cur.execute(
                "SELECT label, loaded_at FROM registry_versions WHERE is_current"
            )
            current_registry = cur.fetchone()

    if current_registry and current_registry.get("loaded_at"):
        current_registry["loaded_at"] = current_registry["loaded_at"].isoformat()

    return {
        "data": {
            "source": source,
            "grain": "listing",
            "totals": totals,
            "mixes": mixes,
            "pin_histogram": pin_histogram,
            "top_clusters": top_clusters,
            "current_registry": current_registry,
        },
        "metadata": {
            "tool": "location_quality.source_overview",
            "queried_at": _utcnow(),
            "grain": "listing",
        },
    }


def corpus_summary(conn: psycopg.Connection) -> dict[str, Any]:
    with conn.transaction():
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_S}s'")
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT p.source,
                       count(*) AS active_rows,
                       count(*) FILTER (WHERE p.granularity >= 'street') AS street_or_better,
                       count(*) FILTER (WHERE p.granularity IN ('address_point','building'))
                           AS building_or_better,
                       count(*) FILTER (WHERE p.geo_blockable) AS geo_blockable,
                       count(*) FILTER (WHERE p.location_disputed) AS disputed,
                       count(*) FILTER (WHERE p.ruian_adm_kod IS NOT NULL) AS with_adm_kod
                FROM listing_location_current p
                JOIN listings l ON l.id = p.listing_id AND l.is_active
                GROUP BY p.source
                ORDER BY active_rows DESC
                """
            )
            rows = cur.fetchall()
    return {
        "data": {"grain": "listing", "sources": rows},
        "metadata": {
            "tool": "location_quality.corpus_summary",
            "queried_at": _utcnow(),
            "grain": "listing",
        },
    }


def w1v_gate(conn: psycopg.Connection) -> dict[str, Any]:
    """The W1v acceptance gate (06 6.4, wave W1v), measured live.

    Primary: >= 95 % of active bezrealitky rows carry a ruianId claim that
    resolves to exactly one current address point (kod_adm is the mirror PK,
    so the join is 0-or-1 by construction). Fallback: >= 90 % at
    address_point/building granularity through the matcher. Projection R0 is
    identified by match_confidence='exact' - R0 is the only rung that emits it.
    """
    with conn.transaction():
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_S}s'")
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                WITH active AS (
                  SELECT id FROM listings WHERE source = 'bezrealitky' AND is_active
                ), claim AS (
                  SELECT c.listing_id,
                         bool_or(ap.kod_adm IS NOT NULL) AS matched_one
                  FROM location_claims_live c
                  JOIN active a ON a.id = c.listing_id
                  LEFT JOIN ruian_address_points ap
                         ON ap.kod_adm = nullif(regexp_replace(c.value_text, '\\D', '', 'g'), '')::bigint
                        AND ap.valid_to IS NULL
                  WHERE c.source = 'bezrealitky' AND c.claim_type = 'address_point_id'
                  GROUP BY c.listing_id
                )
                SELECT
                  (SELECT count(*) FROM active) AS active_rows,
                  (SELECT count(*) FROM claim) AS with_ruian_claim,
                  (SELECT count(*) FROM claim WHERE matched_one) AS claim_matches_one_point,
                  (SELECT count(*)
                     FROM listing_location_current p JOIN active a ON a.id = p.listing_id
                    WHERE p.source = 'bezrealitky'
                      AND p.granularity = 'address_point'
                      AND p.position_source = 'registry_point'
                      AND p.match_confidence = 'exact') AS projection_r0,
                  (SELECT count(*)
                     FROM listing_location_current p JOIN active a ON a.id = p.listing_id
                    WHERE p.source = 'bezrealitky'
                      AND p.granularity IN ('address_point','building'))
                      AS projection_building_or_better
                """
            )
            row = cur.fetchone()

    active = row["active_rows"] or 0
    pct = lambda n: round(100.0 * n / active, 2) if active else None  # noqa: E731
    primary_pct = pct(row["claim_matches_one_point"])
    fallback_pct = pct(row["projection_building_or_better"])
    return {
        "data": {
            **row,
            "primary_pct": primary_pct,
            "fallback_pct": fallback_pct,
            "primary_pass": primary_pct is not None and primary_pct >= 95.0,
            "fallback_pass": fallback_pct is not None and fallback_pct >= 90.0,
            "grain": "listing",
        },
        "metadata": {
            "tool": "location_quality.w1v_gate",
            "queried_at": _utcnow(),
            "grain": "listing",
        },
    }


def listing_inspector(
    conn: psycopg.Connection,
    listing_id: int | None = None,
    source: str | None = None,
    native_id: str | None = None,
) -> dict[str, Any] | None:
    """One listing through the stack: projection row + live claims + the current
    resolution's candidate ladder. The read-your-writes surface (05 5.5.5)."""
    with conn.cursor(row_factory=dict_row) as cur:
        if listing_id is None:
            if not (source and native_id):
                return None
            cur.execute(
                "SELECT id FROM listings WHERE source = %s AND source_id_native = %s",
                (source, native_id),
            )
            hit = cur.fetchone()
            if hit is None:
                return None
            listing_id = hit["id"]

        cur.execute(
            """
            SELECT p.*, l.source AS listing_source, l.source_id_native, l.is_active
            FROM listings l
            LEFT JOIN listing_location_current p ON p.listing_id = l.id
            WHERE l.id = %s
            """,
            (listing_id,),
        )
        projection = cur.fetchone()
        if projection is None:
            return None
        # geometry is not JSON-serializable and the API shape rule (05 5.5.1)
        # forbids a bare coordinate without its precision object anyway; the
        # inspector serves the axes, not the point.
        projection.pop("geom", None)

        cur.execute(
            """
            SELECT id, claim_type::text, surface::text, extraction_method::text,
                   extractor_id, value_text, value_num, licence_class::text,
                   claim_confidence::text, blur_evidence::text,
                   first_observed_at, subject_scoped
            FROM location_claims_live
            WHERE listing_id = %s
            ORDER BY claim_type, first_observed_at DESC, id DESC
            LIMIT 200
            """,
            (listing_id,),
        )
        claims = cur.fetchall()

        candidates: list[dict[str, Any]] = []
        if projection.get("resolution_id") is not None:
            cur.execute(
                """
                SELECT rank, score, target_kind::text, granularity::text,
                       position_source::text, match_confidence::text,
                       component_match, distance_to_pin_m, rejected_reason
                FROM location_resolution_candidates
                WHERE resolution_id = %s
                ORDER BY rank
                LIMIT 25
                """,
                (projection["resolution_id"],),
            )
            candidates = cur.fetchall()

    for row in (projection, *claims):
        for key, val in list(row.items()):
            if isinstance(val, datetime):
                row[key] = val.isoformat()

    return {
        "data": {
            "listing_id": listing_id,
            "projection": projection,
            "claims": claims,
            "candidates": candidates,
        },
        "metadata": {
            "tool": "location_quality.listing_inspector",
            "queried_at": _utcnow(),
            "grain": "listing",
        },
    }
