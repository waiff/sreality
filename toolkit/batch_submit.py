"""Provider-agnostic primitives shared by every batch-submit lane (dedup,
condition scoring, description enrichment). Each lane owns its own tables and
persist logic (they key on different things: pair+room for dedup, snapshot for
condition/enrichment), but the chunk-sizing rule, the batch-discount constant,
and the transient-retry loop around `provider.submit_batch` are identical —
this module is the one place that logic lives, instead of one lane copying
another's script (submit_enrich_batch previously imported these constants
straight from submit_condition_batch).
"""

from __future__ import annotations

import logging
import time
from typing import Any

LOG = logging.getLogger("batch_submit")

# The provider Batch APIs reject very large request bodies; flush with a wide
# safety margin so a chunk never nears either provider's cap. Practical
# ceiling is NOT the API's own limit but the edge/LB upload window: a large
# single chunk can take minutes to upload from a GitHub runner and the LB
# intermittently 502s it. ~45MB uploads in well under 2 minutes.
MAX_BATCH_BYTES = 45 * 1024 * 1024
MAX_BATCH_REQUESTS = 600

# Every provider Batch API bills usage at half of standard synchronous
# pricing; applied uniformly to whichever provider's usage a batch result
# reports, not tied to one provider's naming.
BATCH_DISCOUNT = 0.5


def should_flush(
    *,
    n_items: int,
    chunk_bytes: int,
    next_item_bytes: int,
    max_requests: int = MAX_BATCH_REQUESTS,
    max_bytes: int = MAX_BATCH_BYTES,
) -> bool:
    """True when the next request must start a new batch (count or byte cap)."""
    if n_items == 0:
        return False
    return n_items >= max_requests or chunk_bytes + next_item_bytes > max_bytes


# A provider's Batch endpoint throws occasional transient 5xx/overload errors;
# one such error must cost at most one backoff, never the whole submit run.
SUBMIT_ATTEMPTS = 3
SUBMIT_BACKOFF_S = (10, 30)


def submit_chunk_with_retry(
    provider: Any, items: list[tuple[str, dict[str, Any]]], *, label: str = "",
) -> str | None:
    """provider.submit_batch with bounded retries; None when every attempt fails.

    Non-transient failures (auth / invalid-request / credit errors) return
    None immediately — retrying won't heal them and would only waste the
    submit window."""
    last: Exception | None = None
    for attempt in range(SUBMIT_ATTEMPTS):
        try:
            return provider.submit_batch(items)
        except Exception as exc:  # noqa: BLE001 - classified below, never crashes the run
            last = exc
            msg = str(exc).lower()
            transient = any(k in msg for k in (
                "500", "502", "503", "529", "internal", "overloaded",
                "unavailable", "timeout", "connection",
            ))
            if not transient:
                LOG.error("BATCH submit %s non-transient failure (chunk dropped): %s", label, exc)
                return None
            if attempt < SUBMIT_ATTEMPTS - 1:
                wait = SUBMIT_BACKOFF_S[min(attempt, len(SUBMIT_BACKOFF_S) - 1)]
                LOG.warning(
                    "BATCH submit %s transient failure (attempt %d/%d, retry in %ds): %s",
                    label, attempt + 1, SUBMIT_ATTEMPTS, wait, exc,
                )
                time.sleep(wait)
    LOG.error("BATCH submit %s failed after %d attempts (chunk dropped): %s",
              label, SUBMIT_ATTEMPTS, last)
    return None
