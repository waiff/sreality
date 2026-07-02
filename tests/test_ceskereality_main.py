"""The ceskereality region × dynamic-facet split (the cap beater) + the opt-in
residential proxy."""

from __future__ import annotations

import re

from scraper import ceskereality_main as m
from scraper.ceskereality_client import REGION_HOSTS, CeskerealityClient
from scraper.ceskereality_parser import extract_facet_slugs
from scraper.portal import default_config


def _page_html(
    total: int | None, ids: list[str], facets: tuple[str, ...] = (),
    next_page: int | None = None,
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
    return f"<html><head>{meta}</head><body>{cards}{facet_links}{pager}</body></html>"


def _page_num(url: str) -> int:
    mm = re.search(r"strana=(\d+)", url)
    return int(mm.group(1)) if mm else 1


def test_extract_facet_slugs_drops_pure_filters():
    html = (
        '<a href="/prodej/byty/byty-3-1/">x</a>'
        '<a href="/prodej/byty/kladno/">x</a>'
        '<a href="/prodej/byty/pouze-rk/">x</a>'      # pure filter -> dropped
        '<a href="/prodej/byty/bez-realitky/">x</a>'  # pure filter -> dropped
        '<a href="/prodej/byty/kladno/">dup</a>'      # de-duped
        '<a href="/prodej/rodinne-domy/vily/">x</a>'  # other category -> ignored
    )
    assert extract_facet_slugs(html, "prodej", "byty") == ["byty-3-1", "kladno"]


class _FacetClient:
    """A region's bare page advertises two district facets (kladno, beroun); each
    district slice returns its own listings. Unique ids per page-1 fetch."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self._n = 0

    def _nid(self) -> str:
        self._n += 1
        return str(4_000_000 + self._n)

    def fetch_search(self, url):  # noqa: ANN001
        self.urls.append(url)
        if "strana=" in url:
            return _page_html(50, []), 200            # page 2 -> empty, slice ends
        if "/kladno/" in url:
            return _page_html(50, [self._nid(), self._nid()]), 200
        if "/beroun/" in url:
            return _page_html(50, [self._nid()]), 200
        # the bare region page: advertises the facets + one region-wide listing
        return _page_html(50, [self._nid()], facets=("kladno", "beroun")), 200

    def fetch_index(self, sale_type, cat, page):  # nationwide total  # noqa: ANN001
        return _page_html(50, ["9000001"]), 200


def test_walk_category_discovers_and_walks_facets(monkeypatch):
    fake = _FacetClient()
    monkeypatch.setattr(m, "CeskerealityClient", lambda **kw: fake)
    portal = m.CeskerealityPortal(
        default_config("ceskereality"), regions=("stredo.ceskereality.cz",))

    seen, _counts, _total, _pages, _complete = portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=None, dry_run=True, limiter=None,
    )

    # both advertised districts were walked, on the region subdomain
    assert any("stredo.ceskereality.cz/prodej/byty/kladno/" in u for u in fake.urls)
    assert any("stredo.ceskereality.cz/prodej/byty/beroun/" in u for u in fake.urls)
    # union: 1 region-wide backstop + 2 kladno + 1 beroun = 4 distinct listings
    assert len(seen) == 4


class _CappedClient:
    """A dense slice: every page full + a "next" arrow, total far over 240 — the
    walk must stop at page 12 and mark it incomplete (never request the 404 page 13)."""

    def __init__(self) -> None:
        self.pages: list[int] = []

    def fetch_search(self, url):  # noqa: ANN001
        pg = _page_num(url)
        self.pages.append(pg)
        ids = [str(5_000_000 + pg * 100 + k) for k in range(20)]
        return _page_html(300, ids, next_page=pg + 1), 200


def test_walk_slice_caps_at_12_pages_never_requests_404_page13():
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    fake = _CappedClient()
    rows, _pages, total, complete = portal._walk_slice(
        fake, "stredo.ceskereality.cz", "prodej", "byty", "kladno")
    assert max(fake.pages) == 12        # page 13 (the 404) is NEVER requested
    assert total == 300
    assert complete is False            # capped -> suppresses mark_inactive
    assert len(rows) == 12 * 20


def test_region_scope_suppresses_completeness(monkeypatch):
    fake = _FacetClient()
    monkeypatch.setattr(m, "CeskerealityClient", lambda **kw: fake)
    portal = m.CeskerealityPortal(
        default_config("ceskereality"), regions=("stredo.ceskereality.cz",))
    _seen, _counts, _total, _pages, complete = portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=None, dry_run=True, limiter=None,
    )
    assert complete is False            # a one-region test is never a full walk


def test_full_walk_visits_all_seven_regions(monkeypatch):
    fake = _FacetClient()
    monkeypatch.setattr(m, "CeskerealityClient", lambda **kw: fake)
    portal = m.CeskerealityPortal(default_config("ceskereality"))   # no region scope
    portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=None, dry_run=True, limiter=None,
    )
    for host in REGION_HOSTS:
        assert any(host in u for u in fake.urls), f"{host} not walked"


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
                      "seen": {"r1", "r2", "c1"}, "min_unseen_hours": 24}]


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
                      "seen": {"b1"}, "min_unseen_hours": 24}]


def test_client_routes_through_proxy_when_env_set(monkeypatch):
    monkeypatch.setenv("SCRAPER_PROXY_URL", "http://u:p@gw.example.com:823")
    c = CeskerealityClient()
    assert c._session.proxies.get("https") == "http://u:p@gw.example.com:823"


def test_client_no_proxy_when_env_unset(monkeypatch):
    monkeypatch.delenv("SCRAPER_PROXY_URL", raising=False)
    c = CeskerealityClient()
    assert not c._session.proxies            # falls back to the direct IP
