"""idnes_main on the portal framework: IdnesPortal seams + the main() that drives
index-walk then detail-drain through the shared runner, recording an 'index' + a
'detail' scrape_runs row tagged source='idnes'.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from scraper import idnes_main
from scraper.geocoding import GeocodeResult, GeocodingError
from scraper.idnes_main import IdnesPortal
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem


class _Conn:
    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def close(self) -> None:
        pass


def _portal() -> IdnesPortal:
    return IdnesPortal(
        sale_type="prodej", category="byty",
        canon_main="byt", canon_type="prodej",
    )


# --- main(): two-phase run recording ---------------------------------------


def test_main_records_index_and_detail_runs(monkeypatch):
    starts: list[tuple] = []
    finals: list[tuple] = []
    monkeypatch.setattr(idnes_main.db, "connect", lambda: _Conn())
    monkeypatch.setattr(
        idnes_main.db, "scrape_run_start",
        lambda _c, run_type, source: (starts.append((run_type, source)) or len(starts)),
    )
    monkeypatch.setattr(
        idnes_main.db, "scrape_run_finalize",
        lambda _c, run_id, **kw: finals.append((run_id, kw)),
    )
    monkeypatch.setattr(
        idnes_main.portal_runner, "run_index_walk",
        lambda portal, dry_run: (0, {"index_pages": 3, "listings_found_new": 5,
                                     "by_category": [{"category_main": "byt"}]}),
    )
    monkeypatch.setattr(
        idnes_main.portal_runner, "run_detail_drain",
        lambda portal, dry_run, **kw: (0, {"listings_scraped_new": 2, "listings_updated": 1}),
    )

    rc = idnes_main.main([])
    assert rc == 0
    assert starts == [("index", "idnes"), ("detail", "idnes")]
    assert [kw["index_pages"] for _id, kw in finals] == [3, 0]
    assert finals[0][1]["by_category"][0]["category_main"] == "byt"
    assert finals[1][1]["listings_scraped_new"] == 2


def test_dry_run_records_no_scrape_run(monkeypatch):
    starts = {"n": 0}
    monkeypatch.setattr(
        idnes_main.db, "scrape_run_start",
        lambda *_a, **_k: starts.__setitem__("n", starts["n"] + 1) or 1,
    )
    monkeypatch.setattr(idnes_main.db, "scrape_run_finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(
        idnes_main.portal_runner, "run_index_walk", lambda portal, dry_run: (0, {})
    )
    monkeypatch.setattr(
        idnes_main.portal_runner, "run_detail_drain", lambda portal, dry_run, **kw: (0, {})
    )
    rc = idnes_main.main(["--dry-run"])
    assert rc == 0
    assert starts["n"] == 0


# --- IdnesPortal seams ------------------------------------------------------


def test_portal_single_category_and_partial_walk():
    p = _portal()
    assert p.source == "idnes"
    assert p.supports_complete_walk is False
    assert p.categories() == [{"sale_type": "prodej", "category": "byty"}]
    assert p.category_labels({}) == ("byt", "prodej")
    assert p.mark_inactive(None, {}, {"x"}) == 0
    assert p.active_count(None, {}) is None


class _IdxClient:
    def __init__(self, *a, **k):
        self.calls = 0

    def fetch_index(self, *a, **k):
        self.calls += 1
        return ("<html>", 200)


def test_walk_category_enqueues_seen(monkeypatch):
    a_url = "https://reality.idnes.cz/detail/prodej/byt/x/6a18deadbeefdeadbeef0001/"
    b_url = "https://reality.idnes.cz/detail/prodej/byt/y/6a18deadbeefdeadbeef0002/"
    page = SimpleNamespace(
        items=[SimpleNamespace(source_id_native="6a18deadbeefdeadbeef0001", detail_path=a_url),
               SimpleNamespace(source_id_native="6a18deadbeefdeadbeef0002", detail_path=b_url)],
        total=2, next_offset=None,
    )
    monkeypatch.setattr(idnes_main, "parse_index", lambda _h: page)
    monkeypatch.setattr(idnes_main, "IdnesClient", _IdxClient)
    monkeypatch.setattr(idnes_main.db, "upsert_portal_raw_page", lambda *a, **k: 1)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        idnes_main.db, "enqueue_detail",
        lambda _c, source, entries: (captured.update(source=source, entries=list(entries))
                                      or len(captured["entries"])),
    )
    p = _portal()
    seen, counts, result_size, pages, complete = p.walk_category(
        {"sale_type": "prodej", "category": "byty"}, object(), False, _Limiter(),
    )
    assert seen == {"6a18deadbeefdeadbeef0001", "6a18deadbeefdeadbeef0002"}
    assert result_size is None and complete is False     # partial walk
    assert captured["source"] == "idnes"
    # entries: (native_id, detail_ref(absolute url), price, priority)
    assert ("6a18deadbeefdeadbeef0001", a_url, None, idnes_main.db.QUEUE_PRIORITY_NEW) in captured["entries"]


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
    monkeypatch.setattr(idnes_main, "parse_detail", lambda *a, **k: SimpleNamespace(raw={}))
    p = _portal()
    item = p.fetch_detail(_DetailClient("ok"), "6a18deadbeefdeadbeef0001", "/d/a")
    assert item.kind == "ok" and item.native_id == "6a18deadbeefdeadbeef0001"
    assert item.payload["status"] == 200


def test_fetch_detail_gone():
    item = _portal().fetch_detail(_DetailClient("gone"), "a", "/d/a")
    assert item.kind == "gone"


def test_fetch_detail_error():
    item = _portal().fetch_detail(_DetailClient("boom"), "a", "/d/a")
    assert item.kind == "error" and item.error


def test_write_details_ingests_and_counts(monkeypatch):
    listing = SimpleNamespace(raw={"image_urls": ["u1", "u2"]})
    items = [DrainItem("a", "ok", payload={
        "listing": listing, "html": "<h>", "status": 200, "url": "/d/a"})]
    monkeypatch.setattr(idnes_main.db, "upsert_portal_raw_page", lambda *a, **k: 9)
    monkeypatch.setattr(idnes_main.db, "ingest_scraped_listing", lambda _c, _l: (-5, "new"))
    monkeypatch.setattr(idnes_main.db, "record_images", lambda _c, _pk, imgs: len(imgs))
    monkeypatch.setattr(idnes_main.db, "mark_portal_page_parsed", lambda *a, **k: None)
    counts = _portal().write_details(object(), items)
    assert counts["new"] == 1
    assert counts["images_discovered"] == 2


# --- geocoder wiring (fallback when the page omits coordinates) --------------


def test_build_geocoder_none_without_key(monkeypatch):
    monkeypatch.delenv("MAPY_CZ_API_KEY", raising=False)
    assert idnes_main._build_geocoder() is None


def test_build_geocoder_cached_with_key(monkeypatch):
    monkeypatch.setenv("MAPY_CZ_API_KEY", "test-key")
    assert isinstance(idnes_main._build_geocoder(), idnes_main._CachingGeocoder)


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

    geocoder = idnes_main._CachingGeocoder(fn)
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

    monkeypatch.setattr(idnes_main, "parse_detail", fake_parse)
    sentinel = object()
    portal = IdnesPortal(
        sale_type="prodej", category="byty",
        canon_main="byt", canon_type="prodej", geocoder=sentinel,
    )
    item = portal.fetch_detail(_DetailClient("ok"), "a", "/d/a")
    assert item.kind == "ok"
    assert captured["geocoder"] is sentinel
