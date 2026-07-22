"""Wave 1 metering + atomic submit-time gates (Phase 1 items J + A9).

Hermetic unit tests: the pure key/canonicalization helpers, the pre-parse gate
orchestration (_prepare_metered_submit, every branch), the atomic gated
_insert_run SQL assembly + no-row handling, create_estimation_run's metered
routing (short-circuit / 429 / cleared), and the terminal usage_ledger write.
The gate's live atomicity (ON CONFLICT partial-index inference + the
INSERT...SELECT WHERE counts) is validated separately by EXPLAIN against the
real schema; here the sub-helpers are stubbed so each branch is exercised
without a DB.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from api import estimation_runs as er
from api import schemas as s
from api.dependencies import SYSTEM_ACCOUNT_ID

ACC = "11111111-1111-1111-1111-111111111111"


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
        return self._conn.rows.pop(0) if self._conn.rows else self._conn.default_row


class _FakeConn:
    def __init__(self, rows: list[Any] | None = None, default_row: Any = (42,)) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.rows = list(rows or [])
        self.default_row = default_row

    def transaction(self) -> _Ctx:
        return _Ctx()

    def cursor(self) -> _Cur:
        return _Cur(self)


def _agent_body(**kw: Any) -> s.CreateEstimationIn:
    return s.CreateEstimationIn(
        url="https://www.sreality.cz/detail/1", source="extension",
        mode="agent", **kw)


# --- pure helpers --------------------------------------------------------------


def test_is_privileged() -> None:
    assert er._is_privileged(None) is True                       # internal caller
    assert er._is_privileged({"legacy": True}) is True           # static token
    assert er._is_privileged({"is_admin": True}) is True
    assert er._is_privileged({"app_metadata": {"is_admin": True}}) is True
    assert er._is_privileged({"sub": "u1"}) is False             # real tenant


def test_canonical_url_collapses_variance() -> None:
    a = er._canonical_url("HTTP://www.Sreality.CZ/detail/9/?utm_source=x&b=2&a=1#frag")
    b = er._canonical_url("https://www.sreality.cz/detail/9?a=1&b=2")
    assert a == b
    assert "utm_source" not in a and "#frag" not in a and a == a.rstrip("/")


def test_idempotency_key_forms() -> None:
    assert er._idempotency_key(s.CreateEstimationIn(sreality_id=7)) == "sid:7"
    k = er._idempotency_key(s.CreateEstimationIn(url="https://x/1/?utm_a=1"))
    assert k.startswith("url:") and "utm_a" not in k
    # two spellings of the same listing collapse to one key
    assert er._idempotency_key(s.CreateEstimationIn(url="https://x/1")) == \
        er._idempotency_key(s.CreateEstimationIn(url="https://x/1/"))


# --- gate orchestration --------------------------------------------------------


def _stub(monkeypatch: Any, *, entitled=("active", True, 3), inflight=None,
          month=0, concurrent=0, cap=3, budget=True) -> None:
    monkeypatch.setattr(er, "_budget_enabled", lambda conn: budget)
    monkeypatch.setattr(er, "_resolve_entitlement", lambda conn, aid: entitled)
    monkeypatch.setattr(er, "_find_inflight_run", lambda conn, aid, k: inflight)
    monkeypatch.setattr(er, "_count_agent_runs_this_month", lambda conn, aid: month)
    monkeypatch.setattr(er, "_count_inflight_agent_runs", lambda conn, aid: concurrent)
    monkeypatch.setattr(er, "_concurrency_cap", lambda conn: cap)


def test_gate_ungated_paths(monkeypatch: Any) -> None:
    _stub(monkeypatch)
    # claims None (internal) — never even reads settings
    assert er._prepare_metered_submit(object(), None, _agent_body(), ACC) is None
    # admin bypass
    assert er._prepare_metered_submit(
        object(), {"is_admin": True}, _agent_body(), ACC) is None
    # deterministic is free
    det = s.CreateEstimationIn(url="https://x/1", source="extension", mode="deterministic")
    assert er._prepare_metered_submit(object(), {"sub": "u"}, det, ACC) is None
    # SYSTEM account (unresolved) not metered
    assert er._prepare_metered_submit(
        object(), {"sub": "u"}, _agent_body(), SYSTEM_ACCOUNT_ID) is None


def test_gate_disabled_flag_bypasses(monkeypatch: Any) -> None:
    _stub(monkeypatch, budget=False)
    assert er._prepare_metered_submit(object(), {"sub": "u"}, _agent_body(), ACC) is None


def test_gate_blocks_raw_spec_overrides(monkeypatch: Any) -> None:
    _stub(monkeypatch)
    body = _agent_body(spec_overrides={"lat": 50.0})
    with pytest.raises(HTTPException) as e:
        er._prepare_metered_submit(object(), {"sub": "u"}, body, ACC)
    assert e.value.status_code == 403


def test_gate_rejects_unentitled(monkeypatch: Any) -> None:
    _stub(monkeypatch, entitled=("canceled", True, 3))
    with pytest.raises(HTTPException) as e:
        er._prepare_metered_submit(object(), {"sub": "u"}, _agent_body(), ACC)
    assert e.value.status_code == 403
    _stub(monkeypatch, entitled=("active", False, 3))   # estimations agenda off
    with pytest.raises(HTTPException) as e2:
        er._prepare_metered_submit(object(), {"sub": "u"}, _agent_body(), ACC)
    assert e2.value.status_code == 403


def test_gate_short_circuits_duplicate(monkeypatch: Any) -> None:
    _stub(monkeypatch, inflight={"id": 99, "status": "running"})
    dec = er._prepare_metered_submit(object(), {"sub": "u"}, _agent_body(), ACC)
    assert dec is not None and dec.short_circuit_run == {"id": 99, "status": "running"}


def test_gate_429_over_quota(monkeypatch: Any) -> None:
    _stub(monkeypatch, month=3)   # quota is 3
    with pytest.raises(HTTPException) as e:
        er._prepare_metered_submit(object(), {"sub": "u"}, _agent_body(), ACC)
    assert e.value.status_code == 429 and "limit" in e.value.detail.lower()


def test_gate_429_over_concurrency(monkeypatch: Any) -> None:
    _stub(monkeypatch, month=0, concurrent=3, cap=3)
    with pytest.raises(HTTPException) as e:
        er._prepare_metered_submit(object(), {"sub": "u"}, _agent_body(), ACC)
    assert e.value.status_code == 429 and "in progress" in e.value.detail.lower()


def test_gate_clears_with_decision(monkeypatch: Any) -> None:
    _stub(monkeypatch, month=1, concurrent=0, cap=3, entitled=("trialing", True, 10))
    dec = er._prepare_metered_submit(object(), {"sub": "u"}, _agent_body(), ACC)
    assert dec is not None and dec.short_circuit_run is None
    assert dec.quota == 10 and dec.concurrency_cap == 3
    assert dec.idempotency_key.startswith("url:")


# --- _insert_run gated form ----------------------------------------------------


def _base_fields() -> dict[str, Any]:
    return {
        "account_id": ACC, "source": "extension", "mode": "agent",
        "status": "pending", "estimate_kind": "rent",
        "input_url": "https://x/1", "input_sreality_id": None,
    }


def test_insert_run_gated_builds_atomic_sql() -> None:
    conn = _FakeConn(default_row=(7,))
    dec = er._MeterDecision("sid:1", 3, 2)
    rid = er._insert_run(conn, gate=dec, idempotency_key="sid:1", **_base_fields())
    assert rid == 7
    sql, params = conn.executed[0]
    assert "ON CONFLICT (account_id, idempotency_key)" in sql
    assert "date_trunc('month', now())" in sql
    assert "idempotency_key" in sql
    assert params["_g_quota"] == 3 and params["_g_cap"] == 2


def test_insert_run_gated_no_row_returns_none() -> None:
    conn = _FakeConn(default_row=None)   # WHERE excluded / ON CONFLICT DO NOTHING
    dec = er._MeterDecision("sid:1", 3, 2)
    assert er._insert_run(conn, gate=dec, idempotency_key="sid:1", **_base_fields()) is None


def test_insert_run_ungated_omits_gate_sql() -> None:
    conn = _FakeConn(default_row=(7,))
    er._insert_run(conn, **_base_fields())   # no gate, no idempotency_key
    sql, _ = conn.executed[0]
    assert "ON CONFLICT" not in sql and "VALUES" in sql and "idempotency_key" not in sql


# --- create_estimation_run metered routing -------------------------------------


def _stub_pipeline(monkeypatch: Any) -> None:
    monkeypatch.setattr(er, "_job_lane_enabled", lambda conn: False)
    res = er._Resolution(
        input_url="https://x/1", input_sreality_id=1,
        target_spec={"lat": 50.0, "lng": 14.0}, source_kind="sreality",
        parse_confidence="high", parse_confidence_per_field=None, source_html=None)
    monkeypatch.setattr(er, "_resolve_input", lambda *a, **k: res)
    monkeypatch.setattr(er, "load_filter_defaults", lambda *a, **k: object())
    monkeypatch.setattr(er, "_build_target", lambda *a, **k: object())
    monkeypatch.setattr(er, "_build_filters", lambda *a, **k: object())
    monkeypatch.setattr(er, "_derive_yield_inputs", lambda b, r: (None, None, None))
    monkeypatch.setattr(er, "_fetch_run", lambda conn, rid: {"id": rid})
    monkeypatch.setattr(er, "_execute_estimation_run", lambda *a, **k: None)
    monkeypatch.setattr("api.skills.load_skill", lambda conn, name: type(
        "_Sk", (), {"name": name, "version": 1})())


def test_create_run_short_circuits_before_parse(monkeypatch: Any) -> None:
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(er, "_resolve_input", lambda *a, **k: pytest.fail(
        "short-circuit must return BEFORE the parse"))
    monkeypatch.setattr(er, "_prepare_metered_submit", lambda *a, **k: er._MeterDecision(
        "sid:1", 3, 3, short_circuit_run={"id": 55}))
    out = er.create_estimation_run(
        _FakeConn(), None, None, _agent_body(), account_id=ACC, claims={"sub": "u"})
    assert out == {"id": 55}


def test_create_run_gate_rejection_propagates(monkeypatch: Any) -> None:
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(er, "_prepare_metered_submit", lambda *a, **k: (_ for _ in ()).throw(
        HTTPException(status_code=429, detail="Monthly agent-estimation limit reached (3/mo)")))
    with pytest.raises(HTTPException) as e:
        er.create_estimation_run(
            _FakeConn(), None, None, _agent_body(), account_id=ACC, claims={"sub": "u"})
    assert e.value.status_code == 429


def test_create_run_atomic_none_becomes_429(monkeypatch: Any) -> None:
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(er, "_prepare_metered_submit", lambda *a, **k: er._MeterDecision(
        "sid:1", 3, 3))
    monkeypatch.setattr(er, "_insert_run", lambda *a, **k: None)   # gate rejected at INSERT
    monkeypatch.setattr(er, "_find_inflight_run", lambda conn, aid, k: None)
    with pytest.raises(HTTPException) as e:
        er.create_estimation_run(
            _FakeConn(), None, None, _agent_body(), account_id=ACC, claims={"sub": "u"})
    assert e.value.status_code == 429


def test_create_run_atomic_none_returns_existing_on_conflict(monkeypatch: Any) -> None:
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(er, "_prepare_metered_submit", lambda *a, **k: er._MeterDecision(
        "sid:1", 3, 3))
    monkeypatch.setattr(er, "_insert_run", lambda *a, **k: None)
    monkeypatch.setattr(er, "_find_inflight_run", lambda conn, aid, k: {"id": 88})
    out = er.create_estimation_run(
        _FakeConn(), None, None, _agent_body(), account_id=ACC, claims={"sub": "u"})
    assert out == {"id": 88}


# --- usage_ledger terminal write -----------------------------------------------


def test_record_usage_writes_for_metered_agent_run() -> None:
    conn = _FakeConn(rows=[(ACC, "agent")])
    er._record_usage(conn, 42)
    ledger = [e for e in conn.executed if "usage_ledger" in e[0]]
    assert len(ledger) == 1
    assert ledger[0][1]["aid"] == ACC and ledger[0][1]["rid"] == 42


def test_record_usage_skips_deterministic_and_system() -> None:
    conn = _FakeConn(rows=[(ACC, "deterministic")])
    er._record_usage(conn, 42)
    assert not [e for e in conn.executed if "usage_ledger" in e[0]]

    conn2 = _FakeConn(rows=[(SYSTEM_ACCOUNT_ID, "agent")])
    er._record_usage(conn2, 43)
    assert not [e for e in conn2.executed if "usage_ledger" in e[0]]
