"""Tests for toolkit.batch_submit — the primitives shared by the dedup,
condition, and enrichment batch-submit lanes (chunk-sizing, batch discount,
and the transient-retry loop around provider.submit_batch).

should_flush's cap arithmetic is exercised end-to-end via each lane's own
tests (tests/scripts/test_submit_condition_batch.py,
tests/scripts/test_submit_enrich_batch.py, tests/test_submit_dedup_batch.py);
this file adds direct unit coverage plus submit_chunk_with_retry, which had
no dedicated tests before this module existed (only indirectly, via the now-
removed dedup _Submitter._submit_with_retry).
"""

from __future__ import annotations

from typing import Any

from toolkit.batch_submit import (
    BATCH_DISCOUNT,
    MAX_BATCH_BYTES,
    MAX_BATCH_REQUESTS,
    should_flush,
    submit_chunk_with_retry,
)


def test_batch_discount_is_half_price() -> None:
    assert BATCH_DISCOUNT == 0.5


def test_should_flush_false_on_empty_chunk() -> None:
    assert should_flush(n_items=0, chunk_bytes=0, next_item_bytes=MAX_BATCH_BYTES * 2) is False


def test_should_flush_true_at_request_count_cap() -> None:
    assert should_flush(n_items=MAX_BATCH_REQUESTS, chunk_bytes=0, next_item_bytes=1) is True
    assert should_flush(n_items=MAX_BATCH_REQUESTS - 1, chunk_bytes=0, next_item_bytes=1) is False


def test_should_flush_true_at_byte_cap() -> None:
    assert should_flush(n_items=1, chunk_bytes=MAX_BATCH_BYTES - 1, next_item_bytes=2) is True
    assert should_flush(n_items=1, chunk_bytes=MAX_BATCH_BYTES - 1, next_item_bytes=1) is False


def test_should_flush_respects_explicit_overrides() -> None:
    assert should_flush(n_items=2, chunk_bytes=0, next_item_bytes=1, max_requests=2) is True
    assert should_flush(n_items=1, chunk_bytes=0, next_item_bytes=1, max_requests=2) is False


def test_submit_chunk_with_retry_returns_on_first_success() -> None:
    class _Ok:
        def submit_batch(self, items: Any) -> str:
            return "batch_1"

    assert submit_chunk_with_retry(_Ok(), []) == "batch_1"


def test_submit_chunk_with_retry_retries_transient_then_succeeds(monkeypatch: Any) -> None:
    import toolkit.batch_submit as bs

    monkeypatch.setattr(bs.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class _Flaky:
        def submit_batch(self, items: Any) -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("503 Service Unavailable")
            return "batch_ok"

    assert submit_chunk_with_retry(_Flaky(), []) == "batch_ok"
    assert calls["n"] == 3


def test_submit_chunk_with_retry_gives_up_after_exhausting_attempts(monkeypatch: Any) -> None:
    import toolkit.batch_submit as bs

    monkeypatch.setattr(bs.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class _AlwaysDown:
        def submit_batch(self, items: Any) -> str:
            calls["n"] += 1
            raise RuntimeError("529 overloaded")

    assert submit_chunk_with_retry(_AlwaysDown(), []) is None
    assert calls["n"] == bs.SUBMIT_ATTEMPTS


def test_submit_chunk_with_retry_non_transient_error_no_retry(monkeypatch: Any) -> None:
    import toolkit.batch_submit as bs

    slept: list[float] = []
    monkeypatch.setattr(bs.time, "sleep", lambda s: slept.append(s))
    calls = {"n": 0}

    class _AuthDead:
        def submit_batch(self, items: Any) -> str:
            calls["n"] += 1
            raise RuntimeError("401 authentication_error: invalid x-api-key")

    assert submit_chunk_with_retry(_AuthDead(), []) is None
    assert calls["n"] == 1  # no retry on a non-transient failure
    assert slept == []
