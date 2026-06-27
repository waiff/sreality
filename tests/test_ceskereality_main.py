"""walk_category drives ?strana to the page-reported total and survives a
Cloudflare-degraded (barren) page, instead of stopping when the pager's "next"
arrow vanishes — the fix for the ~12-page / ~240-listing early stop."""

from __future__ import annotations

from scraper import ceskereality_main as m
from scraper.portal import default_config


def _page_html(total: int | None, ids: list[str]) -> str:
    cards = "".join(
        '<article class="i-estate">'
        f'<a class="i-estate__image-link" href="/prodej/byty/x/y-{i}.html"></a>'
        "</article>"
        for i in ids
    )
    meta = (
        f'<meta name="description" content="Máme tady {total} bytů">' if total else ""
    )
    return f"<html><head>{meta}</head><body>{cards}</body></html>"


class _FakeClient:
    """Serves `pages[page_num] -> ids`; a page in `barren_once` returns NO cards on
    its FIRST fetch (a degraded response) and real cards on the retry."""

    def __init__(self, total: int, pages: dict[int, list[str]], barren_once: set[int]):
        self.total = total
        self.pages = pages
        self.barren_once = barren_once
        self._served_barren: set[int] = set()
        self.calls: list[int] = []

    def fetch_index(self, sale_type, cat, page):  # noqa: ANN001
        pn = page if page is not None else 1
        self.calls.append(pn)
        if pn in self.barren_once and pn not in self._served_barren:
            self._served_barren.add(pn)
            return _page_html(self.total, []), 200
        return _page_html(self.total, self.pages.get(pn, [])), 200


def test_walk_category_drives_full_total_past_barren_page(monkeypatch):
    total = 60  # 3 pages of 20
    pages = {
        1: [f"1{i:04d}" for i in range(20)],
        2: [f"2{i:04d}" for i in range(20)],
        3: [f"3{i:04d}" for i in range(20)],
    }
    fake = _FakeClient(total, pages, barren_once={2})  # page 2 degraded on first hit
    monkeypatch.setattr(m, "CeskerealityClient", lambda **kw: fake)
    monkeypatch.setattr(m.time, "sleep", lambda *a, **k: None)

    portal = m.CeskerealityPortal(default_config("ceskereality"))
    seen, counts, total_out, pages_walked, complete = portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=None, dry_run=True, limiter=None,
    )

    assert total_out == 60
    assert len(seen) == 60                 # all 3 pages collected (not just page 1)
    assert complete is True                # full walk -> drives mark_inactive
    assert fake.calls.count(2) >= 2        # the barren page 2 was retried
    assert 3 in fake.calls                 # walk continued to the last page


def test_walk_category_stops_at_last_page_no_overrun(monkeypatch):
    total = 25  # 2 pages (20 + 5)
    pages = {1: [f"1{i:04d}" for i in range(20)], 2: [f"2{i:04d}" for i in range(5)]}
    fake = _FakeClient(total, pages, barren_once=set())
    monkeypatch.setattr(m, "CeskerealityClient", lambda **kw: fake)
    monkeypatch.setattr(m.time, "sleep", lambda *a, **k: None)

    portal = m.CeskerealityPortal(default_config("ceskereality"))
    seen, _counts, _total, _pages, _complete = portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=None, dry_run=True, limiter=None,
    )
    assert len(seen) == 25
    assert max(fake.calls) == 2            # never fetched a non-existent page 3
