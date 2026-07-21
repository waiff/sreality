"""R2 GATE 1 — swap listings' PRIMARY KEY from sreality_id to the surrogate id.

The destructive step of the listing-identity refactor
(docs/design/listing-identity-r2-pk-swap-runbook.md § 6). The swap itself is
CATALOG-ONLY — no table scan, no data rewrite — and reversible in place while
zero NULL sreality_id rows exist, which is the state this script enforces.

    BEGIN;
    ALTER TABLE listings DROP CONSTRAINT listings_pkey;
    ALTER TABLE listings ADD CONSTRAINT listings_pkey PRIMARY KEY USING INDEX listings_id_pk_idx;
    ALTER TABLE listings ALTER COLUMN sreality_id DROP NOT NULL;
    COMMIT;

All three statements are ONE transaction: between dropping the old PK and
promoting the new one there is no unique index backing `ON CONFLICT
(sreality_id)`, and any writer that planned in that gap would abort with 42P10.
`_with_lock_retry` in apply_r2_constraints commits per statement and is
therefore NOT usable here — this module has its own transaction-scoped retry.

Run it through the dispatch-only workflow (apply_listings_pk_swap.yml), which
has the repo secrets. NEVER run the window through the Supabase MCP: it pools
with a ~2-minute statement_timeout and has been observed timing out client-side
while still COMMITTING — a half-observed destructive transaction is the worst
possible outcome here.

Modes:
    --preflight            (default) read-only. Verifies every § 5 precondition
                           and both quiet signals. Changes nothing.
    --window --confirm     the gated window: pause pg_cron -> re-verify quiet ->
                           swap -> verify -> resume pg_cron (always, via finally).
    --resume-cron          emergency: re-activate jobs if --window died mid-window.
    --rollback --confirm   re-promote sreality_id as the PK.

The realtime worker must ALREADY be paused (Railway REALTIME_WORKER_ENABLED=
false) before --window: an agent cannot set that, and --preflight/--window both
refuse to proceed while heartbeats are fresh.

Requires SUPABASE_DB_URL (+ SUPABASE_DB_SESSION_URL). Safe to re-run.
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

log = logging.getLogger("apply_listings_pk_swap")

_PK_INDEX = "listings_id_pk_idx"
_LEGACY_UNIQUE_INDEX = "listings_sreality_id_uidx"
_LOCK_TIMEOUT = "3s"
_LOCK_RETRIES = 40
_RETRY_SLEEP = 5.0

# A heartbeat younger than this means the realtime worker has not actually
# stopped yet (it beats far more often than this).
_WORKER_QUIET_SECONDS = 180


def _scalar(conn: Any, sql: str, params: Any = None) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def _preconditions(conn: Any) -> tuple[list[str], list[str], bool]:
    """(failures, notes, already_swapped) — every § 5 gate, read-only."""
    failures: list[str] = []
    notes: list[str] = []

    current_pk = _scalar(
        conn,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'listings'::regclass AND contype = 'p'",
    )
    already_swapped = bool(current_pk) and "sreality_id" not in current_pk
    notes.append(f"listings PK: {current_pk}")

    # The replacement unique index is MANDATORY: the PK is currently the only
    # unique index on sreality_id, so without it every post-swap
    # `ON CONFLICT (sreality_id)` errors instantly (total ingest outage) AND the
    # rollback lever disappears.
    for idx in (_PK_INDEX, _LEGACY_UNIQUE_INDEX):
        ok = _scalar(
            conn,
            "SELECT i.indisunique AND i.indisvalid FROM pg_class c "
            "JOIN pg_index i ON i.indexrelid = c.oid WHERE c.relname = %s",
            (idx,),
        )
        if ok:
            notes.append(f"index {idx}: valid unique")
        else:
            failures.append(f"index {idx} missing or not a valid unique index")

    if not _scalar(
        conn,
        "SELECT attnotnull FROM pg_attribute "
        "WHERE attrelid = 'listings'::regclass AND attname = 'id'",
    ):
        failures.append("listings.id is not NOT NULL (PK promotion would scan/fail)")
    else:
        notes.append("listings.id: NOT NULL")

    legacy_fks = int(_scalar(
        conn,
        "SELECT count(*) FROM pg_constraint c JOIN pg_attribute a "
        "ON a.attrelid = c.confrelid AND a.attnum = c.confkey[1] "
        "WHERE c.contype = 'f' AND c.confrelid = 'listings'::regclass "
        "AND a.attname = 'sreality_id'",
    ) or 0)
    if legacy_fks:
        failures.append(
            f"{legacy_fks} legacy FK(s) still reference listings(sreality_id) — "
            "dropping the PK they depend on will fail")
    else:
        notes.append("legacy child FKs on sreality_id: 0 (all dropped)")

    surrogate_fks = int(_scalar(
        conn,
        "SELECT count(*) FROM pg_constraint c JOIN pg_attribute a "
        "ON a.attrelid = c.confrelid AND a.attnum = c.confkey[1] "
        "WHERE c.contype = 'f' AND c.confrelid = 'listings'::regclass "
        "AND a.attname = 'id'",
    ) or 0)
    notes.append(f"surrogate child FKs on listings(id): {surrogate_fks}")
    if surrogate_fks < 19:
        failures.append(f"expected >= 19 surrogate FKs, found {surrogate_fks}")

    return failures, notes, already_swapped


def _quiet_signals(conn: Any) -> tuple[list[str], list[str]]:
    """(failures, notes) for the two runtime writers this window must exclude."""
    failures: list[str] = []
    notes: list[str] = []

    age = _scalar(
        conn,
        "SELECT extract(epoch FROM now() - max(beat_at)) FROM worker_heartbeats",
    )
    if age is None:
        notes.append("worker_heartbeats: empty (worker never ran)")
    elif float(age) < _WORKER_QUIET_SECONDS:
        failures.append(
            f"realtime worker still beating ({float(age):.0f}s ago) — set Railway "
            "REALTIME_WORKER_ENABLED=false and wait for it to stop")
    else:
        notes.append(f"realtime worker: quiet for {float(age):.0f}s")

    active = int(_scalar(
        conn,
        "SELECT count(*) FROM scrape_runs WHERE ended_at IS NULL "
        "AND started_at > now() - interval '2 hours'",
    ) or 0)
    if active:
        failures.append(f"{active} scrape_run(s) still in flight — wait for them to end")
    else:
        notes.append("in-flight scrape_runs: 0")

    return failures, notes


def _cron_jobs(conn: Any) -> list[tuple[int, str, bool]]:
    with conn.cursor() as cur:
        cur.execute("SELECT jobid, jobname, active FROM cron.job ORDER BY jobid")
        return [(int(r[0]), r[1], bool(r[2])) for r in cur.fetchall()]


def _set_cron_active(conn: Any, jobids: list[int], active: bool) -> None:
    for jobid in jobids:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cron.alter_job(job_id := %s, active := %s)", (jobid, active))
    conn.commit()


def _swap_tx(conn: Any, statements: list[str], label: str) -> None:
    """Run `statements` as ONE transaction, retrying the whole unit on a lock
    conflict. Never leaves the table between the DROP and the ADD."""
    for attempt in range(_LOCK_RETRIES):
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
                for sql in statements:
                    log.info("  %s", sql)
                    cur.execute(sql)
            conn.commit()
            return
        except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected) as exc:
            conn.rollback()
            log.info("%s: %s (attempt %d) — retrying",
                     label, type(exc).__name__, attempt + 1)
            time.sleep(_RETRY_SLEEP)
    raise RuntimeError(f"{label}: could not acquire lock after {_LOCK_RETRIES} attempts")


def _verify_post_swap(conn: Any) -> list[str]:
    failures: list[str] = []
    pk = _scalar(
        conn,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'listings'::regclass AND contype = 'p'",
    )
    if not pk or "(id)" not in pk.replace(" ", ""):
        failures.append(f"PK is not on (id) after swap: {pk}")
    else:
        log.info("VERIFY listings PK: %s", pk)

    if _scalar(
        conn,
        "SELECT attnotnull FROM pg_attribute "
        "WHERE attrelid = 'listings'::regclass AND attname = 'sreality_id'",
    ):
        failures.append("sreality_id is still NOT NULL")
    else:
        log.info("VERIFY sreality_id: nullable")

    # The rollback lever must survive the swap.
    if not _scalar(
        conn, "SELECT 1 FROM pg_class WHERE relname = %s", (_LEGACY_UNIQUE_INDEX,),
    ):
        failures.append(f"{_LEGACY_UNIQUE_INDEX} is gone — rollback lever lost")
    else:
        log.info("VERIFY %s: present (rollback lever intact)", _LEGACY_UNIQUE_INDEX)

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", default=True)
    mode.add_argument("--window", action="store_true")
    mode.add_argument("--resume-cron", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    parser.add_argument("--confirm", action="store_true",
                        help="required for --window and --rollback")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    if (args.window or args.rollback) and not args.confirm:
        print("ERROR: --window/--rollback require --confirm.", file=sys.stderr)
        return 2

    with db.connect_session() as conn:
        if args.resume_cron:
            jobs = _cron_jobs(conn)
            paused = [j for j, _n, a in jobs if not a]
            if not paused:
                log.info("all pg_cron jobs already active; nothing to do")
                return 0
            log.info("re-activating pg_cron jobs: %s", paused)
            _set_cron_active(conn, paused, True)
            log.info("done")
            return 0

        if args.rollback:
            log.info("ROLLBACK re-promoting %s as the PK", _LEGACY_UNIQUE_INDEX)
            _swap_tx(conn, [
                "ALTER TABLE listings DROP CONSTRAINT listings_pkey",
                "ALTER TABLE listings ADD CONSTRAINT listings_pkey "
                f"PRIMARY KEY USING INDEX {_LEGACY_UNIQUE_INDEX}",
            ], "rollback")
            log.info("ROLLBACK done. NOTE: the 19 legacy child FKs are NOT re-added "
                     "here — re-add them NOT VALID then VALIDATE if you need them.")
            return 0

        failures, notes, already_swapped = _preconditions(conn)
        for n in notes:
            log.info("PRE  %s", n)

        if already_swapped:
            log.info("listings PK is ALREADY on the surrogate — nothing to do")
            return 0

        quiet_failures, quiet_notes = _quiet_signals(conn)
        for n in quiet_notes:
            log.info("PRE  %s", n)

        jobs = _cron_jobs(conn)
        log.info("PRE  pg_cron jobs: %s",
                 ", ".join(f"{j}:{n}{'' if a else ' (inactive)'}" for j, n, a in jobs))

        for f in failures + quiet_failures:
            log.error("FAIL %s", f)

        if not args.window:
            log.info("PREFLIGHT %s", "BLOCKED" if failures or quiet_failures else "CLEAR")
            log.info("Reminder: also confirm no GH Actions scraper run is mid-flight "
                     "(`gh run list --limit 20`) — a queued run executes its OWN "
                     "checkout SHA, so a merge does not stop it.")
            return 1 if failures or quiet_failures else 0

        if failures or quiet_failures:
            log.error("WINDOW refusing to proceed: %d precondition(s) failed",
                      len(failures) + len(quiet_failures))
            return 1

        to_pause = [j for j, _n, a in jobs if a]
        log.info("WINDOW pausing pg_cron jobs: %s", to_pause)
        # The pause lives INSIDE the try: _set_cron_active walks the jobs one at a
        # time, so a failure part-way through would otherwise leave some paused
        # with no finally to restore them (--resume-cron is the backstop for a
        # process killed outright).
        try:
            _set_cron_active(conn, to_pause, False)
            # Re-check quiet AFTER the pause: a cron job may have started between
            # the first check and the pause taking effect.
            requiet, _ = _quiet_signals(conn)
            if requiet:
                for f in requiet:
                    log.error("FAIL %s", f)
                raise RuntimeError("quiet signals regressed after pausing cron")

            log.info("WINDOW executing the swap (catalog-only, one transaction)")
            _swap_tx(conn, [
                "ALTER TABLE listings DROP CONSTRAINT listings_pkey",
                "ALTER TABLE listings ADD CONSTRAINT listings_pkey "
                f"PRIMARY KEY USING INDEX {_PK_INDEX}",
                "ALTER TABLE listings ALTER COLUMN sreality_id DROP NOT NULL",
            ], "pk_swap")

            post = _verify_post_swap(conn)
            for f in post:
                log.error("FAIL %s", f)
            if post:
                log.error("WINDOW swap applied but verification FAILED — see rollback "
                          "in the runbook § 6 before resuming writers")
                return 1
        finally:
            log.info("WINDOW re-activating pg_cron jobs: %s", to_pause)
            _set_cron_active(conn, to_pause, True)

    log.info("GATE 1 COMPLETE. Now set Railway REALTIME_WORKER_ENABLED=true.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
