"""bazos_main on the portal framework (Phase 4): BazosPortal seams + the
main() that drives index-walk then detail-drain through the shared runner,
recording an 'index' + a 'detail' scrape_runs row tagged source='bazos'.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from scraper import bazos_main
from scraper.bazos_main import BazosPortal
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
_CHATA_SALE = {"sale_type": "prodam", "category": "chata"}
_POZEMEK_SALE = {"sale_type": "prodam", "category": "pozemek"}
_ZAHRADA_SALE = {"sale_type": "prodam", "category": "zahrada"}
_OSTATNI_SALE = {"sale_type": "prodam", "category": "ostatni"}
_GARAZ_SALE = {"sale_type": "prodam", "category": "garaz"}


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
        lambda portal, dry_run, **kw: (0, {"index_pages": 3, "listings_found_new": 5,
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
        bazos_main.portal_runner, "run_index_walk", lambda portal, dry_run, **kw: (0, {})
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


def test_nomination_is_subtype_scoped(monkeypatch):
    """Rule #3: a walk that reached the portal's end nominates its unseen rows
    for a page check. bazos walks fine sections that collapse onto one category_main,
    so the nomination is scoped to the section's subtype (NULL for byt) or one
    section would nominate its siblings' rows every walk."""
    nominated: dict[str, Any] = {}
    monkeypatch.setattr(
        bazos_main.db, "presence_candidates",
        lambda _c, src, cm, ct, seen, *, subtype, scope_subtype:
            nominated.update(src=src, cm=cm, ct=ct, seen=set(seen), subtype=subtype,
                             scope_subtype=scope_subtype) or ([], 3),
    )
    assert _portal().presence_candidates(object(), _BYT_RENT, {"a", "b"}) == ([], 3, {"subtype": None})
    assert nominated == {"src": "bazos", "cm": "byt", "ct": "pronajem",
                         "seen": {"a", "b"}, "subtype": None, "scope_subtype": True}
    nominated.clear()
    _portal([_CHATA_SALE]).presence_candidates(object(), _CHATA_SALE, {"a"})
    # chata collapses onto category_main=dum but is scoped to subtype=chata, so
    # it never nominates the generic-dum (subtype NULL) section's rows.
    assert nominated["cm"] == "dum" and nominated["subtype"] == "chata"
    assert nominated["scope_subtype"] is True
    # The scope travels with the candidates so the throttle, the operator
    # override and the deferral record all name the section, not just (dum, prodej).
    assert _portal([_CHATA_SALE]).presence_candidates(object(), _CHATA_SALE, {"a"})[2] == {"subtype": "chata"}


def test_nomination_scopes_the_categories_migration_488_added(monkeypatch):
    """The two collapsed groups migration 488 opened up: pozemek+zahrada share
    category_main=pozemek and garaz+ostatni share category_main=ostatni. The
    generic member of each is subtype NULL, the other carries its own slug — so
    neither section nominates the other's rows."""
    nominated: dict[str, Any] = {}
    monkeypatch.setattr(
        bazos_main.db, "presence_candidates",
        lambda _c, src, cm, ct, seen, *, subtype, scope_subtype:
            nominated.update(cm=cm, subtype=subtype, scope_subtype=scope_subtype)
            or ([], 0),
    )
    expected = {
        _POZEMEK_SALE["category"]: ("pozemek", None),
        _ZAHRADA_SALE["category"]: ("pozemek", "zahrada"),
        _OSTATNI_SALE["category"]: ("ostatni", None),
        _GARAZ_SALE["category"]: ("ostatni", "garaz"),
    }
    for scope in (_POZEMEK_SALE, _ZAHRADA_SALE, _OSTATNI_SALE, _GARAZ_SALE):
        section = scope["category"]
        cm, sub = expected[section]
        out = _portal([scope]).presence_candidates(object(), scope, {"a"})
        assert out == ([], 0, {"subtype": sub}), section
        assert (nominated["cm"], nominated["subtype"]) == (cm, sub), section
        assert nominated["scope_subtype"] is True, section


def test_active_count_scopes_the_categories_migration_488_added(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        bazos_main.db, "active_count",
        lambda _c, cm, ct, *, source, subtype, scope_subtype: captured.update(
            cm=cm, subtype=subtype) or 7,
    )
    assert _portal().active_count(object(), _ZAHRADA_SALE) == 7
    assert (captured["cm"], captured["subtype"]) == ("pozemek", "zahrada")
    assert _portal().active_count(object(), _GARAZ_SALE) == 7
    assert (captured["cm"], captured["subtype"]) == ("ostatni", "garaz")


def test_nomination_happens_for_every_category_every_run(monkeypatch):
    # No sweep-window throttle and no staleness rail any more: every category
    # that reached the end nominates every run; the page check is the guard.
    calls: list[str] = []
    monkeypatch.setattr(
        bazos_main.db, "presence_candidates",
        lambda _c, src, cm, ct, seen, **kw: calls.append(ct) or ([], 1),
    )
    p = _portal([_BYT_SALE, _BYT_RENT])
    assert p.presence_candidates(object(), _BYT_SALE, {"a"}) == ([], 1, {"subtype": None})
    assert p.presence_candidates(object(), _BYT_RENT, {"b"}) == ([], 1, {"subtype": None})
    assert calls == ["prodej", "pronajem"]
    assert not hasattr(bazos_main.BazosPortal, "mark_inactive")


def test_active_count_source_scoped(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        bazos_main.db, "active_count",
        lambda _c, cm, ct, *, source, subtype, scope_subtype: captured.update(
            cm=cm, ct=ct, source=source, subtype=subtype,
            scope_subtype=scope_subtype) or 42,
    )
    assert _portal().active_count(object(), _BYT_RENT) == 42
    assert captured == {"cm": "byt", "ct": "pronajem", "source": "bazos",
                        "subtype": None, "scope_subtype": True}


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
            "b": {"id": 42, "sreality_id": -2, "price_czk": 4_000_000, "last_seen_at": None},
            "c": {"id": 41, "sreality_id": -3, "price_czk": 9_999_999, "last_seen_at": None},
        },
    )
    touched: dict[str, Any] = {}
    monkeypatch.setattr(
        bazos_main.db, "touch_listings_by_id",
        lambda _c, ids: touched.update(ids=sorted(ids)),
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        bazos_main.db, "enqueue_detail",
        lambda _c, source, entries: (captured.update(source=source, entries=list(entries))
                                      or len(captured["entries"])),
    )
    p = _portal()
    seen, counts, result_size, pages, reached_end = p.walk_category(
        {"sale_type": "prodam", "category": "byt"}, object(), False, _Limiter(),
    )
    assert seen == {"a", "b", "c"}
    assert result_size == 3 and reached_end is True       # items, and no next page
    assert touched["ids"] == [41, 42]                     # both existing rows touched by surrogate id
    assert counts["found_new"] == 1                       # only "a" is genuinely new
    assert captured["source"] == "bazos"
    natives = {e[0] for e in captured["entries"]}
    assert natives == {"a", "c"}                          # new + price-changed; "b" skipped
    by_native = {e[0]: e for e in captured["entries"]}
    assert by_native["a"][3] == bazos_main.db.QUEUE_PRIORITY_NEW
    assert by_native["c"][3] == bazos_main.db.QUEUE_PRIORITY_CHANGED


def test_walk_category_page_capped_is_not_an_end(monkeypatch):
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
    _seen, _counts, result_size, _pages, reached_end = p.walk_category(
        {"sale_type": "prodam", "category": "byt"}, object(), False, _Limiter(),
    )
    assert result_size == 500
    assert reached_end is False   # a page cap is a stop of OURS, never an end


def _walk_with(monkeypatch, n_items: int, total: int | None):
    page = SimpleNamespace(
        items=[SimpleNamespace(source_id_native=f"n{i}", detail_path=f"/n{i}", price_text=None)
               for i in range(n_items)],
        total=total, next_offset=None,
    )
    # A FULL page that advertises no next page is corroborated by one fetch of the
    # offset it would have pointed at (bazos runs dry there); the blank second
    # page below is that answer. See _confirm_pager_end.
    blank = SimpleNamespace(items=[], total=total, next_offset=None)
    seq = _SeqPages([page, blank])
    monkeypatch.setattr(bazos_main, "parse_index", lambda _h: seq(_h))
    monkeypatch.setattr(bazos_main, "BazosClient", lambda **k: _IdxClient([page]))
    monkeypatch.setattr(bazos_main.db, "upsert_portal_raw_page", lambda *a, **k: 1)
    monkeypatch.setattr(bazos_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(bazos_main.db, "touch_listings", lambda *a, **k: 0)
    monkeypatch.setattr(bazos_main.db, "enqueue_detail", lambda *a, **k: 1)
    return _portal().walk_category(
        {"sale_type": "prodam", "category": "byt"}, object(), False, _Limiter(),
    )


# --- the gate is STRUCTURAL (rule #3, 2026-09-08) ---------------------------
# The 5th tuple element answers "did the walk reach bazos's last page?", not
# "did the count reconcile?". The declared total jitters while a ~50 min walk
# runs, so a deficit is normal on a finished walk; the numeric verdict stays
# (walk_coverage, logged + recorded in scrape_runs.by_category) as an alarm.


def test_walk_category_one_row_short_still_reaches_end(monkeypatch):
    # 19 of a declared 20, and the pager said there is no next page: that is a
    # finished walk over a live index. Under the old 99.5% gate this scope
    # nominated nothing, forever (the ceskereality pathology).
    _seen, _counts, result_size, _pages, reached_end = _walk_with(monkeypatch, 19, 20)
    assert result_size == 20 and reached_end is True


def test_walk_category_coverage_gaps_do_not_veto(monkeypatch):
    # Three walks the numeric gate used to suppress, all ending on a page with
    # items and no next page — bazos's own "there is no more".
    *_rest, reached_end = _walk_with(monkeypatch, 994, 1000)   # 99.4% deficit
    assert reached_end is True
    *_rest, reached_end = _walk_with(monkeypatch, 20, None)    # total unreadable
    assert reached_end is True
    *_rest, reached_end = _walk_with(monkeypatch, 25, 20)      # over-collected
    assert reached_end is True


def test_walk_category_deadline_is_not_an_end(monkeypatch):
    monkeypatch.setattr(bazos_main, "deadline_reached", lambda _d: True)
    _seen, _counts, _result_size, pages, reached_end = _walk_with(monkeypatch, 5, 5)
    assert pages == 0 and reached_end is False


class _RecIdxClient:
    """Records the offset of every index fetch (so the barren re-read's offset is
    visible), and raises ListingGoneError once `ok_pages` fetches have succeeded —
    bazos 404s an offset past the last result page."""

    def __init__(self, ok_pages: int | None = None):
        self._ok = ok_pages
        self.offsets: list[int] = []

    def fetch_index(self, _sale_type, _category, offset=0, **_k):
        self.offsets.append(offset)
        if self._ok is not None and len(self.offsets) > self._ok:
            raise ListingGoneError("/past-end", 404)
        return ("<html>", 200)


class _SeqPages:
    """parse_index over a staged sequence of pages (the last one repeats), so a
    walk can be driven page by page: items, then a blank, then its re-read."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.reads = 0

    def __call__(self, _html):
        page = self._pages[min(self.reads, len(self._pages) - 1)]
        self.reads += 1
        return page


def _page(natives, *, total, next_offset):
    return SimpleNamespace(
        items=[SimpleNamespace(source_id_native=n, detail_path=f"/{n}", price_text=None)
               for n in natives],
        total=total, next_offset=next_offset,
    )


def _walk(monkeypatch, pages, *, client=None):
    client = client or _RecIdxClient()
    monkeypatch.setattr(bazos_main, "parse_index", _SeqPages(pages))
    monkeypatch.setattr(bazos_main, "BazosClient", lambda **k: client)
    monkeypatch.setattr(bazos_main.db, "upsert_portal_raw_page", lambda *a, **k: 1)
    monkeypatch.setattr(bazos_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(bazos_main.db, "touch_listings", lambda *a, **k: 0)
    monkeypatch.setattr(bazos_main.db, "enqueue_detail", lambda *a, **k: 1)
    result = _portal().walk_category(
        {"sale_type": "prodam", "category": "byt"}, object(), False, _Limiter(),
    )
    return result, client


def test_walk_category_stops_when_total_reached(monkeypatch):
    # The pager advertises a next page, but we've already collected `total`, so
    # the walk must stop (and never request the offset bazos would 404 on).
    (seen, _c, result_size, _pages, reached_end), client = _walk(
        monkeypatch, [_page(["a", "b"], total=2, next_offset=20)],
        client=_RecIdxClient(ok_pages=10),
    )
    assert seen == {"a", "b"} and result_size == 2 and reached_end is True
    assert client.offsets == [0]   # stopped after page 1; never requested offset 20


def test_walk_category_gone_page_short_of_total_is_not_an_end(monkeypatch):
    # A 404 while the declared total is still far away is indistinguishable from
    # a soft block wearing a 404: keep the 20 rows we collected, but the scope
    # must not nominate the other 380.
    (seen, _c, result_size, _pages, reached_end), _client = _walk(
        monkeypatch, [_page([str(i) for i in range(20)], total=400, next_offset=20)],
        client=_RecIdxClient(ok_pages=1),
    )
    assert len(seen) == 20            # page-1 items kept despite the 404 on page 2
    assert result_size == 400 and reached_end is False


def test_walk_category_gone_page_with_unknown_total_cannot_be_placed(monkeypatch):
    # A 404 the walk cannot PLACE proves nothing: with no readable counter there is
    # nothing to say this offset is past bazos's end rather than a soft block two
    # pages in. Keep the rows, nominate nothing. (The probe shows bazos's genuine
    # last page carries no "Další" at all, so a healthy walk ends on pager_end and
    # never needs the 404 as a terminator.)
    (seen, _c, _result_size, _pages, reached_end), _client = _walk(
        monkeypatch, [_page(["a", "b"], total=None, next_offset=20)],
        client=_RecIdxClient(ok_pages=1),
    )
    assert seen == {"a", "b"} and reached_end is False


def test_walk_category_gone_page_past_the_declared_end_is_an_end(monkeypatch):
    # ...but a 404 at an offset the portal's own counter puts past the end is
    # bazos's past-the-end marker.
    (seen, _c, result_size, _pages, reached_end), client = _walk(
        monkeypatch, [_page([str(i) for i in range(20)], total=25, next_offset=30)],
        client=_RecIdxClient(ok_pages=1),
    )
    assert len(seen) == 20 and result_size == 25 and reached_end is True
    assert client.offsets == [0, 30]


def test_walk_category_a_full_page_with_no_pager_is_corroborated(monkeypatch):
    """`_next_offset` returns None both for bazos's last page and for a pager we
    failed to parse. A FULL page claiming no next page must be proven: one fetch
    of the offset it would have pointed at. Ads still there → the pager broke, so
    the walk nominates nothing."""
    (seen, _c, _result_size, _pages, reached_end), client = _walk(
        monkeypatch,
        [_page([str(i) for i in range(20)], total=6990, next_offset=None),
         _page([f"p2-{i}" for i in range(20)], total=6990, next_offset=None)],
    )
    assert len(seen) == 20 and reached_end is False
    assert client.offsets == [0, 20]


def test_walk_category_gone_first_page_is_not_an_end(monkeypatch):
    # Nothing corroborates a 404 on the very first page of a scope.
    (seen, _c, _result_size, pages, reached_end), _client = _walk(
        monkeypatch, [_page(["a"], total=100, next_offset=20)],
        client=_RecIdxClient(ok_pages=0),
    )
    assert seen == set() and pages == 0 and reached_end is False


def test_walk_category_barren_page_is_re_read_and_is_not_an_end(monkeypatch):
    # An items-less HTTP 200 mid-walk (soft block, consent shell, renamed
    # selector) is NOT how bazos ends a section. It is re-read once at the SAME
    # offset, and an uncorroborated blank stays a stop of ours.
    blank = _page([], total=None, next_offset=None)
    (seen, _c, _result_size, pages, reached_end), client = _walk(
        monkeypatch, [_page(["a"], total=400, next_offset=20), blank, blank],
    )
    assert client.offsets == [0, 20, 20]   # the blank page re-read, same offset
    assert seen == {"a"} and pages == 2 and reached_end is False


def test_walk_category_barren_page_past_declared_total_is_an_end(monkeypatch):
    # Still blank on the re-read AND at/past what the declared total implies:
    # that is the page after the last one, i.e. bazos's end — even though the
    # walk is one row short of the total.
    blank = _page([], total=None, next_offset=None)
    (seen, _c, _result_size, _pages, reached_end), client = _walk(
        monkeypatch,
        [_page([str(i) for i in range(19)], total=20, next_offset=20), blank, blank],
    )
    assert client.offsets == [0, 20, 20]
    assert len(seen) == 19 and reached_end is True


def test_walk_category_barren_first_page_is_not_an_end(monkeypatch):
    # A scope whose FIRST page is blank with no total is never confirmed: a
    # confirmation that cannot be obtained is not a confirmation.
    blank = _page([], total=None, next_offset=None)
    (seen, _c, _result_size, _pages, reached_end), _client = _walk(
        monkeypatch, [blank],
    )
    assert seen == set() and reached_end is False


def test_walk_category_barren_re_read_with_items_is_not_an_end(monkeypatch):
    # The blank was a fluke — so that page's ads were missed and the walk is
    # truncated, however it ends.
    (seen, _c, _result_size, _pages, reached_end), _client = _walk(
        monkeypatch,
        [_page(["a"], total=2, next_offset=20),
         _page([], total=None, next_offset=None),
         _page(["b"], total=2, next_offset=None)],
    )
    assert seen == {"a"} and reached_end is False


def test_walk_category_pager_stalled_is_not_an_end(monkeypatch):
    # A pager pointing at the offset we just walked would loop forever; it is a
    # stop of ours, not an end.
    (_seen, _c, _result_size, pages, reached_end), _client = _walk(
        monkeypatch, [_page(["a"], total=400, next_offset=0)],
    )
    assert pages == 1 and reached_end is False


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
    monkeypatch.setattr(
        bazos_main.db, "ingest_scraped_listing",
        lambda _c, _l, discovery_seq=None, discovered_at=None: (8105, "new"))
    monkeypatch.setattr(bazos_main.db, "record_images", lambda _c, _sid, imgs, **k: len(imgs))
    monkeypatch.setattr(bazos_main.db, "mark_portal_page_parsed", lambda *a, **k: None)
    counts = _portal().write_details(object(), items)
    assert counts["new"] == 1
    assert counts["images_discovered"] == 2


# --- geocoder wiring (text-first coordinate resolution) ---------------------
# The builder + memo cache now live in scraper.location (shared across portals);
# their behavior is tested in tests/scraper/test_location.py. Here we only pin
# bazos's wiring: the parser receives the geocoder.


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
