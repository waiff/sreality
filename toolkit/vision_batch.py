"""The shared vision-batch engine: N images through one prompt, in parallel,
under a budget checked BEFORE each call.

Extracted from scripts/screen_exam_cohort.py when the suggest lane arrived
needing the identical loop — worker threads, per-worker connections, a
pre-call budget under a lock — and a third user (the machine relabel pass)
is already on the roadmap. One engine, several sinks; a copied loop would be
the kind of drift where one copy learns a lesson and the other repeats it.

The invariants this engine owns, all measured the hard way in the screen lane:

  * Each worker opens its OWN connection and LLMClient. psycopg connections
    are not thread-safe and LLMClient writes an llm_calls row per call —
    sharing one would interleave writes and corrupt the cost ledger.
  * The budget binds in the WORKER, before the call, under a lock. Checking
    afterwards on a parallel lane means discovering the overspend once every
    in-flight call has already been billed.
  * A failure is recorded as an ERROR, never as an empty result. The two look
    identical downstream and mean opposite things.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Protocol

LOG = logging.getLogger("vision_batch")


class RecordFn(Protocol):
    def __call__(self, wconn: Any, image_id: int, ids: list[int] | None,
                 error: str | None) -> None: ...


def run_vision_batch(
    r2: Any, *, rows: list[tuple[int, str]], prompt: str,
    parse: Callable[[str], Any], record: RecordFn,
    model: str, called_for: str, max_tokens: int,
    max_usd: float, max_seconds: int, workers: int,
) -> dict[str, Any]:
    """Run `rows` of (image_id, storage_path) through the model, calling
    `record(wconn, image_id, ids, error)` for each as results land."""
    from api.llm_client import LLMClient
    from api.providers.openai import OpenAIProvider
    from scraper import db
    from toolkit.vision_images import COMPARISON_MAX_EDGE, image_block

    stats = {"ok": 0, "errors": 0, "hits": 0, "spent": 0.0, "aborted": False}
    lock = threading.Lock()
    started = time.monotonic()
    work: queue.Queue = queue.Queue()
    for row in rows:
        work.put(row)

    def _stop() -> bool:
        if max_usd > 0 and stats["spent"] >= max_usd:
            return True
        return max_seconds > 0 and time.monotonic() - started >= max_seconds

    def _worker() -> None:
        # One connection and one client per worker, opened here so the thread
        # that uses them is the thread that owns them.
        wconn = db.connect()
        try:
            llm = LLMClient(wconn, providers={"openai": OpenAIProvider()})
            while True:
                try:
                    image_id, storage_path = work.get_nowait()
                except queue.Empty:
                    return
                with lock:
                    if _stop():
                        stats["aborted"] = True
                        return
                try:
                    block = image_block(r2, storage_path, COMPARISON_MAX_EDGE)
                    res = llm.call(
                        called_for=called_for, model=model, max_tokens=max_tokens,
                        messages=[{"role": "user", "content": [
                            block, {"type": "text", "text": prompt}]}],
                    )
                    cost = float(getattr(res, "cost_usd", 0.0) or 0.0)
                    ids = parse(getattr(res, "text", "") or "")
                except Exception as exc:  # noqa: BLE001 - one image must not kill the pass
                    with lock:
                        stats["errors"] += 1
                    record(wconn, image_id, None, str(exc)[:500])
                    continue
                with lock:
                    stats["ok"] += 1
                    stats["spent"] += cost
                    if ids:
                        stats["hits"] += 1
                record(wconn, image_id, ids, None)
        finally:
            wconn.close()

    threads = [threading.Thread(target=_worker, daemon=True)
               for _ in range(max(1, workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if stats["aborted"]:
        LOG.warning("VISION-BATCH stopped early: ceiling $%.2f or %ds reached",
                    max_usd, max_seconds)
    return stats
