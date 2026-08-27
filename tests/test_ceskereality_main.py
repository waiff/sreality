"""The ceskereality 14-kraj index partition + the opt-in residential proxy.

What this file used to test — a 7-subdomain × rendered-facet fan-out capped at 12
pages — was the defect, not the contract: the facet block is a top-10-by-popularity
list, so whole okresy were never visited and the walk collected 7,566 of a declared
8,828 while the completeness gate suppressed every delisting sweep. The 12-page cap
turned out to belong to UNFILTERED category URLs only. The walk now partitions on
the 14 DECLARED kraje, pages each slice to its own declared tail (up to the site's
real 99-page ceiling on a filtered URL), and descends onto the subtype axis when a
kraj needs more than that.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from scraper import ceskereality_main as m
from scraper import portal as m_portal
from scraper.ceskereality_client import (
    KRAJ_SLUGS,
    SUBTYPE_SLUGS,
    CeskerealityClient,
    search_url,
)
from scraper.portal import default_config


def _page_html(
    total: int | None, ids: list[str], facets: tuple[str, ...] = (),
    next_page: int | None = None, heading: str | None = None,
) -> str:
    cards = "".join(
        '<article class="i-estate">'
        f'<a class="i-estate__image-link" href="/prodej/byty/x/y-{i}.html"></a>'
        "</article>"
        for i in ids
    )
    facet_links = "".join(f'<a href="/prodej/byty/{s}/">x</a>' for s in facets)
    meta = f'<meta name="description" content="Máme tady {total} bytů">' if total else ""
    pager = (
        f'<a class="pagination-arrow --next" href="/x/?strana={next_page}"></a>'
        if next_page else ""
    )
    h1 = f"<h1>{heading}</h1>" if heading else ""
    return (
        f"<html><head>{meta}</head><body>{h1}{cards}{facet_links}{pager}</body></html>"
    )


def _kraj_of(url: str) -> str | None:
    """The kraj segment of a slice URL, for asserting what a walk actually touched."""
    for slug in KRAJ_SLUGS:
        if f"/{slug}/" in url:
            return slug
    return None


class _PartitionClient:
    """A whole category as a dict of {kraj: declared_count}, paged 20 to a page
    with ids derived from (kraj, page) — i.e. a real partition, so the union over
    the 14 kraje is exactly the sum of their counts."""

    def __init__(self, counts: dict[str, int], national: int | None = None) -> None:
        self.urls: list[str] = []
        self._counts = counts
        self._national = national if national is not None else sum(counts.values())

    def fetch_search(self, url):  # noqa: ANN001
        self.urls.append(url)
        kraj = _kraj_of(url)
        total = self._counts.get(kraj, 0)
        pg = _page_num(url)
        if total == 0:
            # The verified empty-slice signature: 200, a correct H1, zero cards,
            # and NO "Máme tady N" phrase anywhere on the page.
            return _page_html(None, [], heading=f"Prodej bytů {kraj}"), 200
        first = (pg - 1) * 20
        ids = [str(1_000_000 + KRAJ_SLUGS.index(kraj) * 100_000 + first + k)
               for k in range(max(0, min(20, total - first)))]
        last = max(1, -(-total // 20))
        return _page_html(
            total, ids, next_page=pg + 1 if pg < last else None,
            heading=f"Prodej bytů {kraj}"), 200

    def fetch_index(self, sale_type, cat, page):  # noqa: ANN001
        return _page_html(self._national, []), 200


def _walk(portal, client, monkeypatch, **kw):
    monkeypatch.setattr(m, "CeskerealityClient", lambda **k: client)
    return portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=None, dry_run=True, limiter=None, **kw,
    )


def _page_num(url: str) -> int:
    mm = re.search(r"strana=(\d+)", url)
    return int(mm.group(1)) if mm else 1


# --- the declared partition ------------------------------------------------


def test_kraj_table_is_declared_complete_and_spells_vysocina_irregularly():
    # 14 modern kraje, DECLARED — never scraped off a page. The two traps this
    # pins: kraj-vysocina's irregular slug (vysocina-kraj and vysocina both 404),
    # and the LEGACY 7-region vocabulary, which double-counts if mixed in.
    assert len(KRAJ_SLUGS) == len(set(KRAJ_SLUGS)) == 14
    assert "kraj-vysocina" in KRAJ_SLUGS
    assert "vysocina-kraj" not in KRAJ_SLUGS and "vysocina" not in KRAJ_SLUGS
    for legacy in ("severocesky", "vychodocesky", "zapadocesky", "severomoravsky",
                   "jihomoravsky", "stredocesky"):
        assert legacy not in KRAJ_SLUGS, f"legacy region {legacy} would double-count"
    assert "zahranicni" not in KRAJ_SLUGS


def test_full_walk_visits_all_fourteen_kraje_and_no_foreign_tree(monkeypatch):
    counts = {k: 40 for k in KRAJ_SLUGS}
    fake = _PartitionClient(counts)
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    seen, _counts, total, _pages, complete = _walk(portal, fake, monkeypatch)

    assert {_kraj_of(u) for u in fake.urls if _kraj_of(u)} == set(KRAJ_SLUGS)
    assert len(seen) == 14 * 40
    assert total == 14 * 40
    assert complete is True
    # never the foreign tree, never a legacy region, never a macro subdomain
    for u in fake.urls:
        assert "zahranicni" not in u
        assert "severo." not in u and "vychodo." not in u and "moravskereality" not in u


def test_a_slice_pages_far_past_twelve(monkeypatch):
    # /prodej/byty/praha/?strana=93 returns the declared tail: the 12-page cap
    # was never a law about filtered URLs, and the old code stopped at 12.
    counts = {k: 20 for k in KRAJ_SLUGS}
    counts["praha"] = 1843
    fake = _PartitionClient(counts)
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    seen, _c, _t, pages, complete = _walk(portal, fake, monkeypatch)

    praha_pages = [_page_num(u) for u in fake.urls if "/praha/" in u]
    assert max(praha_pages) == 93            # ceil(1843/20), the declared tail
    assert 94 not in praha_pages             # ...and never the 404 past it
    assert len(seen) == 1843 + 13 * 20
    assert pages >= 93
    assert complete is True


def test_a_kraj_with_no_listings_is_a_valid_slice(monkeypatch):
    # Reproduced live on pronajem/chaty-chalupy in karlovarsky + olomoucky: 200,
    # a correct H1, zero cards, and no "Máme tady N" phrase AT ALL. Read as a
    # fetch failure it would suppress every sweep forever.
    counts = {k: 40 for k in KRAJ_SLUGS}
    counts["karlovarsky-kraj"] = 0
    counts["olomoucky-kraj"] = 0
    fake = _PartitionClient(counts)
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    seen, _c, _t, _p, complete = _walk(portal, fake, monkeypatch)

    assert len(seen) == 12 * 40
    assert complete is True                  # empty is an ANSWER, not a failure
    empty = portal._walk_slice(fake, "prodej", "byty", "karlovarsky-kraj")
    assert (empty.outcome, empty.declared_total, empty.rows) == ("exhausted", 0, [])


class _DegradedClient(_PartitionClient):
    """The real throttle vector: a 200 with zero cards and no total — but the H1
    does NOT name the kraj we asked for, because the page is not that slice."""

    def fetch_search(self, url):  # noqa: ANN001
        if _kraj_of(url) == "ustecky-kraj":
            self.urls.append(url)
            return _page_html(None, [], heading="Reality na prodej"), 200
        return super().fetch_search(url)


def test_degraded_zero_card_page_is_not_a_finished_slice(monkeypatch):
    fake = _DegradedClient({k: 40 for k in KRAJ_SLUGS})
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    seen, _c, _t, _p, complete = _walk(portal, fake, monkeypatch)

    bad = portal._walk_slice(fake, "prodej", "byty", "ustecky-kraj")
    assert bad.outcome == "degraded"
    assert bad.positive is False
    assert len(seen) == 13 * 40              # the other 13 kraje still collected
    assert complete is False                 # ...but the category is unproven


class _MidSliceBlankClient(_PartitionClient):
    """A slice that serves a correct page 1 and then a blank 200 mid-slice — the
    same throttle, arriving after the H1 has already been proven."""

    def fetch_search(self, url):  # noqa: ANN001
        if _kraj_of(url) == "praha" and _page_num(url) == 3:
            self.urls.append(url)
            return _page_html(None, [], heading="Prodej bytů praha"), 200
        return super().fetch_search(url)


def test_blank_page_mid_slice_is_degraded_not_the_end(monkeypatch):
    counts = {k: 40 for k in KRAJ_SLUGS}
    counts["praha"] = 200
    fake = _MidSliceBlankClient(counts)
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    r = portal._walk_slice(fake, "prodej", "byty", "praha")
    assert r.outcome == "degraded"
    assert len(r.rows) == 40                 # pages 1-2 kept, the slice unproven
    _seen, _c, _t, _p, complete = _walk(portal, fake, monkeypatch)
    assert complete is False


class _BoomClient(_PartitionClient):
    def fetch_search(self, url):  # noqa: ANN001
        if _kraj_of(url) == "zlinsky-kraj":
            raise RuntimeError("connection reset")
        return super().fetch_search(url)


def test_a_fetch_exception_is_an_error_not_a_clean_finish(monkeypatch):
    fake = _BoomClient({k: 40 for k in KRAJ_SLUGS})
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    r = portal._walk_slice(fake, "prodej", "byty", "zlinsky-kraj")
    assert r.outcome == "error" and r.positive is False
    _seen, _c, _t, _p, complete = _walk(portal, fake, monkeypatch)
    assert complete is False


def test_a_missing_kraj_forces_incomplete(monkeypatch):
    fake = _PartitionClient({k: 40 for k in KRAJ_SLUGS})
    portal = m.CeskerealityPortal(
        default_config("ceskereality"), kraje=("praha", "zlinsky-kraj"))
    seen, _c, _t, _p, complete = _walk(portal, fake, monkeypatch)
    assert {_kraj_of(u) for u in fake.urls if _kraj_of(u)} == {"praha", "zlinsky-kraj"}
    assert len(seen) == 80
    assert complete is False                 # 2 of 14 is never a full walk


def test_union_short_of_the_national_total_forces_incomplete(monkeypatch):
    # Every slice exhausted and their declared sum reconciles — but the nationwide
    # probe says the category is far bigger, so the partition is not trusted.
    fake = _PartitionClient({k: 40 for k in KRAJ_SLUGS}, national=5000)
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    _seen, _c, total, _p, complete = _walk(portal, fake, monkeypatch)
    assert total == 5000
    assert complete is False


# --- the subtype descent (a kraj past the site's 99-page ceiling) ------------


def test_a_slice_over_the_page_ceiling_descends_onto_subtypes(monkeypatch):
    """/prodej/rodinne-domy/stredocesky-kraj/ is 2,312 rows = 116 pages, and a
    FILTERED url 404s at ?strana=100 — so 332 rows are unreachable on the kraj
    axis alone. The subtype slugs are declared (the rendered facet block omits
    zero-count subtypes) and sum to the kraj total exactly."""
    subs = SUBTYPE_SLUGS["rodinne-domy"]
    per_sub = {s: 240 for s in subs}
    per_sub[subs[0]] = 2312 - 240 * (len(subs) - 1)

    class _CeilingClient:
        """Path shape: /{sale}/{cat}[/{subtype}]/{kraj}/ — the segment BETWEEN the
        category and the kraj is the subtype (and only that segment)."""

        def __init__(self) -> None:
            self.urls: list[str] = []

        @staticmethod
        def _parts(url):  # noqa: ANN001
            segs = url.split("?")[0].split("/prodej/rodinne-domy/")[1].strip("/")
            segs = segs.split("/") if segs else []
            return (segs[-2] if len(segs) == 2 else None), (segs[-1] if segs else None)

        def fetch_search(self, url):  # noqa: ANN001
            self.urls.append(url)
            sub, kraj = self._parts(url)
            pg = _page_num(url)
            if kraj != "stredocesky-kraj":
                total = 4 if sub else 40
            else:
                total = per_sub[sub] if sub else 2312
            first = (pg - 1) * 20
            n = max(0, min(20, total - first))
            base = 3_000_000 + 100_000 * (subs.index(sub) if sub else 99)
            ids = [str(base + 1_000 * KRAJ_SLUGS.index(kraj) + first + k)
                   for k in range(n)]
            last = max(1, -(-total // 20))
            return _page_html(total, ids, next_page=pg + 1 if pg < last else None,
                              heading=f"Domy {kraj}"), 200

        def fetch_index(self, sale_type, cat, page):  # noqa: ANN001
            return _page_html(2312 + 13 * 40, []), 200

    fake = _CeilingClient()
    monkeypatch.setattr(m, "CeskerealityClient", lambda **k: fake)
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    seen, _c, _t, _p, complete = portal.walk_category(
        {"sale_type": "prodej", "category": "rodinne-domy"},
        conn=None, dry_run=True, limiter=None,
    )

    # the over-ceiling kraj bailed at page 1 and was re-walked per subtype...
    stredo = [u for u in fake.urls if _kraj_of(u) == "stredocesky-kraj"]
    assert max(_page_num(u) for u in stredo) <= 99   # never asks for the 404
    for slug in subs:
        assert any(f"/{slug}/stredocesky-kraj/" in u for u in fake.urls), slug
    assert len(seen) == 2312 + 13 * 40
    assert complete is True


def test_a_descent_that_loses_the_residue_reports_incomplete(monkeypatch):
    """Self-verification: if the children's declared totals do not add back up to
    the parent's, the category reads incomplete rather than silently dropping the
    difference."""
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    parent = m.SliceResult("stredocesky-kraj", None, [], 2312, 1, "ceiling")

    class _ThinClient:
        def fetch_search(self, url):  # noqa: ANN001
            pg = _page_num(url)
            ids = [str(9_100_000 + pg * 20 + k) for k in range(20)]
            return _page_html(100, ids, next_page=pg + 1 if pg < 5 else None,
                              heading="Domy stredocesky-kraj"), 200

    monkeypatch.setattr(m, "SUBTYPE_SLUGS", {"rodinne-domy": ("vily",)})
    kids = portal._descend_slice(_ThinClient(), "prodej", "rodinne-domy", parent)
    assert any(k.outcome == "ceiling" for k in kids)   # residue surfaced
    assert not all(k.positive for k in kids)


# --- the deadline + the unmeasurable total ----------------------------------


class _ClockClient(_PartitionClient):
    """`trip_after` fetches, the fake monotonic clock jumps past the deadline."""

    def __init__(self, counts, clock: dict, trip_after: int) -> None:  # noqa: ANN001
        super().__init__(counts)
        self._clock = clock
        self._trip_after = trip_after

    def fetch_search(self, url):  # noqa: ANN001
        out = super().fetch_search(url)
        if len(self.urls) >= self._trip_after:
            self._clock["t"] = 9_999.0
        return out


def test_deadline_stops_walk_and_forces_incomplete(monkeypatch):
    """A walk cut short by the wall-clock budget must NEVER report complete=True —
    the kraje it never reached hold listings it never saw (rule #3)."""
    clock = {"t": 0.0}
    monkeypatch.setattr(
        m_portal, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
    fake = _ClockClient({k: 40 for k in KRAJ_SLUGS}, clock, trip_after=1)
    portal = m.CeskerealityPortal(default_config("ceskereality"))

    seen, _counts, _total, _pages, complete = _walk(
        portal, fake, monkeypatch, deadline=10.0)

    assert seen                             # rows collected before the stop are kept
    assert complete is False                # the deadline poisons the whole verdict
    assert {_kraj_of(u) for u in fake.urls if _kraj_of(u)} == {KRAJ_SLUGS[0]}


class _NoTotalClient:
    """Cards on the page but no "Máme tady N" and no H1 — unmeasurable, which is
    ceskereality's live failure mode and now reads degraded, not complete."""

    def __init__(self) -> None:
        self._n = 0

    def fetch_search(self, url):  # noqa: ANN001
        self._n += 1
        return _page_html(None, [str(8_000_000 + self._n)]), 200

    def fetch_index(self, sale_type, cat, page):  # noqa: ANN001
        raise RuntimeError("nationwide total unavailable")


def test_unmeasurable_total_is_unknown_not_complete(monkeypatch):
    """A full, un-deadlined walk whose totals are unreadable must still not
    authorise a sweep: an unmeasurable walk is 'unknown', never 'complete'
    (rule #3). The old fail-open said complete=True and delisted on a guess."""
    fake = _NoTotalClient()
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    _seen, _counts, total, _pages, complete = _walk(portal, fake, monkeypatch)

    assert total == 0                       # nothing measurable to reconcile against
    assert complete is False                # ...so coverage is unprovable


def test_walk_slice_reports_deadline_when_budget_already_spent(monkeypatch):
    """The page loop's own guard: budget spent -> no request at all."""
    monkeypatch.setattr(
        m_portal, "time", SimpleNamespace(monotonic=lambda: 100.0))
    fake = _PartitionClient({"praha": 400})
    r = m.CeskerealityPortal(default_config("ceskereality"))._walk_slice(
        fake, "prodej", "byty", "praha", deadline=1.0)
    assert fake.urls == []                  # not one request past the budget
    assert (r.rows, r.pages, r.outcome, r.positive) == ([], 0, "deadline", False)


def test_search_url_puts_subtype_before_kraj():
    # live-verified: /prodej/byty/byty-2-1/stredocesky-kraj/ is correctly filtered
    assert search_url("prodej", "byty", kraj="stredocesky-kraj", subtype="byty-2-1") == (
        "https://www.ceskereality.cz/prodej/byty/byty-2-1/stredocesky-kraj/")
    assert search_url("prodej", "byty", kraj="praha", page=93) == (
        "https://www.ceskereality.cz/prodej/byty/praha/?strana=93")


# --- cross-slice delisting sweep ('rodinne-domy' + 'chaty-chalupy' -> dum) ---

def _sweep_portal(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        m.db, "mark_inactive_native",
        lambda _c, src, cm, ct, seen, *, min_unseen_hours: calls.append(
            {"src": src, "cm": cm, "ct": ct, "seen": set(seen),
             "min_unseen_hours": min_unseen_hours}) or len(seen),
    )
    return m.CeskerealityPortal(default_config("ceskereality")), calls


def test_mark_inactive_sweeps_collapsing_group_once_with_union(monkeypatch):
    portal, calls = _sweep_portal(monkeypatch)
    # First dum slice buffers only — a sweep here would flip every chaty-chalupy
    # row (same (dum, pronajem), never in the rodinne-domy slice's seen set).
    assert portal.mark_inactive(
        object(), {"sale_type": "pronajem", "category": "rodinne-domy"},
        {"r1", "r2"}) == 0
    assert calls == []
    # The group's last complete slice sweeps with the UNION + the 24h rail.
    n = portal.mark_inactive(
        object(), {"sale_type": "pronajem", "category": "chaty-chalupy"}, {"c1"})
    assert n == 3
    assert calls == [{"src": "ceskereality", "cm": "dum", "ct": "pronajem",
                      "seen": {"r1", "r2", "c1"}, "min_unseen_hours": 12}]


def test_mark_inactive_missing_sibling_slice_suppresses_sweep(monkeypatch):
    # The runner only calls mark_inactive for COMPLETE slices; if rodinne-domy
    # walked incomplete/failed, chaty-chalupy alone must not sweep (dum, prodej).
    portal, calls = _sweep_portal(monkeypatch)
    assert portal.mark_inactive(
        object(), {"sale_type": "prodej", "category": "chaty-chalupy"}, {"c1"}) == 0
    assert calls == []


def test_mark_inactive_single_slice_group_sweeps_immediately(monkeypatch):
    portal, calls = _sweep_portal(monkeypatch)
    assert portal.mark_inactive(
        object(), {"sale_type": "prodej", "category": "byty"}, {"b1"}) == 1
    assert calls == [{"src": "ceskereality", "cm": "byt", "ct": "prodej",
                      "seen": {"b1"}, "min_unseen_hours": 12}]


# --- newest-first delta probe (/nejnovejsi/ on the www host) -----------------


def _priced_page_html(
    total: int | None, id_price_pairs: list[tuple[str, str]],
    next_page: int | None = None,
) -> str:
    cards = "".join(
        '<article class="i-estate">'
        f'<a class="i-estate__image-link" href="/prodej/byty/x/y-{i}.html"></a>'
        f'<div class="i-estate__footer-price-value">{p}</div>'
        "</article>"
        for i, p in id_price_pairs
    )
    meta = f'<meta name="description" content="Máme tady {total} bytů">' if total else ""
    pager = (
        f'<a class="pagination-arrow --next" href="/x/?strana={next_page}"></a>'
        if next_page else ""
    )
    return f"<html><head>{meta}</head><body>{cards}{pager}</body></html>"


class _NewestClient:
    """Scripted /nejnovejsi/ pages: page -> [(id, price_text)]."""

    def __init__(self, pages: dict[int, list[tuple[str, str]]], total: int = 8439) -> None:
        self.urls: list[str] = []
        self._pages = pages
        self._total = total

    def fetch_search(self, url):  # noqa: ANN001
        self.urls.append(url)
        pg = _page_num(url)
        pairs = self._pages.get(pg, [])
        return _priced_page_html(
            self._total, pairs, next_page=pg + 1 if pairs else None), 200


def test_probe_category_reads_nejnovejsi_on_www(monkeypatch):
    fake = _NewestClient({
        1: [("7000001", "3 200 000 Kč"), ("7000002", "4 100 000 Kč")],
        2: [("7000003", "2 900 000 Kč")],
    })
    monkeypatch.setattr(m, "CeskerealityClient", lambda **kw: fake)
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    seen, counts, total, pages, complete = portal.probe_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=None, dry_run=True, limiter=None, probe_pages=2,
    )
    # The verified URL shape: nationwide www host + the nejnovejsi sort slug in
    # search_url's sub_slug slot, ?strana=N for page 2+.
    assert fake.urls == [
        "https://www.ceskereality.cz/prodej/byty/nejnovejsi/",
        "https://www.ceskereality.cz/prodej/byty/nejnovejsi/?strana=2",
    ]
    assert pages == 2 and len(seen) == 3
    assert counts["found_new"] == 3
    assert total == 8439
    assert complete is False        # a probe can never justify a delisting sweep


def test_probe_category_early_stops_on_all_known_page(monkeypatch):
    fake = _NewestClient({
        1: [("7000001", "3 200 000 Kč"), ("7000002", "4 100 000 Kč")],
        2: [("7000003", "2 900 000 Kč")],
    })
    monkeypatch.setattr(m, "CeskerealityClient", lambda **kw: fake)
    stored = {
        "7000001": {"id": 51, "sreality_id": -1, "price_czk": 3_200_000, "last_seen_at": None},
        "7000002": {"id": 52, "sreality_id": -2, "price_czk": 4_100_000, "last_seen_at": None},
    }
    touched: list[int] = []
    enqueued: list[tuple] = []
    monkeypatch.setattr(
        m.db, "index_summary_native",
        lambda _c, src, ids: {i: stored[i] for i in ids if i in stored})
    monkeypatch.setattr(
        m.db, "touch_listings_by_id", lambda _c, pks: touched.extend(pks) or len(pks))
    monkeypatch.setattr(
        m.db, "enqueue_detail", lambda _c, src, entries: enqueued.extend(entries) or len(entries))
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    _seen, counts, _total, pages, _complete = portal.probe_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=object(), dry_run=False, limiter=None, probe_pages=3,
    )
    assert pages == 1               # page 1 all-known -> never fetched page 2
    assert len(fake.urls) == 1
    assert counts["found_new"] == 0
    assert enqueued == []           # unchanged prices -> nothing enqueued
    assert sorted(touched) == [51, 52]   # but last_seen was bumped (by surrogate id)


def test_mark_gone_flips_native_inactive(monkeypatch):
    # Gate 2: the gone-flip keys on the native id (mark_listing_inactive_native),
    # NOT a sreality_id resolved out of the DB — a post-Gate-2 ceskereality row has
    # sreality_id = NULL, so the legacy sreality_id-keyed flip would silently no-op.
    captured: dict = {}
    monkeypatch.setattr(
        m.db, "mark_listing_inactive_native",
        lambda _c, source, nid: captured.update(source=source, nid=nid),
    )
    monkeypatch.setattr(
        m.db, "mark_listing_inactive",
        lambda *a, **k: pytest.fail("legacy sreality_id-keyed gone-flip must not be used"),
    )
    m.CeskerealityPortal(default_config("ceskereality")).mark_gone(object(), "7000009")
    assert captured == {"source": "ceskereality", "nid": "7000009"}


def test_probe_category_enqueues_new_and_changed_with_priorities(monkeypatch):
    fake = _NewestClient({
        1: [("7000001", "3 200 000 Kč"), ("7000002", "4 100 000 Kč")],
    })
    monkeypatch.setattr(m, "CeskerealityClient", lambda **kw: fake)
    stored = {  # 7000002 known at an OLD price -> changed; 7000001 unknown -> new
        "7000002": {"sreality_id": -2, "price_czk": 3_900_000, "last_seen_at": None},
    }
    enqueued: list[tuple] = []
    monkeypatch.setattr(
        m.db, "index_summary_native",
        lambda _c, src, ids: {i: stored[i] for i in ids if i in stored})
    monkeypatch.setattr(m.db, "touch_listings", lambda _c, pks: len(pks))
    monkeypatch.setattr(
        m.db, "enqueue_detail", lambda _c, src, entries: enqueued.extend(entries) or len(entries))
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    _seen, counts, _total, _pages, _complete = portal.probe_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=object(), dry_run=False, limiter=None, probe_pages=1,
    )
    assert counts["found_new"] == 1
    by_id = {e[0]: e for e in enqueued}
    assert by_id["7000001"][3] == m.db.QUEUE_PRIORITY_NEW
    assert by_id["7000002"][3] == m.db.QUEUE_PRIORITY_CHANGED
    assert by_id["7000002"][2] == 4_100_000     # refreshed observed price
    # detail_ref is the absolute detail URL the drain fetches
    assert by_id["7000001"][1].startswith("https://www.ceskereality.cz/")


def test_client_routes_through_proxy_when_env_set(monkeypatch):
    monkeypatch.setenv("SCRAPER_PROXY_URL", "http://u:p@gw.example.com:823")
    c = CeskerealityClient()
    assert c._session.proxies.get("https") == "http://u:p@gw.example.com:823"


def test_client_no_proxy_when_env_unset(monkeypatch):
    monkeypatch.delenv("SCRAPER_PROXY_URL", raising=False)
    c = CeskerealityClient()
    assert not c._session.proxies            # falls back to the direct IP


# --- the laundered kraj: a throttled slice must not read as an empty one -----
#
# Found by adversarial review of the first cut of this walk, and REPRODUCED: a
# throttled page renders the shell with a correct H1, zero cards and no count
# phrase — byte-for-byte the shape of a genuinely empty kraj, because the count
# comes from the same query as the cards and vanishes with them. With the
# national cross-check written fail-open, that produced complete=True while a
# whole kraj was missing: 5,200 of 5,600 rows collected, and the walk claimed a
# clean sweep. Throttling is correlated, so the national probe is degraded at
# exactly the moment the slices are.


class _LaunderedKrajClient(_PartitionClient):
    """One kraj is throttled: it answers 200 with a correct H1 and no results,
    exactly as a real empty kraj does. Every other kraj is healthy."""

    def __init__(self, counts, throttled: str, national=None, stay_empty=True):
        super().__init__(counts, national=national)
        self._throttled = throttled
        self._stay_empty = stay_empty
        self.rereads = 0

    def fetch_search(self, url):  # noqa: ANN001
        kraj = _kraj_of(url)
        if kraj == self._throttled:
            self.urls.append(url)
            if not self._stay_empty and self.rereads:
                # A transient throttle: the re-read succeeds and the real page
                # comes back. The walk must NOT have called it empty.
                return super().fetch_search(url)
            self.rereads += 1
            return _page_html(None, [], heading=f"Prodej bytů {kraj}"), 200
        return super().fetch_search(url)


def test_a_throttled_kraj_cannot_launder_itself_into_an_empty_one(monkeypatch):
    counts = {k: 400 for k in KRAJ_SLUGS}
    fake = _LaunderedKrajClient(counts, throttled="zlinsky-kraj")
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    _seen, _c, _t, _pages, complete = _walk(portal, fake, monkeypatch)
    assert complete is False, (
        "a throttled kraj was accepted as empty - the exact path that "
        "reproduced complete=True with a whole kraj missing"
    )


def test_an_empty_slice_is_confirmed_by_a_second_read(monkeypatch):
    """The mechanism: a genuinely empty kraj is stable across two reads, a
    throttle is not. One extra request, only for one-page slices."""
    counts = {k: 400 for k in KRAJ_SLUGS}
    counts["zlinsky-kraj"] = 0
    fake = _PartitionClient(counts)
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    _seen, _c, _t, _pages, complete = _walk(portal, fake, monkeypatch)
    assert complete is True
    empty_reads = [u for u in fake.urls if "zlinsky-kraj" in u]
    assert len(empty_reads) >= 2, "the empty slice was accepted on a single read"


def test_an_unmeasurable_national_probe_cannot_prove_completeness(monkeypatch):
    """The cross-check read `national is None or ...`, so a FAILED probe asserted
    completeness. Throttling is correlated - the national probe degrades at
    exactly the moment the slices do, so the rail was weakest when needed."""
    counts = {k: 400 for k in KRAJ_SLUGS}
    fake = _PartitionClient(counts)
    monkeypatch.setattr(
        m.CeskerealityPortal, "_nationwide_total", lambda *a, **k: None)
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    _seen, _c, _t, _pages, complete = _walk(portal, fake, monkeypatch)
    assert complete is False
