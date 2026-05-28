"""Hermetic tests for scraper.portal_runner: the generic index-walk + detail-drain
loops, driven by a fake Portal. The queue ops the runner calls directly
(db.claim_detail_batch / complete_detail / fail_detail / reclaim_stale_claims)
are monkeypatched.
"""

from __future__ import annotations

from typing import Any

from scraper import portal_runner
from scraper.portal_runner import DrainItem


class _Conn:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakePortal:
    source = "fake"
    index_rate = 1.0

    def __init__(self, *, supports_complete_walk=True, categories=None, complete=True,
                 fetch_kinds=None) -> None:
        self.supports_complete_walk = supports_complete_walk
        self._categories = categories if categories is not None else ["A", "B"]
        self._complete = complete
        self._fetch_kinds = fetch_kinds or {}
        self.conn = _Conn()
        self.calls: dict[str, list] = {
            "walk": [], "mark_inactive": [], "active_count": [],
            "write": [], "gone": [], "failure": [],
        }

    def categories(self):
        return list(self._categories)

    def category_labels(self, c):
        return (str(c), "t")

    def connect_index(self):
        return self.conn

    def connect_drain(self):
        return self.conn

    def walk_category(self, c, conn, dry_run, limiter):
        self.calls["walk"].append(c)
        return ({1, 2}, {"found_new": 2, "enqueued": 2}, 2, 1, self._complete)

    def mark_inactive(self, conn, c, seen):
        self.calls["mark_inactive"].append((c, set(seen)))
        return len(seen)

    def active_count(self, conn, c):
        self.calls["active_count"].append(c)
        return 5

    def make_client(self, limiter):
        return object()

    def fetch_detail(self, client, native_id, ref):
        kind = self._fetch_kinds.get(native_id, "ok")
        return DrainItem(
            native_id=native_id, kind=kind,
            payload=native_id, error=("boom" if kind == "error" else None),
        )

    def write_details(self, conn, items):
        self.calls["write"].append([it.native_id for it in items])
        return {"new": len(items), "updated": 0, "unchanged": 0, "images_discovered": 0}

    def mark_gone(self, conn, native_id):
        self.calls["gone"].append(native_id)

    def record_failure(self, conn, native_id, message):
        self.calls["failure"].append(native_id)

    def claimable_count(self, conn):
        return 0


# --- run_index_walk ---------------------------------------------------------


def test_index_walk_marks_inactive_when_complete_and_supported():
    p = _FakePortal(supports_complete_walk=True, complete=True)
    rc, agg = portal_runner.run_index_walk(p, dry_run=False)
    assert rc == 0
    assert p.calls["walk"] == ["A", "B"]
    assert [c for c, _ in p.calls["mark_inactive"]] == ["A", "B"]
    assert agg["index_pages"] == 2          # 1 page per category
    assert agg["listings_inactive"] == 4    # 2 seen ids flipped per category
    assert agg["listings_scraped_new"] == 0
    assert p.conn.closed


def test_index_walk_skips_inactive_when_incomplete():
    p = _FakePortal(supports_complete_walk=True, complete=False)
    portal_runner.run_index_walk(p, dry_run=False)
    assert p.calls["mark_inactive"] == []   # incomplete walk -> no flip


def test_index_walk_skips_inactive_when_portal_cannot_complete():
    p = _FakePortal(supports_complete_walk=False, complete=True)
    portal_runner.run_index_walk(p, dry_run=False)
    assert p.calls["mark_inactive"] == []   # partial-walk portal never flips


def test_index_walk_dry_run_uses_no_connection():
    p = _FakePortal()
    portal_runner.run_index_walk(p, dry_run=True)
    # walk still runs (conn=None passed through) but no mark_inactive
    assert p.calls["walk"] == ["A", "B"]
    assert p.calls["mark_inactive"] == []


# --- run_detail_drain -------------------------------------------------------


def _patch_queue(monkeypatch, claim_batches):
    cap = {"complete": [], "fail": [], "claim_n": [], "reclaim": 0}
    it = iter(list(claim_batches) + [[]])
    monkeypatch.setattr(
        portal_runner.db, "reclaim_stale_claims",
        lambda _c, _src: cap.__setitem__("reclaim", cap["reclaim"] + 1) or 0,
    )

    def _claim(_c, _src, n):
        cap["claim_n"].append(n)
        return next(it, [])

    monkeypatch.setattr(portal_runner.db, "claim_detail_batch", _claim)
    monkeypatch.setattr(
        portal_runner.db, "complete_detail",
        lambda _c, _src, ids: cap["complete"].append(sorted(ids)),
    )
    monkeypatch.setattr(
        portal_runner.db, "fail_detail",
        lambda _c, _src, ids, msg, **k: cap["fail"].append(sorted(ids)),
    )
    return cap


def test_detail_drain_batches_and_completes(monkeypatch):
    cap = _patch_queue(monkeypatch, [[("1", None, None), ("2", None, None)]])
    p = _FakePortal()
    rc, agg = portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0
    assert p.calls["write"] == [["1", "2"]]
    assert cap["complete"] == [["1", "2"]]
    assert agg["listings_scraped_new"] == 2
    assert p.conn.closed


def test_detail_drain_routes_gone_and_error(monkeypatch):
    cap = _patch_queue(monkeypatch, [[("10", None, None), ("11", None, None), ("12", None, None)]])
    p = _FakePortal(fetch_kinds={"11": "gone", "12": "error"})
    rc, agg = portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert p.calls["gone"] == ["11"]
    assert p.calls["failure"] == ["12"]
    assert cap["fail"] == [["12"]]
    assert sorted(x for b in p.calls["write"] for x in b) == ["10"]
    assert sorted(x for b in cap["complete"] for x in b) == ["10", "11"]
    assert agg["errors"] == 1 and agg["listings_inactive"] == 1


def test_detail_drain_respects_max_claims(monkeypatch):
    cap = _patch_queue(monkeypatch, [[("1", None, None), ("2", None, None)]])
    p = _FakePortal()
    portal_runner.run_detail_drain(p, 2, False, detail_workers=1, detail_rate=1.0)
    assert cap["claim_n"] == [2]


def test_detail_drain_dry_run_does_not_claim(monkeypatch):
    cap = _patch_queue(monkeypatch, [[("1", None, None)]])
    p = _FakePortal()
    rc, agg = portal_runner.run_detail_drain(p, 50, True, detail_workers=1, detail_rate=1.0)
    assert rc == 0 and agg == {}
    assert cap["claim_n"] == []   # dry-run never claims
