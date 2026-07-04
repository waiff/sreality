"""remax_main on the portal framework: RemaxPortal complete-walk via AGENDA-GRAIN
delisting. The agenda key is `sale` (1=prodej / 2=pronajem) — these tests pin the
agenda-grain mark_inactive (sweep the whole agenda once, against every agenda id,
only when the agenda walk reached its reported total).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

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


def _portal() -> RemaxPortal:
    return RemaxPortal(_config())


class _Limiter:
    def acquire(self) -> None: ...
    def penalize(self) -> None: ...


class _IdxClient:
    def __init__(self, *a, **k) -> None: ...
    def fetch_index(self, *, sale=None, stranka=None):
        return ("<html>", 200)


def test_portal_reads_complete_walk_from_config():
    assert _portal().supports_complete_walk is True


def _walk_sale_agenda(monkeypatch, portal, *, total, items):
    """Drive the sale agenda (sale=1) so the portal's agenda cache is populated."""
    page1 = SimpleNamespace(total=total, next_offset=None, items=items)
    empty = SimpleNamespace(total=total, next_offset=None, items=[])
    seq = iter([page1, empty, empty])
    monkeypatch.setattr(remax_main, "parse_index", lambda _h: next(seq))
    monkeypatch.setattr(remax_main, "RemaxClient", _IdxClient)
    monkeypatch.setattr(remax_main.db, "index_summary_native", lambda *a, **k: {})
    monkeypatch.setattr(remax_main.db, "enqueue_detail", lambda *a, **k: 0)
    monkeypatch.setattr(remax_main.db, "touch_listings", lambda *a, **k: None)
    portal.walk_category(_CATEGORIES[0], object(), False, _Limiter())  # byt·prodej, sale=1


def _items(*specs):
    base = "https://www.remax-czech.cz/reality/detail/"
    return [
        SimpleNamespace(source_id_native=n, detail_path=f"{base}{n}/",
                        price_text="5 000 000 Kč", title=t)
        for n, t in specs
    ]


def test_mark_inactive_is_agenda_grain(monkeypatch):
    portal = _portal()
    _walk_sale_agenda(monkeypatch, portal, total=3, items=_items(
        ("r1", "Prodej bytu 2+kk"), ("r2", "Prodej bytu"), ("r3", "Prodej rodinného domu"),
    ))
    captured: list[Any] = []
    monkeypatch.setattr(
        remax_main.db, "mark_inactive_agenda",
        lambda _c, source, ct, seen, *, min_unseen_hours: (
            captured.append((source, ct, set(seen), min_unseen_hours)) or 4
        ),
    )
    # First prodej descriptor (byt) sweeps the whole sale agenda.
    assert portal.mark_inactive(object(), _CATEGORIES[0], {"r1", "r2"}) == 4
    source, ct, seen, hrs = captured[0]
    assert source == "remax" and ct == "prodej" and hrs == 12
    assert seen == {"r1", "r2", "r3"}              # the FULL sale agenda, not the byt slice
    # A second prodej descriptor (dum) must NOT re-sweep.
    assert portal.mark_inactive(object(), _CATEGORIES[1], {"r3"}) == 0
    assert len(captured) == 1


def test_walk_priceless_card_is_unchanged_not_changed(monkeypatch):
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
    page1 = SimpleNamespace(total=4, next_offset=None, items=items)
    empty = SimpleNamespace(total=4, next_offset=None, items=[])
    seq = iter([page1, empty, empty])
    monkeypatch.setattr(remax_main, "parse_index", lambda _h: next(seq))
    monkeypatch.setattr(remax_main, "RemaxClient", _IdxClient)
    monkeypatch.setattr(
        remax_main.db, "index_summary_native",
        lambda *a, **k: {
            "r1": {"sreality_id": -1, "price_czk": 5_000_000},
            "r2": {"sreality_id": -2, "price_czk": None},
            "r3": {"sreality_id": -3, "price_czk": 5_500_000},
        },
    )
    touched: list[list[int]] = []
    enqueued: list[tuple[str, int]] = []
    monkeypatch.setattr(
        remax_main.db, "touch_listings", lambda _c, pks: touched.append(list(pks)))
    monkeypatch.setattr(
        remax_main.db, "enqueue_detail",
        lambda _c, _s, entries: enqueued.extend((n, prio) for n, _r, _p, prio in entries) or len(entries),
    )
    _seen, counts, *_ = portal.walk_category(_CATEGORIES[0], object(), False, _Limiter())
    assert touched == [[-1, -2]]
    assert enqueued == [
        ("r3", remax_main.db.QUEUE_PRIORITY_CHANGED),
        ("r4", remax_main.db.QUEUE_PRIORITY_NEW),
    ]
    assert counts == {"found_new": 1, "enqueued": 2}


def test_mark_inactive_skips_incomplete_agenda(monkeypatch):
    portal = _portal()
    # total=10 but only 2 collected -> walk.complete False -> no index-absence delist.
    _walk_sale_agenda(monkeypatch, portal, total=10, items=_items(
        ("r1", "Prodej bytu"), ("r2", "Prodej bytu"),
    ))
    called = {"n": 0}
    monkeypatch.setattr(
        remax_main.db, "mark_inactive_agenda",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or 0,
    )
    assert portal.mark_inactive(object(), _CATEGORIES[0], {"r1", "r2"}) == 0
    assert called["n"] == 0
