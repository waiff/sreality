"""Tests for toolkit.dedup_batch_defer — the engine-fed §4.1 spool writer.

enqueue_deferred_request is called from inside the dedup engine's cold-call
path (scripts.dedup_engine's deferring fn builders) instead of a live LLM
call. These tests exercise it in isolation: idempotency against an
already-pending custom_id, the INSERT shape on a genuine miss, and the two
"can't defer this" outcomes (build failure, no batch-capable provider).
"""

from __future__ import annotations

from typing import Any

from toolkit.dedup_batch_defer import enqueue_deferred_request


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
        if "SELECT 1 FROM dedup_batch_requests" in sql:
            (custom_id,) = params
            self._conn.last_select = custom_id
        elif "INSERT INTO dedup_batch_requests" in sql:
            self._conn.inserted.append(params)
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._conn.last_select in self._conn.already_pending:
            return (1,)
        return None


class _FakeConn:
    def __init__(self, *, already_pending: set[str] | None = None) -> None:
        self.already_pending = already_pending or set()
        self.inserted: list[Any] = []
        self.last_select: str | None = None

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Ctx:
        return _Ctx()


class _FakeProvider:
    def build_batch_request_params(
        self, *, system: Any, messages: Any, tools: Any, model: str,
    ) -> dict[str, Any]:
        return {"model": model, "system": system, "messages": messages, "tools": tools}


def _build_ok() -> dict[str, Any]:
    return {"system": "s", "messages": [], "tools": [], "model": "claude-sonnet-4-5"}


def test_idempotent_when_custom_id_already_pending() -> None:
    conn = _FakeConn(already_pending={"cmp-1-2-kitchen"})
    called = {"n": 0}

    def build_fn() -> dict[str, Any]:
        called["n"] += 1
        return _build_ok()

    out = enqueue_deferred_request(
        conn, {"anthropic": _FakeProvider()}, custom_id="cmp-1-2-kitchen",
        kind="compare", model="claude-sonnet-4-5", sreality_id_a=1, sreality_id_b=2,
        room_type="kitchen", build_fn=build_fn,
    )
    assert out is True
    assert called["n"] == 0  # never builds when already spooled/in-flight
    assert conn.inserted == []


def test_spools_a_genuine_miss() -> None:
    conn = _FakeConn()
    out = enqueue_deferred_request(
        conn, {"anthropic": _FakeProvider()}, custom_id="cmp-1-2-kitchen",
        kind="compare", model="claude-sonnet-4-5", sreality_id_a=1, sreality_id_b=2,
        room_type="kitchen", build_fn=_build_ok,
    )
    assert out is True
    assert len(conn.inserted) == 1
    (custom_id, kind, model, a, b, room_type, image_ids, params) = conn.inserted[0]
    assert custom_id == "cmp-1-2-kitchen"
    assert kind == "compare"
    assert model == "claude-sonnet-4-5"
    assert (a, b, room_type) == (1, 2, "kitchen")
    assert image_ids is None
    assert params.obj["model"] == "claude-sonnet-4-5"


def test_routes_to_the_models_own_provider() -> None:
    conn = _FakeConn()

    class _OpenAI(_FakeProvider):
        pass

    providers = {"anthropic": _FakeProvider(), "openai": _OpenAI()}
    out = enqueue_deferred_request(
        conn, providers, custom_id="cls-1", kind="classify", model="gpt-5-mini",
        sreality_id_a=1, sreality_id_b=None, room_type=None,
        build_fn=lambda: {"system": "s", "messages": [], "tools": [], "model": "gpt-5-mini"},
    )
    assert out is True
    assert len(conn.inserted) == 1


def test_returns_false_when_build_fn_raises() -> None:
    conn = _FakeConn()

    def _boom() -> dict[str, Any]:
        raise RuntimeError("R2 fetch failed")

    out = enqueue_deferred_request(
        conn, {"anthropic": _FakeProvider()}, custom_id="cmp-1-2-kitchen",
        kind="compare", model="claude-sonnet-4-5", sreality_id_a=1, sreality_id_b=2,
        room_type="kitchen", build_fn=_boom,
    )
    assert out is False
    assert conn.inserted == []


def test_returns_false_when_model_has_no_batch_capable_provider() -> None:
    conn = _FakeConn()
    out = enqueue_deferred_request(
        conn, {"anthropic": _FakeProvider()},  # no 'gemini' key
        custom_id="cmp-1-2-kitchen", kind="compare", model="gemini-3.1-pro",
        sreality_id_a=1, sreality_id_b=2, room_type="kitchen",
        build_fn=lambda: {
            "system": "s", "messages": [], "tools": [], "model": "gemini-3.1-pro"},
    )
    assert out is False
    assert conn.inserted == []
