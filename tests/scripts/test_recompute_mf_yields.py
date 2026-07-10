"""Hermetic tests for the MF-yields deadlock retry — scripted fake conn, no DB."""

from __future__ import annotations

from typing import Any

import pytest
from psycopg import errors

from scripts.recompute_mf_yields import recompute_with_retry


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        if "recompute_mf_gross_yields" in sql:
            self._conn.attempts += 1
            if self._conn.attempts <= self._conn.deadlocks:
                raise errors.DeadlockDetected("deadlock detected")

    def fetchone(self) -> tuple[int]:
        return (42,)


class _Tx:
    def __enter__(self) -> "_Tx":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False  # propagate, like a rolled-back psycopg transaction block


class _Conn:
    def __init__(self, deadlocks: int) -> None:
        self.deadlocks = deadlocks
        self.attempts = 0

    def transaction(self) -> _Tx:
        return _Tx()

    def cursor(self) -> _Cursor:
        return _Cursor(self)


def test_retries_once_after_deadlock_and_succeeds() -> None:
    conn = _Conn(deadlocks=1)
    assert recompute_with_retry(conn) == 42
    assert conn.attempts == 2


def test_no_deadlock_runs_once() -> None:
    conn = _Conn(deadlocks=0)
    assert recompute_with_retry(conn) == 42
    assert conn.attempts == 1


def test_second_deadlock_propagates() -> None:
    # Two consecutive deadlocks = a real contention problem; fail loudly, don't loop.
    conn = _Conn(deadlocks=2)
    with pytest.raises(errors.DeadlockDetected):
        recompute_with_retry(conn)
    assert conn.attempts == 2
