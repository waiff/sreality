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


def test_floor_labels_are_the_stored_value_on_every_portal() -> None:
    """W8 made listings.floor ground = 0 everywhere; a per-source offset here would score
    floor against labels one storey off (Gemma, run 35731041090)."""
    assert bk._label_floor("sreality", 0) == 0
    assert bk._label_floor("sreality", 3) == 3
    assert bk._label_floor("idnes", 3) == 3
    assert bk._label_floor("idnes", None) is None


def test_the_receipt_survives_a_terminate_runpod_did_not_confirm() -> None:
    class _Resp:
        def __init__(self, code: int) -> None:
            self.status_code = code

    class _Err(Exception):
        def __init__(self, code: int) -> None:
            self.response = _Resp(code)

    class _Client:
        def __init__(self, outcome: Any) -> None:
            self._o = outcome

        def get_pod(self, pod_id: str) -> dict[str, Any]:
            if isinstance(self._o, Exception):
                raise self._o
            return self._o

    assert bk._pod_is_gone(_Client(_Err(404)), "p") is True
    assert bk._pod_is_gone(_Client({"desiredStatus": "TERMINATED"}), "p") is True
    assert bk._pod_is_gone(_Client({"desiredStatus": "RUNNING"}), "p") is False
    assert bk._pod_is_gone(_Client(TimeoutError("read timed out")), "p") is False
