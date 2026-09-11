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
  DeadlockDetected or a lock-wait QueryCanceled (the same two failures
  `scraper.db._touch_chunk_with_retry` documents). Both leave an autocommit connection
  usable, and every batch here is idempotent, so a short pause and a replay is the fix.
"""

from __future__ import annotations

import logging
import time
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

_LOCK_RETRY_ATTEMPTS = 3
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
) -> int:
    """Run one idempotent batched statement, replaying it when it loses a lock fight.

    Deadlock -> the victim is already rolled back, the other side completes, a short
    pause suffices. Lock-wait cancel (statement_timeout while locking tuple) -> the
    holder is usually done, a longer pause lets it release. Anything else raises.
    Returns the statement's rowcount.
    """
    for attempt in range(1, _LOCK_RETRY_ATTEMPTS + 1):
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return int(cur.rowcount)
        except (psycopg.errors.DeadlockDetected, psycopg.errors.QueryCanceled) as exc:
            if attempt == _LOCK_RETRY_ATTEMPTS:
                raise
            lock_wait = isinstance(exc, psycopg.errors.QueryCanceled)
            LOG.warning("%s: %s (attempt %d/%d); retrying", label,
                        "lock-wait timeout" if lock_wait else "deadlock",
                        attempt, _LOCK_RETRY_ATTEMPTS)
            time.sleep((_LOCK_WAIT_DELAY if lock_wait else _LOCK_DEADLOCK_DELAY) * attempt)
    raise AssertionError("unreachable")
