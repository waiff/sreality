"""The dedup batch submitter partitions requests by provider.

A batch is single-provider, but with compare/floor/site flipped to gpt-5-mini and
classify still on Haiku, one collection run produces both OpenAI and Anthropic
requests. They must land in separate per-provider buffers and each flush to its own
API, stamping dedup_batches.provider correctly (so ingest polls the right one).
No DB / network: fake conn + fake providers.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any


class _FakeCur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def execute(self, sql: str, params: Any = None) -> None:
        if "INSERT INTO dedup_batches" in sql and "provider_batch_id" in sql:
            # params = (provider, provider_batch_id, request_count)
            self._conn.inserted.append(params[0])

    def fetchone(self) -> tuple[Any, ...]:
        return (1,)  # batch id for the INSERT ... RETURNING

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []  # no in-flight custom_ids

    def executemany(self, sql: str, seq: Any) -> None:
        return None

    def __enter__(self) -> "_FakeCur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self) -> None:
        self.inserted: list[str] = []

    def cursor(self) -> _FakeCur:
        return _FakeCur(self)

    @contextmanager
    def transaction(self):
        yield self


class _FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.submitted: list[list[tuple[str, dict[str, Any]]]] = []

    def build_batch_request_params(self, *, system, messages, tools, model, **kw):
        return {"model": model, "_provider": self.name}

    def submit_batch(self, items):
        self.submitted.append(items)
        return f"{self.name}-batch-1"


def _built(model: str) -> dict[str, Any]:
    return {"system": "s", "messages": [], "tools": [], "model": model, "image_ids": [1]}


def test_submitter_partitions_by_provider():
    from scripts import submit_dedup_batch as S

    conn = _FakeConn()
    provs = {"anthropic": _FakeProvider("anthropic"), "openai": _FakeProvider("openai")}
    sub = S._Submitter(conn, provs, max_requests=100, dry_run=False)

    # classify -> Haiku -> anthropic; compare -> gpt-5-mini -> openai
    sub.add(custom_id="c1", kind="classify", model="claude-haiku-4-5",
            a=1, b=None, room_type=None, build_fn=lambda: _built("claude-haiku-4-5"))
    sub.add(custom_id="c2", kind="compare", model="gpt-5-mini",
            a=1, b=2, room_type="kitchen", build_fn=lambda: _built("gpt-5-mini"))

    assert set(sub._chunks) == {"anthropic", "openai"}
    assert len(sub._chunks["anthropic"]) == 1
    assert len(sub._chunks["openai"]) == 1

    sub.flush_all()

    # each provider submitted exactly its own request
    assert len(provs["anthropic"].submitted) == 1
    assert len(provs["openai"].submitted) == 1
    assert provs["anthropic"].submitted[0][0][0] == "c1"
    assert provs["openai"].submitted[0][0][0] == "c2"
    # dedup_batches rows stamped with the right provider (drives ingest routing)
    assert set(conn.inserted) == {"anthropic", "openai"}


def test_submitter_skips_unwired_provider():
    from scripts import submit_dedup_batch as S

    conn = _FakeConn()
    # only anthropic wired; a qwen model has nowhere to batch -> skipped, not crashed
    sub = S._Submitter(conn, {"anthropic": _FakeProvider("anthropic")},
                       max_requests=100, dry_run=False)
    sub.add(custom_id="q1", kind="compare", model="qwen3-vl-30b-a3b-instruct",
            a=1, b=2, room_type="kitchen",
            build_fn=lambda: _built("qwen3-vl-30b-a3b-instruct"))
    assert "qwen" not in sub._chunks
    assert sub._chunks == {}
