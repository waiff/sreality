"""Hermetic tests for summarize_region_dispositions.

No DB connection: a scripted cursor returns prepared rows in order.
LLMClient is replaced with a fake that records calls and returns
prepared response objects. Same scaffolding shape as test_summaries.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import pytest

from toolkit import region_annotations as ra


# ---- Tool-schema invariants -----------------------------------------------


def test_tool_schema_shape():
    schema = ra.RECORD_DISPOSITION_ANNOTATIONS_TOOL["input_schema"]
    assert schema["required"] == ["annotations"]
    item = schema["properties"]["annotations"]["items"]
    assert set(item["required"]) == {"disposition", "text"}


# ---- Renderable gate ------------------------------------------------------


def test_no_renderable_dispositions_skips_llm_and_db():
    """Dispositions below MIN_BOX_N or without a box never reach the LLM."""
    conn = _make_conn([])  # any DB access would raise (plan is empty)
    llm = _FakeLLM([])

    res = ra.summarize_region_dispositions(
        conn, llm,  # type: ignore[arg-type]
        region_key="k1",
        dispositions=[
            {"disposition": "1+kk", "n": 2, "ppm2_box": _box(n=2)},   # too few
            {"disposition": "2+kk", "n": 4, "ppm2_box": None},        # no box
        ],
    )

    assert llm.calls == []
    assert res["data"]["annotations"] == {}
    assert res["data"]["cache_hit"] is False
    assert res["metadata"]["result_count"] == 0


# ---- Cache hit path -------------------------------------------------------


def test_cache_hit_does_not_call_llm():
    cached = {"2+kk": "Tight cluster around 480 Kč/m²."}
    plan = [
        # _cache_lookup → hit
        ("fetchone", (cached, "claude-sonnet-4-5", 0.0012)),
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([])

    res = ra.summarize_region_dispositions(
        conn, llm,  # type: ignore[arg-type]
        region_key="praha|byt|pronajem",
        dispositions=[{"disposition": "2+kk", "n": 30, "ppm2_box": _box(n=30)}],
    )

    assert llm.calls == []
    assert res["data"]["cache_hit"] is True
    assert res["data"]["annotations"] == cached


# ---- Cache miss path ------------------------------------------------------


def test_cache_miss_calls_llm_then_writes():
    plan = [
        ("fetchone", None),          # cache miss
        ("execute_write", None),     # _cache_store
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([_llm_response({"2+kk": "Clusters around 480 Kč/m²."})])

    res = ra.summarize_region_dispositions(
        conn, llm,  # type: ignore[arg-type]
        region_key="praha|byt|pronajem",
        dispositions=[{"disposition": "2+kk", "n": 30, "ppm2_box": _box(n=30)}],
        ppm2_overall={"p25": 400, "p50": 480, "p75": 560},
    )

    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["called_for"] == "summarize_region_dispositions"
    assert call["tools"][0]["name"] == "record_disposition_annotations"
    assert call["model"] == "claude-sonnet-4-5"
    assert conn.transactions_opened == 1
    assert res["data"]["cache_hit"] is False
    assert res["data"]["annotations"] == {"2+kk": "Clusters around 480 Kč/m²."}


def test_force_refresh_skips_cache_lookup():
    plan = [
        ("execute_write", None),     # straight to _cache_store, no lookup
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([_llm_response({"2+kk": "x"})])

    ra.summarize_region_dispositions(
        conn, llm,  # type: ignore[arg-type]
        region_key="k",
        dispositions=[{"disposition": "2+kk", "n": 30, "ppm2_box": _box(n=30)}],
        force_refresh=True,
    )

    lookups = [
        e for e in conn.cursor_obj.executed
        if "FROM region_disposition_annotations" in e[0]
    ]
    assert lookups == []


# ---- Output filtering -----------------------------------------------------


def test_annotations_filtered_to_input_dispositions():
    """LLM hallucinating a disposition we didn't ask about is dropped."""
    plan = [
        ("fetchone", None),
        ("execute_write", None),
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([_llm_response({"2+kk": "ok", "9+9": "should be dropped"})])

    res = ra.summarize_region_dispositions(
        conn, llm,  # type: ignore[arg-type]
        region_key="k",
        dispositions=[{"disposition": "2+kk", "n": 30, "ppm2_box": _box(n=30)}],
    )

    assert res["data"]["annotations"] == {"2+kk": "ok"}


# ---- LLM response validation ----------------------------------------------


def test_missing_tool_call_raises():
    plan = [("fetchone", None)]
    conn = _make_conn(plan)
    llm = _FakeLLM([_LLMResp(text="oops", tool_calls=[])])

    with pytest.raises(ra.RegionAnnotationError, match="did not invoke"):
        ra.summarize_region_dispositions(
            conn, llm,  # type: ignore[arg-type]
            region_key="k",
            dispositions=[{"disposition": "2+kk", "n": 30, "ppm2_box": _box(n=30)}],
        )


# ---- region_hash ----------------------------------------------------------


def test_region_hash_is_sha256_of_key():
    assert ra._region_hash("hello") == hashlib.sha256(b"hello").hexdigest()


# ---- Envelope -------------------------------------------------------------


def test_envelope_metadata_shape():
    plan = [("fetchone", ({"2+kk": "x"}, "claude-sonnet-4-5", 0.001))]
    conn = _make_conn(plan)
    res = ra.summarize_region_dispositions(
        conn, _FakeLLM([]),  # type: ignore[arg-type]
        region_key="k",
        dispositions=[{"disposition": "2+kk", "n": 30, "ppm2_box": _box(n=30)}],
    )
    md = res["metadata"]
    assert md["tool"] == "summarize_region_dispositions"
    assert md["filters_used"] == {
        "region_key": "k", "min_box_n": 5, "force_refresh": False,
    }
    assert md["result_count"] == 1
    assert md["data_freshness"] is None
    assert md["queried_at"]


# ---- Helpers --------------------------------------------------------------


def _box(*, n: int) -> dict[str, Any]:
    return {"n": n, "min": 300, "p25": 440, "median": 480, "p75": 540, "max": 900}


@dataclass
class _LLMResp:
    text: str
    tool_calls: list[dict[str, Any]]
    model: str = "claude-sonnet-4-5"
    cost_usd: float = 0.0012
    llm_call_id: int = 777
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    duration_ms: int = 0
    completion: Any = field(default=None)


def _llm_response(mapping: dict[str, str]) -> _LLMResp:
    return _LLMResp(
        text="",
        tool_calls=[{
            "name": "record_disposition_annotations",
            "input": {
                "annotations": [
                    {"disposition": k, "text": v} for k, v in mapping.items()
                ],
            },
        }],
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
            raise AssertionError(f"execute past plan end (sql={sql[:80]!r})")
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
