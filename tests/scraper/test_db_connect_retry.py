"""Handshake-retry tests for scraper.db.connect / connect_session.

Hermetic: monkeypatches psycopg.connect and time.sleep so no real socket is
opened and no real delay elapses. Covers the Failure-A fix — a bounded retry on
a pooler handshake drop (OperationalError) — while a non-transient error (a
missing SUPABASE_DB_URL, a bug) still fails fast.
"""

from __future__ import annotations

import time
from typing import Any

import psycopg
import pytest

from scraper import db


def _flaky_connect(fail_times: int, result: Any):
    """Return a fake psycopg.connect that raises OperationalError `fail_times`
    times, then returns `result`. Records the call count on `.calls`."""
    state = {"calls": 0}

    def _connect(url, **kwargs):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        _connect.last_kwargs = kwargs
        _connect.last_url = url
        return result

    _connect.state = state
    return _connect


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)


def test_connect_retries_transient_then_succeeds(monkeypatch):
    sentinel = object()
    fake = _flaky_connect(fail_times=2, result=sentinel)
    monkeypatch.setattr(psycopg, "connect", fake)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://pooler:6543/db")

    assert db.connect() is sentinel
    assert fake.state["calls"] == 3  # 2 failures + 1 success (default attempts=3)
    # kwargs preserved on the winning attempt.
    assert fake.last_kwargs["autocommit"] is True
    assert fake.last_kwargs["prepare_threshold"] is None
    assert fake.last_kwargs["keepalives"] == 1


def test_connect_reraises_after_exhausting_attempts(monkeypatch):
    fake = _flaky_connect(fail_times=99, result=object())
    monkeypatch.setattr(psycopg, "connect", fake)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://pooler:6543/db")

    with pytest.raises(psycopg.OperationalError):
        db.connect()
    assert fake.state["calls"] == db._CONNECT_ATTEMPTS  # exactly the budget


def test_connect_custom_budget_is_honored(monkeypatch):
    """The API fail-fast path passes a smaller budget."""
    fake = _flaky_connect(fail_times=99, result=object())
    monkeypatch.setattr(psycopg, "connect", fake)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://pooler:6543/db")

    with pytest.raises(psycopg.OperationalError):
        db.connect(attempts=2, retry_delay=0.01)
    assert fake.state["calls"] == 2


def test_connect_does_not_retry_non_transient(monkeypatch):
    """A bug (ProgrammingError, not OperationalError) fails fast — no retry."""
    calls = {"n": 0}

    def _connect(url, **kwargs):
        calls["n"] += 1
        raise psycopg.ProgrammingError("boom")

    monkeypatch.setattr(psycopg, "connect", _connect)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://pooler:6543/db")

    with pytest.raises(psycopg.ProgrammingError):
        db.connect()
    assert calls["n"] == 1  # tried once, not retried


def test_connect_missing_url_fails_fast_without_touching_psycopg(monkeypatch):
    """A missing SUPABASE_DB_URL raises RuntimeError before psycopg.connect and
    is never retried (RuntimeError is not an OperationalError)."""
    calls = {"n": 0}
    monkeypatch.setattr(
        psycopg, "connect",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)

    with pytest.raises(RuntimeError):
        db.connect()
    assert calls["n"] == 0  # database_url() raised before psycopg.connect


def test_connect_session_retries_transient_then_succeeds(monkeypatch):
    sentinel = object()
    fake = _flaky_connect(fail_times=1, result=sentinel)
    monkeypatch.setattr(psycopg, "connect", fake)
    monkeypatch.setenv("SUPABASE_DB_SESSION_URL", "postgres://session:5432/db")

    assert db.connect_session() is sentinel
    assert fake.state["calls"] == 2
    # Session pooler: auto-prepare stays ON (no prepare_threshold override).
    assert "prepare_threshold" not in fake.last_kwargs
