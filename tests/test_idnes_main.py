"""idnes_main on the portal framework: IdnesPortal (complete-walk) seams + the
main() that drives index-walk then detail-drain through the shared runner,
recording an 'index' + a 'detail' scrape_runs row tagged source='idnes'.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from scraper import idnes_main
from scraper.idnes_main import IdnesPortal
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


def _config(complete: bool = True) -> PortalConfig:
    return PortalConfig(
        source="idnes",
        supports_complete_walk=complete,
        categories=[{"sale_type": "prodej", "category": "byty"}],
        split_threshold=None,
    )


def _portal(**kw: Any) -> IdnesPortal:
    return IdnesPortal(_config(), **kw)


class _Limiter:
    def acquire(self) -> None:
        pass

    def penalize(self) -> None:
        pass


# --- main(): two-phase run recording ---------------------------------------


def test_main_records_index_and_detail_runs(monkeypatch):
    starts: list[tuple] = []
    finals: list[tuple] = []
    monkeypatch.setattr(idnes_main, "_load_config", lambda dry_run: _config())
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
        lambda portal, dry_run, **kw: (0, {"index_pages": 3, "listings_found_new": 5,
                                     "by_category": [{"category_main": "byt"}]}),
    )
    monkeypatch.setattr(
        idnes_main.portal_runner, "run_detail_drain",
        lambda portal, dry_run, **kw: (0, {"listings_scraped_new": 2, "listings_updated": 1}),
    )

    rc = idnes_main.main(["--max-detail", "10"])
    assert rc == 0
    assert starts == [("index", "idnes"), ("detail", "idnes")]
    assert [kw["index_pages"] for _id, kw in finals] == [3, 0]
    assert finals[1][1]["listings_scraped_new"] == 2


def _stub_phases(monkeypatch, calls):
    monkeypatch.setattr(idnes_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(idnes_main.db, "connect", lambda: _Conn())
    monkeypatch.setattr(
        idnes_main.db, "scrape_run_start",
        lambda _c, run_type, source: (calls.append(run_type) or len(calls)),
    )
    monkeypatch.setattr(idnes_main.db, "scrape_run_finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(
        idnes_main.portal_runner, "run_index_walk", lambda portal, dry_run, **kw: (0, {}))
    monkeypatch.setattr(
        idnes_main.portal_runner, "run_detail_drain", lambda portal, dry_run, **kw: (0, {}))


def test_index_only_skips_drain(monkeypatch):
    calls: list[str] = []
    _stub_phases(monkeypatch, calls)
    assert idnes_main.main(["--index-only"]) == 0
    assert calls == ["index"]            # no detail phase


def test_drain_only_skips_index(monkeypatch):
    calls: list[str] = []
    _stub_phases(monkeypatch, calls)
    assert idnes_main.main(["--drain-only", "--max-detail", "100"]) == 0
    assert calls == ["detail"]           # no index phase


def test_dry_run_records_no_scrape_run(monkeypatch):
    starts = {"n": 0}
    monkeypatch.setattr(idnes_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(
        idnes_main.db, "scrape_run_start",
        lambda *_a, **_k: starts.__setitem__("n", starts["n"] + 1) or 1,
    )
    monkeypatch.setattr(idnes_main.db, "scrape_run_finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(
        idnes_main.portal_runner, "run_index_walk", lambda portal, dry_run, **kw: (0, {})
    )
    monkeypatch.setattr(
        idnes_main.portal_runner, "run_detail_drain", lambda portal, dry_run, **kw: (0, {})
    )
    rc = idnes_main.main(["--dry-run"])
    assert rc == 0
    assert starts["n"] == 0


# --- IdnesPortal seams ------------------------------------------------------


def test_portal_config_and_complete_walk():
    p = _portal()
    assert p.source == "idnes"
    assert p.supports_complete_walk is True
    assert p.categories() == [{"sale_type": "prodej", "category": "byty"}]
    assert p.category_labels({"sale_type": "prodej", "category": "byty"}) == ("byt", "prodej")


class _IdxClient:
    def __init__(self, *a, **k):
        self.calls = 0

    def fetch_index(self, *a, **k):
        self.calls += 1
        return ("<html>", 200)


def test_walk_category_classifies_new_changed_unchanged(monkeypatch):
    a = "6a18deadbeefdeadbeef0001"  # new
    b = "6a18deadbeefdeadbeef0002"  # price changed
    c = "6a18deadbeefdeadbeef0003"  # unchanged
    base = "https://reality.idnes.cz/detail/prodej/byt/x/"
    page = SimpleNamespace(
        total=3, next_offset=None,
        items=[
            SimpleNamespace(source_id_native=a, detail_path=f"{base}{a}/", price_text="5 000 000 Kč"),
            SimpleNamespace(source_id_native=b, detail_path=f"{base}{b}/", price_text="6 000 000 Kč"),
            SimpleNamespace(source_id_native=c, detail_path=f"{base}{c}/", price_text="7 000 000 Kč"),
        ],
    )
    monkeypatch.setattr(idnes_main, "parse_index", lambda _h: page)
    monkeypatch.setattr(idnes_main, "IdnesClient", _IdxClient)
    monkeypatch.setattr(idnes_main.db, "upsert_portal_raw_page", lambda *a, **k: 1)
    monkeypatch.setattr(
        idnes_main.db, "index_summary_native",
        lambda _c, _s, ids: {
            b: {"sreality_id": -2, "price_czk": 5_500_000, "last_seen_at": None},  # differs -> changed
            c: {"sreality_id": -3, "price_czk": 7_000_000, "last_seen_at": None},  # same -> unchanged
        },
    )
    touched: dict[str, Any] = {}
    monkeypatch.setattr(idnes_main.db, "touch_listings", lambda _c, pks: touched.update(pks=list(pks)))
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        idnes_main.db, "enqueue_detail",
        lambda _c, source, entries: (captured.update(source=source, entries=list(entries))
                                      or len(captured["entries"])),
    )
    seen, counts, total, pages, complete = _portal().walk_category(
        {"sale_type": "prodej", "category": "byty"}, object(), False, _Limiter(),
    )
    assert seen == {a, b, c}
    assert total == 3 and complete is True       # full walk (no max_pages), collected == total
    assert touched["pks"] == [-3]                # unchanged listing touched
    refs = {e[0]: e for e in captured["entries"]}
    assert refs[a][3] == idnes_main.db.QUEUE_PRIORITY_NEW      # new
    assert refs[b][3] == idnes_main.db.QUEUE_PRIORITY_CHANGED  # changed
    assert refs[a][1] == f"{base}{a}/"           # detail_ref is the absolute URL
    assert c not in refs                          # unchanged is not enqueued


def test_walk_complete_requires_near_full_walk():
    # mark_inactive only after a ~complete walk (architectural rule #3); the bar
    # is hardcoded (INDEX_MIN_COMPLETENESS=0.995, tolerating mid-walk churn), not
    # operator-tunable — a genuinely truncated walk still reads incomplete.
    assert idnes_main._walk_complete(100, 100) is True
    assert idnes_main._walk_complete(996, 1000) is True   # 0.4% deficit = churn
    assert idnes_main._walk_complete(994, 1000) is False  # 0.6% deficit = truncated
    assert idnes_main._walk_complete(99, 100) is False
    assert idnes_main._walk_complete(90, 100) is False
    assert idnes_main._walk_complete(0, None) is True   # unknown total → trust the walk


def test_walk_category_max_pages_suppresses_complete(monkeypatch):
    page = SimpleNamespace(
        total=1000, next_offset=2,
        items=[SimpleNamespace(
            source_id_native="6a18deadbeefdeadbeef0001",
            detail_path="https://reality.idnes.cz/detail/prodej/byt/x/6a18deadbeefdeadbeef0001/",
            price_text="5 000 000 Kč")],
    )
    monkeypatch.setattr(idnes_main, "parse_index", lambda _h: page)
    monkeypatch.setattr(idnes_main, "IdnesClient", _IdxClient)
    monkeypatch.setattr(idnes_main.db, "upsert_portal_raw_page", lambda *a, **k: 1)
    monkeypatch.setattr(idnes_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(idnes_main.db, "enqueue_detail", lambda *a, **k: 1)
    _, _, total, pages, complete = _portal(max_pages=1).walk_category(
        {"sale_type": "prodej", "category": "byty"}, object(), False, _Limiter(),
    )
    assert pages == 1
    assert complete is False     # max_pages => partial => never mark_inactive


def test_mark_inactive_source_scoped(monkeypatch):
    monkeypatch.setattr(
        idnes_main.db, "index_summary_native",
        lambda _c, _s, ids: {n: {"sreality_id": -i, "price_czk": 1, "last_seen_at": None}
                             for i, n in enumerate(ids, 1)},
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        idnes_main.db, "mark_inactive",
        lambda _c, cm, ct, pks, source, min_unseen_hours: (captured.update(
            cm=cm, ct=ct, pks=set(pks), source=source,
            min_unseen_hours=min_unseen_hours) or 7),
    )
    n = _portal().mark_inactive(object(), {"sale_type": "prodej", "category": "byty"}, {"x", "y"})
    assert n == 7
    assert captured["cm"] == "byt" and captured["ct"] == "prodej"
    assert captured["source"] == "idnes"
    assert captured["min_unseen_hours"] == 24   # staleness rail rides on every sweep


def test_active_count_source_scoped(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        idnes_main.db, "active_count",
        lambda _c, cm, ct, source: (captured.update(cm=cm, ct=ct, source=source) or 42),
    )
    assert _portal().active_count(object(), {"sale_type": "prodej", "category": "byty"}) == 42
    assert captured == {"cm": "byt", "ct": "prodej", "source": "idnes"}


class _DetailClient:
    def __init__(self, behavior):
        self._behavior = behavior

    def fetch_detail(self, ref):
        if self._behavior == "gone":
            raise ListingGoneError("/x", 404)
        if self._behavior == "boom":
            raise RuntimeError("network")
        return ("<html>detail</html>", 200)


def test_fetch_detail_ok_derives_category_from_url(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_parse(html, *, source_url, category_main, category_type):
        captured["cm"], captured["ct"] = category_main, category_type
        # lat/lon present -> the geocode fallback is a no-op for this test.
        return SimpleNamespace(raw={}, lat=49.2, lon=16.6, locality="Brno")

    monkeypatch.setattr(idnes_main, "parse_detail", fake_parse)
    ref = "https://reality.idnes.cz/detail/pronajem/dum/brno/6a18deadbeefdeadbeef0009/"
    item = _portal().fetch_detail(_DetailClient("ok"), "6a18deadbeefdeadbeef0009", ref)
    assert item.kind == "ok"
    assert (captured["cm"], captured["ct"]) == ("dum", "pronajem")  # derived from URL


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


def test_mark_gone_flips_listing_inactive(monkeypatch):
    monkeypatch.setattr(
        idnes_main.db, "index_summary_native",
        lambda _c, _s, ids: {"a": {"sreality_id": -5, "price_czk": 1, "last_seen_at": None}},
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        idnes_main.db, "mark_listing_inactive",
        lambda _c, pk: captured.update(pk=pk),
    )
    _portal().mark_gone(object(), "a")
    assert captured["pk"] == -5
