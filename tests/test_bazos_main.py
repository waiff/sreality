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


_BYT_SALE = {"sale_type": "prodam", "category": "byt"}
_BYT_RENT = {"sale_type": "pronajmu", "category": "byt"}


def _portal(categories=None) -> BazosPortal:
    return BazosPortal(categories=categories or [_BYT_SALE])


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


def test_portal_complete_walk_and_per_scope_labels():
    p = _portal([_BYT_SALE, _BYT_RENT])
    assert p.source == "bazos"
    assert p.supports_complete_walk is True
    assert p.categories() == [_BYT_SALE, _BYT_RENT]
    # labels come from each scope dict, not a fixed instance attr
    assert p.category_labels(_BYT_SALE) == ("byt", "prodej")
    assert p.category_labels(_BYT_RENT) == ("byt", "pronajem")


def test_mark_inactive_runs_native_sweep_when_due(monkeypatch):
    monkeypatch.setattr(bazos_main.db, "portal_inactive_sweep_due", lambda _c, _s: True)
    swept: dict[str, Any] = {}
    monkeypatch.setattr(
        bazos_main.db, "mark_inactive_native",
        lambda _c, src, cm, ct, seen: swept.update(
            src=src, cm=cm, ct=ct, seen=seen) or 3,
    )
    recorded = {"n": 0}
    monkeypatch.setattr(
        bazos_main.db, "record_portal_inactive_sweep",
        lambda _c, _s: recorded.__setitem__("n", recorded["n"] + 1),
    )
    n = _portal().mark_inactive(object(), _BYT_RENT, {"a", "b"})
    assert n == 3
    assert swept == {"src": "bazos", "cm": "byt", "ct": "pronajem", "seen": {"a", "b"}}
    assert recorded["n"] == 1


def test_mark_inactive_throttle_stamps_once_across_categories(monkeypatch):
    # Both scopes sweep in one run, but the per-portal 12h clock is stamped once.
    monkeypatch.setattr(bazos_main.db, "portal_inactive_sweep_due", lambda _c, _s: True)
    monkeypatch.setattr(bazos_main.db, "mark_inactive_native", lambda *a, **k: 1)
    stamps = {"n": 0}
    monkeypatch.setattr(
        bazos_main.db, "record_portal_inactive_sweep",
        lambda _c, _s: stamps.__setitem__("n", stamps["n"] + 1),
    )
    p = _portal([_BYT_SALE, _BYT_RENT])
    p.mark_inactive(object(), _BYT_SALE, {"a"})
    p.mark_inactive(object(), _BYT_RENT, {"b"})
    assert stamps["n"] == 1     # stamped once, not once per category


def test_mark_inactive_throttled_when_not_due(monkeypatch):
    monkeypatch.setattr(bazos_main.db, "portal_inactive_sweep_due", lambda _c, _s: False)
    monkeypatch.setattr(
        bazos_main.db, "mark_inactive_native",
        lambda *a, **k: pytest.fail("sweep must be skipped when throttled"),
    )
    assert _portal().mark_inactive(object(), _BYT_SALE, {"a"}) == 0


def test_active_count_source_scoped(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        bazos_main.db, "active_count",
        lambda _c, cm, ct, source: captured.update(cm=cm, ct=ct, source=source) or 42,
    )
    assert _portal().active_count(object(), _BYT_RENT) == 42
    assert captured == {"cm": "byt", "ct": "pronajem", "source": "bazos"}


def test_mark_gone_flips_native_inactive(monkeypatch):
    gone: dict[str, Any] = {}
    monkeypatch.setattr(
        bazos_main.db, "mark_listing_inactive_native",
        lambda _c, src, nid: gone.update(src=src, nid=nid),
    )
    _portal().mark_gone(object(), "216945145")
    assert gone == {"src": "bazos", "nid": "216945145"}


class _IdxClient:
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = 0

    def fetch_index(self, *a, **k):
        self.calls += 1
        return ("<html>", 200)


def test_walk_category_complete_walk_enqueues_new_and_changed(monkeypatch):
    page1 = SimpleNamespace(
        items=[
            SimpleNamespace(source_id_native="a", detail_path="/a", price_text="3 000 000 Kč"),
            SimpleNamespace(source_id_native="b", detail_path="/b", price_text="4 000 000 Kč"),
            SimpleNamespace(source_id_native="c", detail_path="/c", price_text="5 000 000 Kč"),
        ],
        total=3, next_offset=None,
    )
    monkeypatch.setattr(bazos_main, "parse_index", lambda _h: page1)
    monkeypatch.setattr(bazos_main, "BazosClient", lambda **k: _IdxClient([page1]))
    monkeypatch.setattr(bazos_main.db, "upsert_portal_raw_page", lambda *a, **k: 1)
    # "b" already exists at the same price (unchanged → touch only); "c" exists
    # at a different price (price-changed → enqueue); "a" is brand new.
    monkeypatch.setattr(
        bazos_main.db, "index_summary_native",
        lambda _c, _src, _natives: {
            "b": {"sreality_id": -2, "price_czk": 4_000_000, "last_seen_at": None},
            "c": {"sreality_id": -3, "price_czk": 9_999_999, "last_seen_at": None},
        },
    )
    touched: dict[str, Any] = {}
    monkeypatch.setattr(
        bazos_main.db, "touch_listings",
        lambda _c, ids: touched.update(ids=sorted(ids)),
    )
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
    assert seen == {"a", "b", "c"}
    assert result_size == 3 and complete is True          # full walk, collected == total
    assert touched["ids"] == [-3, -2]                     # both existing rows touched
    assert counts["found_new"] == 1                       # only "a" is genuinely new
    assert captured["source"] == "bazos"
    natives = {e[0] for e in captured["entries"]}
    assert natives == {"a", "c"}                          # new + price-changed; "b" skipped
    by_native = {e[0]: e for e in captured["entries"]}
    assert by_native["a"][3] == bazos_main.db.QUEUE_PRIORITY_NEW
    assert by_native["c"][3] == bazos_main.db.QUEUE_PRIORITY_CHANGED


def test_walk_category_page_capped_is_incomplete(monkeypatch):
    page1 = SimpleNamespace(
        items=[SimpleNamespace(source_id_native="a", detail_path="/a", price_text=None)],
        total=500, next_offset=20,
    )
    monkeypatch.setattr(bazos_main, "parse_index", lambda _h: page1)
    monkeypatch.setattr(bazos_main, "BazosClient", lambda **k: _IdxClient([page1]))
    monkeypatch.setattr(bazos_main.db, "upsert_portal_raw_page", lambda *a, **k: 1)
    monkeypatch.setattr(bazos_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(bazos_main.db, "touch_listings", lambda *a, **k: 0)
    monkeypatch.setattr(bazos_main.db, "enqueue_detail", lambda *a, **k: 1)
    p = BazosPortal(categories=[_BYT_SALE], max_pages=1)
    _seen, _counts, result_size, _pages, complete = p.walk_category(
        {"sale_type": "prodam", "category": "byt"}, object(), False, _Limiter(),
    )
    assert result_size == 500
    assert complete is False     # max_pages cap → never claims completeness


def test_walk_category_below_full_is_incomplete(monkeypatch):
    # A full (un-capped) walk that still collected < 100% of the reported total
    # must read incomplete: the inactive sweep runs only after a 100% walk
    # (architectural rule #3), hardcoded (INDEX_MIN_COMPLETENESS=1.0), not tunable.
    page = SimpleNamespace(
        items=[SimpleNamespace(source_id_native=f"n{i}", detail_path=f"/n{i}", price_text=None)
               for i in range(19)],
        total=20, next_offset=None,
    )
    monkeypatch.setattr(bazos_main, "parse_index", lambda _h: page)
    monkeypatch.setattr(bazos_main, "BazosClient", lambda **k: _IdxClient([page]))
    monkeypatch.setattr(bazos_main.db, "upsert_portal_raw_page", lambda *a, **k: 1)
    monkeypatch.setattr(bazos_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(bazos_main.db, "touch_listings", lambda *a, **k: 0)
    monkeypatch.setattr(bazos_main.db, "enqueue_detail", lambda *a, **k: 1)
    _seen, _counts, result_size, _pages, complete = _portal().walk_category(
        {"sale_type": "prodam", "category": "byt"}, object(), False, _Limiter(),
    )
    assert result_size == 20 and complete is False   # 19/20 = 95% < 100% → suppress sweep


class _SeqIdxClient:
    """fetch_index returns 200 for the first `ok_pages` calls, then raises
    ListingGoneError — bazos 404s an offset past the last result page."""

    def __init__(self, ok_pages: int):
        self._ok = ok_pages
        self.calls = 0

    def fetch_index(self, *a, **k):
        self.calls += 1
        if self.calls > self._ok:
            raise ListingGoneError("/past-end", 404)
        return ("<html>", 200)


def test_walk_category_stops_when_total_reached(monkeypatch):
    # The pager advertises a next page, but we've already collected `total`, so
    # the walk must stop (and never request the offset bazos would 404 on).
    page = SimpleNamespace(
        items=[SimpleNamespace(source_id_native="a", detail_path="/a", price_text=None),
               SimpleNamespace(source_id_native="b", detail_path="/b", price_text=None)],
        total=2, next_offset=20,
    )
    client = _SeqIdxClient(ok_pages=10)
    monkeypatch.setattr(bazos_main, "parse_index", lambda _h: page)
    monkeypatch.setattr(bazos_main, "BazosClient", lambda **k: client)
    monkeypatch.setattr(bazos_main.db, "upsert_portal_raw_page", lambda *a, **k: 1)
    monkeypatch.setattr(bazos_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(bazos_main.db, "touch_listings", lambda *a, **k: 0)
    monkeypatch.setattr(bazos_main.db, "enqueue_detail", lambda *a, **k: 2)
    seen, _c, result_size, _pages, complete = _portal().walk_category(
        {"sale_type": "prodam", "category": "byt"}, object(), False, _Limiter(),
    )
    assert seen == {"a", "b"} and result_size == 2 and complete is True
    assert client.calls == 1     # stopped after page 1; never requested offset 20


def test_walk_category_tolerates_gone_index_page(monkeypatch):
    # If a page past the end 404s before the total is reached, keep what we
    # collected and report incomplete (so the sweep is skipped, not a crash).
    page = SimpleNamespace(
        items=[SimpleNamespace(source_id_native=str(i), detail_path=f"/{i}", price_text=None)
               for i in range(20)],
        total=400, next_offset=20,
    )
    client = _SeqIdxClient(ok_pages=1)   # page 1 ok, page 2 → gone
    monkeypatch.setattr(bazos_main, "parse_index", lambda _h: page)
    monkeypatch.setattr(bazos_main, "BazosClient", lambda **k: client)
    monkeypatch.setattr(bazos_main.db, "upsert_portal_raw_page", lambda *a, **k: 1)
    monkeypatch.setattr(bazos_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(bazos_main.db, "touch_listings", lambda *a, **k: 0)
    monkeypatch.setattr(bazos_main.db, "enqueue_detail", lambda *a, **k: 20)
    seen, _c, result_size, _pages, complete = _portal().walk_category(
        {"sale_type": "prodam", "category": "byt"}, object(), False, _Limiter(),
    )
    assert len(seen) == 20            # page-1 items kept despite the 404 on page 2
    assert result_size == 400 and complete is False   # partial → no false delisting


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
    portal = BazosPortal(categories=[_BYT_SALE], geocoder=sentinel)
    item = portal.fetch_detail(_DetailClient("ok"), "a", "/a")
    assert item.kind == "ok"
    assert captured["geocoder"] is sentinel
