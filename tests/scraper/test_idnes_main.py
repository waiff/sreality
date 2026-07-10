"""Hermetic tests for IdnesPortal.walk_category's price-change diff — the FX
price-jitter tolerance (PortalLimits.price_change_min_pct). idnes re-displays
its foreign inventory's FX-converted CZK price with ~0.04-0.08% daily drift;
the walk must read that as unchanged (touch, no enqueue) while a genuine cut
still enqueues. No network/DB: a fake client feeds canned index HTML and the
db seams are monkeypatched.
"""

from __future__ import annotations

from typing import Any

from scraper import db, idnes_main
from scraper.idnes_main import IdnesPortal
from scraper.portal import default_config

_JITTER_ID = "aaaaaaaaaaaaaaaaaaaaaa01"   # stored 23 692 431, index +0.075%
_CUT_ID = "aaaaaaaaaaaaaaaaaaaaaa02"      # stored 10 000 000, index -1%
_NEW_ID = "aaaaaaaaaaaaaaaaaaaaaa03"      # not in the DB
_NULLPRICE_ID = "aaaaaaaaaaaaaaaaaaaaaa04"  # stored NULL, index has a price

_STORED = {
    _JITTER_ID: {"sreality_id": -101, "price_czk": 23_692_431, "last_seen_at": None},
    _CUT_ID: {"sreality_id": -102, "price_czk": 10_000_000, "last_seen_at": None},
    _NULLPRICE_ID: {"sreality_id": -104, "price_czk": None, "last_seen_at": None},
}

_INDEX_PRICES = {
    _JITTER_ID: "23 710 239 Kč",
    _CUT_ID: "9 900 000 Kč",
    _NEW_ID: "5 000 000 Kč",
    _NULLPRICE_ID: "7 500 000 Kč",
}


def _index_html(ids: list[str]) -> str:
    cards = "".join(
        f'<div class="c-products__item">'
        f'<a class="c-products__link" href="/detail/prodej/byt/praha/{nid}/">x</a>'
        f'<h2 class="c-products__title">Prodej bytu 2+kk</h2>'
        f'<p class="c-products__price">{_INDEX_PRICES[nid]}</p></div>'
        for nid in ids
    )
    return f"<html><body><span>{len(ids)} nemovitostí</span>{cards}</body></html>"


class _FakeClient:
    def __init__(self, html: str) -> None:
        self._html = html

    def fetch_index(self, sale_type: str, cat: str, page: Any, locality: Any = None):
        return self._html, 200


def _walk(monkeypatch, ids: list[str], **portal_kw: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {"touched": [], "entries": []}

    def fake_summary(conn: Any, source: str, native_ids: list[str]) -> dict[str, Any]:
        return {n: _STORED[n] for n in native_ids if n in _STORED}

    def fake_touch(conn: Any, pks: list[int]) -> None:
        captured["touched"] = list(pks)

    def fake_enqueue(conn: Any, source: str, entries: list[tuple]) -> int:
        captured["entries"] = list(entries)
        return len(entries)

    monkeypatch.setattr(db, "index_summary_native", fake_summary)
    monkeypatch.setattr(db, "touch_listings", fake_touch)
    monkeypatch.setattr(db, "enqueue_detail", fake_enqueue)
    monkeypatch.setattr(
        idnes_main, "IdnesClient", lambda limiter=None: _FakeClient(_index_html(ids))
    )
    portal = IdnesPortal(default_config("idnes"), **portal_kw)
    seen, counts, total, pages, complete = portal.walk_category(
        {"sale_type": "prodej", "category": "byty"},
        conn=object(), dry_run=False, limiter=None,
    )
    captured["seen"], captured["counts"] = seen, counts
    return captured


def test_fx_jitter_reads_unchanged_while_genuine_cut_enqueues(monkeypatch):
    got = _walk(monkeypatch, [_JITTER_ID, _CUT_ID, _NEW_ID])
    # the 0.075% FX move is under the 0.5% baked tolerance -> touched, no enqueue
    assert got["touched"] == [-101]
    enqueued = {e[0]: e[3] for e in got["entries"]}
    assert enqueued == {
        _CUT_ID: db.QUEUE_PRIORITY_CHANGED,   # -1% is a real price cut
        _NEW_ID: db.QUEUE_PRIORITY_NEW,
    }
    assert got["counts"] == {"found_new": 1, "enqueued": 2}


def test_zero_tolerance_override_restores_exact_compare(monkeypatch):
    got = _walk(
        monkeypatch, [_JITTER_ID, _CUT_ID], price_change_min_pct=0.0
    )
    assert got["touched"] == []
    enqueued = {e[0]: e[3] for e in got["entries"]}
    assert enqueued == {
        _JITTER_ID: db.QUEUE_PRIORITY_CHANGED,  # exact compare: any move enqueues
        _CUT_ID: db.QUEUE_PRIORITY_CHANGED,
    }


def test_null_to_value_price_transition_always_enqueues(monkeypatch):
    got = _walk(monkeypatch, [_NULLPRICE_ID])
    assert got["touched"] == []
    assert [e[0] for e in got["entries"]] == [_NULLPRICE_ID]
