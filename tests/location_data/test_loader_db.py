"""The loader's connection contract (04 C1.7 rule 1): a session the load owns, proven.

A silent fall-through to the TRANSACTION pooler is the failure this guards: the session
GUCs would be discarded and the 3.02 M-row COPY would run under the pooler's statement
timeout, dying somewhere in the middle of a load that looked configured.
"""

from __future__ import annotations

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
