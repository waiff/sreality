"""The ceskereality www × okres-partition split (the cap beater) + the opt-in
residential proxy. The geographic axis is the COMPLETE okres list from
admin_boundaries (not the site's truncated district facet), unioned with the page's
disposition facets, every slice fetched on www and capped at 12 pages."""

from __future__ import annotations

import re

from scraper import ceskereality_main as m
from scraper.ceskereality_client import CeskerealityClient
from scraper.ceskereality_parser import extract_disposition_paths, extract_facet_slugs
from scraper.portal import default_config


class _StubClient:
    """Returns a fixed HTML body for every fetch_search (for _child_subpaths tests)."""

    def __init__(self, html: str) -> None:
        self._html = html

    def fetch_search(self, url):  # noqa: ANN001
        return self._html, 200


def test_extract_disposition_paths_auto_discovers_dominant_geo():
    # the node's own geo dominates its facet list (the capital's stacked form is "praha",
    # auto-discovered with no per-city special-case); a cross-link geo appears once.
    html = (
        '<a href="/prodej/byty/byty-2-kk/praha/">x</a>'
        '<a href="/prodej/byty/byty-3-1/praha/">x</a>'        # praha: 2 (dominant)
        '<a href="/prodej/byty/byty-2-kk/kladno/">cross-link -> 1, not dominant</a>'
        '<a href="/prodej/byty/byty-2-kk/">nationwide 1-seg -> ignored</a>'
        '<a href="/prodej/byty/praha/">geo-only 1-seg -> ignored</a>'
    )
    assert extract_disposition_paths(html, "prodej", "byty") == [
        "byty-2-kk/praha", "byty-3-1/praha"]


def test_child_subpaths_unions_geo_and_disposition():
    # both axes are unioned (each facet list is sometimes truncated); the capital's
    # stacked geo "praha" is auto-discovered, mc- adds the complete 22-district partition.
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    client = _StubClient(
        '<a href="/prodej/byty/byty-2-kk/praha/">x</a>'
        '<a href="/prodej/byty/byty-3-1/praha/">x</a>'
        '<a href="/prodej/byty/cast-praha-zizkov/">x</a>'
        '<a href="/prodej/byty/mc-praha-3/">x</a>'
    )
    subs = portal._child_subpaths(client, "prodej", "byty", "praha-hlavni-mesto")
    assert {"cast-praha-zizkov", "mc-praha-3", "byty-2-kk/praha", "byty-3-1/praha"} <= set(subs)


def test_child_subpaths_geo_axis_when_no_disposition():
    # pozemky/komercni have no disposition -> finer geography only (incl. mc-)
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    client = _StubClient(
        '<a href="/prodej/pozemky/obec-slany/">x</a>'
        '<a href="/prodej/pozemky/mc-praha-3/">x</a>'
    )
    assert set(portal._child_subpaths(client, "prodej", "pozemky", "kladno")) == {
        "obec-slany", "mc-praha-3"}


def test_child_subpaths_preserves_disposition_when_drilling_geo():
    # a capped disposition node drills geography KEEPING its disposition prefix
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    client = _StubClient('<a href="/prodej/byty/cast-praha-zizkov/">x</a>')
    assert portal._child_subpaths(client, "prodej", "byty", "byty-2-kk/praha") == [
        "byty-2-kk/cast-praha-zizkov"]


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


class _OkresConn:
    """A stand-in DB connection: `_okres_slugs` queries admin_boundaries for the okres
    (id, name) pairs through `conn.cursor()`; synthetic ids 3000+ per name."""

    def __init__(self, names: list[str]) -> None:
        self._rows = [(3000 + i, n) for i, n in enumerate(names)]

    def cursor(self):  # used as `with conn.cursor() as cur:`
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):  # noqa: ANN002
        return False

    def execute(self, sql, params=None):  # noqa: ANN001
        pass

    def fetchall(self):
        return list(self._rows)


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


def test_slugify_folds_diacritics_and_spaces():
    assert m._slugify("Mladá Boleslav") == "mlada-boleslav"
    assert m._slugify("Brno-město") == "brno-mesto"
    assert m._slugify("Ústí nad Labem") == "usti-nad-labem"
    assert m._slugify("Praha-východ") == "praha-vychod"
    assert m._slugify("Žďár nad Sázavou") == "zdar-nad-sazavou"


def test_okres_slugs_complete_partition_with_praha_special_case():
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    conn = _OkresConn(["Kladno", "Mladá Boleslav", "Brno-město"])
    slugs = portal._okres_slugs(conn)
    # the capital is mapped explicitly (its admin name doesn't fold to the slug)
    assert slugs[0] == "praha-hlavni-mesto"
    assert {"kladno", "mlada-boleslav", "brno-mesto"} <= set(slugs)
    # the okres-id join key is captured (for the per-okres mark_inactive flip)
    assert portal._okres_id_by_slug["praha-hlavni-mesto"] == m._PRAHA_OKRES_ID
    assert portal._okres_id_by_slug["kladno"] == 3000
    # cached: a second call doesn't re-query
    assert portal._okres_slugs(_OkresConn(["other"])) == slugs


def test_okres_slugs_without_conn_falls_back_to_praha_only():
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    assert portal._okres_slugs(None) == ["praha-hlavni-mesto"]


class _FacetClient:
    """Every okres slice (and the bare-page backstop) returns its own listings on the
    www host; none cap, so no recursion. Unique ids per page-1 fetch."""

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
        if "/praha-hlavni-mesto/" in url:
            return _page_html(50, [self._nid(), self._nid()]), 200
        # the bare www page (None backstop) + any other okres: one listing each
        return _page_html(50, [self._nid()]), 200

    def fetch_index(self, sale_type, cat, page):  # nationwide total  # noqa: ANN001
        return _page_html(50, ["9000001"]), 200


def test_walk_category_walks_okres_partition(monkeypatch):
    fake = _FacetClient()
    monkeypatch.setattr(m, "CeskerealityClient", lambda **kw: fake)
    portal = m.CeskerealityPortal(default_config("ceskereality"))

    seen, _counts, _total, _pages, _complete = portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=None, dry_run=True, limiter=None,
    )

    # every slice is fetched on the canonical www host (no region subdomains)
    assert all("www.ceskereality.cz" in u for u in fake.urls)
    # the okres axis (conn=None -> praha-only) + the bare-page backstop were walked
    assert any("/prodej/byty/praha-hlavni-mesto/" in u for u in fake.urls)
    assert any(u.rstrip("/").endswith("/prodej/byty") for u in fake.urls)
    # union: 1 bare-page backstop + 2 praha = 3 distinct listings (no disposition fan-out)
    assert len(seen) == 3


def test_walk_category_uses_admin_okres_list(monkeypatch):
    fake = _FacetClient()
    monkeypatch.setattr(m, "CeskerealityClient", lambda **kw: fake)
    monkeypatch.setattr(m.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(m.db, "enqueue_detail", lambda *a, **k: 0)
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    conn = _OkresConn(["Kladno", "Brno-město"])

    portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=conn, dry_run=True, limiter=None,
    )
    # the admin-supplied okresy are walked as slices on www
    assert any("/prodej/byty/kladno/" in u for u in fake.urls)
    assert any("/prodej/byty/brno-mesto/" in u for u in fake.urls)


def test_walk_category_records_per_okres_completeness(monkeypatch):
    fake = _FacetClient()                    # nothing caps -> every okres complete
    monkeypatch.setattr(m, "CeskerealityClient", lambda **kw: fake)
    monkeypatch.setattr(m.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(m.db, "enqueue_detail", lambda *a, **k: 0)
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=_OkresConn(["Kladno"]), dry_run=True, limiter=None,
    )
    # completeness is keyed by the admin okres_id (praha 9999 + kladno's synthetic 3000)
    oc = portal._okres_complete[("byt", "prodej")]
    assert oc[m._PRAHA_OKRES_ID] is True
    assert oc[3000] is True


class _RecursingClient:
    """A dense okres caps (12 full pages, total 300) and its page-1 advertises
    obec-/cast- sub-locality facets; the recursion drills into each (small, complete).
    A 2-segment disposition facet must NOT be taken as a sub-locality."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self._n = 0

    def _nid(self) -> str:
        self._n += 1
        return str(6_000_000 + self._n)

    def fetch_search(self, url):  # noqa: ANN001
        self.urls.append(url)
        pg = _page_num(url)
        if "/cast-praha-" in url or "/obec-" in url:
            return _page_html(40, [] if pg > 1 else [self._nid(), self._nid()]), 200
        if "/praha-hlavni-mesto/" in url:
            ids = [str(7_000_000 + pg * 100 + k) for k in range(20)]
            # page 1 carries the sub-locality facets (+ a stacked disposition decoy)
            facets = (
                ("cast-praha-zizkov", "obec-x", "byty-3-1") if pg == 1 else ()
            )
            return _page_html(300, ids, facets=facets, next_page=pg + 1), 200
        return _page_html(50, [] if pg > 1 else [self._nid()]), 200

    def fetch_index(self, sale_type, cat, page):  # noqa: ANN001
        return _page_html(50, ["9000001"]), 200


def test_walk_okres_recurses_into_sublocalities_when_capped():
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    fake = _RecursingClient()
    rows, _pages, total, _complete = portal._walk_okres(
        fake, "prodej", "byty", "praha-hlavni-mesto")
    # the okres capped at 12 pages, then drilled into its obec-/cast- sub-localities
    praha_pages = [_page_num(u) for u in fake.urls if "praha-hlavni-mesto" in u]
    assert max(praha_pages) == 12       # never requested the 404 page 13
    assert any("/prodej/byty/cast-praha-zizkov/" in u for u in fake.urls)
    assert any("/prodej/byty/obec-x/" in u for u in fake.urls)
    assert total == 300
    # the disposition facet (treated as a sub-locality) is NOT walked
    assert not any(u.rstrip("/").endswith("/byty-3-1") for u in fake.urls)
    # the okres's 240 + the two sub-locality listings were collected
    assert len(rows) == 12 * 20 + 4


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
        fake, "www.ceskereality.cz", "prodej", "byty", "praha-hlavni-mesto")
    assert max(fake.pages) == 12        # page 13 (the 404) is NEVER requested
    assert total == 300
    assert complete is False            # capped -> suppresses mark_inactive
    assert len(rows) == 12 * 20


def test_scope_suppresses_completeness(monkeypatch):
    fake = _FacetClient()
    monkeypatch.setattr(m, "CeskerealityClient", lambda **kw: fake)
    portal = m.CeskerealityPortal(
        default_config("ceskereality"), regions=("praha-hlavni-mesto",))
    _seen, _counts, _total, _pages, complete = portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=None, dry_run=True, limiter=None,
    )
    assert complete is False            # a scoped partial test is never a full walk


def test_dum_has_two_contributing_walk_categories():
    # rodinne-domy AND chaty-chalupy both map to cm='dum' — the conflation that made
    # the complete chaty walk falsely delist rodinne-domy rows.
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    assert portal._cmct_contributors[("dum", "prodej")] == 2
    assert portal._cmct_contributors[("dum", "pronajem")] == 2
    assert portal._cmct_contributors[("byt", "prodej")] == 1   # single-contributor unchanged


def _stub_mark_inactive(calls: list):
    def _fn(conn, cm, ct, pks, source, okres_ids=None, min_unseen_hours=None):  # noqa: ANN001
        calls.append({"pks": sorted(pks), "okres_ids": sorted(okres_ids or []),
                      "min_unseen_hours": min_unseen_hours})
        return len(okres_ids or [])
    return _fn


def test_dum_per_okres_incomplete_sibling_holds_back_only_that_okres(monkeypatch):
    # An incomplete sibling (rodinne-domy's dense okres) no longer suppresses the WHOLE
    # dum scope — only that one okres is spared; every other okres still flips.
    calls: list = []
    monkeypatch.setattr(m.db, "index_summary_native",
                        lambda conn, src, ids: {i: {"sreality_id": int(i)} for i in ids})
    monkeypatch.setattr(m.db, "mark_inactive", _stub_mark_inactive(calls))
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    dum_p = ("dum", "prodej")
    rodinne = {"category": "rodinne-domy", "sale_type": "prodej"}
    chaty = {"category": "chaty-chalupy", "sale_type": "prodej"}

    # okres 10 complete in BOTH walks; okres 20 incomplete (rodinne-domy's dense okres)
    portal._cmct_seen[dum_p] = {"1"}
    portal._okres_complete[dum_p] = {10: True, 20: False}
    portal._cmct_walked[dum_p] = 1
    assert portal.mark_inactive(None, rodinne, {"1"}) == 0   # waits for the sibling
    assert calls == []

    portal._cmct_walked[dum_p] = 2
    portal.mark_inactive(None, chaty, {"1"})
    # flips ONLY okres 10; okres 20's stragglers stay active (rule #3)
    assert len(calls) == 1
    assert calls[0]["okres_ids"] == [10]


def test_dum_per_okres_flips_complete_okresy_with_union_seen_and_rail(monkeypatch):
    calls: list = []
    monkeypatch.setattr(m.db, "index_summary_native",
                        lambda conn, src, ids: {i: {"sreality_id": int(i)} for i in ids})
    monkeypatch.setattr(m.db, "mark_inactive", _stub_mark_inactive(calls))
    portal = m.CeskerealityPortal(default_config("ceskereality"))
    dum_p = ("dum", "prodej")
    rodinne = {"category": "rodinne-domy", "sale_type": "prodej"}
    chaty = {"category": "chaty-chalupy", "sale_type": "prodej"}

    portal._cmct_seen[dum_p] = {"1", "2", "3"}
    portal._okres_complete[dum_p] = {10: True, 11: True}
    portal._cmct_walked[dum_p] = 1
    assert portal.mark_inactive(None, rodinne, {"1", "2"}) == 0   # waits for the sibling
    assert calls == []

    portal._cmct_walked[dum_p] = 2
    portal.mark_inactive(None, chaty, {"3"})
    # ONE flip: the UNION seen {1,2,3}, the complete okresy [10,11], the 24h rail
    assert calls == [{"pks": [1, 2, 3], "okres_ids": [10, 11], "min_unseen_hours": 24}]
    # idempotent: the already-flipped scope never double-flips
    portal.mark_inactive(None, chaty, {"3"})
    assert len(calls) == 1


def test_client_routes_through_proxy_when_env_set(monkeypatch):
    monkeypatch.setenv("SCRAPER_PROXY_URL", "http://u:p@gw.example.com:823")
    c = CeskerealityClient()
    assert c._session.proxies.get("https") == "http://u:p@gw.example.com:823"


def test_client_no_proxy_when_env_unset(monkeypatch):
    monkeypatch.delenv("SCRAPER_PROXY_URL", raising=False)
    c = CeskerealityClient()
    assert not c._session.proxies            # falls back to the direct IP
