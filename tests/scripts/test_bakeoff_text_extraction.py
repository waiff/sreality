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


def _reading(rid: int, kind: str, read: dict[str, str], labels: dict[str, str],
             register: dict[str, Any], claimed: list[str], quoted: list[str]) -> dict[str, Any]:
    location = {s: {"value": read.get(s), "quote": read.get(s) and f"q {read[s]}"}
                for s in ("town", "part_of_town", "street", "house_number_cp",
                          "house_number_co", "house_number_ev")}
    location["ad_kind"] = {"value": "offer", "quote": None}
    labels = {"part_of_town": None, "street": None, "house_number_cp": None, **labels}
    return {"id": rid, "labels": {}, "values": {}, "dropped": {}, "cost_usd": 0.0, "ms": 1,
            "location": location, "label_kind": kind, "stratum": "md5", "source": "x",
            "location_labels": labels, "stored": {"obec_name": labels.get("town"),
                                                  "okres_name": "O"},
            "advert_text": "Headline\nBody", "register": register,
            "claimed": claimed, "quoted": quoted}


def test_the_location_half_scores_agreement_binds_and_gates() -> None:
    """Structured rows grade the street by the register's fold (`ul. Husova` == Husova);
    bazos rows grade only against the STORED town, and a disagreement is review material."""
    results = [
        _reading(1, "structured", {"street": "ul. Husova", "town": "Brno"},
                 {"street": "Husova", "town": "Brno"},
                 {"level": "obec", "binds_stored_town": True}, ["street", "town"],
                 ["street", "town"]),
        _reading(2, "stored", {"town": "Kbel"}, {"town": "Švihov"},
                 {"level": "obec", "name": "Kbel", "okres": "Plzeň-jih", "km": 2.4},
                 ["town", "street"], ["town"]),
    ]
    out = bk.score(results)
    loc = out["location"]
    assert loc["per_slot"]["street"]["structured_agreement"] == 1.0
    assert loc["per_slot"]["street"]["quote_valid"] == 0.5
    assert loc["town_binds"] == 1.0 and loc["bazos_town_agreement"] == 0.0
    assert loc["quote_validity"] == 0.75 and loc["ad_kind"] == {"offer": 2}
    assert out["gates"]["structured street agreement >= 95%"] is True
    assert out["gates"]["quote validity >= 95%"] is False
    sheet = bk.review({"arms": [{"model": "m", "readings": results}]})
    assert "Text: Kbel -> obec Kbel, okres Plzeň-jih, 2.4 km from the pin" in sheet
    assert 'town "Brno" <- "q Brno"' not in sheet  # structured rows are not in the sheet
