"""Tests for the dedup-vision batch FLUSH lane (scripts.submit_dedup_batch).

Engine-fed deferral (§4.1) moved request SELECTION into the engine itself
(scripts.dedup_engine, tested in test_dedup_engine.py / test_dedup_batch_defer.py);
this script's only remaining job is to FLUSH whatever's spooled in
dedup_batch_requests (batch_id IS NULL) — chunk per-provider under the size/count
caps, submit, and attach the resulting batch_id. These tests monkeypatch the DB +
provider so the chunking/retry/skip logic is exercised without a real connection.
"""

from __future__ import annotations

from typing import Any

import scripts.submit_dedup_batch as sub


# --- minimal fakes ----------------------------------------------------------

class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self._one: tuple[Any, ...] | None = None

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        if "SELECT" in sql and "dedup_batch_requests" in sql and "batch_id IS NULL" in sql:
            self._rows = list(self._conn.spooled)
        elif "INSERT INTO dedup_batches" in sql:
            self._conn.batch_counter += 1
            batch_id = self._conn.batch_counter
            provider, provider_batch_id, request_count = params
            self._conn.inserted_batches.append(
                {"id": batch_id, "provider": provider,
                 "provider_batch_id": provider_batch_id, "request_count": request_count})
            self._one = (batch_id,)
        elif "UPDATE dedup_batch_requests SET batch_id" in sql:
            batch_id, ids = params
            for req_id in ids:
                self._conn.attached[req_id] = batch_id
        elif "UPDATE dedup_batch_requests SET status = 'skipped'" in sql:
            (ids,) = params
            self._conn.skipped.extend(ids)
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._one

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConn:
    def __init__(self, spooled: list[tuple[Any, ...]]) -> None:
        self.spooled = spooled
        self.inserted_batches: list[dict[str, Any]] = []
        self.attached: dict[int, int] = {}
        self.skipped: list[int] = []
        self.batch_counter = 0

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Ctx:
        return _Ctx()


class _FakeProvider:
    def __init__(self, name: str = "anthropic") -> None:
        self.name = name

    def submit_batch(self, items: list[Any]) -> str:
        return f"batch_{self.name}"


def _row(
    req_id: int, *, custom_id: str, kind: str = "compare", model: str = "claude-sonnet-4-5",
    a: int | None = 1, b: int | None = 2,
    listing_id_a: int | None = None, listing_id_b: int | None = None,
    room_type: str | None = "kitchen",
    image_ids: list[int] | None = None, request_params: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    return (
        req_id, custom_id, kind, model, a, b, listing_id_a, listing_id_b,
        room_type, image_ids,
        request_params or {"model": model, "system": "s", "messages": [], "tools": []},
    )


# --- flush() -----------------------------------------------------------------

def test_flush_empty_spool_is_a_noop() -> None:
    conn = _FakeConn(spooled=[])
    stats = sub.flush(conn, {"anthropic": _FakeProvider()}, max_requests=100, dry_run=False)
    assert stats == {"flushed": 0, "batches": 0, "skipped_no_provider": 0, "submit_failures": 0}
    assert conn.inserted_batches == []


def test_flush_submits_one_batch_and_attaches_batch_id() -> None:
    rows = [_row(1, custom_id="cmp-1-2-kitchen"), _row(2, custom_id="cmp-1-2-bathroom")]
    conn = _FakeConn(spooled=rows)
    stats = sub.flush(conn, {"anthropic": _FakeProvider()}, max_requests=100, dry_run=False)
    assert stats["flushed"] == 2
    assert stats["batches"] == 1
    assert stats["submit_failures"] == 0
    assert len(conn.inserted_batches) == 1
    assert conn.inserted_batches[0]["provider"] == "anthropic"
    assert conn.attached == {1: 1, 2: 1}


def test_flush_partitions_requests_by_provider_into_separate_batches() -> None:
    rows = [
        _row(1, custom_id="cls-1", kind="classify", model="claude-haiku-4-5"),
        _row(2, custom_id="cmp-1-2-kitchen", model="gpt-5-mini"),
    ]
    conn = _FakeConn(spooled=rows)
    providers = {"anthropic": _FakeProvider("anthropic"), "openai": _FakeProvider("openai")}
    stats = sub.flush(conn, providers, max_requests=100, dry_run=False)
    assert stats["flushed"] == 2
    assert stats["batches"] == 2
    provider_names = {b["provider"] for b in conn.inserted_batches}
    assert provider_names == {"anthropic", "openai"}


def test_fetch_spooled_passes_limit_through_to_the_query() -> None:
    class _LimitCur(_Cur):
        def execute(self, sql: str, params: Any = None) -> None:
            self._conn.seen_limit = params[0] if params else None
            self._rows = list(self._conn.spooled)

    class _LimitConn(_FakeConn):
        def cursor(self) -> _Cur:
            return _LimitCur(self)

    conn = _LimitConn(spooled=[_row(1, custom_id="cls-1", kind="classify", b=None, room_type=None)])
    sub._fetch_spooled(conn, limit=42)
    assert conn.seen_limit == 42


def test_flush_dry_run_reports_without_submitting_or_attaching() -> None:
    rows = [_row(1, custom_id="cmp-1-2-kitchen")]
    conn = _FakeConn(spooled=rows)

    class _Boom(_FakeProvider):
        def submit_batch(self, items: list[Any]) -> str:
            raise AssertionError("dry-run must never call submit_batch")

    stats = sub.flush(conn, {"anthropic": _Boom()}, max_requests=100, dry_run=True)
    assert stats["flushed"] == 1
    assert stats["batches"] == 1
    assert conn.inserted_batches == []
    assert conn.attached == {}


def test_flush_skips_and_marks_requests_with_no_batch_capable_provider() -> None:
    rows = [_row(1, custom_id="cmp-1-2-kitchen", model="gemini-3.1-pro")]
    conn = _FakeConn(spooled=rows)
    stats = sub.flush(conn, {"anthropic": _FakeProvider()}, max_requests=100, dry_run=False)
    assert stats["skipped_no_provider"] == 1
    assert stats["flushed"] == 0
    assert conn.skipped == [1]


def test_flush_dry_run_does_not_mark_no_provider_requests_skipped() -> None:
    rows = [_row(1, custom_id="cmp-1-2-kitchen", model="gemini-3.1-pro")]
    conn = _FakeConn(spooled=rows)
    stats = sub.flush(conn, {"anthropic": _FakeProvider()}, max_requests=100, dry_run=True)
    assert stats["skipped_no_provider"] == 1
    assert conn.skipped == []  # dry-run must not mutate the spool


def test_flush_drops_chunk_on_submit_failure_and_leaves_it_unattached() -> None:
    rows = [_row(1, custom_id="cmp-1-2-kitchen")]
    conn = _FakeConn(spooled=rows)

    class _Dead(_FakeProvider):
        def submit_batch(self, items: list[Any]) -> str:
            # Non-transient (no 5xx/overloaded/timeout keyword) -> the shared
            # retry helper returns None on the FIRST attempt, no backoff sleep.
            raise RuntimeError("401 authentication_error: invalid x-api-key")

    stats = sub.flush(conn, {"anthropic": _Dead()}, max_requests=100, dry_run=False)
    assert stats["submit_failures"] == 1
    assert stats["batches"] == 0
    assert conn.inserted_batches == []
    assert conn.attached == {}  # left in the spool; next scheduled flush retries it


def test_flush_chunks_by_request_count_cap(monkeypatch: Any) -> None:
    monkeypatch.setattr(sub, "MAX_BATCH_REQUESTS", 2)
    rows = [_row(i, custom_id=f"cls-{i}", kind="classify", b=None, room_type=None)
            for i in range(1, 6)]
    conn = _FakeConn(spooled=rows)
    stats = sub.flush(conn, {"anthropic": _FakeProvider()}, max_requests=100, dry_run=False)
    assert stats["flushed"] == 5
    assert stats["batches"] == 3  # 2 + 2 + 1
    assert len({b["id"] for b in conn.inserted_batches}) == 3


def test_kind_counts_groups_by_kind() -> None:
    reqs = [
        sub._SpooledReq(1, "cls-1", "classify", "m", 1, None, None, None, None, None, {}),
        sub._SpooledReq(2, "cmp-1-2-k", "compare", "m", 1, 2, None, None, "kitchen", None, {}),
        sub._SpooledReq(3, "cmp-1-2-b", "compare", "m", 1, 2, None, None, "bathroom", None, {}),
    ]
    assert sub._kind_counts(reqs) == {"classify": 1, "compare": 2}


def test_fetch_spooled_maps_rows_to_dataclass() -> None:
    rows = [_row(1, custom_id="cmp-1-2-kitchen", image_ids=None)]
    conn = _FakeConn(spooled=rows)
    out = sub._fetch_spooled(conn, limit=10)
    assert len(out) == 1
    assert out[0].id == 1
    assert out[0].custom_id == "cmp-1-2-kitchen"
    assert out[0].kind == "compare"
    assert out[0].sreality_id_a == 1
    assert out[0].sreality_id_b == 2
    assert out[0].listing_id_a is None
    assert out[0].listing_id_b is None


def test_fetch_spooled_tolerates_null_sreality_id_gate2_row() -> None:
    """Gate 2 regression: a spooled side identified only by listing_id (NULL
    sreality_id_a) must not crash the whole fetch (wave-5 audit finding #3)."""
    rows = [_row(1, custom_id="cmp-1-2-kitchen", a=None, listing_id_a=501, listing_id_b=502)]
    conn = _FakeConn(spooled=rows)
    out = sub._fetch_spooled(conn, limit=10)
    assert len(out) == 1
    assert out[0].sreality_id_a is None
    assert out[0].listing_id_a == 501
    assert out[0].listing_id_b == 502
