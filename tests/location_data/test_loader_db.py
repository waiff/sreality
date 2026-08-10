"""The loader's connection contract (04 C1.7 rule 1): a session the load owns, proven.

A silent fall-through to the TRANSACTION pooler is the failure this guards: the session
GUCs would be discarded and the 3.02 M-row COPY would run under the pooler's statement
timeout, dying somewhere in the middle of a load that looked configured.
"""

from __future__ import annotations

import psycopg
import pytest

from location_data import loader_db


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)
        if sql == "SHOW statement_timeout":
            self.conn.result = self.conn.statement_timeout
        elif sql == "SHOW lock_timeout":
            self.conn.result = self.conn.lock_timeout
        else:
            self.conn.result = None

    def fetchone(self):
        return (self.conn.result,)


class _Conn:
    def __init__(self, statement_timeout="0", lock_timeout="5s"):
        self.statement_timeout = statement_timeout
        self.lock_timeout = lock_timeout
        self.executed: list[str] = []
        self.closed = False
        self.result = None

    def cursor(self):
        return _Cur(self)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _DeadConn(_Conn):
    """A session the pooler already dropped: every statement raises, as psycopg's does."""

    def cursor(self):
        raise psycopg.OperationalError("the connection is closed")


def _patch(monkeypatch, conn, *, expect: str | None = None):
    def _connect(url=None, **kwargs):
        if expect is not None:
            assert url == expect
        return conn

    monkeypatch.setattr(loader_db.db, "connect", _connect)
    monkeypatch.setattr(loader_db.db, "connect_session", _connect)


def test_no_session_url_aborts_naming_the_env_var(monkeypatch):
    monkeypatch.delenv("LOCATION_DB_DIRECT_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DB_SESSION_URL", raising=False)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://pooler:6543/x")
    with pytest.raises(loader_db.LoadAborted) as exc:
        loader_db.open_loader_connection()
    assert "LOCATION_DB_DIRECT_URL" in str(exc.value)
    assert "SUPABASE_DB_SESSION_URL" in str(exc.value)


def test_the_direct_url_wins_and_the_gucs_are_read_back(monkeypatch):
    conn = _Conn()
    monkeypatch.setenv("LOCATION_DB_DIRECT_URL", "postgres://u:p@db.example:5432/postgres")
    monkeypatch.setenv("SUPABASE_DB_SESSION_URL", "postgres://u:p@pooler:5432/postgres")
    _patch(monkeypatch, conn, expect="postgres://u:p@db.example:5432/postgres")
    assert loader_db.open_loader_connection() is conn
    assert "SET statement_timeout = 0" in conn.executed
    assert "SHOW statement_timeout" in conn.executed
    assert not conn.closed


def test_gucs_that_did_not_take_abort_and_close_the_connection(monkeypatch):
    conn = _Conn(statement_timeout="2min")
    monkeypatch.delenv("LOCATION_DB_DIRECT_URL", raising=False)
    monkeypatch.setenv("SUPABASE_DB_SESSION_URL", "postgres://u:p@pooler:5432/postgres")
    _patch(monkeypatch, conn)
    with pytest.raises(loader_db.LoadAborted) as exc:
        loader_db.open_loader_connection()
    assert "statement_timeout" in str(exc.value)
    assert conn.closed


def test_the_logged_endpoint_never_carries_credentials():
    assert loader_db._endpoint("postgres://user:secret@db.example:5432/postgres") == \
        "db.example:5432"


# --- failure-path isolation ---------------------------------------------------------
#
# The 2026-08 boundary run lost its session mid-pack and then tried to record the
# discrepancy on that same dead handle: the run's visible error became "the connection is
# closed" and the SSL drop that actually killed it never reached the log.

_DISCREPANCY = dict(entity_kind="obec", entity_code=576069,
                    discrepancy="boundary_load_failed", detail={"error": "SSL EOF"})


def test_the_failure_path_writes_on_a_fresh_connection_not_the_dead_one(monkeypatch):
    dead, fresh = _DeadConn(), _Conn()
    monkeypatch.setattr(loader_db, "open_loader_connection", lambda: fresh)
    loader_db.record_discrepancy(dead, 4, own_connection=True, **_DISCREPANCY)
    assert any("registry_load_discrepancies" in sql for sql in fresh.executed)
    assert fresh.closed  # short-lived: the failure path must not hold a second backend


def test_the_default_mode_still_writes_on_the_caller_connection(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(loader_db, "open_loader_connection",
                        lambda: pytest.fail("must not open a connection in the normal mode"))
    loader_db.record_discrepancy(conn, 4, **_DISCREPANCY)
    assert any("registry_load_discrepancies" in sql for sql in conn.executed)


def test_a_failure_path_write_that_itself_fails_never_raises(monkeypatch):
    """Whatever broke the load is the exception the operator needs; this row is not."""
    def _boom():
        raise psycopg.OperationalError("the connection is closed")

    monkeypatch.setattr(loader_db, "open_loader_connection", _boom)
    loader_db.record_discrepancy(None, 4, own_connection=True, **_DISCREPANCY)


def test_abort_raises_the_original_reason_even_if_the_row_cannot_be_written(monkeypatch):
    def _boom(*args, **kwargs):
        raise psycopg.OperationalError("the connection is closed")

    monkeypatch.setattr(loader_db, "record_discrepancy", _boom)
    with pytest.raises(loader_db.LoadAborted) as exc:
        loader_db.abort(_DeadConn(), 4, reason="assertion_failed",
                        detail={"assertion": "golden_point"})
    assert "assertion_failed" in str(exc.value)
    assert "golden_point" in str(exc.value)
    assert "the connection is closed" not in str(exc.value)
