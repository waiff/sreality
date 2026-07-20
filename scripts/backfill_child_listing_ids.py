"""Backfill the R2 surrogate (listings.id) onto every child/carrier table.

Fills the columns migrations 320-325 added, for rows written BEFORE dual-write
shipped. New rows already carry the surrogate from their writer, so this only
ever chases a set that shrinks.

ORDER MATTERS, and it is the opposite of what the original design doc said: run
this only AFTER the dual-write deploy is live. Backfilling first cannot converge
— the always-on worker keeps inserting fresh NULL rows behind the scan, so
"remaining" never reaches zero and the NOT NULL check can never be added.

Two deliberate departures from the R1 template
(scripts/backfill_listing_surrogate_id.py), both of which only bite at child-table
scale:

1. KEYSET WINDOWS, not "WHERE new IS NULL ORDER BY pk LIMIT n". The R1 shape
   re-scans from the low end every batch to find the next unfilled row, which is
   fine over ~9 batches of listings and quadratic over ~160 batches of an 8.08M-row
   images table — the late batches walk past millions of already-filled rows and
   flirt with the statement timeout. Here each batch owns a fixed id window and
   never revisits one.
2. TERMINATION ON REMAINING == 0, not on "a batch updated 0 rows". Under SKIP
   LOCKED an empty batch can simply mean the worker held those rows for a moment;
   treating that as done would silently leave stragglers behind, and stragglers are
   exactly what blocks the Phase B NOT NULL check.

Runs on the SESSION-mode connection: the transaction pooler caps statements at
~2 minutes, which has killed long jobs here twice before.

    python -m scripts.backfill_child_listing_ids --dry-run
    python -m scripts.backfill_child_listing_ids --table images --batch 50000
    python -m scripts.backfill_child_listing_ids                 # every carrier
    python -m scripts.backfill_child_listing_ids --repair        # fix wrong values

`--repair` widens the predicate from "surrogate IS NULL" to "surrogate IS DISTINCT
FROM the right one". The normal backfill only ever fills NULLs, so a writer bug
that stamped the WRONG id (what the parity check calls a mismatch) would survive
it untouched.

Requires SUPABASE_DB_URL (and SUPABASE_DB_SESSION_URL for the session pooler).
Idempotent and restartable: re-running is a no-op once every carrier is clean.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

import psycopg

from scraper import db
from toolkit.listing_identity import R2_CARRIERS, R2_CARRIERS_BY_TABLE, is_ts_cursor

log = logging.getLogger("backfill_child_listing_ids")

_STATEMENT_TIMEOUT = "10min"
_MAX_RETRIES = 5
# Ceiling on the density-scaled window so a nearly-empty carrier cannot produce an
# absurd single window that becomes a de-facto full-table update.
_MAX_WINDOW_IDS = 20_000_000


def _predicate(
    new: str, legacy: str, repair: bool, skip: str | None = None,
) -> str:
    """Which rows this run is allowed to touch.

    `skip` excludes rows an UPDATE legally cannot write (see R2_CARRIERS). It is
    part of the predicate itself, so counting and updating agree — a row skipped by
    one but counted by the other would keep "remaining" permanently above zero and
    make the self-chaining workflow re-dispatch forever.
    """
    if repair:
        base = (
            f"t.{legacy} IS NOT NULL "
            f"AND t.{new} IS DISTINCT FROM (SELECT l.id FROM listings l "
            f"WHERE l.sreality_id = t.{legacy})"
        )
    else:
        base = f"t.{legacy} IS NOT NULL AND t.{new} IS NULL"
    return f"{base} AND NOT ({skip})" if skip else base


def _remaining(conn: "psycopg.Connection", carrier: dict[str, Any], repair: bool) -> int:
    total = 0
    for legacy, new in carrier["cols"]:
        sql = (
            f"SELECT count(*) FROM {carrier['table']} t "
            f"WHERE {_predicate(new, legacy, repair, carrier.get('skip'))}"
        )
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = '{_STATEMENT_TIMEOUT}'")
            cur.execute(sql)
            total += int(cur.fetchone()[0])
    conn.rollback()
    return total


def _bounds(
    conn: "psycopg.Connection", carrier: dict[str, Any],
    legacy: str, new: str, repair: bool,
) -> tuple[Any, Any]:
    """Window bounds: start at the first row still needing work, not at min(id).

    Resuming from the true minimum would re-walk every window a previous run
    already filled — cheap per window, but ~160 no-op windows before reaching live
    work on images. Filtering min() by the same predicate makes each run pick up
    where the last one stopped.
    """
    cursor, table = carrier["cursor"], carrier["table"]
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = '{_STATEMENT_TIMEOUT}'")
        cur.execute(
            f"SELECT min({cursor}), max({cursor}) FROM {table} t "
            f"WHERE {_predicate(new, legacy, repair, carrier.get('skip'))}"
        )
        lo, hi = cur.fetchone()
    conn.rollback()
    return lo, hi


def _update_window(
    conn: "psycopg.Connection",
    carrier: dict[str, Any],
    legacy: str,
    new: str,
    where_window: str,
    params: dict[str, Any],
    repair: bool,
) -> tuple[int, "psycopg.Connection"]:
    """One window's UPDATE, surviving both deadlocks and a dropped connection.

    Returns (rows_updated, live_conn) — the connection may be a FRESH one, so the
    caller must rebind its handle (db.run_resilient's contract).

    A dropped connection is the expected failure here, not an exotic one: these
    windows are wide non-HOT updates against a hot table, and the pooler will cut
    one loose sooner or later. The window is idempotent (it only touches rows still
    matching the predicate), so replaying it after a reconnect re-commits
    identically.
    """
    sql = (
        f"UPDATE {carrier['table']} t SET {new} = l.id "
        f"FROM listings l "
        f"WHERE l.sreality_id = t.{legacy} "
        f"AND {where_window} AND {_predicate(new, legacy, repair, carrier.get('skip'))}"
    )

    def _op(c: "psycopg.Connection") -> int:
        with c.cursor() as cur:
            cur.execute(f"SET statement_timeout = '{_STATEMENT_TIMEOUT}'")
            cur.execute(sql, params)
            updated = cur.rowcount or 0
        c.commit()
        return updated

    return db.run_resilient(
        conn, _op, reconnect=db.connect_session,
        attempts=_MAX_RETRIES,
        label=f"backfill {carrier['table']}.{new}",
    )


def backfill_carrier(
    conn: "psycopg.Connection", carrier: dict[str, Any], batch: int, repair: bool,
    deadline: float | None = None,
) -> tuple[int, "psycopg.Connection"]:
    table = carrier["table"]
    before = _remaining(conn, carrier, repair)
    if before == 0:
        log.info("%-34s clean", table)
        return 0, conn
    log.info("%-34s %d row(s) to fill", table, before)

    done = 0
    for legacy, new in carrier["cols"]:
        if is_ts_cursor(carrier):
            # The two timestamp-cursor carriers are small (hundreds to low
            # thousands of rows); windowing them would be pure ceremony.
            updated, conn = _update_window(
                conn, carrier, legacy, new, "TRUE", {}, repair,
            )
            done += updated
            continue
        lo, hi = _bounds(conn, carrier, legacy, new, repair)
        if lo is None:
            continue
        # Window in ID space, but size it by ROW density. images spans ~182M ids for
        # ~8M rows, so a flat 50k-id window would touch ~2k rows and need thousands
        # of round-trips; scaling by the observed sparsity keeps every window worth
        # roughly `batch` rows regardless of how gappy the id space is.
        span = max(1, int(hi) - int(lo) + 1)
        step = min(_MAX_WINDOW_IDS, max(batch, batch * span // max(1, before)))
        start = int(lo)
        while start <= int(hi):
            if deadline is not None and time.monotonic() > deadline:
                log.warning(
                    "%s.%s: wall-clock budget reached at %s — re-run to continue "
                    "(the next run resumes from the first unfilled row)",
                    table, new, start,
                )
                return done, conn
            end = start + step
            updated, conn = _update_window(
                conn, carrier, legacy, new,
                "t.{c} >= %(lo)s AND t.{c} < %(hi)s".format(c=carrier["cursor"]),
                {"lo": start, "hi": end}, repair,
            )
            done += updated
            if updated:
                log.info("  %-32s %s..%s -> %d", new, start, end - 1, updated)
            start = end

    after = _remaining(conn, carrier, repair)
    log.info("%-34s filled %d, %d remaining", table, done, after)
    if after:
        # Not fatal: rows the worker inserted mid-run are legitimately new. A
        # SECOND pass converges them; only a persistent non-zero means a writer
        # is not dual-writing (which the parity check reports by name).
        log.warning("%s still has %d unfilled row(s) — re-run to converge", table, after)
    return done, conn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", default="",
                        help="Backfill only this carrier (default: all).")
    parser.add_argument("--batch", type=int, default=50000,
                        help="Cursor-window width for id-cursor carriers.")
    parser.add_argument("--repair", action="store_true",
                        help="Also overwrite surrogates that are set but WRONG — the "
                             "plain backfill only fills NULLs, so a mis-stamping writer's "
                             "damage would survive it.")
    parser.add_argument("--max-seconds", type=int, default=0,
                        help="Wall-clock budget; stop cleanly when reached (0 = no "
                             "budget). Lets a CI run finish inside its job timeout — the "
                             "next run resumes from the first unfilled row.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what each carrier needs, write nothing.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    if args.table:
        carrier = R2_CARRIERS_BY_TABLE.get(args.table)
        if carrier is None:
            print(f"ERROR: {args.table} is not an R2 carrier.", file=sys.stderr)
            return 2
        carriers = [carrier]
    else:
        carriers = R2_CARRIERS

    deadline = time.monotonic() + args.max_seconds if args.max_seconds else None
    total = 0
    # Session mode: the transaction pooler's ~2-minute statement cap would kill
    # the wide windows on images / listing_snapshots.
    with db.connect_session() as conn:
        for carrier in carriers:
            if args.dry_run:
                need = _remaining(conn, carrier, args.repair)
                log.info("%-34s %d row(s) would be filled", carrier["table"], need)
                total += need
                continue
            if deadline is not None and time.monotonic() > deadline:
                log.warning("wall-clock budget reached — %s not started", carrier["table"])
                break
            filled, conn = backfill_carrier(
                conn, carrier, args.batch, args.repair, deadline,
            )
            total += filled
        # The chain condition, computed on the SAME connection at the end of the
        # run: how much is genuinely left. Reported as a machine-readable marker so
        # the workflow can decide whether to re-dispatch itself rather than a human
        # having to. Zero means every carrier is clean and the chain stops.
        if not args.dry_run:
            pending = sum(_remaining(conn, c, args.repair) for c in carriers)
            log.info("PENDING=%d", pending)
    log.info("done: %d row(s) %s", total, "pending" if args.dry_run else "filled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
