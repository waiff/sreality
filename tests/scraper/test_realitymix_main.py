"""Hermetic tests for RealitymixPortal.walk_category — the total-driven paging
(drive ?stranka to ceil(total/PER_PAGE), don't trust a pager arrow) and the
barren-page retry (the lesson from ceskereality's reverted #637). No network/DB:
a fake client feeds canned index HTML and conn=None skips the DB writes.
"""

from __future__ import annotations

from dataclasses import dataclass

from scraper import realitymix_main
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
    so a page can return barren first, then populated on retry)."""

    def __init__(self, pages: dict[int, list[str]], **_kw):
        self.pages = {k: list(v) for k, v in pages.items()}
        self.requested: list[int] = []

    def fetch_index(self, sale_type: str, category: str, page: int):
        self.requested.append(page)
        seq = self.pages.get(page) or [_index_html(None, [])]
        html = seq.pop(0) if len(seq) > 1 else seq[0]
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


def test_walk_drives_by_total_and_stops_at_last_page(monkeypatch):
    p1 = [str(1000 + i) for i in range(20)]
    p2 = [str(2000 + i) for i in range(5)]   # total 25 -> last page = ceil(25/20) = 2
    fake, (seen, counts, total, pages, complete) = _walk(
        monkeypatch, {1: [_index_html(25, p1)], 2: [_index_html(25, p2)]}
    )
    assert total == 25
    assert len(seen) == 25
    assert fake.requested == [1, 2]          # did NOT over-fetch a page 3
    assert complete is True


def test_walk_retries_a_barren_page_before_concluding_end(monkeypatch):
    p1 = [str(1000 + i) for i in range(20)]
    p2 = [str(2000 + i) for i in range(5)]
    # Page 2 returns EMPTY first (a transient throttle/degrade), then its items.
    fake, (seen, _counts, total, _pages, complete) = _walk(
        monkeypatch,
        {1: [_index_html(25, p1)], 2: [_index_html(25, []), _index_html(25, p2)]},
    )
    assert len(seen) == 25                    # the retry recovered the tail page
    assert fake.requested == [1, 2, 2]        # page 2 fetched twice (retry)
    assert complete is True


def test_walk_single_page(monkeypatch):
    ids = [str(3000 + i) for i in range(7)]   # total 7 -> last page 1
    fake, (seen, _counts, total, _pages, complete) = _walk(
        monkeypatch, {1: [_index_html(7, ids)]}
    )
    assert total == 7
    assert len(seen) == 7
    assert fake.requested == [1]
    assert complete is True


def test_max_pages_caps_walk_and_suppresses_completeness(monkeypatch):
    p1 = [str(1000 + i) for i in range(20)]
    p2 = [str(2000 + i) for i in range(20)]
    fake = FakeClient({1: [_index_html(100, p1)], 2: [_index_html(100, p2)]})
    monkeypatch.setattr(realitymix_main, "RealitymixClient", lambda limiter=None: fake)
    portal = RealitymixPortal(default_config("realitymix"), max_pages=1)
    seen, _counts, total, pages, complete = portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=None, dry_run=True, limiter=None,
    )
    assert fake.requested == [1]              # capped at one page
    assert len(seen) == 20
    assert complete is False                  # a capped walk never drives mark_inactive


def test_category_labels():
    portal = RealitymixPortal(default_config("realitymix"))
    assert portal.category_labels({"sale_type": "prodej", "category": "byty"}) == ("byt", "prodej")
    assert portal.category_labels({"sale_type": "pronajem", "category": "komerce"}) == ("komercni", "pronajem")
    assert portal.category_labels({"sale_type": "prodej", "category": "chaty"}) == ("dum", "prodej")


# --- cross-slice delisting sweep ('domy' + 'chaty' both collapse onto dum) ---

def _sweep_portal(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        realitymix_main.db, "mark_inactive_native",
        lambda _c, src, cm, ct, seen, *, min_unseen_hours: calls.append(
            {"src": src, "cm": cm, "ct": ct, "seen": set(seen),
             "min_unseen_hours": min_unseen_hours}) or len(seen),
    )
    return RealitymixPortal(default_config("realitymix")), calls


def test_mark_inactive_sweeps_collapsing_group_once_with_union(monkeypatch):
    portal, calls = _sweep_portal(monkeypatch)
    # First dum slice buffers only — a sweep here would flip every chaty row
    # (they share (dum, prodej) but are never in the domy slice's seen set).
    assert portal.mark_inactive(
        object(), {"sale_type": "prodej", "category": "domy"}, {"d1", "d2"}) == 0
    assert calls == []
    # The group's last complete slice sweeps with the UNION + the 24h rail.
    n = portal.mark_inactive(
        object(), {"sale_type": "prodej", "category": "chaty"}, {"c1"})
    assert n == 3
    assert calls == [{"src": "realitymix", "cm": "dum", "ct": "prodej",
                      "seen": {"d1", "d2", "c1"}, "min_unseen_hours": 12}]


def test_mark_inactive_missing_sibling_slice_suppresses_sweep(monkeypatch):
    # The runner only calls mark_inactive for COMPLETE slices; if the domy walk
    # was incomplete/failed, the chaty slice alone must not sweep (dum, prodej).
    portal, calls = _sweep_portal(monkeypatch)
    assert portal.mark_inactive(
        object(), {"sale_type": "prodej", "category": "chaty"}, {"c1"}) == 0
    assert calls == []


def test_mark_inactive_groups_are_sale_type_scoped(monkeypatch):
    # domy/prodej + chaty/pronajem are DIFFERENT (cm, ct) groups — neither
    # completes its own group, so neither sweeps.
    portal, calls = _sweep_portal(monkeypatch)
    assert portal.mark_inactive(
        object(), {"sale_type": "prodej", "category": "domy"}, {"d1"}) == 0
    assert portal.mark_inactive(
        object(), {"sale_type": "pronajem", "category": "chaty"}, {"c1"}) == 0
    assert calls == []


def test_mark_inactive_single_slice_group_sweeps_immediately(monkeypatch):
    portal, calls = _sweep_portal(monkeypatch)
    assert portal.mark_inactive(
        object(), {"sale_type": "prodej", "category": "byty"}, {"b1"}) == 1
    assert calls == [{"src": "realitymix", "cm": "byt", "ct": "prodej",
                      "seen": {"b1"}, "min_unseen_hours": 12}]


# --- shared-resolver wiring (behavior unit-tested in test_location.py) ------

def test_fill_coords_page_wins_then_carry_forward_then_geocode():
    geo_calls: list[str] = []

    def geocoder(q):
        geo_calls.append(q)
        return FakeGeo(50.0, 14.0, "medium", "regional.street")

    portal = RealitymixPortal(default_config("realitymix"))
    portal._coords._geocoder = geocoder
    portal._coords._have_geom = {"77": (48.5, 16.2)}

    # 1. page coords win — untouched, no geocode
    page = _listing(source_id_native="1", lat=49.9, lon=14.1, locality="X")
    assert portal._coords.fill("1", page) is page

    # 2. carry-forward — stored geom used, NO geocode (the footgun gate), and the
    #    provenance is stamped 'carry_forward' (stable across refetches, not None).
    carried = portal._coords.fill("77", _listing(source_id_native="77", locality="X"))
    assert (carried.lat, carried.lon) == (48.5, 16.2)
    assert carried.raw["coords"] == {"source": "carry_forward"}
    assert geo_calls == []

    # 3. genuinely new + map-less — geocode once
    fresh = portal._coords.fill("99", _listing(source_id_native="99", locality="Lidicka, Ostrava"))
    assert (fresh.lat, fresh.lon) == (50.0, 14.0)
    assert geo_calls == ["Lidicka, Ostrava"]
