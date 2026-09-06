"""A touch must survive a lock fight, not throw away a finished walk.

Two ways it loses one, both seen in production on 2026-09-05:

* 10:38 -- a fully-paged sreality komercni/prodej walk was recorded as
  collected=0 and its sweep skipped because the LAST step (bumping last_seen_at)
  lost a deadlock to a concurrent writer. Three of 46 runs.
* 21:26 / 21:27 -- idnes dum/prodej and ceskereality komercni/prodej died within
  a minute of each other with "canceling statement due to statement timeout ...
  while locking tuple": both touches sat the whole 2-minute statement_timeout
  behind the hourly MF-yield recompute's bulk UPDATE, which held row locks for
  ~90 s twice. Same shape, different exception, and the by-id touch (every
  non-sreality portal) had no retry at all.

The retry is safe by construction: the walk connection is autocommit, so either
error aborts only the one statement; both statements in a chunk are idempotent
(SET last_seen_at = now(); INSERT ... ON CONFLICT). These tests pin that BOTH
touch functions retry on BOTH errors, that they stop retrying, that a lock-wait
pauses longer than a deadlock, that unrelated errors are not retried, and that
the count returned is right.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from scraper import db

DEADLOCK = psycopg.errors.DeadlockDetected
LOCKWAIT = psycopg.errors.QueryCanceled


class _Cur:
    """Raises `exc` on the first `fail_times` executes, then works."""

    def __init__(self, fail_times: int, exc: type[Exception] = DEADLOCK) -> None:
        self.fail_times = fail_times
        self.exc = exc
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
            raise self.exc("simulated")
        self.rowcount = len(params[0]) if params else 0


class _Conn:
    def __init__(self, cur: _Cur) -> None:
        self._cur = cur

    def cursor(self) -> _Cur:
        return self._cur


TOUCHES = [db.touch_listings, db.touch_listings_by_id]


@pytest.mark.parametrize("touch", TOUCHES)
@pytest.mark.parametrize("exc", [DEADLOCK, LOCKWAIT])
def test_a_single_lock_loss_is_retried_and_the_chunk_still_counts(monkeypatch, touch, exc) -> None:
    slept: list[float] = []
    monkeypatch.setattr(db.time, "sleep", slept.append)
    cur = _Cur(fail_times=1, exc=exc)
    total = touch(_Conn(cur), [1, 2, 3])
    assert total == 3                       # the retry completed the chunk
    assert len(slept) == 1                  # and paused once before doing so
    # 1 failed execute + 2 successful executes (react CTE + bulk bump)
    assert cur.calls == 3


@pytest.mark.parametrize("touch", TOUCHES)
@pytest.mark.parametrize("exc", [DEADLOCK, LOCKWAIT])
def test_a_persistent_lock_loss_gives_up_and_raises(monkeypatch, touch, exc) -> None:
    """Retrying forever would turn a lock fight into a hung walk. After the
    budgeted attempts the error propagates exactly as before, so the category
    is reported failed rather than silently half-touched."""
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)
    cur = _Cur(fail_times=99, exc=exc)
    with pytest.raises(exc):
        touch(_Conn(cur), [1, 2, 3])
    assert cur.calls == db._TOUCH_DEADLOCK_ATTEMPTS


@pytest.mark.parametrize("touch", TOUCHES)
def test_no_lock_loss_means_no_sleep_and_one_pass(monkeypatch, touch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(db.time, "sleep", slept.append)
    cur = _Cur(fail_times=0)
    assert touch(_Conn(cur), [7, 8]) == 2
    assert slept == []
    assert cur.calls == 2


def test_the_deadlock_backoff_grows_with_the_attempt(monkeypatch) -> None:
    """A double race should wait longer the second time, not hammer the lock."""
    slept: list[float] = []
    monkeypatch.setattr(db.time, "sleep", slept.append)
    db.touch_listings(_Conn(_Cur(fail_times=2)), [1])
    assert slept == [db._TOUCH_DEADLOCK_DELAY * 1, db._TOUCH_DEADLOCK_DELAY * 2]


def test_a_lock_wait_cancel_pauses_longer_than_a_deadlock(monkeypatch) -> None:
    """After a lock-wait cancel the statement has already sat the full
    statement_timeout; the holder is usually committing, so the pause is long
    enough for it to finish releasing rather than an immediate re-queue."""
    slept: list[float] = []
    monkeypatch.setattr(db.time, "sleep", slept.append)
    db.touch_listings_by_id(_Conn(_Cur(fail_times=1, exc=LOCKWAIT)), [1])
    assert slept == [db._TOUCH_LOCKWAIT_DELAY]
    assert db._TOUCH_LOCKWAIT_DELAY > db._TOUCH_DEADLOCK_DELAY


@pytest.mark.parametrize("touch", TOUCHES)
def test_an_unrelated_error_is_not_retried(monkeypatch, touch) -> None:
    """Only a lost lock fight is transient. A constraint or syntax error would
    fail identically on every attempt, and retrying it just hides the bug for
    three rounds of sleep."""
    slept: list[float] = []
    monkeypatch.setattr(db.time, "sleep", slept.append)
    cur = _Cur(fail_times=1, exc=psycopg.errors.UniqueViolation)
    with pytest.raises(psycopg.errors.UniqueViolation):
        touch(_Conn(cur), [1])
    assert slept == []
    assert cur.calls == 1
