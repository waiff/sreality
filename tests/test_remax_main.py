"""remax_main on the portal framework: RemaxPortal's AGENDA-GRAIN nomination and
the STRUCTURAL verdict that gates it. The agenda key is `sale` (1=prodej /
2=pronajem) — these tests pin that the whole agenda is nominated once, against
every agenda id, and (since 2026-09-08, rule #3) that the gate is "the walk
reached remax's own end", never the collected/declared ratio: a walk short of the
reported total still nominates, while a page cap, a deadline, an exception or an
uncorroborated blank page never do.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from scraper.portal import PortalConfig
from scraper import remax_main
from scraper.remax_main import RemaxPortal

_CATEGORIES = [
    {"category_main": "byt", "category_type": "prodej",   "sale": 1},
    {"category_main": "dum", "category_type": "prodej",   "sale": 1},
    {"category_main": "byt", "category_type": "pronajem", "sale": 2},
]


def _config() -> PortalConfig:
    return PortalConfig(
        source="remax", supports_complete_walk=True,
        categories=_CATEGORIES, split_threshold=None,
    )


def _portal(**kw: Any) -> RemaxPortal:
    return RemaxPortal(_config(), **kw)


class _Limiter:
    def acquire(self) -> None: ...
    def penalize(self) -> None: ...


def _items(*specs: tuple[str, str]) -> list[Any]:
    base = "https://www.remax-czech.cz/reality/detail/"
    return [
        SimpleNamespace(source_id_native=n, detail_path=f"{base}{n}/",
                        price_text="5 000 000 Kč", title=t)
        for n, t in specs
    ]


def _page(total: int | None, *specs: tuple[str, str]) -> Any:
    return SimpleNamespace(total=total, next_offset=None, items=_items(*specs))


class _Index:
    """A remax index scripted per (sale, page).

    Each value is the sequence of parse results that page yields on successive
    FETCHES (the last one repeats), because the barren rule re-reads the same url
    once — so a blank page that recovers, and one that stays blank, are both
    expressible. `fetches` records every (sale, page) actually asked for.
    """

    def __init__(
        self, pages: dict[tuple[int, int], list[Any]], *,
        raises: tuple[int, int] | None = None, raise_after: int = 0,
    ) -> None:
        self._pages = {k: list(v) for k, v in pages.items()}
        self._raises = raises
        # How many fetches of `raises` succeed before it starts raising. 0 = the
        # first one already does; 1 lets the page be read once and its barren
        # RE-READ fail, which is the only way to reach _classify_empty_page's
        # unreadable-re-read arm.
        self._raise_after = raise_after
        self.fetches: list[tuple[int, int]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        index = self

        class _Client:
            def __init__(self, *a: Any, **k: Any) -> None: ...

            def fetch_index(self, *, sale: Any = None, stranka: Any = None) -> Any:
                index.fetches.append((sale, stranka))
                if index._raises == (sale, stranka) and (
                    index.fetches.count((sale, stranka)) > index._raise_after
                ):
                    raise RuntimeError("remax 503")
                return (f"{sale}/{stranka}", 200)

        monkeypatch.setattr(remax_main, "RemaxClient", _Client)
        monkeypatch.setattr(remax_main, "parse_index", self._parse)
        monkeypatch.setattr(remax_main.db, "index_summary_native", lambda *a, **k: {})
        monkeypatch.setattr(remax_main.db, "enqueue_detail", lambda *a, **k: 0)
        monkeypatch.setattr(remax_main.db, "touch_listings", lambda *a, **k: None)

    def _parse(self, html: str) -> Any:
        sale, page = (int(x) for x in html.split("/"))
        seq = self._pages[(sale, page)]
        return seq.pop(0) if len(seq) > 1 else seq[0]


def _walk_sale_agenda(
    monkeypatch: pytest.MonkeyPatch, portal: RemaxPortal, pages: dict[tuple[int, int], list[Any]],
    *, deadline: float | None = None, raises: tuple[int, int] | None = None,
) -> _Index:
    """Drive the sale agenda (sale=1) so the portal's agenda cache is populated."""
    index = _Index(pages, raises=raises)
    index.install(monkeypatch)
    portal.walk_category(_CATEGORIES[0], object(), False, _Limiter(), deadline)
    return index


def _nominations(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    captured: list[Any] = []
    monkeypatch.setattr(
        remax_main.db, "presence_candidates",
        lambda _c, source, cm, ct, seen, **kw: (
            captured.append((source, cm, ct, set(seen))) or ([], 4)
        ),
    )
    return captured


def test_portal_reads_complete_walk_from_config() -> None:
    assert _portal().supports_complete_walk is True


def test_nomination_is_agenda_grain(monkeypatch: pytest.MonkeyPatch) -> None:
    portal = _portal()
    _walk_sale_agenda(monkeypatch, portal, {(1, 1): [_page(
        3, ("r1", "Prodej bytu 2+kk"), ("r2", "Prodej bytu"), ("r3", "Prodej rodinného domu"),
    )]})
    captured = _nominations(monkeypatch)
    # First prodej descriptor (byt) sweeps the whole sale agenda.
    assert portal.presence_candidates(object(), _CATEGORIES[0], {"r1", "r2"}) == ([], 4, {"category_main": None})
    source, cm, ct, seen = captured[0]
    assert source == "remax" and ct == "prodej" and cm is None   # agenda scope: category_type alone
    assert seen == {"r1", "r2", "r3"}              # the FULL sale agenda, not the byt slice
    # A second prodej descriptor (dum) must NOT re-nominate.
    assert portal.presence_candidates(object(), _CATEGORIES[1], {"r3"}) is None
    assert len(captured) == 1


def test_reaching_the_portals_own_count_is_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    portal = _portal()
    _walk_sale_agenda(monkeypatch, portal, {(1, 1): [_page(2, ("r1", "Prodej bytu"), ("r2", "Prodej bytu"))]})
    walk = portal._agenda_cache[1]
    assert (walk.stop_reason, walk.reached_end) == ("declared_total_reached", True)


def test_walk_priceless_card_is_unchanged_not_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A card without a parseable price ("Dohodou") carries no change signal.
    Classifying it as changed put ~1,100 remax listings on a permanent
    CHANGED-priority refetch treadmill that starved the NEW rows (rent never
    drained) — it must be touched, never enqueued."""
    portal = _portal()
    base = "https://www.remax-czech.cz/reality/detail/"
    items = [
        SimpleNamespace(source_id_native="r1", detail_path=f"{base}r1/",
                        price_text="5 000 000 Kč", title="Prodej bytu 2+kk"),   # price match
        SimpleNamespace(source_id_native="r2", detail_path=f"{base}r2/",
                        price_text="Dohodou", title="Prodej bytu 1+1"),         # no card price
        SimpleNamespace(source_id_native="r3", detail_path=f"{base}r3/",
                        price_text="6 000 000 Kč", title="Prodej bytu 3+kk"),   # price changed
        SimpleNamespace(source_id_native="r4", detail_path=f"{base}r4/",
                        price_text="4 000 000 Kč", title="Prodej bytu 2+1"),    # brand new
    ]
    index = _Index({(1, 1): [SimpleNamespace(total=4, next_offset=None, items=items)]})
    index.install(monkeypatch)
    monkeypatch.setattr(
        remax_main.db, "index_summary_native",
        lambda *a, **k: {
            "r1": {"id": 61, "sreality_id": -1, "price_czk": 5_000_000},
            "r2": {"id": 62, "sreality_id": -2, "price_czk": None},
            "r3": {"id": 63, "sreality_id": -3, "price_czk": 5_500_000},
        },
    )
    touched: list[list[int]] = []
    enqueued: list[tuple[str, int]] = []
    monkeypatch.setattr(
        remax_main.db, "touch_listings_by_id", lambda _c, pks: touched.append(list(pks)))
    monkeypatch.setattr(
        remax_main.db, "enqueue_detail",
        lambda _c, _s, entries: enqueued.extend((n, prio) for n, _r, _p, prio in entries) or len(entries),
    )
    _seen, counts, *_ = portal.walk_category(_CATEGORIES[0], object(), False, _Limiter())
    assert touched == [[61, 62]]   # unchanged rows touched by surrogate id
    assert enqueued == [
        ("r3", remax_main.db.QUEUE_PRIORITY_CHANGED),
        ("r4", remax_main.db.QUEUE_PRIORITY_NEW),
    ]
    assert counts == {"found_new": 1, "enqueued": 2}


def test_walk_category_returns_the_agendas_structural_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    portal = _portal()
    index = _Index({(1, 1): [_page(2, ("r1", "Prodej bytu"), ("r2", "Prodej rodinného domu"))]})
    index.install(monkeypatch)
    # The byt slice holds one of the two ids, but the 5th element is the AGENDA's.
    seen, _counts, result_size, _pages, reached_end = portal.walk_category(
        _CATEGORIES[0], object(), False, _Limiter())
    assert seen == {"r1"} and result_size == 1 and reached_end is True


def test_a_walk_short_of_the_declared_total_still_reaches_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The change of 2026-09-08: the count no longer vetoes. remax declares 10 but
    serves 2 and then a page that is empty twice, past the last page the total
    implies — structurally that walk reached the end, so it nominates and the
    shortfall is an alarm (COVERAGE), not a refusal."""
    portal = _portal()
    _walk_sale_agenda(monkeypatch, portal, {
        (1, 1): [_page(10, ("r1", "Prodej bytu"), ("r2", "Prodej bytu"))],
        (1, 2): [_page(10)],
    })
    walk = portal._agenda_cache[1]
    assert (walk.stop_reason, walk.reached_end) == ("empty_confirmed", True)
    captured = _nominations(monkeypatch)
    assert portal.presence_candidates(object(), _CATEGORIES[0], {"r1", "r2"}) is not None
    assert captured[0][3] == {"r1", "r2"}


def test_the_barren_rule_re_reads_the_same_url_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portal = _portal()
    index = _walk_sale_agenda(monkeypatch, portal, {
        (1, 1): [_page(10, ("r1", "Prodej bytu"))],
        (1, 2): [_page(10)],
    })
    assert index.fetches == [(1, 1), (1, 2), (1, 2)]


def test_a_blank_page_that_carries_cards_on_re_read_is_our_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items-first: a throttled/blank-shell 200 mid-walk is indistinguishable from
    the page after the last one until it is re-read. It recovers here, so the walk
    stopped on US, not on remax — and nominates nothing."""
    portal = _portal()
    _walk_sale_agenda(monkeypatch, portal, {
        (1, 1): [_page(60, ("r1", "Prodej bytu"))],
        (1, 2): [_page(60), _page(60, ("r2", "Prodej bytu"))],
    })
    walk = portal._agenda_cache[1]
    assert (walk.stop_reason, walk.reached_end) == ("barren", False)
    captured = _nominations(monkeypatch)
    assert portal.presence_candidates(object(), _CATEGORIES[0], {"r1"}) is None
    assert captured == []


def test_a_blank_page_short_of_the_declared_last_page_is_not_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # total=60 spans three 21-card pages, so a page-2 blank — even a repeatable
    # one — sits short of the end the portal's own count implies.
    portal = _portal()
    _walk_sale_agenda(monkeypatch, portal, {
        (1, 1): [_page(60, ("r1", "Prodej bytu"))],
        (1, 2): [_page(60)],
    })
    walk = portal._agenda_cache[1]
    assert (walk.stop_reason, walk.reached_end) == ("barren", False)


def test_a_barren_first_page_with_no_total_is_never_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The soft-block shape: page 1 answers 200 with no cards and no "z celkem"
    header. Nothing corroborates it (no total, no earlier page that carried
    cards), so the agenda nominates nothing."""
    portal = _portal()
    _walk_sale_agenda(monkeypatch, portal, {(1, 1): [_page(None)]})
    walk = portal._agenda_cache[1]
    assert (walk.stop_reason, walk.reached_end) == ("barren", False)
    captured = _nominations(monkeypatch)
    assert portal.presence_candidates(object(), _CATEGORIES[0], set()) is None
    assert captured == []


def test_a_repeated_page_is_our_stop_not_remaxs_clamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """It was read as remax clamping an out-of-range ?stranka=N back onto a valid
    page. The live probe (2026-09-08) refutes the premise — page 81 of an 80-page
    agenda answers 200 with zero cards, no redirect — so a page of cards that adds
    no id is an edge cache, a session reset or a reshuffled index. The loop still
    breaks; the agenda must not nominate off it."""
    portal = _portal()
    _walk_sale_agenda(monkeypatch, portal, {
        (1, 1): [_page(1670, ("r1", "Prodej bytu"), ("r2", "Prodej bytu"))],
        (1, 2): [_page(1670, ("r1", "Prodej bytu"), ("r2", "Prodej bytu"))],
    })
    walk = portal._agenda_cache[1]
    assert (walk.stop_reason, walk.reached_end) == ("pager_stalled", False)
    captured = _nominations(monkeypatch)
    assert portal.presence_candidates(object(), _CATEGORIES[0], {"r1"}) is None
    assert captured == []


def test_a_blank_page_cannot_supply_its_own_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """The position corroboration must come from a page that carried cards. remax
    renders its "z celkem N" line on a lost-filter / backend-error page too, and
    `_TOTAL_RE` reads "z celkem 0" happily — so a blank page declaring a small total
    would otherwise put itself past the end and confirm itself."""
    portal = _portal()
    _walk_sale_agenda(monkeypatch, portal, {
        (1, 1): [_page(1670, ("r1", "Prodej bytu"), ("r2", "Prodej bytu"))],
        (1, 2): [_page(0)],
    })
    walk = portal._agenda_cache[1]
    assert (walk.total, walk.stop_reason, walk.reached_end) == (1670, "barren", False)


def test_a_barren_re_read_that_fails_is_our_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A confirmation that cannot be obtained is not a confirmation: the blank page's
    re-read raising leaves the barren rule with nothing, so the stop is ours."""
    portal = _portal()
    index = _Index(
        {(1, 1): [_page(60, ("r1", "Prodej bytu"))], (1, 2): [_page(60)]},
        raises=(1, 2), raise_after=1,
    )
    index.install(monkeypatch)
    portal.walk_category(_CATEGORIES[0], object(), False, _Limiter())
    walk = portal._agenda_cache[1]
    assert (walk.stop_reason, walk.reached_end) == ("barren", False)
    assert index.fetches == [(1, 1), (1, 2), (1, 2)]


def test_a_page_capped_walk_nominates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every realtime delta probe runs under this cap (set_index_page_cap), and a
    # capped walk has not seen the portal's end even when its one page happens to
    # hold the whole declared total.
    portal = _portal(max_pages=1)
    _walk_sale_agenda(monkeypatch, portal, {(1, 1): [_page(2, ("r1", "Prodej bytu"), ("r2", "Prodej bytu"))]})
    walk = portal._agenda_cache[1]
    assert (walk.stop_reason, walk.reached_end) == ("page_cap", False)
    captured = _nominations(monkeypatch)
    assert portal.presence_candidates(object(), _CATEGORIES[0], {"r1", "r2"}) is None
    assert captured == []


def test_a_deadline_stop_nominates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    portal = _portal()
    _walk_sale_agenda(
        monkeypatch, portal, {(1, 1): [_page(2, ("r1", "Prodej bytu"))]},
        deadline=time.monotonic() - 1.0,
    )
    walk = portal._agenda_cache[1]
    assert (walk.stop_reason, walk.reached_end) == ("deadline", False)
    captured = _nominations(monkeypatch)
    assert portal.presence_candidates(object(), _CATEGORIES[0], set()) is None
    assert captured == []


def test_a_failed_index_fetch_never_reads_as_an_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exception out of the client aborts the agenda: nothing is cached, so the
    runner's own except arm (reached_end=False) is what the category reports and
    the next descriptor re-walks from page 1."""
    portal = _portal()
    index = _Index(
        {(1, 1): [_page(60, ("r1", "Prodej bytu"))], (1, 2): [_page(60)]},
        raises=(1, 2),
    )
    index.install(monkeypatch)
    with pytest.raises(RuntimeError):
        portal.walk_category(_CATEGORIES[0], object(), False, _Limiter())
    assert portal._agenda_cache == {}
    captured = _nominations(monkeypatch)
    assert portal.presence_candidates(object(), _CATEGORIES[0], {"r1"}) is None
    assert captured == []
