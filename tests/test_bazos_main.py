"""bazos_main run-recording wiring (migration 100 — per-portal Health stats).

The bazos crawl now opens a scrape_runs row tagged source='bazos' so the
Health dashboard can show bazos activity alongside sreality. A one-category
crawl is a partial walk, so it is recorded as run_type='delta' and never
marks listings inactive.
"""

from __future__ import annotations

from scraper import bazos_main


class _Conn:
    def close(self) -> None:
        pass


def _stub_walk_and_refetch(monkeypatch, *, pages: int) -> None:
    monkeypatch.setattr(bazos_main, "BazosClient", lambda **_k: object())
    monkeypatch.setattr(
        bazos_main, "_walk_index", lambda *_a, **_k: ([("1", "/p/1")], pages)
    )

    def _fake_refetch(_client, _conn, _args, _details, _cm, _ct, counts, _geocoder):
        counts["new"] += 2
        counts["updated"] += 1
        counts["images"] += 4

    monkeypatch.setattr(bazos_main, "_refetch_details", _fake_refetch)


def test_bazos_main_records_delta_run_for_bazos(monkeypatch):
    calls: dict[str, object] = {}
    monkeypatch.setattr(bazos_main.db, "connect", lambda: _Conn())
    monkeypatch.setattr(
        bazos_main.db, "scrape_run_start",
        lambda _conn, run_type, source: calls.__setitem__("start", (run_type, source)) or 7,
    )
    monkeypatch.setattr(
        bazos_main.db, "scrape_run_finalize",
        lambda _conn, run_id, **kw: calls.__setitem__("finalize", (run_id, kw)),
    )
    _stub_walk_and_refetch(monkeypatch, pages=3)

    rc = bazos_main.main([])
    assert rc == 0

    assert calls["start"] == ("delta", "bazos")
    run_id, kw = calls["finalize"]
    assert run_id == 7
    assert kw["index_pages"] == 3
    assert kw["listings_scraped_new"] == 2
    assert kw["listings_found_new"] == 2
    assert kw["listings_updated"] == 1
    assert kw["listings_inactive"] == 0      # partial walk never marks inactive
    assert kw["images_discovered"] == 4
    assert kw["by_category"][0]["category_main"] == "byt"


def test_bazos_dry_run_records_no_scrape_run(monkeypatch):
    calls = {"start": 0, "finalize": 0}
    monkeypatch.setattr(
        bazos_main.db, "scrape_run_start",
        lambda *_a, **_k: calls.__setitem__("start", calls["start"] + 1) or 1,
    )
    monkeypatch.setattr(
        bazos_main.db, "scrape_run_finalize",
        lambda *_a, **_k: calls.__setitem__("finalize", calls["finalize"] + 1),
    )
    _stub_walk_and_refetch(monkeypatch, pages=1)

    rc = bazos_main.main(["--dry-run"])
    assert rc == 0
    assert calls["start"] == 0
    assert calls["finalize"] == 0
