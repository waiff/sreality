"""Tests for the pure mapping/normalization in toolkit.bazos_enrichment, the
LLM call contract (slim tool + forced tool_choice + negative cache), and the
selection SQL / error-abort loop in scripts.enrich_listing_descriptions
(no DB / no LLM needed)."""

from __future__ import annotations

import json
from typing import Any

from toolkit.bazos_enrichment import (
    ENRICH_LISTING_TOOL,
    _FIELD_MAP,
    _norm_building_type,
    _norm_condition,
    _norm_energy,
    columns_from_extraction,
    enrich_listing_description,
)


def _env(value: Any, confidence: str = "high") -> dict[str, Any]:
    return {"value": value, "confidence": confidence}


_EMPTY = {c: None for c in (
    "floor", "total_floors", "has_balcony", "has_lift", "has_parking",
    "building_type", "condition", "energy_rating",
)}


def test_fills_gap_columns_high_confidence():
    extraction = {
        "floor": _env(3),
        "has_balcony": _env(True),
        "has_lift": _env(False),
        "building_type": _env("Cihla"),
        "condition": _env("velmi dobrý stav"),
        "energy_rating": _env("g"),
    }
    out = columns_from_extraction(extraction, dict(_EMPTY))
    assert out == {
        "floor": 3,
        "has_balcony": True,
        "has_lift": False,
        "building_type": "cihla",
        "condition": "velmi_dobry",
        "energy_rating": "G",
    }


def test_low_confidence_is_dropped():
    out = columns_from_extraction({"floor": _env(3, "low")}, dict(_EMPTY))
    assert out == {}


def test_null_value_is_dropped():
    out = columns_from_extraction({"has_lift": _env(None)}, dict(_EMPTY))
    assert out == {}


def test_never_overwrites_present_column():
    current = dict(_EMPTY, floor=2, condition="dobry")
    extraction = {"floor": _env(5), "condition": _env("novostavba"), "has_lift": _env(True)}
    out = columns_from_extraction(extraction, current)
    assert out == {"has_lift": True}  # floor + condition already set → untouched


def test_deterministic_fields_are_not_mapped():
    # price / area / disposition / locality are authoritative from the HTML and
    # must never be written by the enricher even if the LLM returns them.
    extraction = {
        "price_czk": _env(1_000_000), "area_m2": _env(50), "disposition": _env("2+kk"),
        "locality": _env("Praha"), "category_main": _env("byt"),
    }
    assert columns_from_extraction(extraction, dict(_EMPTY)) == {}


def test_floor_plausibility_guard():
    # floor above the building's total (both from this extraction) -> dropped,
    # total kept.
    out = columns_from_extraction(
        {"floor": _env(8), "total_floors": _env(5)}, dict(_EMPTY)
    )
    assert out == {"total_floors": 5}
    # total from the already-stored column (e.g. the deterministic parser) guards
    # an LLM floor too.
    out = columns_from_extraction({"floor": _env(9)}, dict(_EMPTY, total_floors=4))
    assert out == {}
    # an out-of-band floor is dropped.
    assert columns_from_extraction({"floor": _env(99)}, dict(_EMPTY)) == {}
    # a plausible floor under the total is kept.
    out = columns_from_extraction(
        {"floor": _env(3), "total_floors": _env(6)}, dict(_EMPTY)
    )
    assert out == {"floor": 3, "total_floors": 6}


def test_normalizers():
    assert _norm_condition("Po rekonstrukci") == "po_rekonstrukci"
    assert _norm_condition("velmi dobrý stav") == "velmi_dobry"
    assert _norm_building_type("Smíšená") == "smisena"
    assert _norm_energy("b") == "B"
    assert _norm_energy("not a rating") is None
    assert _norm_energy(None) is None


def test_select_pending_sql_invariants():
    import importlib

    m = importlib.import_module("scripts.enrich_listing_descriptions")

    class _Cur:
        def __init__(self) -> None:
            self.sql = ""
            self.params: Any = None

        def execute(self, sql: str, params: Any = None) -> None:
            self.sql, self.params = sql, params

        def fetchall(self):
            return [(1,), (2,)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def __init__(self) -> None:
            self.cur = _Cur()

        def cursor(self):
            return self.cur

    conn = _Conn()
    out = m._select_pending(conn, source="bazos", model="claude-haiku-4-5", max_age_days=0, limit=500)
    assert out == [1, 2]
    sql = conn.cur.sql
    assert "l.source = %s" in sql
    assert "l.description IS NOT NULL" in sql
    assert "NOT EXISTS (" in sql
    assert "listing_description_enrichments e" in sql
    # The fix: the latest-snapshot check is a per-listing correlated subquery, so
    # there must be NO global `MAX(id) ... GROUP BY` over the whole snapshots table
    # (that form aggregated every listing's history and timed out).
    assert "GROUP BY" not in sql
    assert "MAX(id)" in sql
    # Model-keyed (migration 249): a model upgrade re-attempts every listing.
    assert "e.model = %s" in sql
    # Source-scoped + freshest-first reuses the existing (source, first_seen_at) index.
    assert "ORDER BY l.first_seen_at DESC" in sql
    assert "LIMIT %s" in sql
    # (source, model, limit) — no freshness param when max_age_days=0.
    assert conn.cur.params == ("bazos", "claude-haiku-4-5", 500)

    # max_age_days>0 adds the freshness clause and threads (source, interval, model, limit).
    conn2 = _Conn()
    m._select_pending(conn2, source="bazos", model="m2", max_age_days=7, limit=500)
    assert "last_seen_at > now() - %s::interval" in conn2.cur.sql
    assert conn2.cur.params == ("bazos", "7 days", "m2", 500)


# ----------------------------------------------------------------------
# LLM call contract: slim tool, forced tool_choice, negative cache
# ----------------------------------------------------------------------

def test_slim_tool_matches_field_map():
    """The tool schema and the consumer must agree exactly: every field the
    model can set is consumed, and nothing unconsumed (especially the 8000-char
    description echo the full RECORD_LISTING_TOOL forced) pads the output."""
    props = ENRICH_LISTING_TOOL["input_schema"]["properties"]
    assert set(props) == set(_FIELD_MAP)
    assert set(ENRICH_LISTING_TOOL["input_schema"]["required"]) == set(_FIELD_MAP)
    assert "description" not in props
    assert "warnings" not in props
    for schema in props.values():
        assert schema["properties"]["confidence"]["enum"] == ["high", "medium", "low"]


class _FlowCur:
    """Cursor fake dispatching on SQL substrings for the enrich flow."""

    def __init__(self, conn: "_FlowConn") -> None:
        self._conn = conn

    def execute(self, sql: str, params: Any = None) -> None:
        self._result: Any = None
        if "FROM listings l JOIN latest" in sql:
            self._result = self._conn.target_row
        elif "SELECT 1 FROM listing_description_enrichments" in sql:
            self._result = (1,) if self._conn.cached else None
        elif "INSERT INTO listing_description_enrichments" in sql:
            self._conn.inserts.append(params)
        elif sql.startswith("UPDATE listings SET"):
            self._conn.updates.append((sql, params))
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FlowConn:
    def __init__(self, *, description: str = "Byt s výtahem.", cached: bool = False) -> None:
        # (snapshot_id, description, floor, total_floors, has_balcony,
        #  has_lift, has_parking, building_type, condition, energy_rating)
        self.target_row = (901, description, None, None, None, None, None, None, None, None)
        self.cached = cached
        self.inserts: list[Any] = []
        self.updates: list[Any] = []
        self.commits = 0

    def cursor(self):
        return _FlowCur(self)

    def commit(self):
        self.commits += 1


class _FakeResp:
    def __init__(self, tool_calls: list[dict[str, Any]]) -> None:
        self.tool_calls = tool_calls
        self.llm_call_id = 77
        self.cost_usd = 0.001


class _FakeLLM:
    def __init__(self, tool_calls: list[dict[str, Any]]) -> None:
        self._tool_calls = tool_calls
        self.calls: list[dict[str, Any]] = []

    def call(self, **kwargs: Any) -> _FakeResp:
        self.calls.append(kwargs)
        return _FakeResp(self._tool_calls)


def test_enrich_uses_slim_tool_and_forced_choice():
    conn = _FlowConn()
    llm = _FakeLLM([{
        "id": "tu_1", "name": "record_listing",
        "input": {"has_lift": _env(True)},
    }])
    res = enrich_listing_description(conn, llm, 123)
    assert res["status"] == "ok"
    kwargs = llm.calls[0]
    assert kwargs["tools"] == [ENRICH_LISTING_TOOL]
    assert kwargs["tool_choice"] == "record_listing"
    # Headroom for reasoning models (gpt-5-mini spends max_completion_tokens on
    # reasoning before the tool call; 512 truncated it — 99.6% no_extraction).
    assert kwargs["max_tokens"] == 4096
    assert len(conn.inserts) == 1
    assert conn.updates and "has_lift = %s" in conn.updates[0][0]
    assert conn.commits == 1


def test_no_extraction_writes_negative_cache_row():
    """A response without the tool call must still cache a marker row —
    without one the selector re-bills the same listing every run forever."""
    conn = _FlowConn()
    llm = _FakeLLM([])  # prose answer / truncated: no tool_use came back
    res = enrich_listing_description(conn, llm, 123)
    assert res["status"] == "no_extraction"
    assert len(conn.inserts) == 1
    params = conn.inserts[0]
    assert params[0] == 123 and params[1] == 901
    assert json.loads(params[2]) == {"no_extraction": True}
    assert json.loads(params[3]) == {}
    assert conn.commits == 1
    assert conn.updates == []


def test_cached_snapshot_skips_llm():
    conn = _FlowConn(cached=True)
    llm = _FakeLLM([])
    res = enrich_listing_description(conn, llm, 123)
    assert res["status"] == "cached"
    assert llm.calls == []


# ----------------------------------------------------------------------
# script loop: consecutive-error abort
# ----------------------------------------------------------------------

class _NullConn:
    def rollback(self):
        pass


def test_enrich_loop_aborts_after_consecutive_errors():
    import importlib

    m = importlib.import_module("scripts.enrich_listing_descriptions")

    def _boom(conn: Any, llm: Any, sid: int, *, model: str) -> dict[str, Any]:
        raise RuntimeError("provider down")

    ids = list(range(1, 101))
    stats, aborted = m._enrich_loop(
        _NullConn(), object(), ids,
        model="claude-haiku-4-5", max_cost_usd=10.0, max_seconds=0, enrich=_boom,
    )
    assert aborted is True
    # Stopped at the threshold, not after burning all 100 ids.
    assert stats["errors"] == m._MAX_CONSECUTIVE_ERRORS


def test_enrich_loop_success_resets_error_streak():
    import importlib

    m = importlib.import_module("scripts.enrich_listing_descriptions")
    calls = {"n": 0}

    def _flaky(conn: Any, llm: Any, sid: int, *, model: str) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise RuntimeError("transient")
        return {"status": "ok", "cost_usd": 0.001, "filled": ["floor"]}

    ids = list(range(1, 21))
    stats, aborted = m._enrich_loop(
        _NullConn(), object(), ids,
        model="claude-haiku-4-5", max_cost_usd=10.0, max_seconds=0, enrich=_flaky,
    )
    assert aborted is False
    assert stats["ok"] == 10
    assert stats["errors"] == 10
