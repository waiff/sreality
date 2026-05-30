"""maxima_main on the portal framework: MaximaPortal (pilot, single mixed walk)
seams + the main() that drives index-walk then detail-drain through the shared
runner, recording an 'index' + a 'detail' scrape_runs row tagged source='maxima'.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from scraper import maxima_main
from scraper.maxima_main import MaximaPortal
from scraper.portal import PortalConfig


class _Conn:
    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def close(self) -> None:
        pass


def _config() -> PortalConfig:
    return PortalConfig(
        source="maxima",
        supports_complete_walk=False,
        categories=[{"label": "all"}],
        split_threshold=None,
    )


def _portal(**kw: Any) -> MaximaPortal:
    return MaximaPortal(_config(), **kw)


class _Limiter:
    def acquire(self) -> None:
        pass

    def penalize(self) -> None:
        pass


# --- main(): two-phase run recording ---------------------------------------


def test_main_records_index_and_detail_runs(monkeypatch):
    starts: list[tuple] = []
    finals: list[tuple] = []
    monkeypatch.setattr(maxima_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(maxima_main.db, "connect", lambda: _Conn())
    monkeypatch.setattr(
        maxima_main.db, "scrape_run_start",
        lambda _c, run_type, source: (starts.append((run_type, source)) or len(starts)),
    )
    monkeypatch.setattr(
        maxima_main.db, "scrape_run_finalize",
        lambda _c, run_id, **kw: finals.append((run_id, kw)),
    )
    monkeypatch.setattr(
        maxima_main.portal_runner, "run_index_walk",
        lambda portal, dry_run: (0, {"index_pages": 16, "listings_found_new": 9}),
    )
    monkeypatch.setattr(
        maxima_main.portal_runner, "run_detail_drain",
        lambda portal, dry_run, **kw: (0, {"listings_scraped_new": 9}),
    )

    rc = maxima_main.main([])
    assert rc == 0
    assert starts == [("index", "maxima"), ("detail", "maxima")]
    assert [kw["index_pages"] for _id, kw in finals] == [16, 0]


def _stub_phases(monkeypatch, calls):
    monkeypatch.setattr(maxima_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(maxima_main.db, "connect", lambda: _Conn())
    monkeypatch.setattr(
        maxima_main.db, "scrape_run_start",
        lambda _c, run_type, source: (calls.append(run_type) or len(calls)),
    )
    monkeypatch.setattr(maxima_main.db, "scrape_run_finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(
        maxima_main.portal_runner, "run_index_walk", lambda portal, dry_run: (0, {}))
    monkeypatch.setattr(
        maxima_main.portal_runner, "run_detail_drain", lambda portal, dry_run, **kw: (0, {}))


def test_index_only_skips_drain(monkeypatch):
    calls: list[str] = []
    _stub_phases(monkeypatch, calls)
    assert maxima_main.main(["--index-only"]) == 0
    assert calls == ["index"]


def test_drain_only_skips_index(monkeypatch):
    calls: list[str] = []
    _stub_phases(monkeypatch, calls)
    assert maxima_main.main(["--drain-only"]) == 0
    assert calls == ["detail"]


def test_dry_run_records_no_scrape_run(monkeypatch):
    starts = {"n": 0}
    monkeypatch.setattr(maxima_main, "_load_config", lambda dry_run: _config())
    monkeypatch.setattr(
        maxima_main.db, "scrape_run_start",
        lambda *_a, **_k: starts.__setitem__("n", starts["n"] + 1) or 1,
    )
    monkeypatch.setattr(maxima_main.db, "scrape_run_finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(
        maxima_main.portal_runner, "run_index_walk", lambda portal, dry_run: (0, {}))
    monkeypatch.setattr(
        maxima_main.portal_runner, "run_detail_drain", lambda portal, dry_run, **kw: (0, {}))
    assert maxima_main.main(["--dry-run"]) == 0
    assert starts["n"] == 0


# --- MaximaPortal seams -----------------------------------------------------


def test_portal_config_pilot_single_category():
    p = _portal()
    assert p.source == "maxima"
    assert p.supports_complete_walk is False
    assert p.categories() == [{"label": "all"}]
    # One mixed walk -> no single (cm, ct) label; active_count + mark_inactive are off.
    assert p.category_labels({"label": "all"}) == (None, None)
    assert p.active_count(object(), {"label": "all"}) is None
    assert p.mark_inactive(object(), {"label": "all"}, {"b1", "d2"}) == 0


class _IdxClient:
    def __init__(self, *a, **k):
        self.calls = 0

    def fetch_index(self, *a, **k):
        self.calls += 1
        return ("<html>", 200)


def test_walk_category_classifies_and_stops_on_empty_page(monkeypatch):
    a = "b50000001"  # new
    b = "d40000002"  # price changed
    c = "g70000003"  # unchanged
    base = "https://nemovitosti.maxima.cz/nemovitosti/"
    page1 = SimpleNamespace(
        total=3, next_offset=2,
        items=[
            SimpleNamespace(source_id_native=a, detail_path=f"{base}{a}/", price_text="5 000 000 Kč"),
            SimpleNamespace(source_id_native=b, detail_path=f"{base}{b}/", price_text="6 000 000 Kč"),
            SimpleNamespace(source_id_native=c, detail_path=f"{base}{c}/", price_text="7 000 000 Kč"),
        ],
    )
    empty = SimpleNamespace(total=3, next_offset=None, items=[])
    pages_iter = iter([page1, empty])
    monkeypatch.setattr(maxima_main, "parse_index", lambda _h: next(pages_iter))
    monkeypatch.setattr(maxima_main, "MaximaClient", _IdxClient)
    monkeypatch.setattr(maxima_main.db, "upsert_portal_raw_page", lambda *a, **k: 1)
    monkeypatch.setattr(
        maxima_main.db, "index_summary_native",
        lambda _c, _s, ids: {
            b: {"sreality_id": -2, "price_czk": 5_500_000, "last_seen_at": None},  # differs
            c: {"sreality_id": -3, "price_czk": 7_000_000, "last_seen_at": None},  # same
        },
    )
    touched: dict[str, Any] = {}
    monkeypatch.setattr(maxima_main.db, "touch_listings", lambda _c, pks: touched.update(pks=list(pks)))
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        maxima_main.db, "enqueue_detail",
        lambda _c, source, entries: (captured.update(source=source, entries=list(entries))
                                     or len(captured["entries"])),
    )
    seen, counts, total, pages, complete = _portal().walk_category(
        {"label": "all"}, object(), False, _Limiter(),
    )
    assert seen == {a, b, c}
    assert total == 3
    assert pages == 2                     # walked page 1 then the empty page 2
    assert complete is False              # pilot: never marks inactive
    assert touched["pks"] == [-3]         # unchanged listing touched
    refs = {e[0]: e for e in captured["entries"]}
    assert refs[a][3] == maxima_main.db.QUEUE_PRIORITY_NEW
    assert refs[b][3] == maxima_main.db.QUEUE_PRIORITY_CHANGED
    assert refs[a][1] == f"{base}{a}/"    # detail_ref is the absolute URL
    assert c not in refs                  # unchanged is not enqueued


def test_walk_category_max_pages_caps(monkeypatch):
    page = SimpleNamespace(
        total=220, next_offset=2,
        items=[SimpleNamespace(
            source_id_native="b50000001",
            detail_path="https://nemovitosti.maxima.cz/nemovitosti/b50000001/",
            price_text="5 000 000 Kč")],
    )
    monkeypatch.setattr(maxima_main, "parse_index", lambda _h: page)
    monkeypatch.setattr(maxima_main, "MaximaClient", _IdxClient)
    monkeypatch.setattr(maxima_main.db, "upsert_portal_raw_page", lambda *a, **k: 1)
    monkeypatch.setattr(maxima_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(maxima_main.db, "enqueue_detail", lambda *a, **k: 1)
    _, _, total, pages, complete = _portal(max_pages=1).walk_category(
        {"label": "all"}, object(), False, _Limiter(),
    )
    assert pages == 1
    assert complete is False


# --- detail-drain seams -----------------------------------------------------


class _DetailClient:
    def fetch_detail(self, ref):
        return ("<html>detail</html>", 200)


def test_fetch_detail_ok_derives_category(monkeypatch):
    captured: dict[str, Any] = {}

    def _fake_parse(html, *, source_url):
        captured["url"] = source_url
        return SimpleNamespace(raw={"image_urls": ["a.jpg"]})

    monkeypatch.setattr(maxima_main, "parse_detail", _fake_parse)
    item = _portal().fetch_detail(
        _DetailClient(), "b50000001", "/nemovitosti/b50000001/",
    )
    assert item.kind == "ok"
    assert item.native_id == "b50000001"
    assert captured["url"] == "https://nemovitosti.maxima.cz/nemovitosti/b50000001/"


def test_fetch_detail_gone(monkeypatch):
    from scraper.portal_base import ListingGoneError

    class _GoneClient:
        def fetch_detail(self, ref):
            raise ListingGoneError(ref, 404)

    item = _portal().fetch_detail(_GoneClient(), "b50000001", None)
    assert item.kind == "gone"


def test_mark_gone_flips_native(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        maxima_main.db, "mark_listing_inactive_native",
        lambda _c, source, nid: captured.update(source=source, nid=nid),
    )
    _portal().mark_gone(object(), "b50000001")
    assert captured == {"source": "maxima", "nid": "b50000001"}
