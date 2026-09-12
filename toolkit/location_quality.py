"""Location-quality dashboard reads - the consumer of `listing_location`.

Everything here reads the ONE answer table (migration 501) plus `listings`,
`location_claims` and `registry_versions`, and NOTHING else derives state: the
dashboard is built entirely from the answer table, no new instrumentation.
Every payload states its grain ('listing').

W2-b cut this module over from `listing_location_current` and deleted the
panels whose producers went with it: the pin-collision class mix and the
shared-pin histogram (the collision epoch is gone, and the shared-pin count is a
read-time aggregate W3 computes in the browse_list rebuild, where the map needs
it), `position_source` / `admin_assignment_method` (columns the four-step
resolver does not emit), and the candidate ladder (`location_resolution_candidates`
is dropped - a row REPLAYS from the three version ids stamped on it instead).
The mixes are the two axes a consumer asks: `granularity` and `match_confidence`.

`listing_location` has no `source` column - it is one primary-key join away on
`listings`, which every statement here already joins for `is_active`.

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


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mix(conn: psycopg.Connection, source: str, column: str) -> list[dict[str, Any]]:
    # column is interpolated from a fixed allowlist only - never caller input.
    sql = f"""
        SELECT p.{column}::text AS value, count(*) AS n
        FROM listing_location p
        JOIN listings l ON l.id = p.listing_id AND l.is_active
        WHERE l.source = %(source)s
        GROUP BY 1 ORDER BY n DESC
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, {"source": source})
        return cur.fetchall()


_MIX_COLUMNS = ("granularity", "match_confidence")

# `with_obec_kod` is rule 25's invariant read per portal: every active Czech
# listing has a town. It replaces the four registry-key counters W1 carried
# (stavební objekt was never loaded, parcela was unreachable, and the other two
# collapse to "registry-bound" = ruian_adm_kod).
#
# `stale_registry` is the drain-lag readout that used to be a registry_version
# mix: one number instead of a GROUP BY, and it answers the only question that
# mix was asked - how far behind is the lane after a registry load. The
# `registry_versions` join cannot multiply rows: migration 381 declares a UNIQUE
# partial index on (is_current) WHERE is_current, so there is at most one match,
# and LEFT (not CROSS) so a database with none still counts every other column.
_TOTALS_SQL = """
    SELECT count(*) AS active_rows,
           count(*) FILTER (WHERE p.granularity >= 'street') AS street_or_better,
           count(*) FILTER (WHERE p.granularity IN ('address_point','building'))
               AS building_or_better,
           count(*) FILTER (WHERE p.obec_kod IS NOT NULL) AS with_obec_kod,
           count(*) FILTER (WHERE p.ruian_adm_kod IS NOT NULL) AS with_adm_kod,
           count(*) FILTER (WHERE p.ulice_kod IS NOT NULL) AS with_ulice_kod,
           count(*) FILTER (WHERE p.disputed IS NOT NULL) AS disputed,
           count(*) FILTER (WHERE p.registry_version IS DISTINCT FROM rv.label)
               AS stale_registry
    FROM listing_location p
    JOIN listings l ON l.id = p.listing_id AND l.is_active
    LEFT JOIN registry_versions rv ON rv.is_current
    WHERE l.source = %(source)s
"""

_CORPUS_SUMMARY_SQL = """
    SELECT l.source,
           count(*) AS active_rows,
           count(*) FILTER (WHERE p.granularity >= 'street') AS street_or_better,
           count(*) FILTER (WHERE p.granularity IN ('address_point','building'))
               AS building_or_better,
           count(*) FILTER (WHERE p.obec_kod IS NOT NULL) AS with_obec_kod,
           count(*) FILTER (WHERE p.disputed IS NOT NULL) AS disputed,
           count(*) FILTER (WHERE p.ruian_adm_kod IS NOT NULL) AS with_adm_kod
    FROM listing_location p
    JOIN listings l ON l.id = p.listing_id AND l.is_active
    GROUP BY l.source
    ORDER BY active_rows DESC
"""

_INSPECTOR_ROW_SQL = """
    SELECT p.*, l.source AS listing_source, l.source_id_native, l.is_active
    FROM listings l
    LEFT JOIN listing_location p ON p.listing_id = l.id
    WHERE l.id = %s
"""

_INSPECTOR_CLAIMS_SQL = """
    SELECT id, claim_type::text, surface::text, extraction_method::text,
           value_text, value_num, licence_class::text,
           claim_confidence::text, blur_evidence::text,
           first_observed_at, subject_scoped
    FROM location_claims
    WHERE listing_id = %s
    ORDER BY claim_type, first_observed_at DESC, id DESC
    LIMIT 200
"""


def source_overview(conn: psycopg.Connection, source: str) -> dict[str, Any]:
    with conn.transaction():
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_S}s'")

        mixes = {col: _mix(conn, source, col) for col in _MIX_COLUMNS}

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_TOTALS_SQL, {"source": source})
            totals = cur.fetchone()

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
            cur.execute(_CORPUS_SUMMARY_SQL)
            rows = cur.fetchall()
    return {
        "data": {"grain": "listing", "sources": rows},
        "metadata": {
            "tool": "location_quality.corpus_summary",
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
    """One listing through the stack: its answer row + the claims it was resolved
    from. The read-your-writes surface (05 5.5.5)."""
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

        cur.execute(_INSPECTOR_ROW_SQL, (listing_id,))
        projection = cur.fetchone()
        if projection is None:
            return None
        # geometry is not JSON-serializable and the API shape rule (05 5.5.1)
        # forbids a bare coordinate without its precision object anyway; the
        # inspector serves the axes, not the point.
        projection.pop("geom", None)

        cur.execute(_INSPECTOR_CLAIMS_SQL, (listing_id,))
        claims = cur.fetchall()

    for row in (projection, *claims):
        for key, val in list(row.items()):
            if isinstance(val, datetime):
                row[key] = val.isoformat()
            elif isinstance(val, (bytes, bytearray, memoryview)):
                # claim_set_hash is bytea: hex so the envelope stays JSON.
                row[key] = bytes(val).hex()

    return {
        "data": {
            "listing_id": listing_id,
            "projection": projection,
            "claims": claims,
        },
        "metadata": {
            "tool": "location_quality.listing_inspector",
            "queried_at": _utcnow(),
            "grain": "listing",
        },
    }
