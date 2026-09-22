"""The rails every heal over `listings` shares, and why each exists.

Lifted here when the area/basis/land heals they were written beside were replaced by the
one re-parse seam (`scripts/reparse.py`), so the assertions outlive the jobs that first
needed them: a `SET LOCAL` binds to nothing on an autocommit connection unless the batch
takes the transaction too, an armed `lock_timeout` fires as LockNotAvailable and must be
replayed rather than raised, and a 15-minute read-model rebuild must not look like a failed
heal.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any

import psycopg
import pytest

from scripts import backfill_support as support
from scripts import reparse as mod

_GUARDED_SQL = mod._update_sql(("has_lift",))


# --- SET LOCAL only binds inside a transaction -------------------------------


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self.rowcount = 3

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append(sql)
        if self._conn.fail_once:
            self._conn.fail_once = False
            raise psycopg.errors.LockNotAvailable("lock_timeout")


class _Tx:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Tx":
        self._conn.transactions += 1
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, fail_once: bool = False) -> None:
        self.executed: list[str] = []
        self.transactions = 0
        self.fail_once = fail_once

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Tx:
        return _Tx(self)


def test_setup_statements_run_inside_one_transaction_before_the_batch() -> None:
    conn = _Conn()
    rows = support.execute_with_lock_retry(
        conn, _GUARDED_SQL, {"ids": [1, 2, 3]},
        label="test", setup=mod._BATCH_GUARDS,
    )
    assert rows == 3
    assert conn.transactions == 1
    assert conn.executed[:len(mod._BATCH_GUARDS)] == list(mod._BATCH_GUARDS)
    assert conn.executed[-1] == _GUARDED_SQL


def test_a_lock_timeout_is_replayed_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    # An armed `lock_timeout` fires as LockNotAvailable, not QueryCanceled — the batch is
    # idempotent, so the rail pauses and replays it rather than failing the run.
    monkeypatch.setattr(support.time, "sleep", lambda _s: None)
    conn = _Conn(fail_once=True)
    assert support.execute_with_lock_retry(
        conn, _GUARDED_SQL, {"ids": [1]}, label="test", setup=mod._BATCH_GUARDS,
    ) == 3
    assert conn.transactions == 2


def test_without_setup_the_statement_still_runs_unwrapped() -> None:
    conn = _Conn()
    support.execute_with_lock_retry(conn, "UPDATE x SET y = 1", {}, label="test")
    assert conn.transactions == 0
    assert conn.executed == ["UPDATE x SET y = 1"]


def test_the_retry_window_outlasts_the_mf_yield_lock_hold() -> None:
    """The rail exists for the ~90 s of row locks the hourly MF-yield recompute holds on
    `listings`. A 5 s lock_timeout over 3 attempts came to ~60 s — it would give up while
    the holder was still working — so the bounded wait and the backoff both had to grow."""
    lock_seconds = 30.0
    assert f"lock_timeout = '{int(lock_seconds)}s'" in " ".join(mod._BATCH_GUARDS)
    attempts = support._LOCK_RETRY_ATTEMPTS
    backoff = sum(support._LOCK_WAIT_DELAY * a for a in range(1, attempts))
    assert backoff + attempts * lock_seconds > 120.0


def test_the_resume_cursor_never_advances_past_a_page_that_was_not_written() -> None:
    """The regression this pins: assigning the cursor from the page BEFORE the UPDATE means
    a failed page is skipped on `--after <cursor>` resume, silently and for ever. The
    assignment sits after the write, and the failure path names the last written id."""
    body = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    tail = body.split("if updates and not args.dry_run:", 1)[1]

    assert tail.index("execute_with_lock_retry") < tail.index("cursor = int(rows[-1][0])")
    assert "Resume with --after %d" in tail


# --- waiting for a read-model rebuild gap ------------------------------------
#
# The first live dispatch of a sibling heal exited 3 because a rebuild_% was running, which
# paints a red X on correct behaviour. rebuild_browse_list() runs every 15 minutes and holds
# its locks for 5-10 of them, so refusing outright means colliding about half the time and
# never running.


class _FakeRebuildCursor:
    def __init__(self, conn: "_FakeRebuildConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_FakeRebuildCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.polls += 1

    def fetchone(self) -> tuple[int]:
        # Report `active` from the scripted sequence, then 0 for ever after.
        seq = self._conn.active_sequence
        return (seq.pop(0) if seq else 0,)


class _FakeRebuildConn:
    def __init__(self, active_sequence: list[int]) -> None:
        self.active_sequence = list(active_sequence)
        self.polls = 0

    def cursor(self) -> _FakeRebuildCursor:
        return _FakeRebuildCursor(self)


def test_it_starts_immediately_when_no_rebuild_is_running() -> None:
    conn = _FakeRebuildConn([0])
    assert support.wait_for_rebuild_gap(conn, 900.0) is True
    assert conn.polls == 1


def test_it_waits_for_the_gap_then_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(support.time, "sleep", slept.append)
    conn = _FakeRebuildConn([1, 1, 0])
    assert support.wait_for_rebuild_gap(conn, 900.0) is True
    assert conn.polls == 3
    assert slept == [support._REBUILD_POLL_SECONDS] * 2


def test_an_expired_budget_proceeds_anyway_rather_than_failing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    # The contention is I/O only — these jobs take no lock a rebuild can block on — so a
    # busy cluster must not mean the work never happens.
    monkeypatch.setattr(support.time, "sleep", lambda _s: None)
    conn = _FakeRebuildConn([1] * 50)
    with caplog.at_level(logging.WARNING, logger=support.LOG.name):
        assert support.wait_for_rebuild_gap(conn, 0.0) is False
    assert "starting anyway" in caplog.text


def test_the_wait_never_returns_a_nonzero_exit_path() -> None:
    # Pins the contract: the helper reports whether it found a gap, and main() proceeds
    # either way. Reintroducing `return 3` would make a routine 15-minute rebuild look
    # like a failed heal.
    assert "return 3" not in pathlib.Path(mod.__file__).read_text(encoding="utf-8")
