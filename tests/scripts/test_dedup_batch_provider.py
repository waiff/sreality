"""The dedup batch flush lane partitions spooled requests by provider.

A batch is single-provider, but with compare/floor/site flipped to gpt-5-mini
and classify still on Haiku, one flush run can have both OpenAI and Anthropic
requests spooled. They must land in separate per-provider chunks and each
flush to its own API, stamping dedup_batches.provider correctly (so ingest
polls the right one). No DB / network: fake conn + fake providers.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any


class _FakeCur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        if "SELECT" in sql and "batch_id IS NULL" in sql:
            self._rows = list(self._conn.spooled)
        elif "INSERT INTO dedup_batches" in sql and "provider_batch_id" in sql:
            # params = (provider, provider_batch_id, request_count)
            self._conn.inserted.append(params[0])
        elif "UPDATE dedup_batch_requests SET batch_id" in sql:
            self._conn.attached.extend(params[1])
        elif "UPDATE dedup_batch_requests SET status = 'skipped'" in sql:
            self._conn.skipped.extend(params[0])

    def fetchone(self) -> tuple[Any, ...]:
        return (1,)  # batch id for the INSERT ... RETURNING

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def executemany(self, sql: str, seq: Any) -> None:
        return None

    def __enter__(self) -> "_FakeCur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, spooled: list[tuple[Any, ...]]) -> None:
        self.spooled = spooled
        self.inserted: list[str] = []
        self.attached: list[int] = []
        self.skipped: list[int] = []

    def cursor(self) -> _FakeCur:
        return _FakeCur(self)

    @contextmanager
    def transaction(self):
        yield self


class _FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.submitted: list[list[tuple[str, dict[str, Any]]]] = []

    def submit_batch(self, items):
        self.submitted.append(items)
        return f"{self.name}-batch-1"


def _row(req_id: int, *, custom_id: str, kind: str, model: str,
         a: int | None = 1, b: int | None = 2,
         listing_id_a: int | None = None, listing_id_b: int | None = None,
         room_type: str | None = "kitchen") -> tuple[Any, ...]:
    return (
        req_id, custom_id, kind, model, a, b, listing_id_a, listing_id_b,
        room_type, [1],
        {"model": model, "system": "s", "messages": [], "tools": []},
    )


def test_flush_partitions_by_provider():
    from scripts import submit_dedup_batch as S

    conn = _FakeConn(spooled=[
        _row(1, custom_id="c1", kind="classify", model="claude-haiku-4-5", b=None, room_type=None),
        _row(2, custom_id="c2", kind="compare", model="gpt-5-mini"),
    ])
    provs = {"anthropic": _FakeProvider("anthropic"), "openai": _FakeProvider("openai")}

    stats = S.flush(conn, provs, max_requests=100, dry_run=False)

    assert stats["flushed"] == 2
    assert stats["batches"] == 2
    # each provider submitted exactly its own request
    assert len(provs["anthropic"].submitted) == 1
    assert len(provs["openai"].submitted) == 1
    assert provs["anthropic"].submitted[0][0][0] == "c1"
    assert provs["openai"].submitted[0][0][0] == "c2"
    # dedup_batches rows stamped with the right provider (drives ingest routing)
    assert set(conn.inserted) == {"anthropic", "openai"}
    assert set(conn.attached) == {1, 2}


def test_flush_skips_unwired_provider_without_crashing():
    from scripts import submit_dedup_batch as S

    conn = _FakeConn(spooled=[
        _row(1, custom_id="q1", kind="compare", model="qwen3-vl-30b-a3b-instruct"),
    ])
    # only anthropic wired; a qwen model has nowhere to batch -> skipped, not crashed
    stats = S.flush(conn, {"anthropic": _FakeProvider("anthropic")}, max_requests=100, dry_run=False)

    assert stats["skipped_no_provider"] == 1
    assert stats["flushed"] == 0
    assert conn.inserted == []
    assert conn.skipped == [1]
