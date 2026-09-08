"""maxima_main on the portal framework: MaximaPortal (complete-walk via agenda-
grain delisting, two mixed agendas split per (category_main, category_type) via
id-prefix) seams + the main() that drives index-walk then detail-drain through
the shared runner, recording an 'index' + a 'detail' scrape_runs row tagged
source='maxima'.
"""

from __future__ import annotations

import time
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


_BASE = "https://nemovitosti.maxima.cz/nemovitosti/"


def _item(nid: str, title: str = "Prodej bytu") -> SimpleNamespace:
    return SimpleNamespace(
        source_id_native=nid, detail_path=f"{_BASE}{nid}/",
        price_text="5 000 000 Kč", title=title,
    )


def _page(
    total: int | None, items: list[Any], next_offset: int | None = None,
    pager_present: bool = True,
):
    # pager_present defaults True: these pages stand for maxima RENDERING its pager
    # (with or without a forward link). Pass False for the degraded shape — cards
    # render, the pager fragment does not — which must testify to nothing.
    return SimpleNamespace(
        total=total, next_offset=next_offset, items=items,
        pager_present=pager_present,
    )


def _drive(monkeypatch, portal, pages, *, category=None, **kw):
    """Feed `pages` (parse_index results, in FETCH order — a re-fetched barren
    page consumes one) to one agenda walk. Returns (walk_category result, fetches)."""
    seq = iter(pages)
    fetched: list[tuple[Any, Any]] = []

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def fetch_index(self, page=None, *, af=None):
            fetched.append((page, af))
            return ("<html>", 200)

    monkeypatch.setattr(maxima_main, "parse_index", lambda _h: next(seq))
    monkeypatch.setattr(maxima_main, "MaximaClient", _Client)
    monkeypatch.setattr(maxima_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(maxima_main.db, "enqueue_detail", lambda *a, **k: 0)
    monkeypatch.setattr(maxima_main.db, "touch_listings_by_id", lambda *a, **k: None)
    result = portal.walk_category(
        category or _CATEGORIES[0], object(), False, _Limiter(), **kw)
    return result, fetched


def _walk_one_agenda(monkeypatch, portal, *, total, items, max_pages=None):
    """Drive one agenda walk (af=1) so the portal's agenda cache is populated:
    one page of items whose pager says there is no next page, then the empty page
    after it (re-fetched once by the barren rule)."""
    empty = _page(total, [])
    _drive(monkeypatch, portal, [_page(total, items), empty, empty])


def test_a_blank_page_cannot_supply_its_own_count(monkeypatch):
    """The page the code has just declared untrustworthy must not supply the evidence
    that it is trustworthy. A shell / mis-filtered 200 that renders no cards and a
    counter of its own (0, or anything <= what we hold) would otherwise satisfy
    `collected >= total` and pass as maxima's end — and maxima's agendas are ~220
    rows, below the flip cap's floor, so the whole agenda is nominable in one walk."""
    portal = _portal()
    shell = _page(0, [])
    (_seen, _c, _t, _p, complete), _fetched = _drive(
        monkeypatch, portal,
        [_page(220, [_item("b1"), _item("b2")], next_offset=2), shell, shell],
    )
    walk = portal._agenda_cache[1]
    assert (walk.total, walk.stop) == (220, "barren")
    assert complete is False


def test_a_page_with_no_pager_markup_corroborates_nothing(monkeypatch):
    """`next_offset is None` means "the pager said this is the last page" OR "no
    pager rendered at all" (a CDN/template hiccup that keeps the cards). Only the
    first is evidence, so the barren page after a pager-less page stays ours."""
    portal = _portal()
    empty = _page(220, [])
    (_seen, _c, _t, _p, complete), _fetched = _drive(
        monkeypatch, portal,
        [_page(220, [_item("b1")], next_offset=2),
         _page(220, [_item("b2")], next_offset=None, pager_present=False),
         empty, empty],
    )
    walk = portal._agenda_cache[1]
    assert walk.stop == "barren"
    assert complete is False


def test_nomination_is_agenda_grain(monkeypatch):
    # A complete sale agenda spanning byt + dum + ostatni. presence_candidates must
    # nominate the WHOLE agenda (category_type=prodej) against EVERY agenda id —
    # never the per-category byt slice — and only once per agenda per run.
    items = [_item("b1"), _item("b2"), _item("d1", "Prodej domu"), _item("o1", "Prodej")]
    portal = _portal()
    _walk_one_agenda(monkeypatch, portal, total=4, items=items)

    captured: list[Any] = []
    monkeypatch.setattr(
        maxima_main.db, "presence_candidates",
        lambda _c, source, cm, ct, seen, **kw: (
            captured.append((source, cm, ct, set(seen))) or ([], 7)
        ),
    )
    # First prodej descriptor (byt) triggers the agenda sweep.
    assert portal.presence_candidates(object(), _CATEGORIES[0], {"b1", "b2"}) == ([], 7, {"category_main": None})
    source, cm, ct, seen = captured[0]
    assert source == "maxima" and ct == "prodej" and cm is None   # agenda scope: category_type alone
    assert seen == {"b1", "b2", "d1", "o1"}        # the FULL agenda, not the byt slice
    # A second prodej descriptor (dum) must NOT re-nominate the same agenda.
    assert portal.presence_candidates(object(), _CATEGORIES[1], {"d1"}) is None
    assert len(captured) == 1


def test_nomination_skips_uncorroborated_empty_page(monkeypatch):
    # The agenda declares 10, the walk collected 2 and then hit an items-less page
    # nothing could corroborate (no pager evidence, short of maxima's own count):
    # `barren` is OURS, so the unseen 8 are not nominated.
    portal = _portal()
    _walk_one_agenda(monkeypatch, portal, total=10, items=[_item("b1"), _item("b2")])
    assert portal._agenda_cache[1].stop == "barren"
    called = {"n": 0}
    monkeypatch.setattr(
        maxima_main.db, "presence_candidates",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or ([], 0),
    )
    assert portal.presence_candidates(object(), _CATEGORIES[0], {"b1", "b2"}) is None
    assert called["n"] == 0


def test_nomination_ignores_unmeasurable_count(monkeypatch):
    # The index parsed but its total never did (total=None) and no pager rendered.
    # The walk still ended on an empty page that follows pages which carried items
    # -- nothing can contradict it -- so it reached maxima's end and nominates.
    # The count is an alarm (coverage="unknown", logged), never a veto (rule #3).
    portal = _portal()
    _walk_one_agenda(monkeypatch, portal, total=None, items=[_item("b1"), _item("b2")])
    assert portal._agenda_cache[1].stop == "empty_confirmed"
    assert portal._agenda_cache[1].coverage == "unknown"

    captured: list[Any] = []
    monkeypatch.setattr(
        maxima_main.db, "presence_candidates",
        lambda _c, source, cm, ct, seen, **kw: captured.append(set(seen)) or ([], 3),
    )
    assert portal.presence_candidates(object(), _CATEGORIES[0], {"b1", "b2"}) == (
        [], 3, {"category_main": None})
    assert captured == [{"b1", "b2"}]


def test_nomination_ignores_overcollected_count(monkeypatch):
    # Collected 4 against a declared total of 2: the numeric verdict is
    # "incomplete" (over-collection), but the walk reached maxima's own count, so
    # it nominates and the page decides. Over-collection can only SHRINK the
    # nominated set -- it is an alarm, not a gate.
    portal = _portal()
    items = [_item(n) for n in ("b1", "b2", "b3", "b4")]
    _walk_one_agenda(monkeypatch, portal, total=2, items=items)
    assert portal._agenda_cache[1].stop == "declared_total_reached"
    assert portal._agenda_cache[1].coverage == "incomplete"

    called = {"n": 0}
    monkeypatch.setattr(
        maxima_main.db, "presence_candidates",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or ([], 0),
    )
    assert portal.presence_candidates(object(), _CATEGORIES[0], {"b1"}) is not None
    assert called["n"] == 1


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
    # One sale-agenda page carrying the agenda's whole declared count (3): mixed
    # categories, 2 byty + 1 dum. Its pager still advertises a page 2, so the walk
    # asks for it — the counter alone must not stop the fetching (maxima's is loose:
    # the live tail reads "Zobrazuji 29-42 z celkem 29", and a lagging count would
    # otherwise leave real rows unread and unenqueued for ever).
    base = "https://nemovitosti.maxima.cz/nemovitosti/"
    b1, b2, d1 = "b50000001", "b50000002", "d40000003"  # b1 new, b2 changed, d1 dum
    page1 = _page(3, next_offset=2, items=[
        SimpleNamespace(source_id_native=b1, detail_path=f"{base}{b1}/", price_text="5 000 000 Kč", title="Prodej bytu 2+kk"),
        SimpleNamespace(source_id_native=b2, detail_path=f"{base}{b2}/", price_text="6 000 000 Kč", title="Prodej bytu 3+kk"),
        SimpleNamespace(source_id_native=d1, detail_path=f"{base}{d1}/", price_text="9 000 000 Kč", title="Prodej rodinného domu"),
    ])
    empty = _page(3, next_offset=None, items=[])
    seq = iter([page1, empty, empty])   # the empty page is read twice (barren rule)
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
    # reached_end reflects the AGENDA (3 of maxima's declared 3), not the byt
    # slice (2) — the slice has no completeness proof of its own.
    assert total_b == 2 and complete_b is True
    assert pages_b == 3                       # page 2 asked for, ran dry, re-read once
    assert _IdxClient.fetches[1] == 3

    # dum·prodej: reuses the cached agenda (no new fetch), yields just the dum.
    seen_d, _c, total_d, pages_d, _comp = portal.walk_category(
        _CATEGORIES[1], object(), False, _Limiter(),
    )
    assert seen_d == {d1}
    assert total_d == 1
    assert pages_d == 0                       # cache hit -> no pages counted again
    assert _IdxClient.fetches[1] == 3         # still only the original walk

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
    page1 = _page(1, next_offset=None, items=[
        SimpleNamespace(source_id_native=rent, detail_path=f"{base}{rent}/", price_text="19 000 Kč", title="Pronájem bytu 1 + kk"),
    ])
    empty = _page(1, next_offset=None, items=[])
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


# --- the structural walk verdict (rule #3) ----------------------------------
# The 5th tuple element answers "did the walk reach MAXIMA's end?", never "did
# the count reconcile?". A walk a row short of the declared total has still
# finished; a walk we cut short, or one that ended on an items-less page nobody
# could corroborate, has not.


def _reached_end(monkeypatch, portal, pages, **kw) -> bool:
    (result, fetched) = _drive(monkeypatch, portal, pages, **kw)
    return result[4]


def test_reached_end_when_a_row_short_of_the_declared_total(monkeypatch):
    # THE case this change unblocks: maxima declares 5, the walk collected 4
    # (live churn), and the pager said page 2 was the last. Structurally finished
    # -> the gap is nominated and the page decides.
    portal = _portal()
    pages = [
        _page(5, [_item("b1"), _item("b2"), _item("b3")], next_offset=2),
        _page(5, [_item("b4")], next_offset=None),
        _page(5, []),
        _page(5, []),                       # the barren rule's one re-fetch
    ]
    (seen, _c, _t, walked, reached_end), fetched = _drive(monkeypatch, portal, pages)
    assert reached_end is True
    assert portal._agenda_cache[1].stop == "empty_confirmed"
    assert portal._agenda_cache[1].coverage == "incomplete"   # 4 of 5: an alarm only
    assert len(fetched) == 4 and walked == 4


def test_items_first_barren_page_is_not_the_end(monkeypatch):
    # A blocked/consent/degraded 200 parses to zero items AND exposes no pager --
    # exactly like the page past the last one, which is why the old compound
    # `not items or new_on_page == 0` break could not tell them apart. Here the
    # walk is 3 of a declared 10 and the previous page's pager pointed forward,
    # so nothing corroborates the emptiness: OUR stop.
    portal = _portal()
    pages = [
        _page(10, [_item("b1"), _item("b2"), _item("b3")], next_offset=2),
        _page(10, []),
        _page(10, []),
    ]
    (_s, _c, _t, _p, reached_end), fetched = _drive(monkeypatch, portal, pages)
    assert reached_end is False
    assert portal._agenda_cache[1].stop == "barren"
    assert [pg for pg, _af in fetched] == [1, 2, 2]   # the same page, re-fetched once


def test_barren_page_that_heals_on_the_re_fetch_keeps_walking(monkeypatch):
    # The re-fetch is what makes an empty page evidence: when it comes back with
    # items the walk goes on and keeps them (no truncation from one bad render).
    portal = _portal()
    pages = [
        _page(None, [_item("b1"), _item("b2")], next_offset=2),
        _page(None, []),                                        # transient blank
        _page(None, [_item("b3"), _item("b4")], next_offset=None),
        _page(None, []),
        _page(None, []),
    ]
    (seen, _c, _t, walked, reached_end), fetched = _drive(monkeypatch, portal, pages)
    assert seen == {"b1", "b2", "b3", "b4"}
    assert reached_end is True
    assert portal._agenda_cache[1].stop == "empty_confirmed"
    assert [pg for pg, _af in fetched] == [1, 2, 2, 3, 3]
    assert walked == 5                       # the re-fetches are counted honestly


def test_page_cap_is_our_stop_but_still_collects_its_page(monkeypatch):
    # The cap is tested AFTER the page is collected -- the probe walks under a
    # 1-page cap and must still see (and enqueue) that page's listings.
    portal = _portal(max_pages=1)
    pages = [_page(10, [_item("b1")], next_offset=2)]
    (seen, _c, _t, _p, reached_end), fetched = _drive(monkeypatch, portal, pages)
    assert seen == {"b1"} and len(fetched) == 1
    assert reached_end is False
    assert portal._agenda_cache[1].stop == "page_cap"


def test_page_cap_on_a_barren_page_spends_no_re_fetch(monkeypatch):
    portal = _portal(max_pages=2)
    pages = [_page(10, [_item("b1")], next_offset=2), _page(10, [])]
    (_s, _c, _t, _p, reached_end), fetched = _drive(monkeypatch, portal, pages)
    assert reached_end is False and len(fetched) == 2
    assert portal._agenda_cache[1].stop == "page_cap"


def test_deadline_is_our_stop(monkeypatch):
    portal = _portal()
    pages = [_page(10, [_item("b1")], next_offset=2)]
    reached_end = _reached_end(
        monkeypatch, portal, pages, deadline=time.monotonic() - 1)
    assert reached_end is False
    assert portal._agenda_cache[1].stop == "deadline"


def test_repeated_page_is_the_end_only_when_the_pager_agrees(monkeypatch):
    # A page that adds no new id is the clamped out-of-range page maxima serves
    # past its last one -- but only its pager makes that maxima's word.
    portal = _portal()
    b1, b2 = _item("b1"), _item("b2")
    clamped = [
        _page(10, [b1], next_offset=2),
        _page(10, [b2], next_offset=None),
        _page(10, [b2]),                    # the last page served again
    ]
    assert _reached_end(monkeypatch, portal, clamped) is True
    assert portal._agenda_cache[1].stop == "clamp_repeat"

    stalled = _portal()
    pages = [
        _page(10, [b1], next_offset=2),
        _page(10, [b1], next_offset=3),     # the pager says there is more
    ]
    assert _reached_end(monkeypatch, stalled, pages) is False
    assert stalled._agenda_cache[1].stop == "pager_stalled"


def test_confirm_empty_page_matrix():
    confirm = MaximaPortal._confirm_empty_page
    # maxima's own count reached -> the page past the last one.
    assert confirm(5, 5, pager_advanced=False, pager_end_prev=False) == "empty_confirmed"
    assert confirm(0, 0, pager_advanced=False, pager_end_prev=False) == "empty_confirmed"
    # Short of it, but a working pager said the previous page was the last.
    assert confirm(5, 4, pager_advanced=True, pager_end_prev=True) == "empty_confirmed"
    # Short of it with nothing to corroborate the emptiness -> ours.
    assert confirm(5, 4, pager_advanced=True, pager_end_prev=False) == "barren"
    assert confirm(5, 4, pager_advanced=False, pager_end_prev=True) == "barren"
    # No total and no pager: an empty page after real pages is the end...
    assert confirm(None, 4, pager_advanced=False, pager_end_prev=False) == "empty_confirmed"
    # ...but a pager that is still pointing forward contradicts it, and an
    # agenda barren from page 1 proves nothing at all.
    assert confirm(None, 4, pager_advanced=True, pager_end_prev=False) == "barren"
    assert confirm(None, 0, pager_advanced=False, pager_end_prev=False) == "barren"


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
