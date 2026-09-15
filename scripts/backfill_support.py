"""Rails shared by the one-off `scripts/backfill_*.py` / `reconcile_*.py` jobs over `listings`.

Lifted verbatim out of `scripts/backfill_area_basis.py` the moment a second job needed
them (the portal-URL reconciler, docs/design/portal-listing-url.md), so there is one
copy of each rule:

* `wait_for_rebuild_gap` — `rebuild_browse_list()` holds AccessShareLock on `listings`
  for 5-10 minutes every 15, and is its heaviest reader; starting a table sweep underneath
  it makes both slower. Waiting is I/O courtesy, never a lock conflict, so a job proceeds
  anyway once the budget expires.
* `execute_with_lock_retry` — the hourly MF-yield recompute holds row locks on `listings`
  for ~90 s twice an hour; a batched UPDATE that lands behind it dies with
  DeadlockDetected, a lock-wait QueryCanceled, or — when the job arms its own
  `lock_timeout` — LockNotAvailable (the first two are the failures
  `scraper.db._touch_chunk_with_retry` documents). All three leave an autocommit
  connection usable, and every batch here is idempotent, so a short pause and a replay is
  the fix. Pass `setup` to run the batch inside ONE transaction preceded by `SET LOCAL`
  guards: on an autocommit connection a bare `SET LOCAL` applies to nothing at all, so a
  job that wants a statement/lock timeout on its write must take the transaction too.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

import psycopg

LOG = logging.getLogger(__name__)

_REBUILD_ACTIVE_SQL = """
    SELECT count(*) FROM pg_stat_activity
    WHERE state = 'active' AND query LIKE %(pattern)s AND pid <> pg_backend_pid()
"""

# Bound as a VALUE, not spelled into the query: a literal `%` in an executed SQL
# string is a psycopg placeholder hazard, and `tests/test_sql_placeholders.py`
# guards against exactly that. `\_` escapes the underscore so this matches
# `rebuild_browse_list` / `rebuild_properties_map_mv` and not `rebuildXlist`.
_REBUILD_LIKE = r"%rebuild\_%"

_STATEMENT_TIMEOUT_SQL = "SET statement_timeout = '600s'"

_REBUILD_POLL_SECONDS = 30.0

# The window these three have to cover is the ~90 s of row locks the hourly MF-yield
# recompute holds on `listings` twice an hour — the failure this rail exists for. With a
# caller-armed `lock_timeout` the wait itself is bounded, so the RETRIES are what has to
# outlast the holder: 4 attempts with a 15 s × attempt backoff give 15 + 30 + 45 = 90 s of
# pauses on top of the four bounded waits (≥ 30 s each at the recommended lock_timeout),
# i.e. well past 120 s. Three attempts and a 5 s lock_timeout came to ~60 s and would have
# given up while the holder was still working.
_LOCK_RETRY_ATTEMPTS = 4
_LOCK_DEADLOCK_DELAY = 2.0
_LOCK_WAIT_DELAY = 15.0


def wait_for_rebuild_gap(conn: Any, budget_seconds: float) -> bool:
    """Poll until no `rebuild_%` statement is running, up to a budget.

    `rebuild_browse_list()` runs every 15 minutes and holds its locks for 5-10
    of them, so refusing outright means colliding roughly half the time and
    never running. Waiting is the right move — and if the budget expires this
    proceeds anyway rather than failing, because the contention is I/O only:
    these jobs take no lock a rebuild can block on, so overlapping is slow,
    never incorrect.
    """
    deadline = time.monotonic() + budget_seconds
    while True:
        with conn.cursor() as cur:
            cur.execute(_REBUILD_ACTIVE_SQL, {"pattern": _REBUILD_LIKE})
            active = int(cur.fetchone()[0])
        if not active:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            LOG.warning("BACKFILL starting anyway: %d rebuild_%% statement(s) "
                        "still active after %.0fs. This is I/O contention, not "
                        "a lock conflict.", active, budget_seconds)
            return False
        LOG.info("BACKFILL waiting for a rebuild gap: %d active, %.0fs of "
                 "budget left", active, remaining)
        time.sleep(min(_REBUILD_POLL_SECONDS, remaining))


def execute_with_lock_retry(
    conn: Any, sql: str, params: dict[str, Any], *, label: str,
    setup: Sequence[str] = (),
) -> int:
    """Run one idempotent batched statement, replaying it when it loses a lock fight.

    Deadlock -> the victim is already rolled back, the other side completes, a short
    pause suffices. Lock-wait cancel (statement_timeout while locking tuple) or an armed
    `lock_timeout` firing -> the holder is usually done, a longer pause lets it release.
    Anything else raises. Returns the statement's rowcount.

    `setup` statements (`SET LOCAL ...`) run first, inside the transaction this then opens
    around the batch — the only shape in which a `SET LOCAL` binds on these autocommit
    connections. With no `setup` the statement runs exactly as before, unwrapped.
    """
    for attempt in range(1, _LOCK_RETRY_ATTEMPTS + 1):
        try:
            if setup:
                with conn.transaction(), conn.cursor() as cur:
                    for statement in setup:
                        cur.execute(statement)
                    cur.execute(sql, params)
                    return int(cur.rowcount)
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return int(cur.rowcount)
        except (psycopg.errors.DeadlockDetected, psycopg.errors.QueryCanceled,
                psycopg.errors.LockNotAvailable) as exc:
            if attempt == _LOCK_RETRY_ATTEMPTS:
                raise
            lock_wait = not isinstance(exc, psycopg.errors.DeadlockDetected)
            LOG.warning("%s: %s (attempt %d/%d); retrying", label,
                        "lock-wait timeout" if lock_wait else "deadlock",
                        attempt, _LOCK_RETRY_ATTEMPTS)
            time.sleep((_LOCK_WAIT_DELAY if lock_wait else _LOCK_DEADLOCK_DELAY) * attempt)
    raise AssertionError("unreachable")
