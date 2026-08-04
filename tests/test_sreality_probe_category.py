"""SrealityPortal.probe_category (docs/design/portal-order-fidelity.md, Phase 4).

sreality's own newest-first-ish discovery probe, modeled on
CeskerealityPortal.probe_category: UNSPLIT (no district-split — the
deep-pagination 422 is offset-triggered, not size-triggered, so a shallow probe
never needs it) and early-stopping the moment a page yields zero new ids.

Hermetic: a fake SrealityClient (per fetch_index_page(offset) -> list[dict])
stands in for the network, and db.index_summary / db.touch_listings /
db.enqueue_detail are monkeypatched so no DB is touched — the same style
test_main.py's patched_db fixture uses for _walk_category.
"""

from __future__ import annotations

from typing import Any

from scraper import main as scraper_main


class _FakeProbeClient:
    per_page = 3

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self._pages = pages
        self.result_size = None
        self.calls: list[int] = []

    def fetch_index_page(self, offset: int) -> list[dict[str, Any]]:
        self.calls.append(offset)
        idx = offset // self.per_page
        if idx >= len(self._pages):
            return []
        return self._pages[idx]


def _estate(sid: int, price: int | None = 1000) -> dict[str, Any]:
    d: dict[str, Any] = {"hash_id": sid}
    if price is not None:
        d["price_summary_czk"] = price
    return d


def _patch_probe(monkeypatch, client: _FakeProbeClient, *, existing: dict[int, dict] | None = None):
    calls: dict[str, list] = {
        "touch": [], "enqueue": [], "index_summary_ids": [],
    }
    monkeypatch.setattr(scraper_main, "_build_client", lambda cm, ct, limiter=None: client)

    def _fake_index_summary(_conn, ids):
        ids = set(ids)
        calls["index_summary_ids"].append(ids)
        return {sid: row for sid, row in (existing or {}).items() if sid in ids}

    monkeypatch.setattr(scraper_main.db, "index_summary", _fake_index_summary)
    monkeypatch.setattr(
        scraper_main.db, "touch_listings",
        lambda _conn, ids: calls["touch"].append(sorted(ids)) or len(ids),
    )

    def _fake_enqueue(_conn, source, entries):
        e = list(entries)
        calls["enqueue"].append(e)
        return len(e)

    monkeypatch.setattr(scraper_main.db, "enqueue_detail", _fake_enqueue)
    return calls


def test_probe_category_stops_on_first_all_known_page(monkeypatch):
    # Page 1: two new ids. Page 2: both already known -> early stop; page 3
    # (which WOULD have new ids) must never be fetched.
    client = _FakeProbeClient(pages=[
        [_estate(1), _estate(2)],
        [_estate(3), _estate(4)],
        [_estate(5)],
    ])
    existing = {
        3: {"price_czk": 1000, "last_seen_at": None},
        4: {"price_czk": 1000, "last_seen_at": None},
    }
    calls = _patch_probe(monkeypatch, client, existing=existing)
    portal = scraper_main.SrealityPortal()
    seen, counts, total, pages, complete = portal.probe_category(
        (1, 2), conn=object(), dry_run=False, limiter=None, probe_pages=5,
    )
    assert client.calls == [0, 3]  # never reached offset=6 (page 3)
    assert pages == 2
    assert seen == {1, 2, 3, 4}
    assert counts["found_new"] == 2  # only page 1's ids
    assert complete is False
    # page 2's ids were both unchanged (same price) -> touched, not enqueued.
    assert calls["touch"] == [[3, 4]]
    enqueued_ids = {e[0] for batch in calls["enqueue"] for e in batch}
    assert enqueued_ids == {"1", "2"}


def test_probe_category_respects_probe_pages_cap(monkeypatch):
    # Every page has new ids, so nothing triggers early stop -- probe_pages
    # alone must bound the loop.
    client = _FakeProbeClient(pages=[
        [_estate(1)], [_estate(2)], [_estate(3)], [_estate(4)],
    ])
    calls = _patch_probe(monkeypatch, client)
    portal = scraper_main.SrealityPortal()
    seen, counts, total, pages, complete = portal.probe_category(
        (1, 2), conn=object(), dry_run=False, limiter=None, probe_pages=2,
    )
    assert pages == 2
    assert client.calls == [0, 3]
    assert seen == {1, 2}


def test_probe_category_hard_cap_independent_of_probe_pages(monkeypatch):
    # A misconfigured very-high probe_pages must not let the loop run away --
    # PROBE_MAX_PAGES is the defense-in-depth ceiling.
    pages = [[_estate(i)] for i in range(1, scraper_main.PROBE_MAX_PAGES + 5)]
    client = _FakeProbeClient(pages=pages)
    _patch_probe(monkeypatch, client)
    portal = scraper_main.SrealityPortal()
    _, _, _, pages_fetched, _ = portal.probe_category(
        (1, 2), conn=object(), dry_run=False, limiter=None, probe_pages=999,
    )
    assert pages_fetched == scraper_main.PROBE_MAX_PAGES


def test_probe_category_stops_on_empty_page(monkeypatch):
    client = _FakeProbeClient(pages=[[_estate(1)], []])
    _patch_probe(monkeypatch, client)
    portal = scraper_main.SrealityPortal()
    seen, counts, total, pages, complete = portal.probe_category(
        (1, 2), conn=object(), dry_run=False, limiter=None, probe_pages=5,
    )
    # Page 1 has a new id so the loop doesn't early-stop on it; page 2 is
    # empty, which stops the loop before any further fetch.
    assert pages == 2
    assert seen == {1}


def test_probe_category_price_change_uses_changed_priority(monkeypatch):
    client = _FakeProbeClient(pages=[[_estate(1, price=2000)]])
    existing = {1: {"price_czk": 1000, "last_seen_at": None}}
    calls = _patch_probe(monkeypatch, client, existing=existing)
    portal = scraper_main.SrealityPortal()
    portal.probe_category(
        (1, 2), conn=object(), dry_run=False, limiter=None, probe_pages=1,
    )
    assert len(calls["enqueue"]) == 1
    (native_id, ref, price, priority), = calls["enqueue"][0]
    assert native_id == "1"
    assert priority == scraper_main.db.QUEUE_PRIORITY_CHANGED


def test_probe_category_dry_run_conn_none_skips_db_writes(monkeypatch):
    # conn=None (dry_run) must never call index_summary/touch/enqueue -- only
    # the client is exercised.
    client = _FakeProbeClient(pages=[[_estate(1), _estate(2)]])
    called = {"index_summary": False}
    monkeypatch.setattr(scraper_main, "_build_client", lambda cm, ct, limiter=None: client)
    monkeypatch.setattr(
        scraper_main.db, "index_summary",
        lambda *a, **k: called.__setitem__("index_summary", True) or {},
    )
    portal = scraper_main.SrealityPortal()
    seen, counts, total, pages, complete = portal.probe_category(
        (1, 2), conn=None, dry_run=True, limiter=None, probe_pages=5,
    )
    assert called["index_summary"] is False
    assert seen == {1, 2}
    assert counts["enqueued"] == 0


def test_probe_category_is_recognized_by_run_index_probe_as_bespoke(monkeypatch):
    # run_index_probe prefers probe_category over the generic capper fallback
    # whenever both exist (matches ceskereality's pattern) -- pin that
    # SrealityPortal now qualifies (has probe_category, no set_index_page_cap).
    portal = scraper_main.SrealityPortal()
    assert hasattr(portal, "probe_category")
    assert not hasattr(portal, "set_index_page_cap")
