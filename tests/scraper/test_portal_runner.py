"""Hermetic tests for scraper.portal_runner: the generic index-walk + detail-drain
loops, driven by a fake Portal. The queue ops the runner calls directly
(db.claim_detail_batch / complete_detail / fail_detail / reclaim_stale_claims)
are monkeypatched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from scraper import portal_runner
from scraper.portal_runner import DrainItem


class _Conn:
    def __init__(self, close_error: Exception | None = None) -> None:
        self.closed = False
        self.broken = False
        self.rolled_back = 0
        self._close_error = close_error

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


def _is_drop(exc: BaseException) -> bool:
    """A connection-drop style OperationalError (vs a deadlock/serialization
    rollback, which leaves the socket usable)."""
    return isinstance(exc, psycopg.OperationalError) and not isinstance(
        exc, (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure)
    )


class _FakePortal:
    source = "fake"
    index_rate = 1.0

    def __init__(self, *, supports_complete_walk=True, categories=None, complete=True,
                 fetch_kinds=None, walk_fails=None, conn_close_error=None,
                 write_errors=None, reconnect_conns=False) -> None:
        self.supports_complete_walk = supports_complete_walk
        self._categories = categories if categories is not None else ["A", "B"]
        self._complete = complete
        self._fetch_kinds = fetch_kinds or {}
        self._walk_fails = walk_fails or set()
        # Successive exceptions raised by write_details (None = let it succeed),
        # used to simulate a transient drop/deadlock on a flush.
        self._write_errors = list(write_errors or [])
        self._reconnect_conns = reconnect_conns
        self.conn = _Conn(close_error=conn_close_error)
        self.conns = [self.conn]            # every connection handed out, in order
        self.connect_drain_calls = 0
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
        self.connect_drain_calls += 1
        if self._reconnect_conns and self.connect_drain_calls > 1:
            c = _Conn()
            self.conns.append(c)
            return c
        return self.conn

    def walk_category(self, c, conn, dry_run, limiter):
        self.calls["walk"].append(c)
        if c in self._walk_fails:
            raise RuntimeError(f"blocked {c}")
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
        if self._write_errors:
            exc = self._write_errors.pop(0)
            if exc is not None:
                if _is_drop(exc):
                    conn.broken = True   # a drop kills the socket -> reconnect
                raise exc
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


def test_index_walk_bumps_index_pages_per_committed_category(monkeypatch):
    # With a run_id, each category's pages are committed immediately so Health
    # liveness survives a SIGKILL before finalize.
    p = _FakePortal(supports_complete_walk=True, complete=True)
    bumps: list[tuple[int, int]] = []
    monkeypatch.setattr(
        portal_runner.db, "bump_index_pages",
        lambda conn, run_id, n: bumps.append((run_id, n)),
    )
    portal_runner.run_index_walk(p, dry_run=False, run_id=42)
    assert bumps == [(42, 1), (42, 1)]  # one bump per category (cat_pages=1)


def test_index_walk_does_not_bump_without_run_id(monkeypatch):
    p = _FakePortal()
    bumps: list = []
    monkeypatch.setattr(
        portal_runner.db, "bump_index_pages",
        lambda *a: bumps.append(a),
    )
    portal_runner.run_index_walk(p, dry_run=False)  # no run_id
    assert bumps == []


def test_index_walk_clean_stops_when_budget_already_blown(monkeypatch):
    # max_seconds with a deadline in the past -> stop before any category, finalize
    # cleanly (no SIGKILL). monotonic: first call sets the deadline, later calls
    # are past it.
    p = _FakePortal(supports_complete_walk=True, complete=True)
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 9999.0

    monkeypatch.setattr(portal_runner.time, "monotonic", fake_monotonic)
    rc, agg = portal_runner.run_index_walk(p, dry_run=False, max_seconds=10)
    assert rc == 0
    assert p.calls["walk"] == []            # budget blown -> no category walked
    assert p.calls["mark_inactive"] == []   # nothing walked -> nothing flipped (rule #3)
    assert agg["index_pages"] == 0


def test_index_walk_runs_all_when_budget_not_reached(monkeypatch):
    # A generous budget never trips the deadline -> full walk, same as no budget.
    p = _FakePortal(supports_complete_walk=True, complete=True)
    monkeypatch.setattr(portal_runner.time, "monotonic", lambda: 0.0)
    portal_runner.run_index_walk(p, dry_run=False, max_seconds=10_000)
    assert p.calls["walk"] == ["A", "B"]


def test_index_walk_one_failed_category_stays_green_with_error_recorded():
    # One category's walk raises -> the run stays rc=0 (partial failure is
    # tolerated) but the failure is COUNTED in the aggregate, and the other
    # category is still walked + swept.
    p = _FakePortal(supports_complete_walk=True, complete=True, walk_fails={"A"})
    rc, agg = portal_runner.run_index_walk(p, dry_run=False)
    assert rc == 0
    assert agg["errors"] == 1
    assert p.calls["walk"] == ["A", "B"]
    assert [c for c, _ in p.calls["mark_inactive"]] == ["B"]  # failed cat skips sweep
    assert agg["index_pages"] == 1          # only B contributed pages
    assert agg["listings_inactive"] == 2    # B's sweep still happened


def test_index_walk_all_categories_failed_returns_nonzero_rc():
    # EVERY category failed -> the portal is fully blocked (e.g. WAF 403s the
    # runner egress); the run must go red, not record a green zero-listing walk.
    p = _FakePortal(supports_complete_walk=True, complete=True, walk_fails={"A", "B"})
    rc, agg = portal_runner.run_index_walk(p, dry_run=False)
    assert rc != 0
    assert agg["errors"] == 2
    assert p.calls["mark_inactive"] == []
    assert agg["index_pages"] == 0


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


def test_detail_drain_bumps_counts_per_chunk_without_double_count(monkeypatch):
    # With a run_id the counts are persisted per chunk (crash/SIGKILL-survivable);
    # the SUM of bumps must equal the final agg, NOT 2x (finalize won't re-write
    # them). A small batch size forces both in-loop and post-loop flushes.
    monkeypatch.setattr(portal_runner, "DETAIL_BATCH_SIZE", 2)
    cap = _patch_queue(monkeypatch, [
        [("1", None, None), ("2", None, None), ("3", None, None)],
        [("4", None, None), ("5", None, None)],
    ])
    bumps: list[dict[str, int]] = []
    monkeypatch.setattr(
        portal_runner.db, "bump_scrape_run_counts",
        lambda conn, run_id, **kw: bumps.append({"run_id": run_id, **kw}),
    )
    p = _FakePortal()
    rc, agg = portal_runner.run_detail_drain(
        p, None, False, detail_workers=1, detail_rate=1.0, run_id=7)
    assert rc == 0
    assert bumps and all(b["run_id"] == 7 for b in bumps)
    assert sum(b["scraped_new"] for b in bumps) == agg["listings_scraped_new"] == 5
    assert sum(b["found_new"] for b in bumps) == 5
    assert sum(b["updated"] for b in bumps) == agg["listings_updated"] == 0


def test_detail_drain_does_not_bump_without_run_id(monkeypatch):
    _patch_queue(monkeypatch, [[("1", None, None)]])
    bumps: list = []
    monkeypatch.setattr(
        portal_runner.db, "bump_scrape_run_counts",
        lambda *a, **k: bumps.append((a, k)),
    )
    p = _FakePortal()
    portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert bumps == []   # no run_id -> never bumps


def test_detail_drain_time_budget_finalizes_cleanly(monkeypatch):
    # A wall-clock budget makes the drain stop + finalize rather than overrun the
    # job timeout (which would leave a 'stuck' scrape_run). monotonic() jumps far
    # past the tiny budget on the first loop check, so it stops before claiming.
    cap = _patch_queue(monkeypatch, [[("1", None, None)]])
    seq = iter(range(0, 1_000_000, 1000))
    monkeypatch.setattr(portal_runner.time, "monotonic", lambda: float(next(seq)))
    p = _FakePortal()
    rc, agg = portal_runner.run_detail_drain(
        p, None, False, detail_workers=1, detail_rate=1.0, max_seconds=1.0)
    assert rc == 0
    assert cap["claim_n"] == []      # budget exceeded → stopped before claiming
    assert p.conn.closed             # but finalized cleanly (no stuck run)


def test_detail_drain_swallows_teardown_close_failure(monkeypatch):
    # The pooler can silently drop the long-held drain connection; the teardown
    # conn.close() then raises OperationalError. Every batch already committed and
    # the caller finalizes the scrape_run on a SEPARATE connection, so a teardown
    # failure must NOT red the run (the historical ~1% false-red on detail_drain).
    _patch_queue(monkeypatch, [[("1", None, None), ("2", None, None)]])
    p = _FakePortal(conn_close_error=OSError("server closed the connection unexpectedly"))
    rc, agg = portal_runner.run_detail_drain(
        p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0                            # did NOT propagate the close failure
    assert p.calls["write"] == [["1", "2"]]   # the listing writes still committed
    assert agg["listings_scraped_new"] == 2
    assert p.conn.closed                      # close() was attempted


def test_detail_drain_swallows_counts_bump_failure(monkeypatch):
    # Counts are post-commit bookkeeping; a transient pooler reset on the bump
    # must not red a drain whose listing data already committed.
    _patch_queue(monkeypatch, [[("1", None, None)]])

    def _boom(*a, **k):
        raise OSError("connection reset by peer")

    monkeypatch.setattr(portal_runner.db, "bump_scrape_run_counts", _boom)
    p = _FakePortal()
    rc, agg = portal_runner.run_detail_drain(
        p, None, False, detail_workers=1, detail_rate=1.0, run_id=9)
    assert rc == 0                            # bump failure swallowed
    assert p.calls["write"] == [["1"]]        # data still committed
    assert agg["listings_scraped_new"] == 1


# --- transient-DB resilience (db.run_resilient on the drain's hot path) ------


def test_detail_drain_retries_flush_deadlock_on_same_conn(monkeypatch):
    # A deadlock victim on the batch upsert (sreality's historical ~1% red): the
    # flush is retried on the SAME connection (no reconnect) and the run stays
    # green. The batch write is idempotent, so the replay re-commits identically.
    monkeypatch.setattr(portal_runner.db.time, "sleep", lambda *a, **k: None)
    cap = _patch_queue(monkeypatch, [[("1", None, None), ("2", None, None)]])
    p = _FakePortal(write_errors=[psycopg.errors.DeadlockDetected("deadlock detected"), None])
    rc, agg = portal_runner.run_detail_drain(
        p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0
    assert p.calls["write"] == [["1", "2"], ["1", "2"]]  # attempted twice (retry)
    assert cap["complete"] == [["1", "2"]]               # dequeued once, after success
    assert agg["listings_scraped_new"] == 2              # counts applied once, not doubled
    assert p.connect_drain_calls == 1                    # NO reconnect for a deadlock
    assert p.conn.rolled_back == 1                        # aborted txn cleared before retry


def test_detail_drain_reconnects_on_dropped_flush(monkeypatch):
    # The pooler drops the long-held connection mid-flush (realitymix's observed
    # 'SSL error: unexpected eof while reading'): run_resilient reconnects and
    # retries on a fresh connection, and the run stays green instead of reding.
    monkeypatch.setattr(portal_runner.db.time, "sleep", lambda *a, **k: None)
    cap = _patch_queue(monkeypatch, [[("1", None, None), ("2", None, None)]])
    drop = psycopg.OperationalError("SSL error: unexpected eof while reading")
    p = _FakePortal(write_errors=[drop, None], reconnect_conns=True)
    rc, agg = portal_runner.run_detail_drain(
        p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0
    assert p.calls["write"] == [["1", "2"], ["1", "2"]]
    assert cap["complete"] == [["1", "2"]]
    assert agg["listings_scraped_new"] == 2
    assert p.connect_drain_calls == 2          # reconnected after the drop
    assert p.conns[0].closed                   # the broken connection was closed


def test_detail_drain_reds_on_persistent_db_outage(monkeypatch):
    # A genuine sustained outage must still surface (not spin forever): the flush
    # exhausts its retry budget and the exception propagates -> the run reds.
    monkeypatch.setattr(portal_runner.db.time, "sleep", lambda *a, **k: None)
    _patch_queue(monkeypatch, [[("1", None, None)]])
    drop = psycopg.OperationalError("connection refused")
    p = _FakePortal(write_errors=[drop] * 8, reconnect_conns=True)
    with pytest.raises(psycopg.OperationalError):
        portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert len(p.calls["write"]) == portal_runner.db._RESILIENT_ATTEMPTS  # bounded, then give up


def test_detail_drain_gone_path_survives_transient_drop(monkeypatch):
    # The per-item gone bookkeeping (mark inactive + dequeue) is resilient too: a
    # drop while completing a gone listing reconnects rather than reding the run.
    monkeypatch.setattr(portal_runner.db.time, "sleep", lambda *a, **k: None)
    cap = {"complete": [], "calls": 0}
    monkeypatch.setattr(
        portal_runner.db, "reclaim_stale_claims", lambda *a, **k: 0)
    batches = iter([[("9", None, None)], []])
    monkeypatch.setattr(
        portal_runner.db, "claim_detail_batch", lambda *a, **k: next(batches, []))

    def _complete(_c, _src, ids):
        cap["calls"] += 1
        if cap["calls"] == 1:
            _c.broken = True
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        cap["complete"].append(sorted(ids))

    monkeypatch.setattr(portal_runner.db, "complete_detail", _complete)
    p = _FakePortal(fetch_kinds={"9": "gone"}, reconnect_conns=True)
    rc, agg = portal_runner.run_detail_drain(
        p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0
    assert agg["listings_inactive"] == 1
    assert cap["complete"] == [["9"]]          # dequeued after the reconnect retry
    assert p.connect_drain_calls == 2          # reconnected for the gone op


def test_drain_record_failure_drop_on_queue_bump_does_not_replay_ledger(monkeypatch):
    # The failure path bumps TWO non-idempotent counters: record_failure
    # (listing_fetch_failures.attempts+1) then fail_detail (queue attempts+1). They
    # are SPLIT into two run_resilient calls so a transient drop on the queue bump
    # retries ONLY fail_detail — it must never replay (double-advance) the already-
    # committed ledger bump, which would retire a still-retryable listing early.
    monkeypatch.setattr(portal_runner.db.time, "sleep", lambda *a, **k: None)
    record_calls = {"n": 0}
    fail_calls = {"n": 0}

    def _record_failure(_c, _nid, _msg):
        record_calls["n"] += 1

    def _fail_detail(c, _src, _ids, _msg, **k):
        fail_calls["n"] += 1
        if fail_calls["n"] == 1:          # the queue bump drops the first time
            c.broken = True
            raise psycopg.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(portal_runner.db, "fail_detail", _fail_detail)
    conns: list[_Conn] = []

    def _reconnect() -> _Conn:
        c = _Conn()
        conns.append(c)
        return c

    portal = SimpleNamespace(source="fake", record_failure=_record_failure)
    conn0 = _Conn()
    out = portal_runner._drain_record_failure(portal, conn0, "55", "boom", _reconnect)

    assert record_calls["n"] == 1     # ledger bump applied EXACTLY once (not replayed)
    assert fail_calls["n"] == 2       # queue bump retried after the drop
    assert conn0.broken and conn0.closed
    assert out is conns[-1]           # returned the reconnected conn


def test_run_resilient_closes_self_opened_conn_on_exhaustion(monkeypatch):
    # On the give-up path run_resilient must close a connection IT opened (the
    # caller never received it), but never the caller's original.
    monkeypatch.setattr(portal_runner.db.time, "sleep", lambda *a, **k: None)
    opened: list[_Conn] = []

    def _reconnect() -> _Conn:
        c = _Conn()
        c.broken = True          # every reconnect yields an already-doomed socket
        opened.append(c)
        return c

    original = _Conn()
    original.broken = True

    def _always_drops(_c):
        raise psycopg.OperationalError("connection refused")

    with pytest.raises(psycopg.OperationalError):
        portal_runner.db.run_resilient(
            original, _always_drops, reconnect=_reconnect, attempts=3, base_delay=0)

    assert original.closed                         # broken original closed in-loop
    assert opened and all(c.closed for c in opened)  # every self-opened conn closed
