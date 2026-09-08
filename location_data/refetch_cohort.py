"""Location W4: the consumer for the refetch cohort W1 has been filling since 2026-08-12.

`location_enrichment_state` has a producer (`claims_intake` enrolls a sreality row whose
payload is legacy-shape or truncated) and, until this module, no consumer — while
migration 384 shipped `attempts` / `last_attempt_at` / `last_outcome` / `given_up` /
`next_eligible_at` AND the partial index `les_due (lane, next_eligible_at) where not
given_up`, keyed exactly the way a work-claiming driver reads it. Half a mechanism.

Two consequences of that gap, both fixed here, and both worth stating because a reader
who assumes the table is current-state will mis-write W4's gate:

1. **Nothing ever leaves the cohort.** No code path DELETEs from the table, and a task is
   emitted only while `sreality_payload_shape() != 'post_cutover'` — so the moment a
   refetch succeeds the producer simply stops emitting and the row sits there forever,
   stale, still reading `last_outcome='skipped'`. The cohort is a HIGH-WATER MARK. Never
   compute "legacy share" from it: it cannot go down, so W4 could not pass its own gate by
   succeeding. `reconcile()` is what turns it into a current-state set.
2. **Every row is permanently DUE.** `_ENRICHMENT_WRITE_SQL`'s DO UPDATE is gated
   `WHERE input_hash IS DISTINCT FROM EXCLUDED.input_hash`, so re-seeing an UNCHANGED
   legacy payload no-ops entirely and `next_eligible_at` stays frozen at the
   `now() + 6 hours` it got on first sight. A driver reading `les_due` would re-claim the
   whole cohort on every pass rather than once per window. `mark_dispatched()` is the only
   thing that advances it.

The refetch itself routes through the existing bounded detail drain at `priority = -1`
(06 §6.4: "route through the existing bounded drain rather than a bespoke crawler"), the
W1v pattern — strictly behind real-time discovery, so a 38k-row cohort cannot starve live
ingest. This module never fetches a page itself.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg

from location_data.claims_intake import guarded, sreality_payload_shape
from location_data.resolver import lease
from scraper import db

LOG = logging.getLogger("location_data.refetch_cohort")

# Deliberately NOT spelled `LANE`. That name is reserved for `location_claim_batches.lane`
# — the resume-cursor identity `test_lane_identifiers.py` polices, after the W3 erratum
# found one lane string assigned to two waves. This module stamps no batch row and holds no
# resume cursor; its lane is `location_enrichment_state.lane`, a different column and a
# different namespace, shared on purpose with the producer that fills it.
COHORT_LANE = "sreality_detail_refetch"

# W4(b). bezrealitky's live GraphQL query has requested `ruianId` since W0 item 0m
# (2026-08-10); a row whose payload lacks the KEY outright predates that and is a
# re-FETCH order. A row carrying the key with a `null` value is the portal withholding
# it — the ~48 % ceiling W1v measured — and a refetch returns the same null, so the key's
# presence, not its value, is what "placed" means on this lane. No producer fills it:
# `enroll()` selects the remainder from `listings` directly, idempotently.
BEZREALITKY_LANE = "bezrealitky_ruian_refetch"
ENROLL_VERSION = "refetch_cohort@1"

# `claims_intake.sreality_payload_shape` restated as ONE exhaustive SQL CASE, for the two
# readers that must classify in SQL (the W4 gate report's full-corpus scan and the
# payload-shape drift check). Post-cutover tested first; `IS DISTINCT FROM 'object'`
# because `jsonb_typeof(NULL)` is NULL and a plain `<>` silently drops the truncated rows;
# `ELSE 'absent'` for the classifier's second absent arm. `test_refetch_cohort` and
# `test_location_w4_gate_report` both feed every key named here through the Python
# function, key by key, so the two cannot drift.
SREALITY_SHAPE_CASE_SQL = """
    CASE
      WHEN jsonb_typeof(raw_json->'locality') IS DISTINCT FROM 'object' THEN 'absent'
      WHEN raw_json->'locality' ?| array['gps_lat','gps_lon','entity_type','inaccuracy_type','city','citypart'] THEN 'post_cutover'
      WHEN raw_json->'locality' ?| array['name','value','accuracy'] THEN 'legacy'
      ELSE 'absent'
    END
"""

# The lease-row CAS, never an advisory lock — the transaction-mode pooler strands one. It
# is a second, orthogonal guard to the workflow's concurrency group: it also catches a
# manual local invocation racing the dispatched run, which a GitHub-only group cannot see.
JOB_NAME = "location_refetch_cohort"
CONCURRENCY_GROUP = "location-refetch-cohort"

# Strictly behind real-time discovery. The claim order is (priority DESC, enqueued_at ASC),
# so a negative priority means the cohort drains only out of genuine slack.
REFETCH_PRIORITY = -1

# A refetch that has not flipped the payload shape after this many attempts is a portal
# fact, not a transient failure — the row is retired rather than re-queued forever.
MAX_ATTEMPTS = 5

# How long a dispatched row waits before it is eligible again. Matches the producer's own
# first-sight window so the two halves schedule on one clock.
RETRY_BACKOFF_HOURS = 6

DEFAULT_BATCH_SIZE = 500
DEFAULT_DISPATCH_LIMIT = 5_000

STATEMENT_TIMEOUT_ENV = "LOCATION_REFETCH_TIMEOUT_S"
DEFAULT_STATEMENT_TIMEOUT_S = 300


@dataclass(frozen=True)
class DueRow:
    listing_id: int
    source: str
    source_id_native: str
    attempts: int


@dataclass(frozen=True)
class CohortRow:
    """A cohort member as reconcile sees it. `probe` is the lane's projected probe
    expression ONLY (`raw_json->'locality'` on sreality, `raw_json ? 'ruianId'` on
    bezrealitky) — never the whole payload, which on sreality carries the geometry blob
    that made these rows truncated in the first place."""

    listing_id: int
    is_active: bool
    attempts: int
    probe: Any


@dataclass(frozen=True)
class LaneSpec:
    """What makes a cohort lane: which portal, the scan that projects its probe per row,
    when a row is placed, and — for a lane with no producer — how to enroll its cohort
    (plus the count of what enroll WOULD insert, so a dry run can size the work)."""

    lane: str
    source: str
    scan_sql: str
    placed: Callable[[Any], bool]
    enroll_sql: str | None = None
    enroll_count_sql: str | None = None


def _sreality_placed(probe: Any) -> bool:
    return sreality_payload_shape({"locality": probe}) == "post_cutover"


def _bezrealitky_placed(probe: Any) -> bool:
    return probe is True


# One complete, PREPARE-able statement per lane — never a template with a token in it.
# The schema-replay CI gate discovers every module-level `*_SQL` constant and PREPAREs it
# against the live catalog; a placeholder token is a 42703 there, and the concrete forms
# built inside a function body would be outside the only check that compiles SQL.
# Reconcile scans the whole non-retired cohort, including rows that are NOT due: a row
# retired here is one the driver never has to claim again, and `is_active` / payload shape
# both change out from under this table without touching it. `l.source` is pinned so a
# row of the wrong portal in a lane is never read with the other portal's probe.
_COHORT_SCAN_SREALITY_SQL = """
    SELECT es.listing_id, l.is_active, es.attempts, l.raw_json->'locality'
    FROM location_enrichment_state es
    JOIN listings l ON l.id = es.listing_id
    WHERE es.lane = %(lane)s
      AND l.source = %(source)s
      AND NOT es.given_up
      AND es.listing_id > %(after_id)s
    ORDER BY es.listing_id
    LIMIT %(batch_size)s
"""

_COHORT_SCAN_BEZREALITKY_SQL = """
    SELECT es.listing_id, l.is_active, es.attempts, (l.raw_json ? 'ruianId')
    FROM location_enrichment_state es
    JOIN listings l ON l.id = es.listing_id
    WHERE es.lane = %(lane)s
      AND l.source = %(source)s
      AND NOT es.given_up
      AND es.listing_id > %(after_id)s
    ORDER BY es.listing_id
    LIMIT %(batch_size)s
"""

_ENROLL_BEZREALITKY_SQL = """
    INSERT INTO location_enrichment_state
        (listing_id, method, lane, attempts, last_outcome, last_error,
         extractor_version, next_eligible_at)
    SELECT l.id, 'portal_structured_field'::location_extraction_method, %(lane)s, 0,
           'skipped', 'ruianId key absent: pre-0m payload shape, a re-fetch order (W4b)',
           %(extractor_version)s, now()
    FROM listings l
    WHERE l.source = 'bezrealitky' AND l.is_active AND NOT (l.raw_json ? 'ruianId')
    ON CONFLICT (listing_id, method, lane) DO NOTHING
"""

_ENROLL_BEZREALITKY_COUNT_SQL = """
    SELECT count(*)
    FROM listings l
    WHERE l.source = 'bezrealitky' AND l.is_active AND NOT (l.raw_json ? 'ruianId')
      AND NOT EXISTS (
        SELECT 1 FROM location_enrichment_state es
        WHERE es.listing_id = l.id
          AND es.method = 'portal_structured_field'::location_extraction_method
          AND es.lane = %(lane)s)
"""

# A row retired `not_applicable` was delisted at scan time. A sighting reactivates a
# listing (rule #3), and nothing else clears `given_up`: the producer's DO UPDATE never
# touches it and enroll's DO NOTHING cannot reach an existing row. So reconcile re-arms
# exactly that case — active again, still `not_applicable` — and no other: an `error`
# retirement (MAX_ATTEMPTS refetches without a flip) is a portal fact and stays retired.
_REARM_REACTIVATED_SQL = """
    UPDATE location_enrichment_state es
    SET given_up = false, attempts = 0, last_outcome = 'skipped', last_error = NULL,
        next_eligible_at = now()
    FROM listings l
    WHERE l.id = es.listing_id
      AND es.lane = %(lane)s
      AND l.source = %(source)s
      AND es.given_up
      AND es.last_outcome = 'not_applicable'
      AND l.is_active
"""

LANES: dict[str, LaneSpec] = {
    COHORT_LANE: LaneSpec(COHORT_LANE, "sreality", _COHORT_SCAN_SREALITY_SQL, _sreality_placed),
    BEZREALITKY_LANE: LaneSpec(BEZREALITKY_LANE, "bezrealitky", _COHORT_SCAN_BEZREALITKY_SQL,
                               _bezrealitky_placed, _ENROLL_BEZREALITKY_SQL,
                               _ENROLL_BEZREALITKY_COUNT_SQL),
}


def lane_spec(lane: str) -> LaneSpec:
    try:
        return LANES[lane]
    except KeyError:
        raise ValueError(f"unknown refetch lane {lane!r}; known: {sorted(LANES)}") from None


def statement_timeout_s() -> int:
    raw = os.environ.get(STATEMENT_TIMEOUT_ENV)
    if not raw:
        return DEFAULT_STATEMENT_TIMEOUT_S
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_STATEMENT_TIMEOUT_S


_DUE_SQL = """
    SELECT es.listing_id, l.source, l.source_id_native, es.attempts
    FROM location_enrichment_state es
    JOIN listings l ON l.id = es.listing_id
    WHERE es.lane = %(lane)s
      AND l.source = %(source)s
      AND NOT es.given_up
      AND es.next_eligible_at IS NOT NULL
      AND es.next_eligible_at <= now()
      AND l.is_active
    ORDER BY es.next_eligible_at, es.listing_id
    LIMIT %(limit)s
"""

# Completion. `next_eligible_at = NULL` is the retirement, NOT `given_up`: 384 gives
# `given_up` the "stopped trying" meaning it carries in `listing_fetch_failures`, and a
# row that succeeded did not give up. NULL also survives the producer — an unchanged
# payload no-ops, and a payload that regresses to legacy shape changes the hash, which
# re-arms `next_eligible_at` and pulls the row back into the cohort on its own.
_MARK_PLACED_SQL = """
    UPDATE location_enrichment_state
    SET last_outcome = 'placed', last_error = NULL, next_eligible_at = NULL
    WHERE lane = %(lane)s AND listing_id = ANY(%(ids)s)
"""

_MARK_RETIRED_SQL = """
    UPDATE location_enrichment_state
    SET given_up = true, last_outcome = %(outcome)s, next_eligible_at = NULL
    WHERE lane = %(lane)s AND listing_id = ANY(%(ids)s)
"""

_MARK_DISPATCHED_SQL = """
    UPDATE location_enrichment_state
    SET attempts = attempts + 1,
        last_attempt_at = now(),
        next_eligible_at = now() + %(backoff)s::interval
    WHERE lane = %(lane)s AND listing_id = ANY(%(ids)s)
"""


def classify(row: CohortRow, spec: LaneSpec = LANES[COHORT_LANE]) -> str:
    """`placed` | `not_applicable` | `exhausted` | `pending`.

    The sreality lane's placed test calls W1's own `sreality_payload_shape` rather than
    restating it in SQL. The classifier returns `absent` from TWO arms (not-a-dict, and an
    object carrying neither key set) and tests post-cutover BEFORE legacy, so a hand-written
    SQL mirror drifts on exactly the truncation cohort this lane exists to drain.
    """
    if not row.is_active:
        return "not_applicable"
    if spec.placed(row.probe):
        return "placed"
    if row.attempts >= MAX_ATTEMPTS:
        return "exhausted"
    return "pending"


def enroll(conn: psycopg.Connection, spec: LaneSpec, dry_run: bool = False) -> int:
    """Fill a producer-less lane's cohort from `listings`, idempotently (ON CONFLICT DO
    NOTHING keeps every existing row's attempts and schedule). A dry run COUNTS what a
    real run would insert instead of inserting nothing silently — on a lane whose rows do
    not exist until enroll creates them, "0 scanned" would otherwise read as "no work"."""
    if spec.enroll_sql is None:
        return 0
    if dry_run:
        with conn.cursor() as cur:
            cur.execute(spec.enroll_count_sql, {"lane": spec.lane})
            row = cur.fetchone()
            return int(row[0]) if row else 0
    with guarded(conn, statement_timeout_s()) as cur:
        cur.execute(spec.enroll_sql, {"lane": spec.lane, "extractor_version": ENROLL_VERSION})
        return cur.rowcount or 0


def rearm_reactivated(conn: psycopg.Connection, spec: LaneSpec, dry_run: bool = False) -> int:
    if dry_run:
        return 0
    with guarded(conn, statement_timeout_s()) as cur:
        cur.execute(_REARM_REACTIVATED_SQL, {"lane": spec.lane, "source": spec.source})
        return cur.rowcount or 0


def claim_due(
    conn: psycopg.Connection, lane: str = COHORT_LANE, limit: int = DEFAULT_DISPATCH_LIMIT,
) -> list[DueRow]:
    spec = lane_spec(lane)
    with conn.cursor() as cur:
        cur.execute(_DUE_SQL, {"lane": lane, "source": spec.source, "limit": limit})
        return [DueRow(listing_id=r[0], source=r[1], source_id_native=str(r[2]),
                       attempts=r[3])
                for r in cur.fetchall()]


def mark_dispatched(conn: psycopg.Connection, ids: list[int], lane: str = COHORT_LANE) -> int:
    if not ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(_MARK_DISPATCHED_SQL, {
            "lane": lane, "ids": ids, "backoff": f"{RETRY_BACKOFF_HOURS} hours"})
        return cur.rowcount or 0


def dispatch(
    conn: psycopg.Connection,
    lane: str = COHORT_LANE,
    limit: int = DEFAULT_DISPATCH_LIMIT,
    dry_run: bool = False,
) -> dict[str, int]:
    """Claim due rows, enqueue them onto the source-generic detail queue, advance their
    schedule. The enqueue and the schedule advance share one transaction: a row enqueued
    but not advanced is re-claimed on the next pass forever, which is the bug this whole
    module exists to close."""
    due = claim_due(conn, lane=lane, limit=limit)
    stats = {"due": len(due), "enqueued": 0, "dispatched": 0}
    if not due or dry_run:
        return stats

    by_source: dict[str, list[DueRow]] = {}
    for row in due:
        by_source.setdefault(row.source, []).append(row)

    with guarded(conn, statement_timeout_s()):
        for source, rows in by_source.items():
            # detail_ref is None for sreality — the drain derives the URL from the id.
            entries = [(r.source_id_native, None, None, REFETCH_PRIORITY) for r in rows]
            stats["enqueued"] += db.enqueue_detail(conn, source, entries)
        stats["dispatched"] = mark_dispatched(conn, [r.listing_id for r in due], lane=lane)
    return stats


def reconcile(
    conn: psycopg.Connection,
    lane: str = COHORT_LANE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_seconds: float | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Retire every cohort row whose work is done, impossible, or exhausted.

    Without this the table only grows and the gate reads a number that can never fall."""
    started = time.monotonic()
    spec = lane_spec(lane)
    stats = {"rearmed": rearm_reactivated(conn, spec, dry_run=dry_run),
             "scanned": 0, "placed": 0, "not_applicable": 0, "exhausted": 0, "pending": 0}
    after_id = 0

    while True:
        with conn.cursor() as cur:
            cur.execute(spec.scan_sql, {
                "lane": lane, "source": spec.source, "after_id": after_id,
                "batch_size": batch_size})
            batch = [CohortRow(listing_id=r[0], is_active=r[1], attempts=r[2], probe=r[3])
                     for r in cur.fetchall()]
        if not batch:
            break
        after_id = batch[-1].listing_id
        stats["scanned"] += len(batch)

        verdicts: dict[str, list[int]] = {}
        for row in batch:
            verdicts.setdefault(classify(row, spec), []).append(row.listing_id)
        for verdict, ids in verdicts.items():
            stats[verdict] += len(ids)

        if not dry_run:
            with guarded(conn, statement_timeout_s()) as cur:
                if verdicts.get("placed"):
                    cur.execute(_MARK_PLACED_SQL, {"lane": lane, "ids": verdicts["placed"]})
                for verdict, outcome in (("not_applicable", "not_applicable"),
                                         ("exhausted", "error")):
                    if verdicts.get(verdict):
                        cur.execute(_MARK_RETIRED_SQL, {
                            "lane": lane, "ids": verdicts[verdict], "outcome": outcome})

        if max_seconds is not None and time.monotonic() - started >= max_seconds:
            LOG.info("REFETCH reconcile stopping: --max-seconds reached")
            break
    return stats


def run(
    conn: psycopg.Connection,
    lane: str = COHORT_LANE,
    limit: int = DEFAULT_DISPATCH_LIMIT,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_seconds: float | None = None,
    reconcile_only: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Enroll (producer-less lanes only), reconcile, THEN dispatch — so a row the last
    pass already fixed is retired before this pass can spend a fetch on it. On a dry run
    `enrolled` is the count a real run WOULD insert."""
    stats: dict[str, Any] = {"lane": lane, "dry_run": dry_run}
    stats["enrolled" if not dry_run else "would_enroll"] = enroll(
        conn, lane_spec(lane), dry_run=dry_run)
    stats["reconcile"] = reconcile(
        conn, lane=lane, batch_size=batch_size, max_seconds=max_seconds, dry_run=dry_run)
    stats["dispatch"] = (
        {"skipped": True} if reconcile_only
        else dispatch(conn, lane=lane, limit=limit, dry_run=dry_run))
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", default=COHORT_LANE, choices=sorted(LANES))
    parser.add_argument("--limit", type=int, default=DEFAULT_DISPATCH_LIMIT)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-seconds", type=float, default=None)
    parser.add_argument("--reconcile-only", action="store_true",
                        help="retire finished rows without enqueuing any refetch")
    parser.add_argument("--dry-run", action="store_true",
                        help="classify and count, write nothing")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    with db.connect() as conn:
        with lease.held(conn, JOB_NAME, cadence="6 hours",
                        concurrency_group=CONCURRENCY_GROUP) as owned:
            if not owned:
                LOG.info("REFETCH skipped: another run holds the %s lease", JOB_NAME)
                return 0
            stats = run(conn, lane=args.lane, limit=args.limit, batch_size=args.batch_size,
                        max_seconds=args.max_seconds, reconcile_only=args.reconcile_only,
                        dry_run=args.dry_run)
    LOG.info("REFETCH done %s", json.dumps(stats, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
