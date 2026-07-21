"""Tests for account_id stamping on estimation_runs writes (Wave 1 W1-1).

Hermetic: exercises `_insert_run`'s SQL assembly directly with a fake cursor.
`create_estimation_run`'s full path needs a live SrealityClient + LLMClient to
reach `_resolve_input`, well outside this unit; the column list building —
whether `account_id` lands in the INSERT and binds the caller's resolved
value rather than silently defaulting to NULL — is the actual risk surface
this PR touches (an explicit NULL bind overrides the column DEFAULT, unlike
omitting the column).
"""

from __future__ import annotations

from typing import Any

from api import estimation_runs as er
from api.dependencies import SYSTEM_ACCOUNT_ID


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
        return (42,)


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def transaction(self) -> _Ctx:
        return _Ctx()

    def cursor(self) -> _Cur:
        return _Cur(self)


def _base_fields() -> dict[str, Any]:
    return {
        "source": "extension", "mode": "deterministic", "status": "pending",
        "estimate_kind": "rent", "input_url": "https://example.test/1",
        "input_sreality_id": 1,
    }


def test_insert_run_stamps_explicit_account_id() -> None:
    conn = _FakeConn()
    tenant_id = "11111111-1111-1111-1111-111111111111"
    er._insert_run(conn, account_id=tenant_id, **_base_fields())
    sql, params = conn.executed[0]
    assert "account_id" in sql
    assert params["account_id"] == tenant_id


def test_insert_run_omitted_account_id_binds_none_not_default() -> None:
    """Documents the sharp edge the account_id column addition introduces:
    _insert_run's setdefault(col, None) means a caller that forgets to pass
    account_id binds an explicit NULL, which OVERRIDES the column's SYSTEM
    DEFAULT (migration 291) rather than falling back to it. Every real caller
    (create_estimation_run, _persist_failed_run) guards against this by
    resolving `account_id or SYSTEM_ACCOUNT_ID` before calling _insert_run —
    this test pins the underlying behavior those guards exist to prevent."""
    conn = _FakeConn()
    er._insert_run(conn, **_base_fields())
    _, params = conn.executed[0]
    assert params["account_id"] is None


def test_create_estimation_run_defaults_to_system_account(monkeypatch: Any) -> None:
    """account_id=None (legacy/unclaimed caller) must stamp the platform
    SYSTEM account, not NULL -- preserving pre-Wave-1 behavior (every run
    used to land on the column DEFAULT, which was the SYSTEM account)."""
    from api import schemas as s

    conn = _FakeConn()
    resolution = er._Resolution(
        input_url="https://example.test/1", input_sreality_id=1,
        target_spec={"lat": 50.0, "lon": 14.0, "area_m2": 40, "disposition": "2+kk"},
        source_kind="sreality", parse_confidence="high",
        parse_confidence_per_field=None, source_html=None,
    )
    monkeypatch.setattr(er, "_resolve_input", lambda *a, **k: resolution)
    monkeypatch.setattr(er, "load_filter_defaults", lambda *a, **k: object())
    monkeypatch.setattr(
        er, "_build_target", lambda *a, **k: object(),
    )
    monkeypatch.setattr(er, "_build_filters", lambda *a, **k: object())
    monkeypatch.setattr(
        er, "_derive_yield_inputs", lambda body, res: (None, None, None),
    )
    monkeypatch.setattr(
        er, "_execute_estimation_run", lambda *a, **k: None,
    )
    monkeypatch.setattr(er, "_fetch_run", lambda conn, run_id: {"id": run_id})

    body = s.CreateEstimationIn(url="https://example.test/1", source="extension")
    er.create_estimation_run(conn, sreality_client=None, llm_client=None, body=body)

    insert_sql, insert_params = conn.executed[0]
    assert "account_id" in insert_sql
    assert insert_params["account_id"] == SYSTEM_ACCOUNT_ID
