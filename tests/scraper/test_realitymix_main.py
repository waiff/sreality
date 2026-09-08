"""Hermetic tests for RealitymixPortal.walk_category — the total-driven paging
(drive ?stranka to ceil(total/PER_PAGE), don't trust a pager arrow), the
barren-page retry (the lesson from ceskereality's reverted #637) and the
STRUCTURAL nomination gate (rule #3, 2026-09-08): the 5th tuple element says the
walk reached realitymix's own end, not that the row count reconciled with the
declared total. No network/DB: a fake client feeds canned index HTML and
conn=None skips the DB writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scraper import portal as portal_mod
from scraper import realitymix_main
from scraper.portal_base import ListingGoneError
from scraper.portal import default_config
from scraper.realitymix_main import RealitymixPortal
from scraper.scraped_listing import ScrapedListing


@dataclass
class FakeGeo:
    lat: float
    lng: float
    confidence: str
    matched_type: str


def _listing(**kw) -> ScrapedListing:
    base = dict(source="realitymix", source_id_native="1", source_url="u")
    base.update(kw)
    return ScrapedListing(**base)


def _index_html(total: int | None, ids: list[str]) -> str:
    cards = "".join(
        f'<div class="w-full advert-item">'
        f'<a href="https://realitymix.cz/detail/x/y-{i}.html"></a>'
        f'<div class="text-xl font-extrabold"><span>1 000 000 Kč</span></div></div>'
        for i in ids
    )
    head = f'<div>z celkem {total} nalezených</div>' if total is not None else ""
    return f"<html><body>{head}{cards}</body></html>"


class FakeClient:
    """Returns the next canned HTML for a requested ?stranka page (a per-page list
    so a page can return barren first, then populated on retry). An entry that is
    an exception is raised instead — the 404/410 realitymix serves past the end."""

    def __init__(self, pages: dict[int, list[Any]], **_kw):
        self.pages = {k: list(v) for k, v in pages.items()}
        self.requested: list[int] = []

    def fetch_index(self, sale_type: str, category: str, page: int):
        self.requested.append(page)
        seq = self.pages.get(page) or [_index_html(None, [])]
        html = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(html, BaseException):
            raise html
        return html, 200


def _walk(monkeypatch, pages):
    fake = FakeClient(pages)
    monkeypatch.setattr(realitymix_main, "RealitymixClient", lambda limiter=None: fake)
    portal = RealitymixPortal(default_config("realitymix"))
    result = portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=None, dry_run=True, limiter=None,
    )
    return fake, result


# --- the structural gate: the walk reached realitymix's end ---------------------
#
# Rule #3 (2026-09-08): the 5th tuple element is `reached_end`, not "the count
# reconciled". It is True only when the page loop stopped because realitymix said
# there is nothing after the page we just read, and no stop of OURS fired.

def test_walk_drives_by_total_and_stops_at_last_page(monkeypatch):
    p1 = [str(1000 + i) for i in range(20)]
    p2 = [str(2000 + i) for i in range(5)]   # total 25 -> last page = ceil(25/20) = 2
    fake, (seen, counts, total, pages, reached_end) = _walk(
        monkeypatch, {1: [_index_html(25, p1)], 2: [_index_html(25, p2)]}
    )
    assert total == 25
    assert len(seen) == 25
    assert fake.requested == [1, 2]          # did NOT over-fetch a page 3
    assert reached_end is True


def test_a_walk_one_row_short_of_the_declared_total_still_reached_the_end(monkeypatch, caplog):
    # THE behaviour change. 86 of a declared 87 over the full 5 pages: every page
    # index the portal's own count says exists was fetched, so the walk reached
    # realitymix's end and its unseen rows are nominated for a page check. The
    # count is reported (coverage=incomplete), it does not veto.
    pages = {
        n: [_index_html(87, [str(n * 1000 + i) for i in range(20)])] for n in (1, 2, 3, 4)
    }
    pages[5] = [_index_html(87, [str(5000 + i) for i in range(6)])]
    with caplog.at_level("INFO", logger="scraper.realitymix_main"):
        fake, (seen, _counts, total, walked, reached_end) = _walk(monkeypatch, pages)
    assert len(seen) == 86 and total == 87 and fake.requested == [1, 2, 3, 4, 5]
    assert reached_end is True
    end_line = [r.getMessage() for r in caplog.records if "walk end" in r.msg]
    assert end_line and "stop=declared_total_reached" in end_line[-1]
    assert "coverage=incomplete" in end_line[-1]   # measured, logged, never gating


def test_walk_retries_a_barren_page_before_concluding_end(monkeypatch):
    p1 = [str(1000 + i) for i in range(20)]
    p2 = [str(2000 + i) for i in range(5)]
    # Page 2 returns EMPTY first (a transient throttle/degrade), then its items.
    fake, (seen, _counts, total, _pages, reached_end) = _walk(
        monkeypatch,
        {1: [_index_html(25, p1)], 2: [_index_html(25, []), _index_html(25, p2)]},
    )
    assert len(seen) == 25                    # the retry recovered the tail page
    assert fake.requested == [1, 2, 2]        # page 2 fetched twice (retry)
    assert reached_end is True


def test_a_barren_page_below_the_last_page_is_our_stop_not_the_portals(monkeypatch):
    # ITEMS-FIRST. A page that stays empty after its retry, at an offset the
    # declared total says still has rows, is a hole in OUR walk — indistinguishable
    # from the page after the last one if you only look at "no items, no pager".
    # Nominating here would send the whole untouched tail (~9k rows on byty/prodej)
    # to the drain on the strength of one throttled fetch.
    p1 = [str(1000 + i) for i in range(20)]
    fake, (seen, _counts, total, _pages, reached_end) = _walk(
        monkeypatch,
        {1: [_index_html(100, p1)], 2: [_index_html(100, [])]},
    )
    assert fake.requested == [1, 2, 2]        # retried once, still barren
    assert len(seen) == 20 and total == 100
    assert reached_end is False


def test_a_barren_page_past_the_declared_total_is_confirmed_empty(monkeypatch):
    # The corroborated case: the count says 25 rows (last page 2), churn left 20,
    # and page 2 is empty on both reads — at the last page the portal's own count
    # implies, so it is realitymix's end and not a throttle.
    p1 = [str(1000 + i) for i in range(20)]
    fake, (seen, _counts, total, _pages, reached_end) = _walk(
        monkeypatch,
        {1: [_index_html(25, p1)], 2: [_index_html(25, [])]},
    )
    assert fake.requested == [1, 2, 2]
    assert len(seen) == 20 and total == 25
    assert reached_end is True


def test_a_barren_page_cannot_supply_its_own_total(monkeypatch):
    """The suspect page must not supply its own corroboration. A degraded /
    "no results" render carrying its own smaller (or zero) "z celkem N" would put
    ITSELF past the end — so only a page that carried items may move the count."""
    p1 = [str(1000 + i) for i in range(20)]
    fake, (seen, _counts, total, _pages, reached_end) = _walk(
        monkeypatch,
        {1: [_index_html(1000, p1)], 2: [_index_html(0, [])]},
    )
    assert fake.requested == [1, 2, 2]
    assert len(seen) == 20 and total == 1000
    assert reached_end is False


def test_a_repeated_page_below_the_declared_end_is_our_stop(monkeypatch):
    """An edge cache serving page 1's body for every ?stranka. The row-identity test
    used to sit behind an `elif` that only ran when no total was readable — the mode
    realitymix is never in — so this paged to `last_page` on 20 ids and reported
    `declared_total_reached`."""
    p1 = [str(1000 + i) for i in range(20)]
    _fake, (seen, _counts, _total, _pages, reached_end) = _walk(
        monkeypatch,
        {1: [_index_html(200, p1)], 2: [_index_html(200, p1)]},
    )
    assert len(seen) == 20
    assert reached_end is False


def test_a_barren_page_with_no_total_after_pages_that_carried_items_is_confirmed(monkeypatch):
    # No "z celkem N" was ever readable, but earlier pages of this category did
    # carry items and page 2 is empty twice: the other half of the barren rule.
    p1 = [str(1000 + i) for i in range(20)]
    fake, (seen, _counts, total, _pages, reached_end) = _walk(
        monkeypatch,
        {1: [_index_html(None, p1)], 2: [_index_html(None, [])]},
    )
    assert fake.requested == [1, 2, 2]
    assert total is None and len(seen) == 20
    assert reached_end is True


def test_a_first_page_that_stays_barren_with_no_total_is_never_confirmed(monkeypatch):
    # A confirmation that cannot be obtained is not a confirmation: this is also
    # exactly what a 200 soft-block shell page looks like on page 1.
    fake, (seen, _counts, total, _pages, reached_end) = _walk(
        monkeypatch, {1: [_index_html(None, [])]}
    )
    assert fake.requested == [1, 1]
    assert seen == set() and total is None
    assert reached_end is False


def test_a_declared_zero_category_is_confirmed_empty(monkeypatch):
    fake, (seen, _counts, total, _pages, reached_end) = _walk(
        monkeypatch, {1: [_index_html(0, [])]}
    )
    assert seen == set() and total == 0
    assert reached_end is True


def test_a_gone_page_past_the_end_ends_the_category_but_one_mid_walk_does_not(monkeypatch):
    # realitymix answers 404 on a ?stranka past the end (the total can be off by
    # one). Past what the count implies that is the portal's end; below the
    # declared last page the same 404 is a hole in our walk.
    p1 = [str(1000 + i) for i in range(20)]
    _fake, (seen, _c, _t, _p, past_end) = _walk(
        monkeypatch,
        {1: [_index_html(None, p1)], 2: [ListingGoneError("u", 410)]},
    )
    assert len(seen) == 20 and past_end is True
    _fake2, (seen2, _c2, _t2, _p2, mid_walk) = _walk(
        monkeypatch,
        {1: [_index_html(400, p1)], 2: [ListingGoneError("u", 404)]},
    )
    assert len(seen2) == 20 and mid_walk is False


def test_a_gone_page_on_the_last_declared_page_is_the_portals_end(monkeypatch):
    """realitymix's REAL production shape, and the one no test covered: "z celkem N"
    over-declares by a few rows, so the 404 lands ON the last page the count implies
    (page 2 of a declared 40 that really holds 20). The old predicate could never be
    true for a page the loop actually fetched, so this read as OUR stop and the
    category never nominated a single row."""
    p1 = [str(1000 + i) for i in range(20)]
    fake, (seen, _c, total, _p, reached_end) = _walk(
        monkeypatch,
        {1: [_index_html(40, p1)], 2: [ListingGoneError("u", 404)]},
    )
    assert fake.requested == [1, 2]
    assert len(seen) == 20 and total == 40
    assert reached_end is True


def test_no_total_and_a_page_that_repeats_ids_reads_as_the_clamp(monkeypatch):
    # No total ever parsed and page 2 came back with only ids we already hold (a
    # strict subset): realitymix clamping an out-of-range ?stranka onto the last
    # page. The weakest terminator, and it can only fire at page >= 2.
    p1 = [str(1000 + i) for i in range(20)]
    fake, (seen, _counts, total, _pages, reached_end) = _walk(
        monkeypatch, {1: [_index_html(None, p1)], 2: [_index_html(None, p1)]}
    )
    assert fake.requested == [1, 2]
    assert len(seen) == 20 and total is None
    assert reached_end is True


def test_walk_single_page(monkeypatch):
    ids = [str(3000 + i) for i in range(7)]   # total 7 -> last page 1
    fake, (seen, _counts, total, _pages, reached_end) = _walk(
        monkeypatch, {1: [_index_html(7, ids)]}
    )
    assert total == 7
    assert len(seen) == 7
    assert fake.requested == [1]
    assert reached_end is True


def test_max_pages_caps_walk_and_suppresses_completeness(monkeypatch):
    p1 = [str(1000 + i) for i in range(20)]
    p2 = [str(2000 + i) for i in range(20)]
    fake = FakeClient({1: [_index_html(100, p1)], 2: [_index_html(100, p2)]})
    monkeypatch.setattr(realitymix_main, "RealitymixClient", lambda limiter=None: fake)
    portal = RealitymixPortal(default_config("realitymix"), max_pages=1)
    seen, _counts, total, pages, reached_end = portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=None, dry_run=True, limiter=None,
    )
    assert fake.requested == [1]              # capped at one page
    assert len(seen) == 20
    assert reached_end is False               # a capped walk nominates nothing


def test_a_declared_page_cap_suppresses_even_a_walk_that_finished(monkeypatch):
    # --max-pages / --probe makes the run partial BY DECLARATION: the cap did not
    # fire here (the category is one page), but a capped run must never nominate.
    ids = [str(3000 + i) for i in range(7)]
    fake = FakeClient({1: [_index_html(7, ids)]})
    monkeypatch.setattr(realitymix_main, "RealitymixClient", lambda limiter=None: fake)
    portal = RealitymixPortal(default_config("realitymix"), max_pages=5)
    _seen, _counts, _total, _pages, reached_end = portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=None, dry_run=True, limiter=None,
    )
    assert fake.requested == [1]
    assert reached_end is False


# --- wall-clock deadline (--max-seconds): stop cleanly, never claim complete ---

class _FakeClock:
    """Stand-in for scraper.portal's `time`, advanced by the fake index fetch."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


def _deadline_walk(monkeypatch, pages, deadline, *, per_page_seconds=10.0):
    clock = _FakeClock()
    monkeypatch.setattr(portal_mod, "time", clock)
    fake = FakeClient(pages)
    inner = fake.fetch_index

    def fetch_index(sale_type, category, page):
        clock.now += per_page_seconds
        return inner(sale_type, category, page)

    fake.fetch_index = fetch_index
    monkeypatch.setattr(realitymix_main, "RealitymixClient", lambda limiter=None: fake)
    portal = RealitymixPortal(default_config("realitymix"))
    return fake, portal.walk_category(
        {"sale_type": "prodej", "category": "byty"}, None, True, None, deadline,
    )


def test_deadline_mid_walk_stops_and_forces_incomplete(monkeypatch):
    # 3 pages of a 60-item category, 10s each, 15s of budget -> stop after page 2.
    pages = {
        n: [_index_html(60, [str(n * 1000 + i) for i in range(20)])]
        for n in (1, 2, 3)
    }
    fake, (seen, _counts, total, walked, reached_end) = _deadline_walk(
        monkeypatch, pages, 15.0)
    assert fake.requested == [1, 2]            # page 3 never fetched
    assert walked == 2
    assert len(seen) == 40 and total == 60
    # The dangerous outcome would be reached_end=True here: the 20 listings the
    # walk simply never reached would be nominated for a page check (rule #3).
    assert reached_end is False


def test_deadline_already_spent_stops_before_the_first_page(monkeypatch):
    pages = {1: [_index_html(20, [str(1000 + i) for i in range(20)])]}
    fake, (seen, _counts, _total, walked, reached_end) = _deadline_walk(
        monkeypatch, pages, -1.0)
    assert fake.requested == []
    assert walked == 0 and seen == set()
    assert reached_end is False


def test_no_deadline_walk_still_completes(monkeypatch):
    pages = {
        n: [_index_html(40, [str(n * 1000 + i) for i in range(20)])] for n in (1, 2)
    }
    _fake, (seen, _counts, total, walked, reached_end) = _deadline_walk(
        monkeypatch, pages, None)
    assert len(seen) == 40 and total == 40 and walked == 2
    assert reached_end is True


def test_category_labels():
    portal = RealitymixPortal(default_config("realitymix"))
    assert portal.category_labels({"sale_type": "prodej", "category": "byty"}) == ("byt", "prodej")
    assert portal.category_labels({"sale_type": "pronajem", "category": "komerce"}) == ("komercni", "pronajem")
    assert portal.category_labels({"sale_type": "prodej", "category": "chaty"}) == ("dum", "prodej")


# --- cross-slice nomination ('domy' + 'chaty' -> dum) --------------------------
#
# Rule #3 since 2026-09-07: a complete walk nominates its unseen rows for a page
# check, it does not delist. Sibling slices that collapse onto one canonical
# category still have to be buffered, because the runner calls the seam per
# slice and one slice's seen set would nominate the sibling's whole population.

def _nominating_portal(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        realitymix_main.db, "presence_candidates",
        lambda _c, src, cm, ct, seen, **kw: calls.append(
            {"src": src, "cm": cm, "ct": ct, "seen": set(seen), **kw}) or ([], len(seen)),
    )
    return RealitymixPortal(default_config("realitymix")), calls


def test_nomination_buffers_the_collapsing_group_and_nominates_once_with_union(monkeypatch):
    portal, calls = _nominating_portal(monkeypatch)
    # First dum slice buffers only: nominating here would send every chaty row
    # (same (dum, prodej), never in the domy slice's seen set) to the drain.
    assert portal.presence_candidates(
        object(), {"sale_type": "prodej", "category": "domy"}, {"r1", "r2"}) is None
    assert calls == []
    # The group's last complete slice nominates with the UNION.
    out = portal.presence_candidates(
        object(), {"sale_type": "prodej", "category": "chaty"}, {"c1"})
    assert out == ([], 3)
    assert calls == [{"src": "realitymix", "cm": "dum", "ct": "prodej", "seen": {"r1", "r2", "c1"}}]


def test_nomination_missing_sibling_slice_nominates_nothing(monkeypatch):
    # The runner only reaches this seam for COMPLETE slices; if domy walked
    # incomplete or failed, chaty alone must not nominate (dum, prodej).
    portal, calls = _nominating_portal(monkeypatch)
    assert portal.presence_candidates(
        object(), {"sale_type": "prodej", "category": "chaty"}, {"c1"}) is None
    assert calls == []


def test_an_empty_sibling_slice_still_counts_toward_the_group(monkeypatch):
    """The runner refuses to nominate from a walk that saw NOTHING (an empty seen set
    would nominate the whole scope), so an empty slice never reaches
    presence_candidates. Without note_empty_slice the group's counter would stay one
    short for ever and NO row of (dum, prodej) would be nominated again — the silent
    stall rule #3 exists to remove."""
    portal, calls = _nominating_portal(monkeypatch)
    portal.note_empty_slice({"sale_type": "prodej", "category": "chaty"})
    out = portal.presence_candidates(
        object(), {"sale_type": "prodej", "category": "domy"}, {"r1", "r2"})
    assert out == ([], 2)
    assert calls == [{"src": "realitymix", "cm": "dum", "ct": "prodej",
                      "seen": {"r1", "r2"}}]


def test_nomination_single_slice_group_nominates_immediately(monkeypatch):
    portal, calls = _nominating_portal(monkeypatch)
    assert portal.presence_candidates(
        object(), {"sale_type": "prodej", "category": "byty"}, {"b1"}) == ([], 1)
    assert calls == [{"src": "realitymix", "cm": "byt", "ct": "prodej", "seen": {"b1"}}]


def test_the_old_sweep_seam_is_gone():
    """No portal may flip rows from index absence any more; the runner never
    calls mark_inactive and the seam must not linger to tempt anyone."""
    assert not hasattr(RealitymixPortal, "mark_inactive")




def test_nomination_groups_are_sale_type_scoped(monkeypatch):
    # domy/prodej + chaty/pronajem are DIFFERENT (cm, ct) groups; neither
    # completes its own group, so neither nominates.
    portal, calls = _nominating_portal(monkeypatch)
    assert portal.presence_candidates(
        object(), {"sale_type": "prodej", "category": "domy"}, {"d1"}) is None
    assert portal.presence_candidates(
        object(), {"sale_type": "pronajem", "category": "chaty"}, {"c1"}) is None
    assert calls == []
