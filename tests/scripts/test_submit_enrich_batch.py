"""Tests for the bazos-enrichment batch SUBMIT lane (scripts.submit_enrich_batch).

No DB, no network: psycopg / AnthropicProvider / the selection + request-builder
functions are all monkeypatched at the module attribute they're imported from
(main() does every import lazily, so patching before calling main() takes
effect). Covers the in-flight skip and the chunk-flush boundary; the
should_flush cap arithmetic itself is already pinned in
tests/test_batch_submit.py — this lane reuses that exact function/constants
from toolkit.batch_submit (shared with the dedup and condition batch lanes).
"""

from __future__ import annotations

import sys
import types
from typing import Any

import api.providers.anthropic as anthropic_module
import scripts.enrich_listing_descriptions as enrich_mod
import scripts.submit_enrich_batch as sub
import toolkit.batch_submit as batch_submit
import toolkit.bazos_enrichment as bazos_enrichment


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
        if "SELECT DISTINCT r.sreality_id" in sql:
            self._rows = [(sid,) for sid in sorted(self._conn.in_flight)]
        elif "INSERT INTO listing_description_enrichment_batches" in sql:
            batch_id = self._conn.next_batch_id
            self._conn.next_batch_id += 1
            self._rows = [(batch_id,)]
        else:
            self._rows = []

    def executemany(self, sql: str, rows: list[Any]) -> None:
        self._conn.inserted_requests.append(list(rows))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConn:
    def __init__(self, *, in_flight: set[int] | None = None) -> None:
        self.in_flight = in_flight or set()
        self.next_batch_id = 1
        self.inserted_requests: list[list[Any]] = []

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Ctx:
        return _Ctx()


class _FakeProvider:
    def __init__(self) -> None:
        self.name = "anthropic"  # real providers carry .name (stamped into the batch row)
        self.build_calls: list[dict[str, Any]] = []
        self.submitted: list[list[Any]] = []

    def build_batch_request_params(
        self, *, system: str, messages: Any, tools: Any, model: str,
        tool_choice: str | None = None, max_tokens: int = 4096,
    ) -> dict[str, Any]:
        self.build_calls.append({"system": system, "model": model, "tool_choice": tool_choice})
        return {"model": model, "system": system, "messages": messages, "tools": tools}

    def submit_batch(self, items: list[Any]) -> str:
        self.submitted.append(list(items))
        return f"batch_{len(self.submitted)}"


def _fake_request(sid: int, snapshot_id: int = 900) -> dict[str, Any]:
    return {
        "system": "SYS",
        "messages": [{"role": "user", "content": f"desc-{sid}"}],
        "tools": [bazos_enrichment.ENRICH_LISTING_TOOL],
        "tool_choice": "record_listing",
        "model": "claude-haiku-4-5",
        "max_tokens": 512,
        "snapshot_id": snapshot_id,
        "current": {},
    }


def _run_main(
    monkeypatch: Any, *, pending: list[int], in_flight: set[int],
    build_fn: Any, argv: list[str] | None = None,
) -> tuple[_FakeConn, _FakeProvider]:
    conn = _FakeConn(in_flight=in_flight)
    provider = _FakeProvider()

    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setitem(
        sys.modules, "psycopg",
        types.SimpleNamespace(connect=lambda *a, **k: conn),
    )
    monkeypatch.setattr(anthropic_module, "AnthropicProvider", lambda: provider)
    monkeypatch.setattr(enrich_mod, "_select_pending", lambda c, **k: list(pending))
    monkeypatch.setattr(bazos_enrichment, "build_enrich_request", build_fn)
    monkeypatch.setattr(sys, "argv", ["submit_enrich_batch", *(argv or [])])

    assert sub.main() == 0
    return conn, provider


def test_in_flight_listings_are_skipped(monkeypatch: Any) -> None:
    seen: list[int] = []

    def build_fn(conn: Any, sid: int, *, model: str) -> dict[str, Any] | None:
        seen.append(sid)
        return _fake_request(sid)

    conn, provider = _run_main(
        monkeypatch, pending=[1, 2, 3], in_flight={2}, build_fn=build_fn,
    )

    # sid 2 is in-flight: skipped before a request is even built for it.
    assert seen == [1, 3]
    inserted_sids = {row[2] for batch in conn.inserted_requests for row in batch}
    assert inserted_sids == {1, 3}
    assert len(provider.submitted) == 1


def test_build_enrich_request_none_is_skipped(monkeypatch: Any) -> None:
    # A race between selection and build (e.g. another run just cached it)
    # surfaces as build_enrich_request returning None; must not be submitted.
    def build_fn(conn: Any, sid: int, *, model: str) -> dict[str, Any] | None:
        return None if sid == 5 else _fake_request(sid)

    conn, provider = _run_main(
        monkeypatch, pending=[4, 5, 6], in_flight=set(), build_fn=build_fn,
    )

    inserted_sids = {row[2] for batch in conn.inserted_requests for row in batch}
    assert inserted_sids == {4, 6}


def test_chunk_flush_boundary_creates_multiple_batches(monkeypatch: Any) -> None:
    # Force a flush after every single request so 3 candidates -> 3 batches.
    monkeypatch.setattr(batch_submit, "MAX_BATCH_REQUESTS", 1)
    monkeypatch.setattr(batch_submit, "MAX_BATCH_BYTES", 45 * 1024 * 1024)

    def build_fn(conn: Any, sid: int, *, model: str) -> dict[str, Any]:
        return _fake_request(sid)

    conn, provider = _run_main(
        monkeypatch, pending=[10, 11, 12], in_flight=set(), build_fn=build_fn,
    )

    assert len(provider.submitted) == 3
    assert len(conn.inserted_requests) == 3
    all_sids = [row[2] for batch in conn.inserted_requests for row in batch]
    assert sorted(all_sids) == [10, 11, 12]


def test_no_candidates_is_a_clean_noop(monkeypatch: Any) -> None:
    conn, provider = _run_main(
        monkeypatch, pending=[], in_flight=set(), build_fn=lambda *a, **k: None,
    )
    assert conn.inserted_requests == []
    assert provider.submitted == []


def test_resolve_enrichment_model_setting_and_fallback() -> None:
    from toolkit.bazos_enrichment import DEFAULT_MODEL, resolve_enrichment_model

    class _Cur:
        def __init__(self, row: Any) -> None:
            self._row = row

        def execute(self, sql: str, params: Any) -> None:
            return None

        def fetchone(self) -> Any:
            return self._row

        def __enter__(self) -> "_Cur":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    class _Conn:
        def __init__(self, row: Any) -> None:
            self._row = row

        def cursor(self) -> "_Cur":
            return _Cur(self._row)

    # app_settings.enrichment_model set -> that model; absent -> Haiku default.
    assert resolve_enrichment_model(_Conn(("gpt-5-mini",))) == "gpt-5-mini"
    assert resolve_enrichment_model(_Conn(None)) == DEFAULT_MODEL
