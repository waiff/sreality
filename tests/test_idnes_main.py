"""idnes_main on the portal framework: IdnesPortal (complete-walk) seams + the
main() that drives index-walk then detail-drain through the shared runner,
recording an 'index' + a 'detail' scrape_runs row tagged source='idnes'.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from scraper import idnes_main
from scraper import portal as portal_mod
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
            b: {"id": 8102, "sreality_id": -2, "price_czk": 5_500_000, "last_seen_at": None},  # differs -> changed
            c: {"id": 8103, "sreality_id": -3, "price_czk": 7_000_000, "last_seen_at": None},  # same -> unchanged
        },
    )
    touched: dict[str, Any] = {}
    monkeypatch.setattr(idnes_main.db, "touch_listings_by_id", lambda _c, pks: touched.update(pks=list(pks)))
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
    assert touched["pks"] == [8103]              # unchanged listing touched by surrogate id
    refs = {e[0]: e for e in captured["entries"]}
    assert refs[a][3] == idnes_main.db.QUEUE_PRIORITY_NEW      # new
    assert refs[b][3] == idnes_main.db.QUEUE_PRIORITY_CHANGED  # changed
    assert refs[a][1] == f"{base}{a}/"           # detail_ref is the absolute URL
    assert c not in refs                          # unchanged is not enqueued


def test_walk_complete_requires_near_full_walk():
    # mark_inactive only after a ~complete walk (architectural rule #3); the bar
    # is hardcoded (portal.INDEX_MIN_COMPLETENESS=0.995, tolerating mid-walk
    # churn), not operator-tunable — a genuinely truncated walk reads incomplete.
    assert idnes_main.walk_is_complete(100, 100) is True
    assert idnes_main.walk_is_complete(996, 1000) is True   # 0.4% deficit = churn
    assert idnes_main.walk_is_complete(994, 1000) is False  # 0.6% deficit = truncated
    assert idnes_main.walk_is_complete(99, 100) is False
    assert idnes_main.walk_is_complete(90, 100) is False
    # An unmeasurable total is "unknown", never "complete". The old local
    # _walk_complete returned True here and this test asserted it — that
    # expectation was the DEFECT, not the spec: it let a walk that measured
    # nothing authorise mark_inactive to delist everything it never reached.
    assert idnes_main.walk_is_complete(0, None) is False
    assert idnes_main.walk_is_complete(500, 1000, stopped_early=True) is False
    # Over-collection means the denominator is wrong (overlapping slices or
    # foreign stock), so contamination must not read as completeness either.
    assert idnes_main.walk_is_complete(1030, 1000) is False


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


def test_walk_category_deadline_stops_walk_and_suppresses_complete(monkeypatch):
    # A walk cut short must read incomplete, or mark_inactive would delist the
    # slices it never fetched (rule #3). Under the sliced walk the cut happens
    # between slices as well as between pages, and BOTH must suppress complete:
    # 14 of 15 slices walked is a walk with a hole in it, and a hole is exactly
    # what the sweep would read as "these listings are gone".
    def _page(_html):
        nid = "6a18deadbeefdeadbeef0001"
        return SimpleNamespace(
            total=1, next_offset=None,
            items=[SimpleNamespace(
                source_id_native=nid,
                detail_path=f"https://reality.idnes.cz/detail/prodej/byt/x/{nid}/",
                price_text="5 000 000 Kč")],
        )

    monkeypatch.setattr(idnes_main, "parse_index", _page)
    monkeypatch.setattr(idnes_main, "IdnesClient", _IdxClient)
    monkeypatch.setattr(idnes_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(idnes_main.db, "enqueue_detail", lambda *a, **k: 1)
    monkeypatch.setattr(idnes_main.db, "record_index_slice", lambda *a, **k: None)
    # The budget expires after a handful of deadline checks, mid-slice-list.
    checks = {"n": 0}

    def _clock() -> float:
        checks["n"] += 1
        return 10.0 if checks["n"] < 6 else 999.0

    monkeypatch.setattr(portal_mod, "time", SimpleNamespace(monotonic=_clock))

    seen, _counts, national, pages, complete = _portal().walk_category(
        {"sale_type": "prodej", "category": "byty"}, _Conn(), False, _Limiter(),
        50.0,
    )
    assert complete is False        # the whole point
    assert seen                     # what it did reach is still enqueued
    assert pages >= 1
    assert national == 1


def test_walk_category_walks_all_fifteen_slices(monkeypatch):
    """14 kraje + the abroad bucket. Abroad is not a nicety: on idnes the kraj
    slices sum to 15,319 of 27,372 flats for sale, so a slice set built from the
    region nav alone would report 56% of the portal as 100% of it."""
    asked: list[tuple[Any, bool]] = []

    class _Spy(_IdxClient):
        def fetch_index(self, sale_type, category, page=None, *, locality=None,
                        abroad=False):
            asked.append((locality, abroad))
            return ("<html>", 200)

    nid = "6a18deadbeefdeadbeef0001"
    monkeypatch.setattr(idnes_main, "parse_index", lambda _h: SimpleNamespace(
        total=1, next_offset=None,
        items=[SimpleNamespace(
            source_id_native=nid,
            detail_path=f"https://reality.idnes.cz/detail/prodej/byt/x/{nid}/",
            price_text="5 000 000 Kč")]))
    monkeypatch.setattr(idnes_main, "IdnesClient", _Spy)
    monkeypatch.setattr(idnes_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(idnes_main.db, "enqueue_detail", lambda *a, **k: 1)
    recorded: list[str] = []
    monkeypatch.setattr(idnes_main.db, "record_index_slice",
                        lambda *a, **k: recorded.append(k["slice_key"]))

    _portal().walk_category(
        {"sale_type": "prodej", "category": "byty"}, _Conn(), False, _Limiter(), None)

    localities = [loc for loc, ab in asked if loc is not None]
    assert set(localities) == set(portal_mod.CZ_KRAJ_SLUGS)
    assert sum(1 for _loc, ab in asked if ab) == 1        # exactly one abroad slice
    assert set(recorded) == set(portal_mod.CZ_KRAJ_SLUGS) | {portal_mod.ABROAD_SLICE}
    # The first call is the national cross-check: neither a kraj nor abroad.
    assert asked[0] == (None, False)


def test_one_unfinished_slice_forces_the_whole_category_incomplete(monkeypatch):
    """Fourteen good slices and one that failed is not 93% coverage for the
    purposes of delisting — it is a walk with a hole in it."""
    class _Flaky(_IdxClient):
        def fetch_index(self, sale_type, category, page=None, *, locality=None,
                        abroad=False):
            if locality == "zlinsky-kraj":
                raise RuntimeError("connection reset")
            return ("<html>", 200)

    nid = "6a18deadbeefdeadbeef0001"
    monkeypatch.setattr(idnes_main, "parse_index", lambda _h: SimpleNamespace(
        total=1, next_offset=None,
        items=[SimpleNamespace(
            source_id_native=nid,
            detail_path=f"https://reality.idnes.cz/detail/prodej/byt/x/{nid}/",
            price_text="5 000 000 Kč")]))
    monkeypatch.setattr(idnes_main, "IdnesClient", _Flaky)
    monkeypatch.setattr(idnes_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(idnes_main.db, "enqueue_detail", lambda *a, **k: 1)
    outcomes: dict[str, str] = {}
    monkeypatch.setattr(idnes_main.db, "record_index_slice",
                        lambda *a, **k: outcomes.__setitem__(k["slice_key"], k["outcome"]))

    _seen, _c, _n, _p, complete = _portal().walk_category(
        {"sale_type": "prodej", "category": "byty"}, _Conn(), False, _Limiter(), None)
    assert complete is False
    assert outcomes["zlinsky-kraj"] == "error"
    assert outcomes["praha"] == "exhausted"


def test_a_never_walked_slice_goes_first(monkeypatch):
    """The starvation guard. A slice with no ledger row must sort as infinitely
    stale, not as fresh — treating unknown as 0 hours would put exactly the
    never-walked slices LAST, which is the behaviour the ledger exists to end."""
    portal = _portal()
    portal._staleness = {
        ("byt", "prodej", k): 1.0 for k in portal.SLICES if k != "zlinsky-kraj"
    }
    portal._staleness[("byt", "prodej", "praha")] = 99.0
    order = portal._slice_order("byt", "prodej")
    assert order[0] == "zlinsky-kraj"   # never walked
    assert order[1] == "praha"          # then the stalest known


def test_the_stalest_category_is_walked_first(monkeypatch):
    """Slice ordering alone is not enough: the runner walks categories in order,
    so a category that eats the whole budget every run starves the rest however
    its own slices are sorted. That is what left 8 of 10 idnes categories never
    walked at all."""
    cfg = PortalConfig(
        source="idnes", supports_complete_walk=True,
        categories=[{"sale_type": "prodej", "category": "byty"},
                    {"sale_type": "pronajem", "category": "domy"}],
        split_threshold=None,
    )
    portal = IdnesPortal(cfg)
    # byty/prodej fully walked an hour ago; domy/pronajem missing one slice.
    portal._staleness = {("byt", "prodej", k): 1.0 for k in portal.SLICES}
    portal._staleness.update(
        {("dum", "pronajem", k): 0.5 for k in portal.SLICES if k != "praha"})
    assert portal.categories()[0]["sale_type"] == "pronajem"


def test_the_page_capped_probe_keeps_the_flat_national_walk(monkeypatch):
    """The realtime probe reads the newest-first head of the NATIONAL list.
    Slicing would scatter that head across 15 requests and defeat it — and being
    partial, it must never read as complete."""
    asked: list[Any] = []

    class _Spy(_IdxClient):
        def fetch_index(self, sale_type, category, page=None, *, locality=None,
                        abroad=False):
            asked.append((locality, abroad))
            return ("<html>", 200)

    nid = "6a18deadbeefdeadbeef0001"
    monkeypatch.setattr(idnes_main, "parse_index", lambda _h: SimpleNamespace(
        total=9999, next_offset=None,
        items=[SimpleNamespace(
            source_id_native=nid,
            detail_path=f"https://reality.idnes.cz/detail/prodej/byt/x/{nid}/",
            price_text="5 000 000 Kč")]))
    monkeypatch.setattr(idnes_main, "IdnesClient", _Spy)
    monkeypatch.setattr(idnes_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(idnes_main.db, "enqueue_detail", lambda *a, **k: 1)
    monkeypatch.setattr(idnes_main.db, "record_index_slice",
                        lambda *a, **k: pytest.fail("a probe must not write the ledger"))

    portal = _portal()
    portal.set_index_page_cap(2)
    _seen, _c, _n, _p, complete = portal.walk_category(
        {"sale_type": "prodej", "category": "byty"}, _Conn(), False, _Limiter(), None)
    assert asked == [(None, False)]     # one flat national request, no slicing
    assert complete is False


def test_mark_inactive_source_scoped(monkeypatch):
    # Listing-identity Gate 2: non-sreality rows carry sreality_id = NULL, and
    # ONE NULL inside `<> ALL(...)` makes the predicate NULL for every row —
    # idnes's 108k-listing delisting sweep would become a permanent no-op
    # (rule #3). The sweep must key on the native id the index walked.
    monkeypatch.setattr(
        idnes_main.db, "mark_inactive",
        lambda *a, **k: pytest.fail("legacy sreality_id-keyed sweep must not be used"),
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        idnes_main.db, "mark_inactive_native",
        lambda _c, source, cm, ct, natives, min_unseen_hours: (captured.update(
            cm=cm, ct=ct, natives=set(natives), source=source,
            min_unseen_hours=min_unseen_hours) or 7),
    )
    n = _portal().mark_inactive(object(), {"sale_type": "prodej", "category": "byty"}, {"x", "y"})
    assert n == 7
    assert captured["cm"] == "byt" and captured["ct"] == "prodej"
    assert captured["source"] == "idnes"
    assert captured["natives"] == {"x", "y"}    # raw walked ids, no PK round-trip
    assert captured["min_unseen_hours"] == 12   # staleness rail rides on every sweep


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
    monkeypatch.setattr(
        idnes_main.db, "ingest_scraped_listing",
        lambda _c, _l, discovery_seq=None, discovered_at=None: (8105, "new"))
    monkeypatch.setattr(idnes_main.db, "record_images", lambda _c, _sid, imgs, **k: len(imgs))
    monkeypatch.setattr(idnes_main.db, "mark_portal_page_parsed", lambda *a, **k: None)
    counts = _portal().write_details(object(), items)
    assert counts["new"] == 1
    assert counts["images_discovered"] == 2


def test_mark_gone_flips_listing_inactive(monkeypatch):
    # Gate 2: the gone-flip keys on the native id (mark_listing_inactive_native),
    # NOT a sreality_id resolved back out of the DB — a post-Gate-2 idnes row has
    # sreality_id = NULL, so the legacy sreality_id-keyed flip would silently no-op.
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        idnes_main.db, "mark_listing_inactive_native",
        lambda _c, source, nid: captured.update(source=source, nid=nid),
    )
    monkeypatch.setattr(
        idnes_main.db, "mark_listing_inactive",
        lambda *a, **k: pytest.fail("legacy sreality_id-keyed gone-flip must not be used"),
    )
    _portal().mark_gone(object(), "a")
    assert captured == {"source": "idnes", "nid": "a"}


# --- the empty slice ---------------------------------------------------------
#
# A slice with nothing in it publishes no count, so `total` is None — which is
# byte-for-byte what a degraded or throttled page returns. Conflating the two is
# how a broken fetch gets read as "this region is empty" and drives a delisting
# sweep. idnes states emptiness out loud, so a confirmed zero is a REAL
# measurement and an empty slice IS complete; an unconfirmed one is not.


def _one_page(*, total, items, empty_confirmed=False):
    return SimpleNamespace(total=total, next_offset=None, items=items,
                           empty_confirmed=empty_confirmed)


def _walk_with(monkeypatch, page_for, national_total=0):
    """The FIRST parse is the national cross-check, not a slice: it supplies the
    denominator the slice union has to satisfy. Give it a real total, or the
    category fails closed for want of a denominator no matter how the slices
    went — which is correct behaviour, and would mask what these tests assert."""
    calls = {"n": 0}

    def _parse(_h):
        calls["n"] += 1
        if calls["n"] == 1:
            return _one_page(total=national_total, items=[])
        return page_for()

    monkeypatch.setattr(idnes_main, "parse_index", _parse)
    monkeypatch.setattr(idnes_main, "IdnesClient", _IdxClient)
    monkeypatch.setattr(idnes_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(idnes_main.db, "enqueue_detail", lambda *a, **k: 0)
    outcomes: dict[str, str] = {}
    monkeypatch.setattr(idnes_main.db, "record_index_slice",
                        lambda *a, **k: outcomes.__setitem__(k["slice_key"], k["outcome"]))
    result = _portal().walk_category(
        {"sale_type": "prodej", "category": "byty"}, _Conn(), False, _Limiter(), None)
    return outcomes, result


def test_a_confirmed_empty_slice_counts_as_finished(monkeypatch):
    """Verified live: idnes garages-for-rent has zero abroad listings, and the
    page says so. Without this the category could never read complete, because
    one legitimately empty slice would hold it open forever."""
    outcomes, (_seen, _c, national, _p, complete) = _walk_with(
        monkeypatch, lambda: _one_page(total=None, items=[], empty_confirmed=True))
    assert set(outcomes.values()) == {"exhausted"}
    assert complete is True
    assert national == 0


def test_an_UNconfirmed_empty_page_is_missing_evidence(monkeypatch):
    """The same shape without the site's own confirmation is a degraded page —
    a throttle, a WAF interstitial, a parser drift — and it must never read as
    'this region has nothing in it'."""
    outcomes, (_seen, _c, _n, _p, complete) = _walk_with(
        monkeypatch, lambda: _one_page(total=None, items=[], empty_confirmed=False))
    assert set(outcomes.values()) == {"degraded"}
    assert complete is False


def test_the_empty_marker_cannot_override_a_page_that_has_results(monkeypatch):
    """Belt and braces: a confirmed-empty flag on a page that actually carries a
    count must not short-circuit the walk at page one."""
    nid = "6a18deadbeefdeadbeef0001"
    item = SimpleNamespace(
        source_id_native=nid,
        detail_path=f"https://reality.idnes.cz/detail/prodej/byt/x/{nid}/",
        price_text="5 000 000 Kč")
    outcomes, (seen, _c, _n, _p, complete) = _walk_with(
        monkeypatch, lambda: _one_page(total=1, items=[item], empty_confirmed=True),
        national_total=1)
    assert seen == {nid}
    assert set(outcomes.values()) == {"exhausted"}
