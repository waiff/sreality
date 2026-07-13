"""Tests for the bazos-enrichment batch INGEST lane (scripts.ingest_enrich_batch).

Exercises the REAL toolkit.bazos_enrichment.resolve_current +
persist_enrich_result (not monkeypatched — those already have unit coverage
via columns_from_extraction, but here we check the SQL they actually issue
against a recording fake connection) so a normal successful ingest is proven
to write the listings UPDATE + cache row, and a batch result with no
tool_use block is proven to hit the negative-cache path — both at the
50%-discounted cost.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import scripts.ingest_enrich_batch as ing


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._last_sql = ""

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._last_sql = sql
        self._conn.calls.append((sql, params))

    def executemany(self, sql: str, rows: list[Any]) -> None:
        self._conn.calls.append((sql, rows))

    def fetchone(self) -> tuple[Any, ...] | None:
        if "WITH latest AS" in self._last_sql:
            return self._conn.target_row
        if "SELECT count(*)" in self._last_sql:
            return (0,)
        return None

    def fetchall(self) -> list[Any]:
        return []


class _Txn:
    def __enter__(self) -> "_Txn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, *, target_row: tuple[Any, ...]) -> None:
        self.target_row = target_row
        self.calls: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Txn:
        return _Txn()

    def commit(self) -> None:
        pass

    @property
    def marks(self) -> list[tuple[Any, Any, Any]]:
        out = []
        for sql, params in self.calls:
            if "UPDATE listing_description_enrichment_batch_requests SET status" in sql:
                out.append(params)
        return out


class _FakeLLM:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    def record_external_call(
        self, *, called_for: str, provider: str, model: str, usage: Any, cost_usd: float,
    ) -> int:
        self.recorded.append({
            "called_for": called_for, "provider": provider,
            "model": model, "cost_usd": cost_usd,
        })
        return 555


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=1000, output_tokens=100, cache_read_tokens=0, cache_write_tokens=0,
    )


def _completion(tool_calls: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(tool_calls=tool_calls, usage=_usage())


def _target_row(
    *, snapshot_id: int = 42,
    floor: Any = None, total_floors: Any = None, has_balcony: Any = None,
    has_lift: Any = None, has_parking: Any = None, building_type: Any = None,
    condition: Any = None, energy_rating: Any = None,
) -> tuple[Any, ...]:
    return (
        snapshot_id, "some description", floor, total_floors, has_balcony,
        has_lift, has_parking, building_type, condition, energy_rating,
    )


def _fake_compute_cost_usd(*, price: Any, model: str, usage: Any) -> float:
    return 0.02


def test_batch_discount_constant() -> None:
    assert ing.BATCH_DISCOUNT == 0.5


def test_ingest_one_writes_gap_columns_at_half_cost() -> None:
    conn = _FakeConn(target_row=_target_row())
    llm = _FakeLLM()
    tool_call = SimpleNamespace(
        name="record_listing",
        input={
            "floor": {"value": 3, "confidence": "high"},
            "total_floors": {"value": None, "confidence": "high"},
            "has_balcony": {"value": True, "confidence": "high"},
            "has_lift": {"value": None, "confidence": "high"},
            "has_parking": {"value": None, "confidence": "high"},
            "building_type": {"value": None, "confidence": "high"},
            "condition": {"value": None, "confidence": "high"},
            "energy_rating": {"value": None, "confidence": "high"},
        },
    )

    cost = ing._ingest_one(
        conn, llm, _fake_compute_cost_usd, price=None,
        completion=_completion([tool_call]), model="claude-haiku-4-5",
        sreality_id=7, snapshot_id=42, request_id=99,
    )

    assert cost == 0.01  # $0.02 * BATCH_DISCOUNT
    assert llm.recorded == [{
        "called_for": "enrich_listing_description", "provider": "anthropic",
        "model": "claude-haiku-4-5", "cost_usd": 0.01,
    }]
    update_calls = [c for c in conn.calls if c[0].startswith("UPDATE listings")]
    assert len(update_calls) == 1
    assert update_calls[0][1] == (3, True, 7)
    insert_calls = [c for c in conn.calls if "INSERT INTO listing_description_enrichments" in c[0]]
    assert len(insert_calls) == 1
    assert conn.marks == [("scored", None, 99)]


def test_ingest_one_no_tool_use_block_negative_caches() -> None:
    # A batch result with NO record_listing tool_use block -> the same
    # no_extraction negative-cache path the sync enricher takes, not an error.
    conn = _FakeConn(target_row=_target_row())
    llm = _FakeLLM()

    cost = ing._ingest_one(
        conn, llm, _fake_compute_cost_usd, price=None,
        completion=_completion([]), model="claude-haiku-4-5",
        sreality_id=8, snapshot_id=42, request_id=100,
    )

    assert cost == 0.01
    update_calls = [c for c in conn.calls if c[0].startswith("UPDATE listings")]
    assert update_calls == []
    insert_calls = [c for c in conn.calls if "INSERT INTO listing_description_enrichments" in c[0]]
    assert len(insert_calls) == 1
    assert '"no_extraction": true' in insert_calls[0][1][2]
    assert conn.marks == [("scored", None, 100)]


def test_ingest_one_stale_snapshot_marks_errored_no_write() -> None:
    # resolve_current guards against writing an extraction whose snapshot is no
    # longer the listing's latest — mapped snapshot_id=42 but the fresh row is 43.
    conn = _FakeConn(target_row=_target_row(snapshot_id=43))
    llm = _FakeLLM()
    tool_call = SimpleNamespace(
        name="record_listing",
        input={k: {"value": None, "confidence": "high"} for k in (
            "floor", "total_floors", "has_balcony", "has_lift",
            "has_parking", "building_type", "condition", "energy_rating")},
    )

    cost = ing._ingest_one(
        conn, llm, _fake_compute_cost_usd, price=None,
        completion=_completion([tool_call]), model="claude-haiku-4-5",
        sreality_id=9, snapshot_id=42, request_id=101,
    )

    assert cost is None
    assert llm.recorded == []  # no LLM cost recorded when we bail before persisting
    assert conn.marks == [("errored", "snapshot no longer current", 101)]


def test_process_batch_ended_ingests_mixed_results(monkeypatch: Any) -> None:
    conn = _FakeConn(target_row=_target_row())
    llm = _FakeLLM()

    class _FakeProvider:
        def poll_batch(self, provider_batch_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                ended=True, raw_status="ended",
                counts={"succeeded": 2, "errored": 0},
            )

        def price_for(self, model: str) -> None:
            return None

        def iter_batch_results(self, provider_batch_id: str):
            yield SimpleNamespace(
                custom_id="s1-snap42", status="succeeded", error=None,
                completion=_completion([SimpleNamespace(
                    name="record_listing",
                    input={k: {"value": None, "confidence": "high"} for k in (
                        "floor", "total_floors", "has_balcony", "has_lift",
                        "has_parking", "building_type", "condition", "energy_rating")},
                )]),
            )
            yield SimpleNamespace(
                custom_id="s2-snap42", status="succeeded", error=None,
                completion=_completion([]),  # no_extraction
            )

    monkeypatch.setattr(
        ing, "_pending_requests",
        lambda c, batch_id: {
            "s1-snap42": {"id": 1, "sreality_id": 1, "snapshot_id": 42},
            "s2-snap42": {"id": 2, "sreality_id": 2, "snapshot_id": 42},
        },
    )
    import api.providers as providers_mod
    monkeypatch.setattr(providers_mod, "compute_cost_usd", _fake_compute_cost_usd)

    scored = ing._process_batch(
        conn, _FakeProvider(), llm,
        {"id": 1, "provider_batch_id": "batch_1", "model": "claude-haiku-4-5"},
    )

    assert scored == 2
    assert len(llm.recorded) == 2
    assert all(r["cost_usd"] == 0.01 for r in llm.recorded)
    finalize_calls = [c for c in conn.calls if "SET status = %s" in c[0]
                      and "listing_description_enrichment_batches" in c[0]]
    assert len(finalize_calls) == 1
    assert finalize_calls[0][1][0] == "ingested"  # both requests resolved
