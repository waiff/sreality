"""Tests for account_id stamping on property_notes writes (Wave 1 W1-1).

Hermetic: property_notes has no owning-parent to derive account_id from by
trigger (unlike collection_properties/property_tags, migration 292) and no
column DEFAULT (unlike estimation_runs, migration 291) -- a tenant_conn
caller that leaves it unset fails the table's WITH CHECK closed. This pins
that create_note actually names the column in its INSERT.
"""

from __future__ import annotations

from typing import Any

from api import curation
from api import schemas as s


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((" ".join(sql.split()), params))

    def fetchone(self) -> Any:
        return (7, 99, "note body", None, "2026-07-21T00:00:00Z")


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def transaction(self) -> _Ctx:
        return _Ctx()

    def cursor(self) -> _Cur:
        return _Cur(self)


def test_create_note_stamps_explicit_account_id(monkeypatch: Any) -> None:
    monkeypatch.setattr(curation, "resolve_active_property_id", lambda conn, pid: pid)
    conn = _FakeConn()
    tenant_id = "22222222-2222-2222-2222-222222222222"
    curation.create_note(
        conn, 99, s.CreateNoteIn(body="hello"), account_id=tenant_id,
    )
    sql, params = conn.executed[0]
    assert "account_id" in sql
    assert params[-1] == tenant_id


def test_create_note_omitted_account_id_binds_none(monkeypatch: Any) -> None:
    """Documents the sharp edge: a caller on the (still) service-role bridge
    that passes no account_id binds NULL -- harmless there (RLS never
    applies), but would fail closed under tenant_conn. main.py's
    post_property_note always resolves one before calling in."""
    monkeypatch.setattr(curation, "resolve_active_property_id", lambda conn, pid: pid)
    conn = _FakeConn()
    curation.create_note(conn, 99, s.CreateNoteIn(body="hello"))
    _, params = conn.executed[0]
    assert params[-1] is None
