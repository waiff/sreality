"""The shared LLM-batch engine: N rows through one model, in parallel, under a budget
checked BEFORE each call.

Extracted from scripts/screen_exam_cohort.py when the suggest lane arrived needing the
identical loop — worker threads, per-worker connections, a pre-call budget under a lock —
and named for the only sink that existed then, an image. W7's post-publication text lane is
the third kind of sink and the first with no image in it, so the loop is `run_batch` and
`run_vision_batch` is the image adapter over it. One engine, several sinks; a copied loop
would be the kind of drift where one copy learns a lesson and the other repeats it. The
module keeps the narrower name only because `autodedup/judge_lane.py` imports `is_fatal`
from it and R12 forbids this program editing that tree.

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

LOG = logging.getLogger("llm_batch")


# Errors that will not get better by trying the next row. Measured live: a
# 2,500-image pass hit "no credits remaining" on image 461 and then retried the
# dead account 2,039 more times, burning seven minutes of runner and filling the
# log — and still reported success, because some images HAD landed. Matched on
# the provider's message rather than the status code, because a plain 429 rate
# limit shares that code and IS worth retrying.
FATAL_MARKERS = (
    "no credits remaining",
    "insufficient_quota",
    "exceeded your current quota",
    "invalid_api_key",
    "incorrect api key",
    "http 401",
    "http 403",
)


def is_fatal(message: str) -> bool:
    low = (message or "").lower()
    return any(marker in low for marker in FATAL_MARKERS)


class CallFn(Protocol):
    def __call__(self, llm: Any, row: Any) -> tuple[float, Any]: ...


class RowRecordFn(Protocol):
    def __call__(self, wconn: Any, row: Any, result: Any,
                 error: str | None) -> None: ...


class RecordFn(Protocol):
    def __call__(self, wconn: Any, image_id: int, ids: list[int] | None,
                 error: str | None) -> None: ...


def run_batch(
    *, rows: list[Any], call: CallFn, record: RowRecordFn,
    max_usd: float, max_seconds: int, workers: int,
    providers: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run `rows` through `call(llm, row) -> (cost_usd, result)` on `workers` threads,
    handing each result to `record(wconn, row, result, error)` as it lands.

    `providers` builds the per-worker provider registry; the default is OpenAI alone,
    which is what every image sink uses. A sink whose model may resolve to a self-hosted
    pod passes its own.
    """
    from api.llm_client import LLMClient
    from scraper import db

    stats: dict[str, Any] = {"ok": 0, "errors": 0, "hits": 0, "spent": 0.0,
                             "aborted": False, "fatal": None}
    lock = threading.Lock()
    started = time.monotonic()
    work: queue.Queue = queue.Queue()
    for row in rows:
        work.put(row)

    def _default_providers() -> dict[str, Any]:
        from api.providers.openai import OpenAIProvider
        return {"openai": OpenAIProvider()}

    build = providers or _default_providers

    def _stop() -> bool:
        # A dead key or an empty balance stops the pass, it does not slow it
        # down: every remaining row would fail identically.
        if stats["fatal"]:
            return True
        if max_usd > 0 and stats["spent"] >= max_usd:
            return True
        return max_seconds > 0 and time.monotonic() - started >= max_seconds

    def _worker() -> None:
        # One connection and one client per worker, opened here so the thread
        # that uses them is the thread that owns them.
        wconn = db.connect()
        try:
            llm = LLMClient(wconn, providers=build())
            while True:
                try:
                    row = work.get_nowait()
                except queue.Empty:
                    return
                with lock:
                    if _stop():
                        stats["aborted"] = True
                        return
                try:
                    cost, result = call(llm, row)
                except Exception as exc:  # noqa: BLE001 - one row must not kill the pass
                    message = str(exc)
                    with lock:
                        stats["errors"] += 1
                        if stats["fatal"] is None and is_fatal(message):
                            stats["fatal"] = message[:200]
                            stats["aborted"] = True
                    record(wconn, row, None, message[:500])
                    continue
                with lock:
                    stats["ok"] += 1
                    stats["spent"] += float(cost or 0.0)
                    if result:
                        stats["hits"] += 1
                record(wconn, row, result, None)
        finally:
            wconn.close()

    threads = [threading.Thread(target=_worker, daemon=True)
               for _ in range(max(1, workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if stats["fatal"]:
        LOG.error("LLM-BATCH STOPPED: the provider is not answering and will "
                  "not for the next row either — %s", stats["fatal"])
    elif stats["aborted"]:
        LOG.warning("LLM-BATCH stopped early: ceiling $%.2f or %ds reached",
                    max_usd, max_seconds)
    return stats


def run_vision_batch(
    r2: Any, *, rows: list[tuple[int, str]], prompt: str,
    parse: Callable[[str], Any], record: RecordFn,
    model: str, called_for: str, max_tokens: int,
    max_usd: float, max_seconds: int, workers: int,
) -> dict[str, Any]:
    """The image sink over `run_batch`: each row is (image_id, storage_path), fetched
    from R2, downscaled and sent under one shared prompt."""
    from toolkit.vision_images import COMPARISON_MAX_EDGE, image_block

    def _call(llm: Any, row: tuple[int, str]) -> tuple[float, Any]:
        _, storage_path = row
        block = image_block(r2, storage_path, COMPARISON_MAX_EDGE)
        res = llm.call(
            called_for=called_for, model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": [
                block, {"type": "text", "text": prompt}]}],
        )
        return (float(getattr(res, "cost_usd", 0.0) or 0.0),
                parse(getattr(res, "text", "") or ""))

    def _record(wconn: Any, row: tuple[int, str], result: Any,
                error: str | None) -> None:
        record(wconn, row[0], result, error)

    return run_batch(rows=list(rows), call=_call, record=_record,
                     max_usd=max_usd, max_seconds=max_seconds, workers=workers)
