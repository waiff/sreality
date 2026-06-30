"""Unit tests for scraper.db.run_resilient — the transient-DB-error retry +
reconnect guard the long-held drain/walk connections run every DB op through.

Hermetic: no real sockets. A transient error is any psycopg.OperationalError
subclass (connection drop, deadlock, serialization rollback, admin shutdown);
everything else fails loud. sleep is monkeypatched to a no-op so the backoff
adds no wall-clock to the suite.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from scraper import db


class _FakeConn:
    """A connection whose `broken` flag drives the reconnect-vs-same-conn choice."""

    def __init__(self, *, broken: bool = False) -> None:
        self.closed = False
        self.broken = broken
        self.rolled_back = 0

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(db.time, "sleep", lambda *_a, **_k: None)


def test_returns_result_and_same_conn_on_first_success():
    conn = _FakeConn()
    result, live = db.run_resilient(
        conn, lambda c: ("ok", c), reconnect=lambda: _FakeConn(), base_delay=0,
    )
    assert result == ("ok", conn)
    assert live is conn


def test_retries_deadlock_on_same_conn_then_succeeds():
    conn = _FakeConn(broken=False)  # a deadlock leaves the conn usable
    calls = {"n": 0}

    def op(c: Any) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.errors.DeadlockDetected("deadlock detected")
        return "done"

    result, live = db.run_resilient(
        conn, op, reconnect=lambda: pytest.fail("must not reconnect on a deadlock"),
        base_delay=0,
    )
    assert result == "done"
    assert live is conn            # same connection reused (no reconnect)
    assert calls["n"] == 2
    assert conn.rolled_back == 1   # aborted txn cleared before the same-conn retry


def test_reconnects_on_dropped_connection_then_succeeds():
    dead = _FakeConn(broken=True)  # SSL EOF / pooler recycle -> socket is dead
    fresh = _FakeConn()
    calls = {"n": 0}

    def op(c: Any) -> str:
        calls["n"] += 1
        if c is dead:
            raise psycopg.OperationalError("SSL error: unexpected eof while reading")
        return "wrote"

    result, live = db.run_resilient(
        dead, op, reconnect=lambda: fresh, base_delay=0,
    )
    assert result == "wrote"
    assert live is fresh       # caller gets the fresh connection back
    assert dead.closed         # the broken one was closed
    assert calls["n"] == 2


def test_non_transient_error_raises_immediately_without_retry():
    conn = _FakeConn()
    calls = {"n": 0}

    def op(c: Any) -> str:
        calls["n"] += 1
        raise psycopg.errors.UniqueViolation("duplicate key")  # a bug, not an outage

    with pytest.raises(psycopg.errors.UniqueViolation):
        db.run_resilient(conn, op, reconnect=lambda: _FakeConn(), base_delay=0)
    assert calls["n"] == 1     # no retry on a non-transient (non-Operational) error


def test_raises_after_exhausting_attempts_on_persistent_outage():
    conn = _FakeConn(broken=True)
    calls = {"n": 0}

    def op(c: Any) -> str:
        calls["n"] += 1
        raise psycopg.OperationalError("connection refused")

    with pytest.raises(psycopg.OperationalError):
        db.run_resilient(
            conn, op, reconnect=lambda: _FakeConn(broken=True),
            attempts=3, base_delay=0,
        )
    assert calls["n"] == 3     # exactly `attempts` tries, then give up (run reds)


def test_reconnect_failure_is_itself_retried_within_budget():
    # The op drops the connection; the first reconnect attempt also fails
    # transiently (the pooler is briefly down), the second succeeds.
    dead = _FakeConn(broken=True)
    fresh = _FakeConn()
    reconnects = {"n": 0}

    def reconnect() -> _FakeConn:
        reconnects["n"] += 1
        if reconnects["n"] == 1:
            raise psycopg.OperationalError("pooler not ready")
        return fresh

    def op(c: Any) -> str:
        if c is dead:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return "ok"

    result, live = db.run_resilient(
        dead, op, reconnect=reconnect, attempts=5, base_delay=0,
    )
    assert result == "ok"
    assert live is fresh
    assert reconnects["n"] == 2


def test_is_transient_db_error_classification():
    assert db.is_transient_db_error(psycopg.OperationalError("x"))
    assert db.is_transient_db_error(psycopg.errors.DeadlockDetected("x"))
    assert db.is_transient_db_error(psycopg.errors.SerializationFailure("x"))
    assert db.is_transient_db_error(psycopg.errors.AdminShutdown("x"))
    # Bugs in the SQL / data are NOT transient and must fail loud.
    assert not db.is_transient_db_error(psycopg.errors.UniqueViolation("x"))
    assert not db.is_transient_db_error(psycopg.errors.UndefinedColumn("x"))
    assert not db.is_transient_db_error(ValueError("x"))
