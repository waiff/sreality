"""mmreality_main on the portal framework: MmRealityPortal (ten per-type indexes,
each proved against its own declared count) seams + the main() that drives index-walk then detail-drain through
the shared runner, recording an 'index' + a 'detail' scrape_runs row tagged
source='mmreality'.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from scraper import mmreality_main
from scraper.mmreality_main import MmRealityPortal
from scraper.portal import PortalConfig
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem


class _Conn:
    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def close(self) -> None:
        pass


CATS = [
    {"sale_type": sale, "category": cat}
    for sale in ("prodej", "pronajem")
    for cat in ("byty", "domy", "pozemky", "komercni-objekty", "ostatni")
]
BYTY = {"sale_type": "prodej", "category": "byty"}


def _config() -> PortalConfig:
    return PortalConfig(
        source="mmreality",
        supports_complete_walk=False,
        categories=[dict(c) for c in CATS],
        split_threshold=None,
    )


def _portal(**kw: Any) -> MmRealityPortal:
    return MmRealityPortal(_config(), **kw)


class _Limiter:
    def acquire(self) -> None:
        pass

    def penalize(self) -> None:
        pass


# --- main(): two-phase run recording ---------------------------------------


def test_main_records_index_and_detail_runs(monkeypatch):
    starts: list[tuple] = []
    finals: list[tuple] = []
    monkeypatch.setattr(mmreality_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(mmreality_main.db, "connect", lambda: _Conn())
    monkeypatch.setattr(
        mmreality_main.db, "scrape_run_start",
        lambda _c, run_type, source: (starts.append((run_type, source)) or len(starts)),
    )
    monkeypatch.setattr(
        mmreality_main.db, "scrape_run_finalize",
        lambda _c, run_id, **kw: finals.append((run_id, kw)),
    )
    monkeypatch.setattr(
        mmreality_main.portal_runner, "run_index_walk",
        lambda portal, dry_run, **kw: (0, {"index_pages": 2, "listings_found_new": 4}),
    )
    monkeypatch.setattr(
        mmreality_main.portal_runner, "run_detail_drain",
        lambda portal, dry_run, **kw: (0, {"listings_scraped_new": 3}),
    )

    rc = mmreality_main.main(["--max-detail", "10"])
    assert rc == 0
    assert starts == [("index", "mmreality"), ("detail", "mmreality")]
    assert [kw["index_pages"] for _id, kw in finals] == [2, 0]
    assert finals[1][1]["listings_scraped_new"] == 3


def _stub_phases(monkeypatch, calls):
    monkeypatch.setattr(mmreality_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(mmreality_main.db, "connect", lambda: _Conn())
    monkeypatch.setattr(
        mmreality_main.db, "scrape_run_start",
        lambda _c, run_type, source: (calls.append(run_type) or len(calls)),
    )
    monkeypatch.setattr(mmreality_main.db, "scrape_run_finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(
        mmreality_main.portal_runner, "run_index_walk", lambda portal, dry_run, **kw: (0, {}))
    monkeypatch.setattr(
        mmreality_main.portal_runner, "run_detail_drain", lambda portal, dry_run, **kw: (0, {}))


def test_index_only_skips_drain(monkeypatch):
    calls: list[str] = []
    _stub_phases(monkeypatch, calls)
    assert mmreality_main.main(["--index-only"]) == 0
    assert calls == ["index"]


def test_drain_only_skips_index(monkeypatch):
    calls: list[str] = []
    _stub_phases(monkeypatch, calls)
    assert mmreality_main.main(["--drain-only", "--max-detail", "100"]) == 0
    assert calls == ["detail"]


def test_dry_run_records_no_scrape_run(monkeypatch):
    starts = {"n": 0}
    monkeypatch.setattr(mmreality_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(
        mmreality_main.db, "scrape_run_start",
        lambda *_a, **_k: starts.__setitem__("n", starts["n"] + 1) or 1,
    )
    monkeypatch.setattr(mmreality_main.db, "scrape_run_finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(
        mmreality_main.portal_runner, "run_index_walk", lambda portal, dry_run, **kw: (0, {}))
    monkeypatch.setattr(
        mmreality_main.portal_runner, "run_detail_drain", lambda portal, dry_run, **kw: (0, {}))
    assert mmreality_main.main(["--dry-run"]) == 0
    assert starts["n"] == 0


# --- MmRealityPortal seams --------------------------------------------------


def test_portal_config_categories_and_labels():
    p = _portal()
    assert p.source == "mmreality"
    # The flag is the live registry row's; the coverage gate flips it from
    # ledger evidence, never code.
    assert p.supports_complete_walk is False
    assert len(p.categories()) == 10
    assert p.category_labels(BYTY) == ("byt", "prodej")
    assert p.category_labels({"sale_type": "pronajem", "category": "komercni-objekty"}) == (
        "komercni", "pronajem")
    assert p.category_labels({"index": "nemovitosti"}) == (None, None)
    assert {p.category_labels(c) for c in p.categories()} == {
        (m, t) for m in ("byt", "dum", "pozemek", "komercni", "ostatni")
        for t in ("prodej", "pronajem")
    }


def test_old_mixed_index_config_falls_back_to_the_per_type_list():
    """A registry row still on the pre-2026-09 shape must walk the ten per-type
    indexes, not nothing — and not the old prodej-only feed."""
    cfg = PortalConfig(
        source="mmreality", supports_complete_walk=False,
        categories=[{"index": "nemovitosti"}], split_threshold=None,
    )
    p = MmRealityPortal(cfg)
    assert len(p.categories()) == 10
    assert all({"sale_type", "category"} <= set(c) for c in p.categories())


class _ScriptedClient:
    """fetch_index returns the page number as the 'html'; parse_index is
    monkeypatched to look it up in a script."""

    calls: list[tuple] = []

    def __init__(self, *a, **k):
        pass

    def fetch_index(self, sale_type, category, page=None):
        _ScriptedClient.calls.append((sale_type, category, page))
        return (str(page or 1), 200)


def _item(nid: str, price: str = "5 000 000 Kč") -> SimpleNamespace:
    return SimpleNamespace(
        source_id_native=nid,
        detail_path=f"https://www.mmreality.cz/nemovitosti/{nid}/",
        price_text=price,
    )


def _page(items, *, total, next_offset=None) -> SimpleNamespace:
    return SimpleNamespace(total=total, next_offset=next_offset, items=list(items))


def _stub_walk(monkeypatch, script: dict[str, SimpleNamespace], existing=None) -> dict[str, Any]:
    """Wire the walk's DB seams to capture dicts; returns the capture."""
    cap: dict[str, Any] = {"ledger": [], "touched": [], "entries": []}
    _ScriptedClient.calls = []
    monkeypatch.setattr(mmreality_main, "MmRealityClient", _ScriptedClient)
    monkeypatch.setattr(mmreality_main, "parse_index", lambda h: script[h])
    monkeypatch.setattr(
        mmreality_main.db, "index_summary_native", lambda _c, _s, ids: dict(existing or {}))
    monkeypatch.setattr(
        mmreality_main.db, "touch_listings_by_id", lambda _c, pks: cap["touched"].extend(pks))
    monkeypatch.setattr(
        mmreality_main.db, "enqueue_detail",
        lambda _c, source, entries: (cap["entries"].extend(entries) or len(entries)))
    monkeypatch.setattr(
        mmreality_main.db, "record_index_slice", lambda _c, **kw: cap["ledger"].append(kw))
    return cap


def test_walk_category_classifies_and_proves_complete_against_the_declared_count(monkeypatch):
    a, b, c = "944001", "944002", "944003"  # new, changed, unchanged
    cap = _stub_walk(
        monkeypatch,
        {"1": _page([_item(a), _item(b, "6 000 000 Kč"), _item(c, "7 000 000 Kč")], total=3)},
        existing={
            b: {"id": 8102, "sreality_id": -2, "price_czk": 5_500_000, "last_seen_at": None},
            c: {"id": 8103, "sreality_id": -3, "price_czk": 7_000_000, "last_seen_at": None},
        },
    )
    seen, counts, total, pages, complete = _portal().walk_category(
        BYTY, object(), False, _Limiter())
    assert seen == {a, b, c}
    assert (total, pages, complete) == (3, 1, True)
    assert _ScriptedClient.calls == [("prodej", "byty", None)]
    assert cap["touched"] == [8103]
    refs = {e[0]: e for e in cap["entries"]}
    assert refs[a][3] == mmreality_main.db.QUEUE_PRIORITY_NEW
    assert refs[b][3] == mmreality_main.db.QUEUE_PRIORITY_CHANGED
    assert refs[a][1] == f"https://www.mmreality.cz/nemovitosti/{a}/"
    assert c not in refs
    [row] = cap["ledger"]
    assert row == {
        "source": "mmreality", "category_main": "byt", "category_type": "prodej",
        "slice_key": "national", "outcome": "exhausted", "declared_total": 3,
        "collected": 3, "pages": 1,
    }


def test_walk_pages_to_the_tail_and_reports_every_page(monkeypatch):
    cap = _stub_walk(monkeypatch, {
        "1": _page([_item("1"), _item("2")], total=3, next_offset=2),
        "2": _page([_item("3")], total=3),
    })
    seen, _c, total, pages, complete = _portal().walk_category(BYTY, object(), False, _Limiter())
    assert (len(seen), total, pages, complete) == (3, 3, 2, True)
    assert _ScriptedClient.calls == [("prodej", "byty", None), ("prodej", "byty", 2)]
    assert cap["ledger"][0]["outcome"] == "exhausted"


def test_walk_short_of_the_declared_count_is_incomplete_and_degraded(monkeypatch):
    """Rule #3: 3 of 10 is not a walk that saw everything, so no sweep and the
    ledger says so."""
    cap = _stub_walk(monkeypatch, {"1": _page([_item("1"), _item("2"), _item("3")], total=10)})
    _s, _c, total, _p, complete = _portal().walk_category(BYTY, object(), False, _Limiter())
    assert (total, complete) == (10, False)
    assert cap["ledger"][0]["outcome"] == "degraded"


def test_walk_with_no_declared_count_is_never_complete(monkeypatch):
    """An SSR-less page (throttle, error) measures nothing. 'unknown' must not
    read as complete — that fail-open was the 2026-08-27 audit hole."""
    cap = _stub_walk(monkeypatch, {"1": _page([_item("1")], total=None)})
    _s, _c, total, _p, complete = _portal().walk_category(BYTY, object(), False, _Limiter())
    assert (total, complete) == (None, False)
    assert cap["ledger"][0]["outcome"] == "degraded"


def test_walk_of_an_empty_category_with_declared_zero_is_complete(monkeypatch):
    cap = _stub_walk(monkeypatch, {"1": _page([], total=0)})
    seen, _c, total, _p, complete = _portal().walk_category(BYTY, object(), False, _Limiter())
    assert (seen, total, complete) == (set(), 0, True)
    assert cap["ledger"][0]["outcome"] == "exhausted"


def test_deadline_stops_before_fetching_and_is_incomplete(monkeypatch):
    cap = _stub_walk(monkeypatch, {"1": _page([_item("1")], total=1)})
    monkeypatch.setattr(mmreality_main, "deadline_reached", lambda _d: True)
    _s, _c, total, pages, complete = _portal().walk_category(
        BYTY, object(), False, _Limiter(), deadline=1.0)
    assert (total, pages, complete) == (None, 0, False)
    assert _ScriptedClient.calls == []
    assert cap["ledger"][0]["outcome"] == "deadline"


def test_page_cap_is_a_partial_walk(monkeypatch):
    cap = _stub_walk(monkeypatch, {
        "1": _page([_item("1")], total=2, next_offset=2),
        "2": _page([_item("2")], total=2),
    })
    _s, _c, _t, pages, complete = _portal(max_pages=1).walk_category(
        BYTY, object(), False, _Limiter())
    assert (pages, complete) == (1, False)
    assert cap["ledger"][0]["outcome"] == "ceiling"


def test_a_pager_that_does_not_advance_stops_the_walk(monkeypatch):
    """A next link pointing at the current page would loop forever; stop, and
    let the arithmetic call the result short."""
    cap = _stub_walk(monkeypatch, {"1": _page([_item("1"), _item("2")], total=6, next_offset=1)})
    _s, _c, _t, pages, complete = _portal().walk_category(BYTY, object(), False, _Limiter())
    assert (pages, complete) == (1, False)
    assert cap["ledger"][0]["outcome"] == "degraded"


def test_delisting_uses_the_runners_default_nomination():
    """Rule #3 since 2026-09-07: the walk nominates unseen rows for a page
    check and the runner does it generically, keyed on the native id the index
    walked (a NULL sreality_id under listing-identity Gate 2 can never poison
    it). mmreality has no special scoping, so it carries neither the old sweep
    seam nor an override."""
    p = _portal()
    assert not hasattr(p, "mark_inactive")
    assert not hasattr(p, "presence_candidates")
    assert getattr(p, "seen_key", "native") == "native"


def test_active_count_is_category_scoped(monkeypatch):
    monkeypatch.setattr(
        mmreality_main.db, "active_count",
        lambda conn, cm, ct, source: {("byt", "prodej", "mmreality"): 42}[(cm, ct, source)])
    assert _portal().active_count(object(), BYTY) == 42
    assert _portal().active_count(object(), {"index": "nemovitosti"}) is None


class _DetailClient:
    def __init__(self, behavior):
        self._behavior = behavior

    def fetch_detail(self, ref):
        if self._behavior == "gone":
            raise ListingGoneError("/x", 404)
        if self._behavior == "boom":
            raise RuntimeError("network")
        return ("<html>detail</html>", 200)


def test_fetch_detail_ok_passes_source_url(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_parse(html, *, source_url):
        captured["url"] = source_url
        return SimpleNamespace(raw={}, lat=50.0, lon=14.0)

    monkeypatch.setattr(mmreality_main, "parse_detail", fake_parse)
    ref = "https://www.mmreality.cz/nemovitosti/944445/"
    item = _portal().fetch_detail(_DetailClient("ok"), "944445", ref)
    assert item.kind == "ok"
    assert captured["url"] == ref


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
    monkeypatch.setattr(mmreality_main.db, "upsert_portal_raw_page", lambda *a, **k: 9)
    monkeypatch.setattr(
        mmreality_main.db, "ingest_scraped_listing",
        lambda _c, _l, discovery_seq=None, discovered_at=None: (8105, "new"))
    monkeypatch.setattr(mmreality_main.db, "record_images", lambda _c, _sid, imgs, **k: len(imgs))
    monkeypatch.setattr(mmreality_main.db, "mark_portal_page_parsed", lambda *a, **k: None)
    counts = _portal().write_details(object(), items)
    assert counts["new"] == 1
    assert counts["images_discovered"] == 2


def test_mark_gone_flips_listing_inactive_native(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        mmreality_main.db, "mark_listing_inactive_native",
        lambda _c, source, native_id: captured.update(source=source, native_id=native_id),
    )
    _portal().mark_gone(object(), "944445")
    assert captured == {"source": "mmreality", "native_id": "944445"}
