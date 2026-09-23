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


def test_area_agrees_within_three_percent_or_one_square_metre() -> None:
    assert bk.agrees("area_m2", 54.0, 55.5)      # 68 vs 68,5-style restatement
    assert bk.agrees("area_m2", 12.0, 13.0)      # one square metre on a small unit
    assert not bk.agrees("area_m2", 54.0, 60.0)  # a different measure


def _record(description: str, area: float | None) -> dict[str, Any]:
    row = {f: None for f in bk.FIELDS}
    row.update(id=1, source="idnes", category_main="byt", description=description,
               area_m2=area)
    return row


def test_area_is_labelled_only_where_the_grammar_reads_nothing() -> None:
    """The lane is only ever asked on adverts `scraper.area` found no figure in, so the
    panel scores area on exactly those — a row the grammar reads is out of domain."""
    blind = bk._panel_row(_record("Byt 2+kk o výměře padesát čtyři metrů", 54.0),
                          label_source="idnes")
    assert blind["labels"]["area_m2"] == 54.0
    readable = bk._panel_row(_record("Byt 2+kk, 54 m²", 54.0), label_source="idnes")
    assert "area_m2" not in readable["labels"]
    unstated = bk._panel_row(_record("Byt 2+kk o výměře padesát čtyři metrů", None),
                             label_source="idnes")
    assert "area_m2" not in unstated["labels"]


def test_the_bazos_slice_never_labels_area() -> None:
    """Its sibling key IS the grammar's area, so a label there would score the grammar."""
    row = _record("Byt bez jediného čísla", 75.0)
    row["source"] = "bazos"
    assert "area_m2" not in bk._panel_row(row, label_source="idnes")["labels"]


def test_a_gate_needs_a_sample_and_the_receipt_says_why_a_value_was_refused() -> None:
    rows = [{"id": i, "category_main": "byt" if i % 2 else "pozemek",
             "labels": {"area_m2": 54.0}, "values": {"area_m2": 54.0},
             "dropped": {}, "cost_usd": 0.0, "ms": 1} for i in range(3)]
    rows.append({"id": 9, "category_main": "byt", "labels": {"area_m2": 54.0},
                 "values": {}, "dropped": {"area_m2": "figure_not_in_quote"},
                 "cost_usd": 0.0, "ms": 1})
    s = bk.score(rows)["per_field"]["area_m2"]
    assert s["precision"] == 1.0 and s["passes_gate"] is False   # 3 < MIN_ANSWERED
    assert s["dropped"] == {"figure_not_in_quote": 1}
    assert set(s["by_category"]) == {"byt", "pozemek"}
    assert s["by_category"]["pozemek"]["answered"] == 2
    assert s["by_category"]["byt"]["dropped"] == {"figure_not_in_quote": 1}


def test_the_pool_is_drawn_on_the_eight_prose_fields_not_on_area() -> None:
    assert "l.area_m2 IS NOT NULL" not in bk.panel_sql([f for f in bk.FIELDS if f != "area_m2"])



def test_an_arm_returns_its_rows_beside_the_summary(monkeypatch: Any) -> None:
    """A gate reading that cannot be broken down by portal, category or spelling cannot be
    questioned; the rows the summary was computed over ride in the receipt."""
    panel = [{"id": i, "source": "idnes", "label_source": "idnes", "category_main": "byt",
              "description": "x", "labels": {}} for i in range(3)]
    monkeypatch.setattr(bk, "_extract_one", lambda model, tool, row: {
        "id": row["id"], "category_main": "byt", "labels": {}, "values": {},
        "dropped": {}, "cost_usd": 0.0, "ms": 1})
    arm = bk.run_model(None, "m", panel, workers=2)
    assert [r["id"] for r in arm["rows"]] == [0, 1, 2]
    assert bk._rows_by_source(panel) == {0: "idnes", 1: "idnes", 2: "idnes"}
