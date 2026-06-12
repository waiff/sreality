"""Hermetic tests for the batch-chunking decision in submit_condition_batch.

No DB, no network — psycopg/anthropic imports live inside main(), so
importing the pure should_flush helper is clean.
"""

from __future__ import annotations

from scripts.submit_condition_batch import (
    MAX_BATCH_BYTES,
    MAX_BATCH_REQUESTS,
    should_flush,
)


def test_caps_sit_safely_under_the_api_limit():
    # 256MB is the Anthropic Message Batches hard cap; the byte budget must
    # leave a wide margin, and the count cap matches the pre-Jun-4 value.
    assert MAX_BATCH_BYTES == 45 * 1024 * 1024
    assert MAX_BATCH_BYTES < 256 * 1024 * 1024
    assert MAX_BATCH_REQUESTS == 600


def test_never_flushes_an_empty_chunk():
    # Even an oversized single item goes into a fresh chunk rather than
    # flushing nothing.
    assert should_flush(
        n_items=0, chunk_bytes=0, next_item_bytes=MAX_BATCH_BYTES + 1,
    ) is False


def test_no_flush_while_under_both_caps():
    assert should_flush(n_items=1, chunk_bytes=70_000, next_item_bytes=70_000) is False
    assert should_flush(
        n_items=MAX_BATCH_REQUESTS - 1,
        chunk_bytes=MAX_BATCH_BYTES - 70_000,
        next_item_bytes=70_000,
    ) is False


def test_flushes_at_count_cap():
    assert should_flush(
        n_items=MAX_BATCH_REQUESTS, chunk_bytes=1_000, next_item_bytes=1_000,
    ) is True


def test_flushes_when_next_item_would_breach_byte_budget():
    assert should_flush(
        n_items=1,
        chunk_bytes=MAX_BATCH_BYTES - 50_000,
        next_item_bytes=70_000,
    ) is True


def test_byte_budget_is_a_hard_per_chunk_ceiling():
    # Landing exactly on the budget is fine; one byte over flushes first.
    assert should_flush(n_items=1, chunk_bytes=100, next_item_bytes=MAX_BATCH_BYTES - 100) is False
    assert should_flush(n_items=1, chunk_bytes=100, next_item_bytes=MAX_BATCH_BYTES - 99) is True


def test_5000_requests_of_61kb_split_into_multiple_batches():
    # The Jun-4 failure shape: 5000 requests x ~61.5KB system prompt each
    # (~300MB total) must yield more than one chunk, none above the caps.
    item_bytes = 63_000
    chunks: list[tuple[int, int]] = []
    n_items, chunk_bytes = 0, 0
    for _ in range(5000):
        if should_flush(
            n_items=n_items, chunk_bytes=chunk_bytes, next_item_bytes=item_bytes,
        ):
            chunks.append((n_items, chunk_bytes))
            n_items, chunk_bytes = 0, 0
        n_items += 1
        chunk_bytes += item_bytes
    chunks.append((n_items, chunk_bytes))

    assert len(chunks) > 1
    assert sum(n for n, _ in chunks) == 5000
    for n, size in chunks:
        assert n <= MAX_BATCH_REQUESTS
        assert size <= MAX_BATCH_BYTES
