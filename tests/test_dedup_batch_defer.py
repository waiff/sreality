"""Tests for toolkit.dedup_batch_defer — the engine-fed §4.1 spool writer.

enqueue_deferred_request is called from inside the dedup engine's cold-call
path (scripts.dedup_engine's deferring fn builders) instead of a live LLM
call. These tests exercise it in isolation: identity resolution across both
id-spaces, the surrogate-derived custom_id scheme and its order-independence,
idempotency against an already-pending custom_id, the INSERT shape on a genuine
miss, and the "can't defer this" outcomes (unresolvable identity, build
failure, no batch-capable provider).

The fake listings table deliberately gives a listing a surrogate `id` DIFFERENT
from its `sreality_id` (id = sid + 900_000, the fixture convention the R2 work
uses elsewhere) so a site still keying on the legacy id fails loudly instead of
passing by coincidence.
"""

from __future__ import annotations

from typing import Any

from toolkit.dedup_batch_defer import enqueue_deferred_request

# sreality_id -> surrogate listings.id
_LISTINGS = {1: 900_001, 2: 900_002, 7: 900_007}


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        if "FROM listings WHERE sreality_id = ANY" in sql:
            self._conn.resolved += 1
            sids, lids = params["sids"], params["lids"]
            rows = [(s, _LISTINGS[s]) for s in sids if s in self._conn.listings]
            by_lid = {v: k for k, v in _LISTINGS.items()}
            rows += [(by_lid[i], i) for i in lids
                     if i in by_lid and by_lid[i] in self._conn.listings]
            self._rows = rows
        elif "SELECT 1 FROM dedup_batch_requests" in sql:
            (custom_id,) = params
            self._conn.last_select = custom_id
        elif "INSERT INTO dedup_batch_requests" in sql:
            self._conn.inserted.append(params)
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._conn.last_select in self._conn.already_pending:
            return (1,)
        return None


class _FakeConn:
    def __init__(
        self,
        *,
        already_pending: set[str] | None = None,
        listings: set[int] | None = None,
    ) -> None:
        self.already_pending = already_pending or set()
        self.listings = listings if listings is not None else set(_LISTINGS)
        self.inserted: list[Any] = []
        self.last_select: str | None = None
        self.resolved = 0

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
    conn = _FakeConn(already_pending={"cmpL-900001-900002-kitchen"})
    called = {"n": 0}

    def build_fn() -> dict[str, Any]:
        called["n"] += 1
        return _build_ok()

    out = enqueue_deferred_request(
        conn, {"anthropic": _FakeProvider()},
        kind="compare", model="claude-sonnet-4-5", sreality_id_a=1, sreality_id_b=2,
        room_type="kitchen", build_fn=build_fn,
    )
    assert out is True
    assert called["n"] == 0  # never builds when already spooled/in-flight
    assert conn.inserted == []


def test_spools_a_genuine_miss_with_both_id_spaces() -> None:
    conn = _FakeConn()
    out = enqueue_deferred_request(
        conn, {"anthropic": _FakeProvider()},
        kind="compare", model="claude-sonnet-4-5", sreality_id_a=1, sreality_id_b=2,
        room_type="kitchen", build_fn=_build_ok,
    )
    assert out is True
    assert len(conn.inserted) == 1
    (custom_id, kind, model, sa, sb, la, lb, room_type, image_ids, params) = conn.inserted[0]
    assert custom_id == "cmpL-900001-900002-kitchen"  # surrogate-derived, new prefix
    assert kind == "compare"
    assert model == "claude-sonnet-4-5"
    assert (sa, sb) == (1, 2)          # legacy space still written (Gate 1 invariant)
    assert (la, lb) == (900_001, 900_002)  # surrogate resolved, POSITIONALLY matching
    assert room_type == "kitchen"
    assert image_ids is None
    assert params.obj["model"] == "claude-sonnet-4-5"


def test_custom_id_is_order_independent_but_columns_are_positional() -> None:
    """The pair's custom_id must not change when the caller's (a, b) order flips
    — otherwise a later PR that re-canonicalizes the engine would re-spool, and
    re-bill, every in-flight request. The COLUMNS must still follow the caller,
    because the request payload's image sides are ordered to match them."""
    forward, reverse = _FakeConn(), _FakeConn()
    for conn, (a, b) in ((forward, (1, 2)), (reverse, (2, 1))):
        enqueue_deferred_request(
            conn, {"anthropic": _FakeProvider()}, kind="compare",
            model="claude-sonnet-4-5", sreality_id_a=a, sreality_id_b=b,
            room_type="kitchen", build_fn=_build_ok,
        )
    assert forward.inserted[0][0] == reverse.inserted[0][0] == "cmpL-900001-900002-kitchen"
    # (sreality_id_a, sreality_id_b, listing_id_a, listing_id_b) — both spaces
    # flip together with the caller, so listing_id_a stays the surrogate OF
    # sreality_id_a on both sides.
    assert forward.inserted[0][3:7] == (1, 2, 900_001, 900_002)
    assert reverse.inserted[0][3:7] == (2, 1, 900_002, 900_001)


def test_accepts_the_surrogate_directly_and_backfills_the_legacy_id() -> None:
    """Callers may identify by either key — this is what lets the post-swap
    engine pass listing_id without this module changing again."""
    conn = _FakeConn()
    out = enqueue_deferred_request(
        conn, {"anthropic": _FakeProvider()}, kind="site_plan",
        model="claude-sonnet-4-5", listing_id_a=900_001, listing_id_b=900_002,
        build_fn=_build_ok,
    )
    assert out is True
    (custom_id, _kind, _model, sa, sb, la, lb, *_rest) = conn.inserted[0]
    assert custom_id == "splL-900001-900002"
    assert (sa, sb) == (1, 2)
    assert (la, lb) == (900_001, 900_002)


def test_classify_keys_on_one_surrogate() -> None:
    conn = _FakeConn()
    out = enqueue_deferred_request(
        conn, {"anthropic": _FakeProvider()}, kind="classify",
        model="claude-sonnet-4-5", sreality_id_a=7, sreality_id_b=None,
        room_type=None, build_fn=_build_ok,
    )
    assert out is True
    (custom_id, _kind, _model, sa, sb, la, lb, *_rest) = conn.inserted[0]
    assert custom_id == "clsL-900007"
    assert (sa, sb, la, lb) == (7, None, 900_007, None)


def test_returns_false_when_a_side_has_no_listing_row() -> None:
    """A listing deleted between the engine's load and the defer cannot be
    keyed on the surrogate, so it must not be spooled at all."""
    conn = _FakeConn(listings={1})  # sreality_id 2 is gone
    out = enqueue_deferred_request(
        conn, {"anthropic": _FakeProvider()}, kind="compare",
        model="claude-sonnet-4-5", sreality_id_a=1, sreality_id_b=2,
        room_type="kitchen", build_fn=_build_ok,
    )
    assert out is False
    assert conn.inserted == []


def test_routes_to_the_models_own_provider() -> None:
    conn = _FakeConn()

    class _OpenAI(_FakeProvider):
        pass

    providers = {"anthropic": _FakeProvider(), "openai": _OpenAI()}
    out = enqueue_deferred_request(
        conn, providers, kind="classify", model="gpt-5-mini",
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
        conn, {"anthropic": _FakeProvider()},
        kind="compare", model="claude-sonnet-4-5", sreality_id_a=1, sreality_id_b=2,
        room_type="kitchen", build_fn=_boom,
    )
    assert out is False
    assert conn.inserted == []


def test_returns_false_when_model_has_no_batch_capable_provider() -> None:
    conn = _FakeConn()
    out = enqueue_deferred_request(
        conn, {"anthropic": _FakeProvider()},  # no 'gemini' key
        kind="compare", model="gemini-3.1-pro",
        sreality_id_a=1, sreality_id_b=2, room_type="kitchen",
        build_fn=lambda: {
            "system": "s", "messages": [], "tools": [], "model": "gemini-3.1-pro"},
    )
    assert out is False
    assert conn.inserted == []
