"""bazos_main on the portal framework (Phase 4): BazosPortal seams + the
main() that drives index-walk then detail-drain through the shared runner,
recording an 'index' + a 'detail' scrape_runs row tagged source='bazos'.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from scraper import bazos_main
from scraper.bazos_main import BazosPortal
from scraper.geocoding import GeocodeResult, GeocodingError
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem


class _Conn:
    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def close(self) -> None:
        pass


def _portal() -> BazosPortal:
    return BazosPortal(
        sale_type="prodam", category="byt",
        canon_main="byt", canon_type="prodej",
    )


# --- main(): two-phase run recording ---------------------------------------


def test_main_records_index_and_detail_runs(monkeypatch):
    starts: list[tuple] = []
    finals: list[tuple] = []
    monkeypatch.setattr(bazos_main.db, "connect", lambda: _Conn())
    monkeypatch.setattr(
        bazos_main.db, "scrape_run_start",
        lambda _c, run_type, source: (starts.append((run_type, source)) or len(starts)),
    )
    monkeypatch.setattr(
        bazos_main.db, "scrape_run_finalize",
        lambda _c, run_id, **kw: finals.append((run_id, kw)),
    )
    monkeypatch.setattr(
        bazos_main.portal_runner, "run_index_walk",
        lambda portal, dry_run: (0, {"index_pages": 3, "listings_found_new": 5,
                                      "by_category": [{"category_main": "byt"}]}),
    )
    monkeypatch.setattr(
        bazos_main.portal_runner, "run_detail_drain",
        lambda portal, dry_run, **kw: (0, {"listings_scraped_new": 2, "listings_updated": 1}),
    )

    rc = bazos_main.main([])
    assert rc == 0
    assert starts == [("index", "bazos"), ("detail", "bazos")]
    assert [kw["index_pages"] for _id, kw in finals] == [3, 0]
    assert finals[0][1]["by_category"][0]["category_main"] == "byt"
    assert finals[1][1]["listings_scraped_new"] == 2


def test_dry_run_records_no_scrape_run(monkeypatch):
    starts = {"n": 0}
    monkeypatch.setattr(
        bazos_main.db, "scrape_run_start",
        lambda *_a, **_k: starts.__setitem__("n", starts["n"] + 1) or 1,
    )
    monkeypatch.setattr(bazos_main.db, "scrape_run_finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(
        bazos_main.portal_runner, "run_index_walk", lambda portal, dry_run: (0, {})
    )
    monkeypatch.setattr(
        bazos_main.portal_runner, "run_detail_drain", lambda portal, dry_run, **kw: (0, {})
    )
    rc = bazos_main.main(["--dry-run"])
    assert rc == 0
    assert starts["n"] == 0


def test_main_rejects_unmapped_scope(monkeypatch):
    # argparse choices already constrain these, but the guard is belt-and-braces.
    assert bazos_main.SALE_TYPE.get("prodam") is not None


# --- BazosPortal seams ------------------------------------------------------


def test_portal_single_category_and_partial_walk():
    p = _portal()
    assert p.source == "bazos"
    assert p.supports_complete_walk is False
    assert p.categories() == [{"sale_type": "prodam", "category": "byt"}]
    assert p.category_labels({}) == ("byt", "prodej")
    assert p.mark_inactive(None, {}, {"x"}) == 0
    assert p.active_count(None, {}) is None


class _IdxClient:
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = 0

    def fetch_index(self, *a, **k):
        self.calls += 1
        return ("<html>", 200)


def test_walk_category_enqueues_seen(monkeypatch):
    page1 = SimpleNamespace(
        items=[SimpleNamespace(source_id_native="a", detail_path="/a"),
               SimpleNamespace(source_id_native="b", detail_path="/b")],
        total=2, next_offset=None,
    )
    monkeypatch.setattr(bazos_main, "parse_index", lambda _h: page1)
    monkeypatch.setattr(bazos_main, "BazosClient", lambda **k: _IdxClient([page1]))
    monkeypatch.setattr(bazos_main.db, "upsert_portal_raw_page", lambda *a, **k: 1)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        bazos_main.db, "enqueue_detail",
        lambda _c, source, entries: (captured.update(source=source, entries=list(entries))
                                      or len(captured["entries"])),
    )
    p = _portal()
    seen, counts, result_size, pages, complete = p.walk_category(
        {"sale_type": "prodam", "category": "byt"}, object(), False, _Limiter(),
    )
    assert seen == {"a", "b"}
    assert result_size is None and complete is False     # partial walk
    assert captured["source"] == "bazos"
    # entries: (native_id, detail_ref, price, priority)
    assert ("a", "/a", None, bazos_main.db.QUEUE_PRIORITY_NEW) in captured["entries"]


class _Limiter:
    def acquire(self) -> None:
        pass

    def penalize(self) -> None:
        pass


class _DetailClient:
    def __init__(self, behavior):
        self._behavior = behavior

    def fetch_detail(self, ref):
        if self._behavior == "gone":
            raise ListingGoneError("/x", 404)
        if self._behavior == "boom":
            raise RuntimeError("network")
        return ("<html>detail</html>", 200)


def test_fetch_detail_ok(monkeypatch):
    monkeypatch.setattr(bazos_main, "parse_detail", lambda *a, **k: SimpleNamespace(raw={}))
    p = _portal()
    item = p.fetch_detail(_DetailClient("ok"), "a", "/a")
    assert item.kind == "ok" and item.native_id == "a"
    assert item.payload["status"] == 200


def test_fetch_detail_gone():
    p = _portal()
    item = p.fetch_detail(_DetailClient("gone"), "a", "/a")
    assert item.kind == "gone"


def test_fetch_detail_error():
    p = _portal()
    item = p.fetch_detail(_DetailClient("boom"), "a", "/a")
    assert item.kind == "error" and item.error


def test_write_details_ingests_and_counts(monkeypatch):
    listing = SimpleNamespace(raw={"image_urls": ["u1", "u2"]})
    items = [DrainItem("a", "ok", payload={
        "listing": listing, "html": "<h>", "status": 200, "url": "/a"})]
    monkeypatch.setattr(bazos_main.db, "upsert_portal_raw_page", lambda *a, **k: 9)
    monkeypatch.setattr(bazos_main.db, "ingest_scraped_listing", lambda _c, _l: (-5, "new"))
    monkeypatch.setattr(bazos_main.db, "record_images", lambda _c, _pk, imgs: len(imgs))
    monkeypatch.setattr(bazos_main.db, "mark_portal_page_parsed", lambda *a, **k: None)
    counts = _portal().write_details(object(), items)
    assert counts["new"] == 1
    assert counts["images_discovered"] == 2


# --- geocoder wiring (text-first coordinate resolution) ---------------------


def test_build_geocoder_none_without_key(monkeypatch):
    monkeypatch.delenv("MAPY_CZ_API_KEY", raising=False)
    assert bazos_main._build_geocoder() is None


def test_build_geocoder_cached_with_key(monkeypatch):
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test-key")
    geocoder = bazos_main._build_geocoder()
    assert isinstance(geocoder, bazos_main._CachingGeocoder)


def test_caching_geocoder_memoises_hits_and_misses():
    calls = {"n": 0}

    def fn(query: str) -> GeocodeResult:
        calls["n"] += 1
        if "bad" in query:
            raise GeocodingError("no result")
        return GeocodeResult(
            lat=50.0, lng=14.0, confidence="high", matched_address="a",
            matched_type="regional.address", bbox=None, raw={},
        )

    geocoder = bazos_main._CachingGeocoder(fn)
    assert geocoder("Praha").lat == 50.0
    assert geocoder("  praha ").lat == 50.0     # normalized key -> cache hit
    assert calls["n"] == 1
    with pytest.raises(GeocodingError):
        geocoder("bad street")
    with pytest.raises(GeocodingError):
        geocoder("bad street")                  # cached miss, not re-queried
    assert calls["n"] == 2


def test_fetch_detail_passes_geocoder_to_parser(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_parse(html, *, source_url, category_main, category_type, geocoder=None):
        captured["geocoder"] = geocoder
        return SimpleNamespace(raw={})

    monkeypatch.setattr(bazos_main, "parse_detail", fake_parse)
    sentinel = object()
    portal = BazosPortal(
        sale_type="prodam", category="byt",
        canon_main="byt", canon_type="prodej", geocoder=sentinel,
    )
    item = portal.fetch_detail(_DetailClient("ok"), "a", "/a")
    assert item.kind == "ok"
    assert captured["geocoder"] is sentinel
