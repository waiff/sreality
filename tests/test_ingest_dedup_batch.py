"""Tests for the dedup-vision batch INGEST lane (scripts.ingest_dedup_batch).

Focus on _ingest_one: it records the (paid) llm_calls row and routes each
result by `kind` to the owning toolkit persist helper. The persist helpers are
monkeypatched (they have their own unit tests + write the same cache rows the
sync tools do); here we verify the ROUTING, the called_for tag, the cost/llm
plumbing, and the request status transitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

import scripts.ingest_dedup_batch as ing
import toolkit.image_classification as ic
import toolkit.visual_match as vm
from toolkit.visual_match import VisualMatchError


@dataclass
class _TC:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class _Usage:
    input_tokens: int = 100
    output_tokens: int = 20
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class _Completion:
    tool_calls: list[_TC]
    usage: _Usage = field(default_factory=_Usage)


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.calls.append((sql, params))


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    @property
    def marks(self) -> list[tuple[Any, Any, Any]]:
        # _mark_request: UPDATE ... SET status=%s, error=%s WHERE id=%s
        out = []
        for sql, params in self.calls:
            if "UPDATE dedup_batch_requests SET status" in sql:
                out.append(params)  # (status, error, id)
        return out


class _FakeLLM:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    def record_external_call(self, *, called_for: str, provider: str, model: str,
                             usage: Any, cost_usd: float) -> int:
        self.recorded.append({"called_for": called_for, "provider": provider,
                              "model": model, "cost_usd": cost_usd})
        return 77


def test_ingest_routes_classify(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_persist(conn, *, image_ids, tool_calls, model, llm_call_id, cost_usd):
        captured.update(image_ids=image_ids, model=model, call=llm_call_id,
                        cost=cost_usd, tcs=tool_calls)

    monkeypatch.setattr(ic, "persist_room_classifications", fake_persist)
    conn, llm = _FakeConn(), _FakeLLM()
    completion = _Completion([_TC("t", "record_room_types", {"rooms": []})])
    req = {"id": 5, "kind": "classify", "model": "claude-haiku-4-5",
           "sreality_id_a": 9, "sreality_id_b": None, "room_type": None,
           "image_ids": [101, 102]}

    outcome, cost = ing._ingest_one(conn, llm, req, completion=completion,
                                    model="claude-haiku-4-5", cost=0.002)

    assert outcome == "done" and cost == 0.002
    assert captured["image_ids"] == [101, 102] and captured["call"] == 77
    assert llm.recorded[0]["called_for"] == "classify_listing_images"
    assert conn.marks == [("done", None, 5)]


def test_ingest_routes_compare(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_persist(conn, *, sreality_id_a, sreality_id_b, room_type, tool_calls,
                     model, llm_call_id, cost_usd):
        captured.update(a=sreality_id_a, b=sreality_id_b, room=room_type,
                        model=model, call=llm_call_id, cost=cost_usd)

    monkeypatch.setattr(vm, "persist_visual_match", fake_persist)
    conn, llm = _FakeConn(), _FakeLLM()
    completion = _Completion([_TC("t", "record_visual_match", {"verdict": "High", "rationale": "x"})])
    req = {"id": 6, "kind": "compare", "model": "claude-sonnet-4-5",
           "sreality_id_a": 1, "sreality_id_b": 2, "room_type": "kitchen", "image_ids": None}

    outcome, cost = ing._ingest_one(conn, llm, req, completion=completion,
                                    model="claude-sonnet-4-5", cost=0.05)

    assert outcome == "done"
    assert captured["a"] == 1 and captured["b"] == 2 and captured["room"] == "kitchen"
    assert llm.recorded[0]["called_for"] == "compare_listings_visually"
    assert conn.marks == [("done", None, 6)]


def test_ingest_routes_site_plan(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_persist(conn, *, sreality_id_a, sreality_id_b, tool_calls, model,
                     llm_call_id, cost_usd):
        captured.update(a=sreality_id_a, b=sreality_id_b, model=model)

    monkeypatch.setattr(vm, "persist_site_plan_match", fake_persist)
    conn, llm = _FakeConn(), _FakeLLM()
    completion = _Completion([_TC("t", "record_site_plan_match", {"verdict": "same_unit", "rationale": "x"})])
    req = {"id": 7, "kind": "site_plan", "model": "claude-sonnet-4-5",
           "sreality_id_a": 3, "sreality_id_b": 4, "room_type": None, "image_ids": None}

    outcome, _ = ing._ingest_one(conn, llm, req, completion=completion,
                                 model="claude-sonnet-4-5", cost=0.04)

    assert outcome == "done"
    assert captured["a"] == 3 and captured["b"] == 4
    assert llm.recorded[0]["called_for"] == "compare_listing_site_plans"


def test_ingest_parse_error_marks_errored_but_records_cost(monkeypatch: Any) -> None:
    # A malformed tool call: the batch ran (cost is real, recorded) but persist
    # raises -> request marked errored, no cache row.
    def boom(conn, **kw):
        raise VisualMatchError("LLM did not invoke record_visual_match")

    monkeypatch.setattr(vm, "persist_visual_match", boom)
    conn, llm = _FakeConn(), _FakeLLM()
    completion = _Completion([_TC("t", "wrong_tool", {})])
    req = {"id": 8, "kind": "compare", "model": "claude-sonnet-4-5",
           "sreality_id_a": 1, "sreality_id_b": 2, "room_type": "kitchen", "image_ids": None}

    outcome, cost = ing._ingest_one(conn, llm, req, completion=completion,
                                    model="claude-sonnet-4-5", cost=0.05)

    assert outcome == "errored" and cost == 0.05
    assert len(llm.recorded) == 1  # the real cost is still audited
    assert conn.marks[0][0] == "errored" and conn.marks[0][2] == 8


def test_batch_discount_constant() -> None:
    assert ing.BATCH_DISCOUNT == 0.5


def test_rooms_to_produced_maps_index_to_image_id() -> None:
    # The classify-ingest mapping: tool-call index -> the ordered image_id the
    # request sent. Unknown room -> 'other', bad confidence -> 'low', out-of-range
    # index skipped.
    rooms = [
        {"index": 0, "room_type": "kitchen", "confidence": "high"},
        {"index": 2, "room_type": "bogus", "confidence": "weird"},
        {"index": 5, "room_type": "bedroom", "confidence": "low"},  # out of range
    ]
    out = ic.rooms_to_produced(rooms, [101, 102, 103])
    assert out == {
        101: {"room_type": "kitchen", "confidence": "high"},
        103: {"room_type": "other", "confidence": "low"},
    }
