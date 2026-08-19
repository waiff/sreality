"""W3 gate verifier — read-only.

The W3 gate has four arms. Three of them are properties of the extractor and are
already held by unit tests: the eight portals whose snapshot hash excluded location
are stamped `history_completeness='locality_text_only'`, the licence ladder is applied
at the input so a Mapy-derived coordinate never becomes a claim, and the scan is
bounded (per-batch commit, keyset pagination, an attempt row for every candidate
including negatives).

The fourth arm is a property of the CORPUS, not of the code: a sreality per-listing
precision/coordinate time series exists, and oscillation is visible in it. Nothing in
the test suite can show that — only the written claims can. Hence this module.

It writes nothing and takes no lease. A verifier must be runnable while a backfill
window is in flight, and one that mutates its own subject is not a verifier. For the
same reason its workflow sits in its own concurrency group rather than the heavy
`location-batch` one: a read-only aggregate is not a heavy lane, and queueing it behind
the lane it audits would make it unrunnable.

Exits 0 when every arm passes, 1 when any arm fails, 2 on a refusal (schema absent,
no W3 claims to verify).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

import psycopg

from location_data import loader_db
from location_data.claims_intake import IntakeRefused, guarded, missing_relations
from location_data.claims_remine import (
    COORDINATE_HISTORY_SOURCES,
    LANE,
    REMINE_VERSION,
    W3_HISTORY_COMPLETENESS,
)
from scraper import db

LOG = logging.getLogger("location_data.claims_remine_verify")

STATEMENT_TIMEOUT_ENV = "LOCATION_REMINE_VERIFY_TIMEOUT_S"
DEFAULT_STATEMENT_TIMEOUT_S = 300
DEFAULT_SAMPLE_LISTINGS = 5

# The time-series arm is stated over the two claim types that carry sreality's geometry
# and its self-declared precision. Both are mined only for sreality (design 06 section
# 6.2.2 read against scraper.scraped_listing._HASH_FIELDS: the other eight portals'
# snapshot hash excluded lat/lon, so their snapshots carry no coordinate to mine).
SERIES_CLAIM_TYPES = ("coordinate", "precision_declaration")

# Every claim this lane wrote, and nothing else. `snapshot_anchor` alone would be too
# wide if another wave ever anchors to snapshots; the extractor version pins it to W3.
# The pattern is BOUND rather than inlined: a literal wildcard in executed SQL is the
# one thing psycopg's parameter interpolation reliably trips over.
_VERSION_LIKE = f"{REMINE_VERSION.split('@')[0]}@%"
_W3_CLAIMS = """
      c.extractor_version LIKE %(version_like)s
  AND c.snapshot_anchor = 'snapshot'
"""

_COVERAGE_SQL = f"""
    SELECT c.source, c.claim_type::text, count(*) AS claims,
           count(DISTINCT c.listing_id) AS listings,
           min(c.first_observed_at) AS earliest,
           max(c.first_observed_at) AS latest
    FROM location_claims c
    WHERE {_W3_CLAIMS}
    GROUP BY 1, 2
    ORDER BY 1, 2
"""

_COMPLETENESS_SQL = f"""
    SELECT c.source, c.history_completeness, count(*) AS claims
    FROM location_claims c
    WHERE {_W3_CLAIMS}
    GROUP BY 1, 2
    ORDER BY 1, 2
"""

_LICENCE_SQL = f"""
    SELECT c.licence_class::text, c.claim_type::text, c.source, count(*) AS claims
    FROM location_claims c
    WHERE {_W3_CLAIMS}
    GROUP BY 1, 2, 3
    ORDER BY 4 DESC
"""

# Oscillation, defined on the observation series rather than on claim counts. The claim
# fingerprint is time-free, so one claim row IS one distinct value and its observations
# are the times that value was seen. Compress the per-listing series into runs of a
# constant value: `changes` is how many times the value moved, and `returns` counts a
# value that starts more than one run — a value that came back after being replaced,
# which is oscillation proper rather than a one-time correction.
_SERIES_SQL = f"""
    WITH obs AS (
        SELECT c.listing_id, o.claim_id, o.observed_at, o.seq
        FROM location_claims c
        JOIN location_claim_observations o ON o.claim_id = c.id
        WHERE {_W3_CLAIMS}
          AND c.source = %(source)s
          AND c.claim_type = %(claim_type)s
    ),
    ordered AS (
        SELECT listing_id, claim_id,
               lag(claim_id) OVER (PARTITION BY listing_id ORDER BY observed_at, seq)
                 AS prev_claim
        FROM obs
    ),
    run_starts AS (
        SELECT listing_id, claim_id
        FROM ordered
        WHERE prev_claim IS NULL OR prev_claim <> claim_id
    ),
    per_listing AS (
        SELECT listing_id,
               count(*) - 1 AS changes,
               count(*) - count(DISTINCT claim_id) AS returns
        FROM run_starts
        GROUP BY listing_id
    )
    SELECT count(*) AS listings,
           count(*) FILTER (WHERE changes >= 1) AS listings_changed,
           count(*) FILTER (WHERE changes >= 2) AS listings_changed_twice,
           count(*) FILTER (WHERE returns >= 1) AS listings_returned_to_a_prior_value,
           coalesce(max(changes), 0) AS max_changes
    FROM per_listing
"""

# One readable series per sampled listing, so the arm can be eyeballed and not merely
# counted. Ordered by how much the value moved, because a flat series proves nothing.
_SAMPLE_SQL = f"""
    WITH obs AS (
        SELECT c.listing_id, o.claim_id, o.observed_at, o.seq,
               coalesce(ST_AsText(c.value_geom), c.value_norm, c.value_text) AS value
        FROM location_claims c
        JOIN location_claim_observations o ON o.claim_id = c.id
        WHERE {_W3_CLAIMS}
          AND c.source = %(source)s
          AND c.claim_type = %(claim_type)s
    ),
    picked AS (
        SELECT listing_id
        FROM obs
        GROUP BY listing_id
        HAVING count(DISTINCT claim_id) >= 2
        ORDER BY count(DISTINCT claim_id) DESC, listing_id
        LIMIT %(limit)s
    )
    SELECT o.listing_id, o.observed_at, o.claim_id, o.value
    FROM obs o
    JOIN picked p ON p.listing_id = o.listing_id
    ORDER BY o.listing_id, o.observed_at, o.seq
"""

_BATCHES_SQL = """
    SELECT id, source, scan_mode, outcome, row_count, cursor_after_id,
           started_at, finished_at
    FROM location_claim_batches
    WHERE lane = %(lane)s
    ORDER BY id DESC
    LIMIT %(limit)s
"""


def _rows(cur: psycopg.Cursor, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
    cur.execute(sql, {"version_like": _VERSION_LIKE, **(params or {})})
    cols = [d.name for d in cur.description or []]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def check_completeness(rows: list[dict]) -> dict[str, Any]:
    """Arm: the eight non-coordinate portals are stamped locality_text_only."""
    seen: dict[str, set[str]] = {}
    for row in rows:
        seen.setdefault(row["source"], set()).add(row["history_completeness"])
    wrong = {
        source: sorted(v for v in values if v != W3_HISTORY_COMPLETENESS.get(source))
        for source, values in seen.items()
    }
    wrong = {s: v for s, v in wrong.items() if v}
    return {
        "passed": not wrong,
        "by_source": {s: sorted(v) for s, v in sorted(seen.items())},
        "unexpected": wrong,
    }


def check_licence(rows: list[dict]) -> dict[str, Any]:
    """Arm: the licence ladder held at the input.

    Two ways it could have failed. A class E value reaching the table at all is the
    direct failure. A coordinate claim from a portal whose snapshots carry no
    coordinate is the indirect one: the only place such a value could have come from
    is the Mapy-derived geometry on the live row, which is exactly what the ladder
    exists to keep out of an append-only table.
    """
    ephemeral = [r for r in rows if r["licence_class"] == "ephemeral_display_only"]
    off_source = [
        r for r in rows
        if r["claim_type"] == "coordinate" and r["source"] not in COORDINATE_HISTORY_SOURCES
    ]
    return {
        "passed": not ephemeral and not off_source,
        "ephemeral_display_only_claims": sum(r["claims"] for r in ephemeral),
        "coordinate_claims_from_non_coordinate_portals": [
            {"source": r["source"], "claims": r["claims"]} for r in off_source
        ],
        "by_licence_class": sorted(
            {r["licence_class"] for r in rows}
        ),
    }


def check_series(series: dict[str, dict]) -> dict[str, Any]:
    """Arm: the series exists AND oscillation is visible in it.

    Existence alone is not the gate. A corpus where every listing reports one value
    forever would satisfy "a time series exists" and show nothing, so the arm passes
    only when at least one listing's value actually moved.
    """
    changed = sum(s["listings_changed"] for s in series.values())
    returned = sum(s["listings_returned_to_a_prior_value"] for s in series.values())
    listings = sum(s["listings"] for s in series.values())
    return {
        "passed": listings > 0 and changed > 0,
        "listings_with_a_series": listings,
        "listings_whose_value_changed": changed,
        "listings_that_returned_to_a_prior_value": returned,
        "by_claim_type": series,
    }


def verify(
    conn: psycopg.Connection,
    *,
    source: str,
    statement_timeout: int,
    sample_listings: int,
) -> dict[str, Any]:
    missing = missing_relations(conn)
    if missing:
        raise IntakeRefused(
            f"location schema not applied; missing {', '.join(missing)} "
            f"(migrations 380-389)")

    report: dict[str, Any] = {"extractor_version": REMINE_VERSION, "series_source": source}

    with guarded(conn, statement_timeout) as cur:
        coverage = _rows(cur, _COVERAGE_SQL)
        if not coverage:
            raise IntakeRefused(
                "no snapshot-anchored claims from this lane yet: dispatch "
                "location_claims_remine.yml before verifying its gate")
        report["coverage"] = coverage
        report["claims_total"] = sum(r["claims"] for r in coverage)

        report["history_completeness"] = check_completeness(_rows(cur, _COMPLETENESS_SQL))
        report["licence_ladder"] = check_licence(_rows(cur, _LICENCE_SQL))

        series: dict[str, dict] = {}
        samples: dict[str, list] = {}
        for claim_type in SERIES_CLAIM_TYPES:
            params = {"source": source, "claim_type": claim_type}
            got = _rows(cur, _SERIES_SQL, params)
            series[claim_type] = got[0] if got else {}
            if sample_listings > 0:
                samples[claim_type] = _rows(
                    cur, _SAMPLE_SQL, {**params, "limit": sample_listings})
        report["time_series"] = check_series(
            {k: v for k, v in series.items() if v})
        report["samples"] = samples

        batches = _rows(cur, _BATCHES_SQL, {"lane": LANE, "limit": 20})
        report["batches"] = batches
        report["reached_end"] = any(
            b["outcome"] == "ok" and b["scan_mode"] == "full" for b in batches)

    arms = ("history_completeness", "licence_ladder", "time_series")
    report["arms_passed"] = {a: bool(report[a]["passed"]) for a in arms}
    report["passed"] = all(report["arms_passed"].values())
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="sreality",
                        help="portal whose time series is checked (default sreality)")
    parser.add_argument("--sample-listings", type=int, default=DEFAULT_SAMPLE_LISTINGS)
    parser.add_argument(
        "--statement-timeout", type=int,
        default=loader_db.env_timeout_s(STATEMENT_TIMEOUT_ENV, DEFAULT_STATEMENT_TIMEOUT_S))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    with db.connect() as conn:
        try:
            report = verify(
                conn, source=args.source, statement_timeout=args.statement_timeout,
                sample_listings=args.sample_listings)
        except IntakeRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2

    print(json.dumps(report, default=str, indent=2, sort_keys=True))
    for arm, ok in report["arms_passed"].items():
        LOG.info("W3 GATE %s %s", "PASS" if ok else "FAIL", arm)
    LOG.info("W3 GATE %s (reached_end=%s)",
             "PASS" if report["passed"] else "FAIL", report["reached_end"])
    return 0 if report["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
