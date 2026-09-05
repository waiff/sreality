"""touch_listings must survive a lock race, not throw away a finished walk.

On 2026-09-05 10:38 a fully-paged sreality komercni/prodej walk — every page
fetched, every id in hand — was recorded as collected=0 and its delisting sweep
skipped, because the LAST step (bumping last_seen_at) lost a deadlock to a
concurrent writer. Three of 46 runs. The work was already done; only the
bookkeeping failed, and it took the whole category's evidence with it.

The retry is safe by construction: the walk connection is autocommit, so a
deadlock aborts only the one statement; both statements in a chunk are idempotent
(SET last_seen_at = now(); INSERT ... ON CONFLICT); and Postgres has already rolled
back the victim, so the other side of the race completes and a short pause is
enough. These tests pin that it retries, that it stops retrying, and that the
count it returns is right.
"""

from __future__ import annotations

from typing import Any

import psycopg

from scraper import db


class _Cur:
    """Raises DeadlockDetected on the first `fail_times` executes, then works."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise psycopg.errors.DeadlockDetected("deadlock detected")
        self.rowcount = len(params[0]) if params else 0


class _Conn:
    def __init__(self, cur: _Cur) -> None:
        self._cur = cur

    def cursor(self) -> _Cur:
        return self._cur


def test_a_single_deadlock_is_retried_and_the_chunk_still_counts(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(db.time, "sleep", slept.append)
    cur = _Cur(fail_times=1)
    total = db.touch_listings(_Conn(cur), [1, 2, 3])
    assert total == 3                       # the retry completed the chunk
    assert len(slept) == 1                  # and paused once before doing so
    # 1 failed execute + 2 successful executes (react CTE + bulk bump)
    assert cur.calls == 3


def test_a_persistent_deadlock_gives_up_and_raises(monkeypatch) -> None:
    """Retrying forever would turn a lock race into a hung walk. After the
    budgeted attempts the error propagates exactly as before, so the category
    is reported failed rather than silently half-touched."""
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)
    cur = _Cur(fail_times=99)
    try:
        db.touch_listings(_Conn(cur), [1, 2, 3])
    except psycopg.errors.DeadlockDetected:
        pass
    else:
        raise AssertionError("a persistent deadlock must still raise")
    assert cur.calls == db._TOUCH_DEADLOCK_ATTEMPTS


def test_no_deadlock_means_no_sleep_and_one_pass(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(db.time, "sleep", slept.append)
    cur = _Cur(fail_times=0)
    assert db.touch_listings(_Conn(cur), [7, 8]) == 2
    assert slept == []
    assert cur.calls == 2


def test_the_backoff_grows_with_the_attempt(monkeypatch) -> None:
    """A double race should wait longer the second time, not hammer the lock."""
    slept: list[float] = []
    monkeypatch.setattr(db.time, "sleep", slept.append)
    cur = _Cur(fail_times=2)
    db.touch_listings(_Conn(cur), [1])
    assert slept == [db._TOUCH_DEADLOCK_DELAY * 1, db._TOUCH_DEADLOCK_DELAY * 2]
