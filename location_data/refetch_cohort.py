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
from dataclasses import dataclass
from typing import Any

import psycopg

from location_data.claims_intake import guarded, sreality_payload_shape
from scraper import db

LOG = logging.getLogger("location_data.refetch_cohort")

# Deliberately NOT spelled `LANE`. That name is reserved for `location_claim_batches.lane`
# — the resume-cursor identity `test_lane_identifiers.py` polices, after the W3 erratum
# found one lane string assigned to two waves. This module stamps no batch row and holds no
# resume cursor; its lane is `location_enrichment_state.lane`, a different column and a
# different namespace, shared on purpose with the producer that fills it.
COHORT_LANE = "sreality_detail_refetch"

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
    """A cohort member as reconcile sees it. `locality` is the projected
    `raw_json->'locality'` ONLY — never the whole payload, which on sreality carries the
    geometry blob that made these rows truncated in the first place."""

    listing_id: int
    is_active: bool
    attempts: int
    locality: Any


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
      AND NOT es.given_up
      AND es.next_eligible_at IS NOT NULL
      AND es.next_eligible_at <= now()
      AND l.is_active
    ORDER BY es.next_eligible_at, es.listing_id
    LIMIT %(limit)s
"""

# Reconcile scans the whole non-retired cohort, including rows that are NOT due: a row
# retired here is one the driver never has to claim again, and `is_active` / payload shape
# both change out from under this table without touching it.
_COHORT_SCAN_SQL = """
    SELECT es.listing_id, l.is_active, es.attempts, l.raw_json->'locality'
    FROM location_enrichment_state es
    JOIN listings l ON l.id = es.listing_id
    WHERE es.lane = %(lane)s
      AND NOT es.given_up
      AND es.listing_id > %(after_id)s
    ORDER BY es.listing_id
    LIMIT %(batch_size)s
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


def classify(row: CohortRow) -> str:
    """`placed` | `not_applicable` | `exhausted` | `pending`.

    The shape test calls W1's own `sreality_payload_shape` rather than restating it in
    SQL. The classifier returns `absent` from TWO arms (not-a-dict, and an object carrying
    neither key set) and tests post-cutover BEFORE legacy, so a hand-written SQL mirror
    drifts on exactly the truncation cohort this lane exists to drain.
    """
    if not row.is_active:
        return "not_applicable"
    if sreality_payload_shape({"locality": row.locality}) == "post_cutover":
        return "placed"
    if row.attempts >= MAX_ATTEMPTS:
        return "exhausted"
    return "pending"


def claim_due(
    conn: psycopg.Connection, lane: str = COHORT_LANE, limit: int = DEFAULT_DISPATCH_LIMIT,
) -> list[DueRow]:
    with conn.cursor() as cur:
        cur.execute(_DUE_SQL, {"lane": lane, "limit": limit})
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
    stats = {"scanned": 0, "placed": 0, "not_applicable": 0, "exhausted": 0, "pending": 0}
    after_id = 0

    while True:
        with conn.cursor() as cur:
            cur.execute(_COHORT_SCAN_SQL, {
                "lane": lane, "after_id": after_id, "batch_size": batch_size})
            batch = [CohortRow(listing_id=r[0], is_active=r[1], attempts=r[2], locality=r[3])
                     for r in cur.fetchall()]
        if not batch:
            break
        after_id = batch[-1].listing_id
        stats["scanned"] += len(batch)

        verdicts: dict[str, list[int]] = {}
        for row in batch:
            verdicts.setdefault(classify(row), []).append(row.listing_id)
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
    """Reconcile FIRST, then dispatch — so a row the last pass already fixed is retired
    before this pass can spend a fetch on it."""
    stats: dict[str, Any] = {"lane": lane, "dry_run": dry_run}
    stats["reconcile"] = reconcile(
        conn, lane=lane, batch_size=batch_size, max_seconds=max_seconds, dry_run=dry_run)
    stats["dispatch"] = (
        {"skipped": True} if reconcile_only
        else dispatch(conn, lane=lane, limit=limit, dry_run=dry_run))
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", default=COHORT_LANE)
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
        stats = run(conn, lane=args.lane, limit=args.limit, batch_size=args.batch_size,
                    max_seconds=args.max_seconds, reconcile_only=args.reconcile_only,
                    dry_run=args.dry_run)
    LOG.info("REFETCH done %s", json.dumps(stats, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
