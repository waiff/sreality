"""Hermetic tests for extract_building_units.

No DB connection, no R2, no Anthropic SDK: a scripted cursor returns
prepared rows, image_storage is monkey-patched to fall back, and the
LLMClient is replaced with a fake that returns prepared tool calls.

Mirrors tests/toolkit/test_summaries.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from toolkit import building_extraction


_NOW = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)


def _example_payload() -> dict[str, Any]:
    return {
        "units": [
            {
                "unit_id": "u1", "label": "ground floor flat", "floor": "ground",
                "area_m2": 72.0, "disposition": "2+kk",
                "condition": "po_rekonstrukci", "is_potential": False,
                "source": "both", "notes": None,
            },
            {
                "unit_id": "u2", "label": "attic", "floor": "attic",
                "area_m2": 55.0, "disposition": None,
                "condition": "unknown", "is_potential": True,
                "source": "floor_plan", "notes": "nezkolaudované podkroví",
            },
        ],
        "building": {
            "floor_count": 2, "has_attic": True, "year_built": 1932,
            "construction_type": "cihla", "total_area_m2": 127.0,
            "condition": "dobry", "notes": None,
        },
        "confidence": "high",
        "warnings": [],
    }


def _listing_row() -> tuple[Any, ...]:
    return (
        "dum", "prodej", 9_500_000, "kus", 127.0, 245.0, None,
        "Kralupy nad Vltavou", "Mělník",
        "cihla", "dobry", "C", "osobni",
    )


# ---- Tool-schema invariants ------------------------------------------------


def test_tool_schema_requires_units_building_confidence_warnings():
    schema = building_extraction.RECORD_BUILDING_UNITS_TOOL["input_schema"]
    assert set(schema["required"]) == {"units", "building", "confidence", "warnings"}


def test_unit_schema_requires_unit_id_and_is_potential():
    schema = building_extraction.RECORD_BUILDING_UNITS_TOOL["input_schema"]
    unit = schema["properties"]["units"]["items"]
    assert "unit_id" in unit["required"]
    assert "is_potential" in unit["required"]


def test_confidence_enum_is_three_values():
    schema = building_extraction.RECORD_BUILDING_UNITS_TOOL["input_schema"]
    assert set(schema["properties"]["confidence"]["enum"]) == {"high", "medium", "low"}


# ---- Cache hit path --------------------------------------------------------


def test_cache_hit_does_not_call_llm(monkeypatch):
    monkeypatch.setattr(
        building_extraction.image_storage, "is_configured", lambda: False,
    )
    payload = _example_payload()
    plan = [
        # _resolve_snapshot
        ("fetchone", (42, _NOW, {"text": "..."})),
        # _resolve_max_images runs before cache lookup (it gates the
        # vision call's image cap, so we resolve it up-front even on
        # cache hit).
        ("fetchone", (8,)),
        # _cache_lookup → hit
        ("fetchone", (
            payload["units"], payload["building"], payload["confidence"],
            payload["warnings"], 0, "claude-sonnet-4-5", 0.012,
        )),
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([])

    res = building_extraction.extract_building_units(
        conn, llm, sreality_id=123,  # type: ignore[arg-type]
    )

    assert llm.calls == []
    assert res["data"]["cache_hit"] is True
    assert len(res["data"]["units"]) == 2
    assert res["data"]["units"][0]["unit_id"] == "u1"
    assert res["metadata"]["result_count"] == 2


# ---- Cache miss path -------------------------------------------------------


def test_cache_miss_calls_llm_then_writes(monkeypatch):
    """No R2 → description-only path; should fire one LLM call and store."""
    monkeypatch.setattr(
        building_extraction.image_storage, "is_configured", lambda: False,
    )
    payload = _example_payload()
    plan = [
        ("fetchone", (42, _NOW, {"text": "Vinohradský činžák"})),  # snapshot
        ("fetchone", (8,)),                                          # _resolve_max_images
        ("fetchone", None),                                          # cache miss
        ("fetchone", _listing_row()),                                # _fetch_listing
        ("execute_write", None),                                     # _cache_store
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([_llm_response(payload)])

    res = building_extraction.extract_building_units(
        conn, llm, sreality_id=123,  # type: ignore[arg-type]
    )

    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["called_for"] == "extract_building_units"
    assert call["tools"][0]["name"] == "record_building_units"
    assert call["model"] == "claude-sonnet-4-5"
    assert res["data"]["cache_hit"] is False
    assert res["data"]["confidence"] == "medium", (
        "fallback warning should downgrade confidence from high"
    )
    # The fallback "R2 not configured" warning should be appended.
    assert any("R2" in w for w in res["data"]["warnings"])
    assert conn.transactions_opened == 1


# ---- Force refresh ---------------------------------------------------------


def test_force_refresh_skips_cache_lookup(monkeypatch):
    monkeypatch.setattr(
        building_extraction.image_storage, "is_configured", lambda: False,
    )
    payload = _example_payload()
    plan = [
        ("fetchone", (42, _NOW, {"text": "..."})),  # snapshot
        ("fetchone", (8,)),                          # _resolve_max_images
        ("fetchone", _listing_row()),                # listing
        ("execute_write", None),                     # cache write
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([_llm_response(payload)])

    building_extraction.extract_building_units(
        conn, llm, sreality_id=123, force_refresh=True,  # type: ignore[arg-type]
    )
    cache_reads = [
        e for e in conn.cursor_obj.executed
        if "FROM building_unit_extractions" in e[0]
    ]
    assert cache_reads == [], "force_refresh should skip cache lookup"


# ---- Snapshot resolution ---------------------------------------------------


def test_no_snapshot_raises():
    plan = [("fetchone", None)]
    conn = _make_conn(plan)
    llm = _FakeLLM([])
    with pytest.raises(building_extraction.BuildingExtractionError, match="no snapshot"):
        building_extraction.extract_building_units(
            conn, llm, sreality_id=999,  # type: ignore[arg-type]
        )


# ---- LLM response validation -----------------------------------------------


def test_missing_tool_call_raises(monkeypatch):
    monkeypatch.setattr(
        building_extraction.image_storage, "is_configured", lambda: False,
    )
    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", (8,)),
        ("fetchone", None),
        ("fetchone", _listing_row()),
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([_LLMResp(text="oops", tool_calls=[])])

    with pytest.raises(building_extraction.BuildingExtractionError, match="did not invoke"):
        building_extraction.extract_building_units(
            conn, llm, sreality_id=123,  # type: ignore[arg-type]
        )


def test_empty_units_raises(monkeypatch):
    monkeypatch.setattr(
        building_extraction.image_storage, "is_configured", lambda: False,
    )
    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", (8,)),
        ("fetchone", None),
        ("fetchone", _listing_row()),
    ]
    conn = _make_conn(plan)
    bad = {**_example_payload(), "units": []}
    llm = _FakeLLM([_LLMResp(
        text="",
        tool_calls=[{"name": "record_building_units", "input": bad}],
    )])
    with pytest.raises(building_extraction.BuildingExtractionError, match="no units"):
        building_extraction.extract_building_units(
            conn, llm, sreality_id=123,  # type: ignore[arg-type]
        )


# ---- Envelope --------------------------------------------------------------


def test_envelope_metadata_shape(monkeypatch):
    monkeypatch.setattr(
        building_extraction.image_storage, "is_configured", lambda: False,
    )
    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", (8,)),
        ("fetchone", (
            _example_payload()["units"],
            _example_payload()["building"],
            "high", [], 0, "claude-sonnet-4-5", 0.01,
        )),
    ]
    conn = _make_conn(plan)
    res = building_extraction.extract_building_units(
        conn, _FakeLLM([]), sreality_id=123,  # type: ignore[arg-type]
    )
    md = res["metadata"]
    assert md["tool"] == "extract_building_units"
    assert md["result_count"] == 2
    assert md["data_freshness"] == _NOW.isoformat()
    assert md["filters_used"]["sreality_id"] == 123


# ---- Helpers ---------------------------------------------------------------


@dataclass
class _LLMResp:
    text: str
    tool_calls: list[dict[str, Any]]
    model: str = "claude-sonnet-4-5"
    cost_usd: float = 0.012
    llm_call_id: int = 777
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    duration_ms: int = 0
    raw: Any = None


def _llm_response(payload: dict[str, Any]) -> _LLMResp:
    return _LLMResp(
        text="",
        tool_calls=[{"name": "record_building_units", "input": payload}],
    )


class _FakeLLM:
    def __init__(self, responses: list[_LLMResp]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def resolve_system_prompt(self, key: str) -> str:
        return f"[prompt for {key}]"

    def resolve_model(self, key: str) -> str:
        return "claude-sonnet-4-5"

    def call(self, **kwargs: Any) -> _LLMResp:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("Unexpected LLM.call")
        return self._responses.pop(0)


class _ScriptedCursor:
    def __init__(self, plan: list[tuple[str, Any]]) -> None:
        self._plan = plan
        self._idx = 0
        self.executed: list[tuple[str, Any]] = []
        self._next: tuple[str, Any] | None = None

    def execute(self, sql: str, params: Any = None) -> None:
        if self._idx >= len(self._plan):
            raise AssertionError(
                f"execute past plan end (sql={sql[:80]!r})"
            )
        step = self._plan[self._idx]
        self.executed.append((sql, params))
        if step[0] == "execute_write":
            self._idx += 1
            self._next = None
            return
        self._next = step

    def fetchone(self) -> Any:
        assert self._next is not None and self._next[0] == "fetchone"
        out = self._next[1]
        self._idx += 1
        self._next = None
        return out

    def fetchall(self) -> list[Any]:
        assert self._next is not None and self._next[0] == "fetchall"
        out = self._next[1] or []
        self._idx += 1
        self._next = None
        return out

    def __enter__(self) -> "_ScriptedCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Transaction:
    def __init__(self, conn: "_ScriptedConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Transaction":
        self._conn.transactions_opened += 1
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _ScriptedConn:
    def __init__(self, plan: list[tuple[str, Any]]) -> None:
        self.cursor_obj = _ScriptedCursor(plan)
        self.transactions_opened = 0

    def cursor(self) -> _ScriptedCursor:
        return self.cursor_obj

    def transaction(self) -> _Transaction:
        return _Transaction(self)


def _make_conn(plan: list[tuple[str, Any]]) -> _ScriptedConn:
    return _ScriptedConn(plan)
