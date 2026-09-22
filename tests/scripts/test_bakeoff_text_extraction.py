"""The bake-off arm keeps the panel's order while running adverts concurrently."""

from __future__ import annotations

import threading
import time
from typing import Any

from scripts import bakeoff_text_extraction as bk


def test_run_model_is_concurrent_and_order_preserving(monkeypatch: Any) -> None:
    seen_threads: set[int] = set()
    lock = threading.Lock()

    def fake_extract(model: str, tool: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        with lock:
            seen_threads.add(threading.get_ident())
        time.sleep(0.05)
        return {"id": row["id"], "labels": row["labels"], "values": {}, "dropped": {},
                "cost_usd": 0.0, "ms": 1}

    monkeypatch.setattr(bk, "_extract_one", fake_extract)
    monkeypatch.setattr(bk.tx, "extraction_tool", lambda fields: {"name": "t"})
    panel = [{"id": i, "labels": {}, "description": "x"} for i in range(16)]
    started = time.monotonic()
    out = bk.run_model(None, "gpt-5.6-luna", panel, workers=8)
    elapsed = time.monotonic() - started
    assert out["model"] == "gpt-5.6-luna"
    assert out["n"] == 16
    assert len(seen_threads) > 1, "adverts ran on one thread — the arm is serial again"
    assert elapsed < 16 * 0.05, "sixteen 50 ms adverts took as long as a serial run"
