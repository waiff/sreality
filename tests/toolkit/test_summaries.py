"""Hermetic tests for summarize_listing.

No DB connection: a scripted cursor returns prepared rows in order.
LLMClient is replaced with a fake that records calls and returns
prepared LLMResponse objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from toolkit import summaries


_NOW = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)


# ---- Tool-schema invariants -----------------------------------------------


def test_tool_schema_has_all_required_fields():
    schema = summaries.RECORD_LISTING_SUMMARY_TOOL["input_schema"]
    required = set(schema["required"])
    assert required == {
        "headline", "key_highlights", "concerns",
        "condition_assessment", "target_audience",
    }


def test_condition_enum_includes_unknown():
    schema = summaries.RECORD_LISTING_SUMMARY_TOOL["input_schema"]
    enum = schema["properties"]["condition_assessment"]["enum"]
    assert "unknown" in enum
    assert "excellent" in enum


# ---- Cache hit path -------------------------------------------------------


def test_cache_hit_does_not_call_llm():
    summary = _example_summary()
    plan = [
        # _resolve_snapshot
        ("fetchone", (42, _NOW, {"text": "..."})),
        # _cache_lookup → hit
        ("fetchone", (summary, "claude-sonnet-4-5", 0.0042)),
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([])

    res = summaries.summarize_listing(
        conn, llm,  # type: ignore[arg-type]
        sreality_id=123,
    )

    assert llm.calls == []
    assert res["data"]["cache_hit"] is True
    assert res["data"]["summary"] == summary
    assert res["data"]["sreality_id"] == 123
    assert res["data"]["snapshot_id"] == 42


# ---- Cache miss path ------------------------------------------------------


def test_cache_miss_calls_llm_then_writes():
    summary = _example_summary()
    plan = [
        ("fetchone", (42, _NOW, {"text": "Krásný byt"})),    # snapshot
        ("fetchone", None),                                    # cache miss
        ("fetchone", _listing_row()),                          # _fetch_listing
        ("execute_write", None),                               # _cache_store
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([_llm_response(summary)])

    res = summaries.summarize_listing(
        conn, llm,  # type: ignore[arg-type]
        sreality_id=123,
    )

    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["called_for"] == "summarize_listing"
    assert call["tools"][0]["name"] == "record_listing_summary"
    assert call["model"] == "claude-sonnet-4-5"
    assert conn.transactions_opened == 1
    assert res["data"]["cache_hit"] is False
    assert res["data"]["summary"] == summary


def test_force_refresh_skips_cache_lookup():
    summary = _example_summary()
    plan = [
        ("fetchone", (42, _NOW, {"text": "..."})),     # snapshot
        ("fetchone", _listing_row()),                  # listing
        ("execute_write", None),                       # cache write
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([_llm_response(summary)])

    summaries.summarize_listing(
        conn, llm, sreality_id=123, force_refresh=True,  # type: ignore[arg-type]
    )

    # Only one fetchone for snapshot + one for listing; no cache lookup.
    fetchones = [e for e in conn.cursor_obj.executed if "FROM listing_summaries" in e[0]]
    assert fetchones == []


# ---- Snapshot resolution --------------------------------------------------


def test_explicit_snapshot_id_filters_by_both_columns():
    plan = [
        ("fetchone", (777, _NOW, {"text": "..."})),
        ("fetchone", (_example_summary(), "claude-sonnet-4-5", 0.001)),
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([])

    summaries.summarize_listing(
        conn, llm, sreality_id=123, snapshot_id=777,  # type: ignore[arg-type]
    )

    snap_sql = conn.cursor_obj.executed[0]
    assert "WHERE id = %s AND sreality_id = %s" in snap_sql[0]
    assert snap_sql[1] == (777, 123)


def test_no_snapshot_raises():
    plan = [("fetchone", None)]
    conn = _make_conn(plan)
    llm = _FakeLLM([])
    with pytest.raises(summaries.SummarizeError, match="no snapshot"):
        summaries.summarize_listing(
            conn, llm, sreality_id=999,  # type: ignore[arg-type]
        )


# ---- LLM response validation ----------------------------------------------


def test_missing_tool_call_raises():
    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", None),
        ("fetchone", _listing_row()),
    ]
    conn = _make_conn(plan)
    # LLM returns a response with NO record_listing_summary call.
    llm = _FakeLLM([_LLMResp(text="oops", tool_calls=[])])

    with pytest.raises(summaries.SummarizeError, match="did not invoke"):
        summaries.summarize_listing(
            conn, llm, sreality_id=123,  # type: ignore[arg-type]
        )


def test_tool_call_missing_field_raises():
    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", None),
        ("fetchone", _listing_row()),
    ]
    conn = _make_conn(plan)
    bad = {"headline": "h", "key_highlights": [], "concerns": []}  # missing 2 fields
    llm = _FakeLLM([_LLMResp(
        text="",
        tool_calls=[{"name": "record_listing_summary", "input": bad}],
    )])

    with pytest.raises(summaries.SummarizeError, match="missing field"):
        summaries.summarize_listing(
            conn, llm, sreality_id=123,  # type: ignore[arg-type]
        )


# ---- Envelope -------------------------------------------------------------


def test_envelope_metadata_shape():
    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", (_example_summary(), "claude-sonnet-4-5", 0.0042)),
    ]
    conn = _make_conn(plan)
    res = summaries.summarize_listing(
        conn, _FakeLLM([]), sreality_id=123,  # type: ignore[arg-type]
    )
    md = res["metadata"]
    assert md["tool"] == "summarize_listing"
    assert md["filters_used"] == {
        "sreality_id": 123, "snapshot_id": None, "force_refresh": False,
    }
    assert md["result_count"] == 1
    assert md["data_freshness"] == _NOW.isoformat()
    assert md["queried_at"]


# ---- Helpers --------------------------------------------------------------


def _example_summary() -> dict[str, Any]:
    return {
        "headline": "Renovated 2+kk in Vinohrady",
        "key_highlights": ["balcony", "elevator"],
        "concerns": ["ground floor"],
        "condition_assessment": "good",
        "target_audience": "couple",
    }


def _listing_row() -> tuple[Any, ...]:
    return (
        "byt", "pronajem", 25000, "měsíc", 65.0, "2+kk",
        "Praha 2", "Praha 2", 3, True, False, True,
        "cihla", "po rekonstrukci", "B",
    )


@dataclass
class _LLMResp:
    text: str
    tool_calls: list[dict[str, Any]]
    model: str = "claude-sonnet-4-5"
    cost_usd: float = 0.0042
    llm_call_id: int = 555
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    duration_ms: int = 0
    raw: Any = None


def _llm_response(summary: dict[str, Any]) -> _LLMResp:
    return _LLMResp(
        text="",
        tool_calls=[{"name": "record_listing_summary", "input": summary}],
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
