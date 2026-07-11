"""maxima_main on the portal framework: MaximaPortal (complete-walk via agenda-
grain delisting, two mixed agendas split per (category_main, category_type) via
id-prefix) seams + the main() that drives index-walk then detail-drain through
the shared runner, recording an 'index' + a 'detail' scrape_runs row tagged
source='maxima'.
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


_CATEGORIES = [
    {"category_main": "byt",      "category_type": "prodej",   "af": 1},
    {"category_main": "dum",      "category_type": "prodej",   "af": 1},
    {"category_main": "ostatni",  "category_type": "prodej",   "af": 1},
    {"category_main": "byt",      "category_type": "pronajem", "af": 2},
]


def _config() -> PortalConfig:
    return PortalConfig(
        source="maxima",
        supports_complete_walk=True,
        categories=_CATEGORIES,
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
        lambda portal, dry_run, **kw: (0, {"index_pages": 16, "listings_found_new": 9}),
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
        maxima_main.portal_runner, "run_index_walk", lambda portal, dry_run, **kw: (0, {}))
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
        maxima_main.portal_runner, "run_index_walk", lambda portal, dry_run, **kw: (0, {}))
    monkeypatch.setattr(
        maxima_main.portal_runner, "run_detail_drain", lambda portal, dry_run, **kw: (0, {}))
    assert maxima_main.main(["--dry-run"]) == 0
    assert starts["n"] == 0


# --- MaximaPortal seams -----------------------------------------------------


def test_portal_config_categories_and_labels():
    p = _portal()
    assert p.source == "maxima"
    assert p.supports_complete_walk is True
    assert p.categories() == _CATEGORIES
    assert p.category_labels(_CATEGORIES[0]) == ("byt", "prodej")
    assert p.category_labels(_CATEGORIES[3]) == ("byt", "pronajem")


def test_active_count_source_scoped(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        maxima_main.db, "active_count",
        lambda _c, cm, ct, source: captured.update(active=(cm, ct, source)) or 12,
    )
    assert _portal().active_count(object(), _CATEGORIES[0]) == 12
    assert captured["active"] == ("byt", "prodej", "maxima")


def _walk_one_agenda(monkeypatch, portal, *, total, items, max_pages=None):
    """Drive one agenda walk (af=1) so the portal's agenda cache is populated."""
    page1 = SimpleNamespace(total=total, next_offset=None, items=items)
    empty = SimpleNamespace(total=total, next_offset=None, items=[])
    seq = iter([page1, empty, empty, empty])
    monkeypatch.setattr(maxima_main, "parse_index", lambda _h: next(seq))
    monkeypatch.setattr(maxima_main, "MaximaClient", _IdxClient)
    _IdxClient.fetches = {}
    monkeypatch.setattr(maxima_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(maxima_main.db, "enqueue_detail", lambda *a, **k: 0)
    monkeypatch.setattr(maxima_main.db, "touch_listings", lambda *a, **k: None)
    portal.walk_category(_CATEGORIES[0], object(), False, _Limiter())  # byt·prodej, af=1


def test_mark_inactive_is_agenda_grain(monkeypatch):
    # A complete sale agenda spanning byt + dum + ostatni. mark_inactive must
    # sweep the WHOLE agenda (category_type=prodej) against EVERY agenda id —
    # never the per-category byt slice — and only once per agenda per run.
    base = "https://nemovitosti.maxima.cz/nemovitosti/"
    items = [
        SimpleNamespace(source_id_native=n, detail_path=f"{base}{n}/",
                        price_text="5 000 000 Kč", title=t)
        for n, t in [("b1", "Prodej bytu"), ("b2", "Prodej bytu"),
                     ("d1", "Prodej domu"), ("o1", "Prodej")]
    ]
    portal = _portal()
    _walk_one_agenda(monkeypatch, portal, total=4, items=items)

    captured: list[Any] = []
    monkeypatch.setattr(
        maxima_main.db, "mark_inactive_agenda",
        lambda _c, source, ct, seen, *, min_unseen_hours: (
            captured.append((source, ct, set(seen), min_unseen_hours)) or 7
        ),
    )
    # First prodej descriptor (byt) triggers the agenda sweep.
    assert portal.mark_inactive(object(), _CATEGORIES[0], {"b1", "b2"}) == 7
    source, ct, seen, hrs = captured[0]
    assert source == "maxima" and ct == "prodej" and hrs == 12
    assert seen == {"b1", "b2", "d1", "o1"}        # the FULL agenda, not the byt slice
    # A second prodej descriptor (dum) must NOT re-sweep the same agenda.
    assert portal.mark_inactive(object(), _CATEGORIES[1], {"d1"}) == 0
    assert len(captured) == 1


def test_mark_inactive_skips_incomplete_agenda(monkeypatch):
    # Agenda reports total=10 but only 2 collected -> walk.complete is False, so
    # no index-absence delisting (avoids false-flipping the unseen 8).
    base = "https://nemovitosti.maxima.cz/nemovitosti/"
    items = [
        SimpleNamespace(source_id_native=n, detail_path=f"{base}{n}/",
                        price_text="5 000 000 Kč", title="Prodej bytu")
        for n in ("b1", "b2")
    ]
    portal = _portal()
    _walk_one_agenda(monkeypatch, portal, total=10, items=items)
    called = {"n": 0}
    monkeypatch.setattr(
        maxima_main.db, "mark_inactive_agenda",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or 0,
    )
    assert portal.mark_inactive(object(), _CATEGORIES[0], {"b1", "b2"}) == 0
    assert called["n"] == 0


class _IdxClient:
    """A fake MaximaClient that hands back a per-agenda page sequence and records
    how many times the agenda was actually fetched (to prove the cache works)."""

    pages_by_af: dict[int, list[Any]] = {}
    fetches: dict[int, int] = {}

    def __init__(self, *a, **k):
        self._cursor: dict[int, int] = {}

    def fetch_index(self, page=None, *, af=None):
        af = af or 1
        _IdxClient.fetches[af] = _IdxClient.fetches.get(af, 0) + 1
        return ("<html>", 200)


def test_walk_category_filters_by_category_and_caches_agenda(monkeypatch):
    # One sale-agenda page with mixed categories: 2 byty, 1 dum, then an empty page.
    base = "https://nemovitosti.maxima.cz/nemovitosti/"
    b1, b2, d1 = "b50000001", "b50000002", "d40000003"  # b1 new, b2 changed, d1 dum
    page1 = SimpleNamespace(total=3, next_offset=2, items=[
        SimpleNamespace(source_id_native=b1, detail_path=f"{base}{b1}/", price_text="5 000 000 Kč", title="Prodej bytu 2+kk"),
        SimpleNamespace(source_id_native=b2, detail_path=f"{base}{b2}/", price_text="6 000 000 Kč", title="Prodej bytu 3+kk"),
        SimpleNamespace(source_id_native=d1, detail_path=f"{base}{d1}/", price_text="9 000 000 Kč", title="Prodej rodinného domu"),
    ])
    empty = SimpleNamespace(total=3, next_offset=None, items=[])
    seq = iter([page1, empty])
    monkeypatch.setattr(maxima_main, "parse_index", lambda _h: next(seq))
    _IdxClient.fetches = {}
    monkeypatch.setattr(maxima_main, "MaximaClient", _IdxClient)
    monkeypatch.setattr(maxima_main.db, "upsert_portal_raw_page", lambda *a, **k: 1)
    monkeypatch.setattr(
        maxima_main.db, "index_summary_native",
        lambda _c, _s, ids: {b2: {"sreality_id": -2, "price_czk": 5_500_000}} if b2 in ids else {},
    )
    monkeypatch.setattr(maxima_main.db, "touch_listings", lambda *a, **k: None)
    enq: list[Any] = []
    monkeypatch.setattr(
        maxima_main.db, "enqueue_detail",
        lambda _c, source, entries: (enq.extend(entries) or len(entries)),
    )

    portal = _portal()
    # byt·prodej: only the two byty, and it triggers the (only) HTTP walk.
    seen_b, counts_b, total_b, pages_b, complete_b = portal.walk_category(
        _CATEGORIES[0], object(), False, _Limiter(),
    )
    assert seen_b == {b1, b2}
    # complete reflects the AGENDA (3 of 3 collected), not the byt slice (2).
    assert total_b == 2 and complete_b is True
    assert pages_b == 2                       # page 1 + the empty terminator
    assert _IdxClient.fetches[1] == 2

    # dum·prodej: reuses the cached agenda (no new fetch), yields just the dum.
    seen_d, _c, total_d, pages_d, _comp = portal.walk_category(
        _CATEGORIES[1], object(), False, _Limiter(),
    )
    assert seen_d == {d1}
    assert total_d == 1
    assert pages_d == 0                       # cache hit -> no pages counted again
    assert _IdxClient.fetches[1] == 2         # still only the original 2 fetches

    enq_ids = {e[0]: e for e in enq}
    assert enq_ids[b1][3] == maxima_main.db.QUEUE_PRIORITY_NEW       # new
    assert enq_ids[b2][3] == maxima_main.db.QUEUE_PRIORITY_CHANGED   # price changed
    assert enq_ids[d1][3] == maxima_main.db.QUEUE_PRIORITY_NEW
    assert enq_ids[b1][1] == f"{base}{b1}/"  # detail_ref is the absolute URL


def test_walk_category_walks_rent_agenda(monkeypatch):
    # Rent ids carry prefixes the sale taxonomy doesn't cover (real maxima: 'a'),
    # so category MUST come from the title ("Pronájem bytu") -> byt, not the prefix.
    base = "https://nemovitosti.maxima.cz/nemovitosti/"
    rent = "a10009999"
    page1 = SimpleNamespace(total=1, next_offset=None, items=[
        SimpleNamespace(source_id_native=rent, detail_path=f"{base}{rent}/", price_text="19 000 Kč", title="Pronájem bytu 1 + kk"),
    ])
    empty = SimpleNamespace(total=1, next_offset=None, items=[])
    seq = iter([page1, empty])
    afs: list[int] = []

    class _RentClient(_IdxClient):
        def fetch_index(self, page=None, *, af=None):
            afs.append(af)
            return ("<html>", 200)

    monkeypatch.setattr(maxima_main, "parse_index", lambda _h: next(seq))
    monkeypatch.setattr(maxima_main, "MaximaClient", _RentClient)
    monkeypatch.setattr(maxima_main.db, "upsert_portal_raw_page", lambda *a, **k: 1)
    monkeypatch.setattr(maxima_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(maxima_main.db, "enqueue_detail", lambda *a, **k: 1)

    seen, _c, total, _p, _comp = _portal().walk_category(
        _CATEGORIES[3], object(), False, _Limiter(),  # byt·pronajem, af=2
    )
    assert seen == {rent}
    assert afs and all(af == 2 for af in afs)   # the rent agenda was walked with af=2


# --- detail-drain seams -----------------------------------------------------


class _DetailClient:
    def fetch_detail(self, ref):
        return ("<html>detail</html>", 200)


def test_fetch_detail_ok_derives_category(monkeypatch):
    captured: dict[str, Any] = {}

    def _fake_parse(html, *, source_url):
        captured["url"] = source_url
        # lat/lon present like every real ScrapedListing — the drain routes
        # parsed listings through CoordResolver.fill (page coords short-circuit).
        return SimpleNamespace(raw={"image_urls": ["a.jpg"]}, lat=50.0, lon=14.0)

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
