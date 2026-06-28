"""The ceskereality region × disposition/type split (the cap beater) + the
opt-in residential proxy."""

from __future__ import annotations

import re

from scraper import ceskereality_main as m
from scraper.ceskereality_client import REGION_HOSTS, SUB_SLUGS, CeskerealityClient
from scraper.portal import default_config


def _page_html(total: int | None, ids: list[str], next_page: int | None = None) -> str:
    cards = "".join(
        '<article class="i-estate">'
        f'<a class="i-estate__image-link" href="/prodej/byty/x/y-{i}.html"></a>'
        "</article>"
        for i in ids
    )
    meta = f'<meta name="description" content="Máme tady {total} bytů">' if total else ""
    pager = (
        f'<a class="pagination-arrow --next" href="/x/?strana={next_page}"></a>'
        if next_page else ""
    )
    return f"<html><head>{meta}</head><body>{cards}{pager}</body></html>"


def _page_num(url: str) -> int:
    m_ = re.search(r"strana=(\d+)", url)
    return int(m_.group(1)) if m_ else 1


class _SliceClient:
    """One unique listing per (region×facet) slice on page 1; page 2 is empty —
    so each slice fetches one page and stops."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self._ids: dict[str, str] = {}

    def fetch_search(self, url):  # noqa: ANN001
        self.urls.append(url)
        if "strana=" in url:
            return _page_html(70, []), 200
        nid = self._ids.setdefault(url, str(3_000_000 + len(self._ids)))
        return _page_html(70, [nid]), 200

    def fetch_index(self, sale_type, cat, page):  # nationwide total  # noqa: ANN001
        return _page_html(70, ["9000001"]), 200


def test_walk_category_splits_by_region_and_facet(monkeypatch):
    fake = _SliceClient()
    monkeypatch.setattr(m, "CeskerealityClient", lambda **kw: fake)
    portal = m.CeskerealityPortal(default_config("ceskereality"))

    seen, counts, total, pages, complete = portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=None, dry_run=True, limiter=None,
    )

    expected_slices = len(REGION_HOSTS) * len(SUB_SLUGS["byty"])  # 7 × 10 = 70
    assert len(seen) == expected_slices            # one unique listing per slice
    assert total == 70 and complete is True        # collected == nationwide total
    # every region × every byty disposition was visited
    assert sum(1 for u in fake.urls if "byty-3-1" in u and "strana=" not in u) == len(REGION_HOSTS)
    assert any("stredo.ceskereality.cz" in u for u in fake.urls)
    assert any("severo.moravskereality.cz" in u for u in fake.urls)


class _CappedClient:
    """A dense slice: every page is full and a "next" arrow always beckons, total
    far over the 240 cap — the walk must stop at page 12 and mark it incomplete."""

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
    rows, pages, total, complete = portal._walk_slice(
        fake, "stredo.ceskereality.cz", "prodej", "byty", "byty-3-1")
    assert max(fake.pages) == 12        # page 13 (the 404) is NEVER requested
    assert total == 300
    assert complete is False            # capped -> suppresses mark_inactive
    assert len(rows) == 12 * 20


def test_region_scope_suppresses_completeness(monkeypatch):
    fake = _SliceClient()
    monkeypatch.setattr(m, "CeskerealityClient", lambda **kw: fake)
    portal = m.CeskerealityPortal(
        default_config("ceskereality"), regions=("stredo.ceskereality.cz",))
    _seen, _counts, _total, _pages, complete = portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=None, dry_run=True, limiter=None,
    )
    assert complete is False            # a one-region test is never a full walk
    assert all("stredo.ceskereality.cz" in u or "www.ceskereality.cz" in u for u in fake.urls)


def test_client_routes_through_proxy_when_env_set(monkeypatch):
    monkeypatch.setenv("SCRAPER_PROXY_URL", "http://u:p@gw.example.com:823")
    c = CeskerealityClient()
    assert c._session.proxies.get("https") == "http://u:p@gw.example.com:823"


def test_client_no_proxy_when_env_unset(monkeypatch):
    monkeypatch.delenv("SCRAPER_PROXY_URL", raising=False)
    c = CeskerealityClient()
    assert not c._session.proxies            # falls back to the direct IP
