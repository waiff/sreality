"""The ceskereality 14-kraj index partition + the opt-in residential proxy.

What this file used to test — a 7-subdomain × rendered-facet fan-out capped at 12
pages — was the defect, not the contract: the facet block is a top-10-by-popularity
list, so whole okresy were never visited and the walk collected 7,566 of a declared
8,828 while the completeness gate suppressed every delisting sweep. The 12-page cap
turned out to belong to UNFILTERED category URLs only. The walk now partitions on
the 14 DECLARED kraje, pages each slice to its own declared tail (up to the site's
real 99-page ceiling on a filtered URL), and descends onto the subtype axis when a
kraj needs more than that.

The 5th element of walk_category is STRUCTURAL since 2026-09-08 (rule #3): it says
the walk reached ceskereality's OWN end — every kraj stopped on the pager, on the
region's declared tail, or on a confirmed-empty region — not that the counts
reconciled. A region one row short of its declared count still reaches the end;
a barren page, an error, a cap or the deadline still does not. The arithmetic is
unchanged and still decides the slice ledger's `outcome` and the subtype descent.
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
    # The site's real last page still renders the arrow, carrying `--disabled` —
    # that marker is what says "there is no more". A page with NO pagination block
    # is a truncated body, and the walk must not read the two alike.
    pager = (
        f'<a class="pagination-arrow --next" href="/x/?strana={next_page}"></a>'
        if next_page
        else '<a class="pagination-arrow --disabled --next" href="/x/?strana=1"></a>'
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
    assert (empty.stop, empty.reached_end) == ("empty_confirmed", True)


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
    # An items-less 200 nobody could corroborate is OURS, never the portal's end.
    assert (bad.stop, bad.reached_end) == ("barren", False)
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
    # Items-first: the blank page has no next arrow either, and reading it as the
    # last page is exactly how a soft block would become a nomination.
    assert (r.stop, r.reached_end) == ("barren", False)
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
    assert (r.stop, r.reached_end) == ("error", False)
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


def test_union_short_of_the_national_total_is_still_complete(monkeypatch):
    """Rule #3 since 2026-09-07: the verdict is per region, against each
    region's own count. The nationwide total includes listings filed under no
    region (foreign flats in the domestic tree, Czech flats with no kraj), so
    the kraj union can never reach it -- demanding it parked rentals for good.
    Those region-less rows are nominated for a page check on every walk
    instead. The national number is still reported as the result size, so the
    RECONCILE line keeps showing the gap."""
    fake = _PartitionClient({k: 40 for k in KRAJ_SLUGS}, national=5000)
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    _seen, _c, total, _p, complete = _walk(portal, fake, monkeypatch)
    assert total == 5000
    assert complete is True


# --- the structural verdict: the walk reached ceskereality's end -------------
#
# Rule #3, 2026-09-08. What the 5th element answers is "did every kraj stop
# because the portal said there was no more?", not "did the counts reconcile".
# The two came apart in production: prodej/rodinne-domy is 20,964 rows over 14
# regions, one of which (karlovarsky, 87 rows) came back one row short on every
# walk — and that single row held the whole category's nomination shut for two
# days while 20,963 rows sat unchecked.


class _ShortByOneClient(_PartitionClient):
    """One kraj declares 87 and serves 86: the tail moved under the walk. Every
    page is real, the pager runs out on the declared last page."""

    def __init__(self, counts, short_kraj: str) -> None:  # noqa: ANN001
        super().__init__(counts)
        self._short = short_kraj

    def fetch_search(self, url):  # noqa: ANN001
        kraj = _kraj_of(url)
        if kraj != self._short:
            return super().fetch_search(url)
        self.urls.append(url)
        total = self._counts[kraj]
        pg, last = _page_num(url), max(1, -(-self._counts[kraj] // 20))
        first = (pg - 1) * 20
        n = max(0, min(20, total - first)) - (1 if pg == last else 0)
        ids = [str(2_000_000 + first + k) for k in range(n)]
        return _page_html(total, ids, next_page=pg + 1 if pg < last else None,
                          heading=f"Prodej bytu {kraj}"), 200


def test_a_slice_one_row_short_of_its_declared_total_still_reaches_end(monkeypatch):
    """86 of 87 = 0.9885, under the 0.995 the arithmetic gate demanded. The slice
    walked every page it has and the pager ran out: that is a FINISHED walk over a
    live index, so it reaches the portal's end and the category nominates. The
    ledger still records the shortfall as `degraded` — the number did not change,
    only what it is allowed to veto."""
    counts = {k: 40 for k in KRAJ_SLUGS}
    counts["karlovarsky-kraj"] = 87
    fake = _ShortByOneClient(counts, "karlovarsky-kraj")
    portal = m.CeskerealityPortal(default_config("ceskereality"))

    short = portal._walk_slice(fake, "prodej", "byty", "karlovarsky-kraj")
    assert len({r[0] for r in short.rows}) == 86
    assert short.outcome == "degraded"       # the numeric verdict is unchanged...
    assert short.positive is False           # ...and still numeric in the ledger
    assert m_portal.walk_is_complete(86, 87) is False
    assert short.stop == "pager_end"         # ...but the PORTAL ended the loop
    assert short.reached_end is True

    _seen, _c, _t, _p, complete = _walk(portal, fake, monkeypatch)
    assert complete is True                  # 20,963 rows are no longer hostage


class _EarlyPagerClient(_PartitionClient):
    """A kraj whose next arrow goes disabled at page 2 of a declared 5 pages."""

    def fetch_search(self, url):  # noqa: ANN001
        html, status = super().fetch_search(url)
        if _kraj_of(url) == "praha" and _page_num(url) == 2:
            html = re.sub(
                r'<a class="pagination-arrow --next".*?</a>',
                '<a class="pagination-arrow --disabled --next" href="/x/?strana=2"></a>',
                html,
            )
        return html, status


class _PagerlessPageClient(_PartitionClient):
    """A kraj whose page 2 comes back with its cards but NO pagination block at all
    — the truncated body / edge shell an over-eager CDN serves."""

    def fetch_search(self, url):  # noqa: ANN001
        html, status = super().fetch_search(url)
        if _kraj_of(url) == "praha" and _page_num(url) == 2:
            html = re.sub(r'<a class="pagination-arrow[^>]*>.*?</a>', "", html)
        return html, status


def test_a_page_with_no_pagination_block_is_not_an_end(monkeypatch):
    """`_next_page` returns None for the site's own disabled arrow AND for a page
    that renders no pagination at all. Only the first is ceskereality saying "there
    is no more"; treating absence alike let a shortened Cloudflare body carrying one
    card end a slice at page 2 of 5 and nominate the rest of the category."""
    counts = {k: 40 for k in KRAJ_SLUGS}
    counts["praha"] = 100
    fake = _PagerlessPageClient(counts)
    portal = m.CeskerealityPortal(default_config("ceskereality"))

    r = portal._walk_slice(fake, "prodej", "byty", "praha")
    assert (r.stop, r.reached_end) == ("barren", False)
    _seen, _c, _t, _p, complete = _walk(portal, fake, monkeypatch)
    assert complete is False


def test_a_pager_that_ends_before_the_declared_tail_is_the_portals_end(monkeypatch):
    """The disabled next arrow is the site's own last-page signal, and it means
    the same thing wherever it appears. Reading an early one as a truncation was
    the second numeric veto, and it fired on exactly the live-tail churn the
    declared count cannot keep up with."""
    counts = {k: 40 for k in KRAJ_SLUGS}
    counts["praha"] = 100
    fake = _EarlyPagerClient(counts)
    portal = m.CeskerealityPortal(default_config("ceskereality"))

    r = portal._walk_slice(fake, "prodej", "byty", "praha")
    assert r.pages == 2 and len(r.rows) == 40
    assert (r.stop, r.reached_end) == ("pager_end", True)
    assert r.outcome == "degraded"           # 40 of 100 is on the record
    _seen, _c, _t, _p, complete = _walk(portal, fake, monkeypatch)
    assert complete is True


def test_a_full_slice_stops_on_the_portals_own_count(monkeypatch):
    fake = _PartitionClient({k: 40 for k in KRAJ_SLUGS})
    r = m.CeskerealityPortal(default_config("ceskereality"))._walk_slice(
        fake, "prodej", "byty", "praha")
    # page 2 of 2: the count says the tail and the arrow is gone; either way the
    # portal ended it, and both are in PORTAL_ENDS.
    assert r.stop in m_portal.PORTAL_ENDS
    assert (r.outcome, r.reached_end) == ("exhausted", True)


def test_a_barren_first_page_that_does_not_confirm_is_our_stop(monkeypatch):
    """The transient throttle: the shell comes back once, the re-read returns the
    real page. An items-less 200 nobody could corroborate is `barren` — OURS — so
    the category cannot nominate this walk. It costs one walk, not a region."""
    counts = {k: 400 for k in KRAJ_SLUGS}
    fake = _LaunderedKrajClient(counts, throttled="zlinsky-kraj", stay_empty=False)
    portal = m.CeskerealityPortal(default_config("ceskereality"))

    r = portal._walk_slice(fake, "prodej", "byty", "zlinsky-kraj")
    assert (r.stop, r.reached_end, r.outcome) == ("barren", False, "degraded")
    assert fake.rereads == 1                 # exactly one re-read, not a loop

    fresh = _LaunderedKrajClient(counts, throttled="zlinsky-kraj", stay_empty=False)
    _seen, _c, _t, _p, complete = _walk(portal, fresh, monkeypatch)
    assert complete is False


def test_max_pages_is_our_stop_even_when_every_slice_finishes(monkeypatch):
    """--max-pages is ours by definition: the slices under it stop where WE said,
    not where the portal did, so a capped walk may never nominate."""
    fake = _PartitionClient({k: 40 for k in KRAJ_SLUGS})
    portal = m.CeskerealityPortal(default_config("ceskereality"), max_pages=1)
    _seen, _c, _t, _p, complete = _walk(portal, fake, monkeypatch)
    assert complete is False
    r = portal._walk_slice(fake, "prodej", "byty", "praha")
    assert (r.stop, r.reached_end, r.outcome) == ("page_cap", False, "ceiling")


def test_a_category_whose_slices_all_failed_to_start_never_reaches_the_end(monkeypatch):
    """all([]) is True: seeding the stop list from the slices that RAN rather than
    from the declared 14 would make a walk that collected nothing report the
    portal's end."""
    class _AllBoomClient(_PartitionClient):
        def fetch_search(self, url):  # noqa: ANN001
            raise RuntimeError("blocked")

    fake = _AllBoomClient({k: 40 for k in KRAJ_SLUGS})
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    seen, _c, _t, _p, complete = _walk(portal, fake, monkeypatch)
    assert seen == set()
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
    parent = m.SliceResult(
        "stredocesky-kraj", None, [], 2312, 1, "ceiling", "page_cap")

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
    # The residue child carries the parent's stop, so the category cannot nominate
    # while a slice of it is unreachable.
    assert not all(k.reached_end for k in kids)


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
    """A page with cards but no count is a BROKEN page, not a tail: the count
    renders from the same query as the cards, so one without the other is the
    throttle signature. Unmeasurable is never the portal's end (rule #3); the old
    fail-open said complete=True and delisted on a guess."""
    fake = _NoTotalClient()
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    _seen, _counts, total, _pages, complete = _walk(portal, fake, monkeypatch)

    assert total == 0                       # nothing measurable to reconcile against
    assert complete is False                # ...so the walk cannot nominate
    r = m.CeskerealityPortal(default_config("ceskereality"))._walk_slice(
        fake, "prodej", "byty", "praha")
    assert (r.stop, r.reached_end) == ("error", False)


def test_walk_slice_reports_deadline_when_budget_already_spent(monkeypatch):
    """The page loop's own guard: budget spent -> no request at all."""
    monkeypatch.setattr(
        m_portal, "time", SimpleNamespace(monotonic=lambda: 100.0))
    fake = _PartitionClient({"praha": 400})
    r = m.CeskerealityPortal(default_config("ceskereality"))._walk_slice(
        fake, "prodej", "byty", "praha", deadline=1.0)
    assert fake.urls == []                  # not one request past the budget
    assert (r.rows, r.pages, r.outcome, r.positive) == ([], 0, "deadline", False)
    assert (r.stop, r.reached_end) == ("deadline", False)


def test_search_url_puts_subtype_before_kraj():
    # live-verified: /prodej/byty/byty-2-1/stredocesky-kraj/ is correctly filtered
    assert search_url("prodej", "byty", kraj="stredocesky-kraj", subtype="byty-2-1") == (
        "https://www.ceskereality.cz/prodej/byty/byty-2-1/stredocesky-kraj/")
    assert search_url("prodej", "byty", kraj="praha", page=93) == (
        "https://www.ceskereality.cz/prodej/byty/praha/?strana=93")


# --- cross-slice nomination ('rodinne-domy' + 'chaty-chalupy' -> dum) --------------------------
#
# Rule #3 since 2026-09-07: a complete walk nominates its unseen rows for a page
# check, it does not delist. Sibling slices that collapse onto one canonical
# category still have to be buffered, because the runner calls the seam per
# slice and one slice's seen set would nominate the sibling's whole population.

def _nominating_portal(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        m.db, "presence_candidates",
        lambda _c, src, cm, ct, seen, **kw: calls.append(
            {"src": src, "cm": cm, "ct": ct, "seen": set(seen), **kw}) or ([], len(seen)),
    )
    return m.CeskerealityPortal(default_config("ceskereality")), calls


def test_nomination_buffers_the_collapsing_group_and_nominates_once_with_union(monkeypatch):
    portal, calls = _nominating_portal(monkeypatch)
    # First dum slice buffers only: nominating here would send every chaty-chalupy row
    # (same (dum, pronajem), never in the rodinne-domy slice's seen set) to the drain.
    assert portal.presence_candidates(
        object(), {"sale_type": "pronajem", "category": "rodinne-domy"}, {"r1", "r2"}) is None
    assert calls == []
    # The group's last complete slice nominates with the UNION.
    out = portal.presence_candidates(
        object(), {"sale_type": "pronajem", "category": "chaty-chalupy"}, {"c1"})
    assert out == ([], 3)
    assert calls == [{"src": "ceskereality", "cm": "dum", "ct": "pronajem", "seen": {"r1", "r2", "c1"}}]


def test_nomination_missing_sibling_slice_nominates_nothing(monkeypatch):
    # The runner only reaches this seam for COMPLETE slices; if rodinne-domy walked
    # incomplete or failed, chaty-chalupy alone must not nominate (dum, prodej).
    portal, calls = _nominating_portal(monkeypatch)
    assert portal.presence_candidates(
        object(), {"sale_type": "prodej", "category": "chaty-chalupy"}, {"c1"}) is None
    assert calls == []


def test_nomination_single_slice_group_nominates_immediately(monkeypatch):
    portal, calls = _nominating_portal(monkeypatch)
    assert portal.presence_candidates(
        object(), {"sale_type": "prodej", "category": "byty"}, {"b1"}) == ([], 1)
    assert calls == [{"src": "ceskereality", "cm": "byt", "ct": "prodej", "seen": {"b1"}}]


def test_the_old_sweep_seam_is_gone():
    """No portal may flip rows from index absence any more; the runner never
    calls mark_inactive and the seam must not linger to tempt anyone."""
    assert not hasattr(m.CeskerealityPortal, "mark_inactive")


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
    # The site's real last page still renders the arrow, carrying `--disabled` —
    # that marker is what says "there is no more". A page with NO pagination block
    # is a truncated body, and the walk must not read the two alike.
    pager = (
        f'<a class="pagination-arrow --next" href="/x/?strana={next_page}"></a>'
        if next_page
        else '<a class="pagination-arrow --disabled --next" href="/x/?strana=1"></a>'
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


def test_a_throttled_kraj_laundered_as_empty_costs_fetches_not_listings(monkeypatch):
    """A throttled kraj that renders an empty shell twice reads as an empty
    region, and the national cross-check that used to catch it is gone from
    the verdict (it made rentals impossible to complete). What protects the
    region's rows now is the rule itself: a complete walk NOMINATES, the page
    decides. The 400 unseen rows get a page check, come back alive, and are
    refreshed; nothing is deleted. The walk still reports the national total
    as its result size, so the shortfall stays visible on the RECONCILE line."""
    counts = {k: 400 for k in KRAJ_SLUGS}
    fake = _LaunderedKrajClient(counts, throttled="zlinsky-kraj")
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    seen, _c, total, _pages, complete = _walk(portal, fake, monkeypatch)
    assert complete is True
    assert total == 5600 and len(seen) == 5200      # the gap is on the record


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
    empty = portal._walk_slice(fake, "prodej", "byty", "zlinsky-kraj")
    assert (empty.stop, empty.reached_end) == ("empty_confirmed", True)


def test_an_unmeasurable_national_probe_no_longer_decides(monkeypatch):
    """The national probe is observability, not verdict: fourteen exhausted
    regions make the category complete whether or not the national page could
    be read, and the result size falls back to the regions' declared sum."""
    counts = {k: 400 for k in KRAJ_SLUGS}
    fake = _PartitionClient(counts)
    monkeypatch.setattr(
        m.CeskerealityPortal, "_nationwide_total", lambda *a, **k: None)
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    _seen, _c, total, _pages, complete = _walk(portal, fake, monkeypatch)
    assert complete is True
    assert total == 5600                            # declared_sum, not None


# --- the slice ledger (migration 454) ----------------------------------------
#
# ceskereality has been parked since migration 449 and, until this, had NO WAY
# BACK: the coverage gate un-parks a portal on ledger evidence, and this walk
# wrote none. The gate reported "no slice ledger for this portal" every cycle,
# forever.


def _portal_for_ledger():
    return m.CeskerealityPortal(default_config("ceskereality"))


def _record(monkeypatch, results, *, kraje=("praha", "stredocesky-kraj")):
    written: list[dict] = []
    monkeypatch.setattr(
        m.db, "record_index_slice",
        lambda _conn, **kw: written.append(kw))
    _portal_for_ledger()._record_slices(
        object(), {"sale_type": "prodej", "category": "byty"},
        list(kraje), results, False)
    return written


def _sr(kraj, outcome, *, subtype=None, declared=100, ids=("a",), pages=4,
        stop="declared_total_reached"):
    return m.SliceResult(
        kraj=kraj, subtype=subtype,
        rows=[(i, f"https://x/{i}", None) for i in ids],
        declared_total=declared, pages=pages, outcome=outcome, stop=stop)


def test_one_row_per_kraj_not_per_subtype(monkeypatch):
    """The aggregation that keeps the ledger's key set stable. A kraj past the
    page ceiling is re-walked per subtype and comes back as several results; if
    those were recorded individually, the `kraj/subtype-*` rows would linger the
    first time that kraj stopped needing the descent — never re-walked, ageing
    forever, and one permanently stale row holds the portal parked for good."""
    written = _record(monkeypatch, [
        _sr("praha", "exhausted", subtype="byty-2-1", declared=60, ids=("a", "b")),
        _sr("praha", "exhausted", subtype="byty-3-1", declared=40, ids=("c",)),
        _sr("stredocesky-kraj", "exhausted", declared=100, ids=("d",)),
    ])
    assert [w["slice_key"] for w in written] == ["praha", "stredocesky-kraj"]
    praha = written[0]
    assert praha["declared_total"] == 100          # children partition the parent
    assert praha["collected"] == 3                 # union of their rows
    assert praha["pages"] == 8


def test_a_kraj_is_exhausted_only_if_every_part_is(monkeypatch):
    """Fourteen good subtypes and one that failed is not 93% of a kraj — it is a
    kraj with a hole, and mark_inactive would read the hole as 'these are gone'."""
    written = _record(monkeypatch, [
        _sr("praha", "exhausted", subtype="byty-2-1"),
        _sr("praha", "ceiling", subtype="byty-3-1"),
    ])
    assert len(written) == 1
    assert written[0]["outcome"] == "ceiling"


def test_a_kraj_the_deadline_never_reached_is_not_written(monkeypatch):
    """It must keep its OLD timestamp so it sorts first next run. Writing a fresh
    row for a kraj we never walked would make the stalest slice look the newest —
    the exact inversion db.slice_staleness treats absence as infinity to avoid."""
    written = _record(monkeypatch, [_sr("praha", "exhausted")],
                      kraje=("praha", "stredocesky-kraj", "jihocesky-kraj"))
    assert [w["slice_key"] for w in written] == ["praha"]


def test_a_dry_run_writes_nothing(monkeypatch):
    written: list[dict] = []
    monkeypatch.setattr(
        m.db, "record_index_slice",
        lambda _conn, **kw: written.append(kw))
    _portal_for_ledger()._record_slices(
        None, {"sale_type": "prodej", "category": "byty"},
        ["praha"], [_sr("praha", "exhausted")], False)
    assert written == []
