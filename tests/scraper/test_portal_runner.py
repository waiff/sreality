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
            "walk": [], "active_count": [],
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

    def walk_category(self, c, conn, dry_run, limiter, deadline=None):
        self.calls["walk"].append(c)
        self.calls.setdefault("deadline", []).append(deadline)
        if c in self._walk_fails:
            raise RuntimeError(f"blocked {c}")
        return ({1, 2}, {"found_new": 2, "enqueued": 2}, 2, 1, self._complete)

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
        # Sorted: intra-batch order is NOT a contract. The drain collects fetch
        # results via as_completed(), which yields already-finished futures in
        # set order — memory-address dependent, so even detail_workers=1 can
        # buffer ["2","1"]. (Same posture as the `complete` capture below.)
        self.calls["write"].append(sorted(it.native_id for it in items))
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


def _nominations(monkeypatch):
    """Capture what the runner nominates for a page check (rule #3, 2026-09-07:
    a complete walk nominates unseen rows; the drain's page visit decides)."""
    cap: dict[str, list] = {"candidates": [], "queued": []}

    def fake_candidates(conn, source, cm, ct, seen, *, seen_key="native", **kw):
        cap["candidates"].append((cm, set(seen), seen_key))
        return ([("7", "https://x/7", None), ("8", "https://x/8", None)], 9)

    def fake_enqueue(conn, source, cm, ct, candidates, *, active_rows, subtype=None):
        cap["queued"].append((cm, [c[0] for c in candidates], active_rows))
        cap.setdefault("scopes", []).append((cm, subtype))
        return len(candidates), 0

    monkeypatch.setattr(portal_runner.db, "presence_candidates", fake_candidates)
    monkeypatch.setattr(portal_runner.db, "enqueue_presence_checks", fake_enqueue)
    return cap


def test_index_walk_nominates_unseen_rows_when_complete(monkeypatch):
    cap = _nominations(monkeypatch)
    p = _FakePortal(supports_complete_walk=True, complete=True)
    rc, agg = portal_runner.run_index_walk(p, dry_run=False)
    assert rc == 0
    assert p.calls["walk"] == ["A", "B"]
    assert [c for c, _, _ in cap["candidates"]] == ["A", "B"]
    assert cap["candidates"][0][1] == {1, 2}            # the walk's seen set is what is excluded
    assert [c for c, _, _ in cap["queued"]] == ["A", "B"]
    assert agg["index_pages"] == 2
    assert agg["listings_inactive"] == 0                # the walk itself flips nothing now
    assert sum(c["listings_to_verify"] for c in agg["by_category"]) == 4
    assert agg["listings_scraped_new"] == 0
    assert p.conn.closed


def test_index_walk_nominates_nothing_when_incomplete(monkeypatch):
    """An unproven walk's unseen set is the part of the portal it never reached,
    not evidence; nominating it would flood the drain with fetches."""
    cap = _nominations(monkeypatch)
    p = _FakePortal(supports_complete_walk=True, complete=False)
    portal_runner.run_index_walk(p, dry_run=False)
    assert cap["candidates"] == [] and cap["queued"] == []


def test_index_walk_nominates_even_when_portal_flag_is_down(monkeypatch):
    """supports_complete_walk gated a sweep that could be wrong. A nomination
    cannot be -- the page decides -- so the flag no longer gates it."""
    cap = _nominations(monkeypatch)
    p = _FakePortal(supports_complete_walk=False, complete=True)
    portal_runner.run_index_walk(p, dry_run=False)
    assert [c for c, _, _ in cap["queued"]] == ["A", "B"]


def test_index_walk_uses_a_portal_override_for_nomination(monkeypatch):
    """A portal whose index sections do not map 1:1 onto a category (bazos
    subtypes, ceskereality sibling slices) supplies its own candidates; None
    means 'not yet' and queues nothing."""
    cap = _nominations(monkeypatch)
    p = _FakePortal(complete=True)
    seen_by: list = []

    def presence_candidates(conn, category, seen):
        seen_by.append(category)
        return None if category == "A" else ([("42", "https://x/42", 100)], 3)

    p.presence_candidates = presence_candidates
    portal_runner.run_index_walk(p, dry_run=False)
    assert seen_by == ["A", "B"]
    assert cap["candidates"] == []                      # default nomination bypassed
    assert cap["queued"] == [("B", ["42"], 3)]


def test_index_walk_override_scope_reaches_the_throttle(monkeypatch):
    """bazos narrows to a subtype, remax/maxima widen to an agenda; the throttle
    and the operator override must see the scope the candidates came from."""
    cap = _nominations(monkeypatch)
    p = _FakePortal(complete=True, categories=["A"])
    p.presence_candidates = lambda conn, category, seen: ([("1", "https://x/1", None)], 8, {"subtype": "chata"})
    portal_runner.run_index_walk(p, dry_run=False)
    assert cap["scopes"] == [("A", "chata")]
    p2 = _FakePortal(complete=True, categories=["A"])
    p2.presence_candidates = lambda conn, category, seen: ([("1", "https://x/1", None)], 8, {"category_main": None})
    cap2 = _nominations(monkeypatch)
    portal_runner.run_index_walk(p2, dry_run=False)
    assert cap2["scopes"] == [(None, None)]


def test_index_walk_that_saw_nothing_nominates_nothing(monkeypatch):
    """A measured zero is a complete walk, but an empty seen set would nominate
    the WHOLE scope (`<> ALL('{}')` is true for every row) -- exactly when the
    portal is least trustworthy (a retired slug, a throttled shell page)."""
    cap = _nominations(monkeypatch)
    p = _FakePortal(complete=True, categories=["A"])
    p.walk_category = lambda c, conn, dry_run, limiter, deadline=None: (set(), {"found_new": 0, "enqueued": 0}, 0, 1, True)
    portal_runner.run_index_walk(p, dry_run=False)
    assert cap["candidates"] == [] and cap["queued"] == []


def test_index_walk_dry_run_uses_no_connection(monkeypatch):
    cap = _nominations(monkeypatch)
    p = _FakePortal()
    portal_runner.run_index_walk(p, dry_run=True)
    # walk still runs (conn=None passed through) but nothing is nominated
    assert p.calls["walk"] == ["A", "B"]
    assert cap["queued"] == []


def test_index_walk_bumps_index_pages_per_committed_category(monkeypatch):
    _nominations(monkeypatch)
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
    _nominations(monkeypatch)
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
    cap = _nominations(monkeypatch)
    rc, agg = portal_runner.run_index_walk(p, dry_run=False, max_seconds=10)
    assert rc == 0
    assert p.calls["walk"] == []            # budget blown -> no category walked
    assert cap["queued"] == []              # nothing walked -> nothing nominated (rule #3)
    assert agg["index_pages"] == 0


def test_index_walk_runs_all_when_budget_not_reached(monkeypatch):
    _nominations(monkeypatch)
    # A generous budget never trips the deadline -> full walk, same as no budget.
    p = _FakePortal(supports_complete_walk=True, complete=True)
    monkeypatch.setattr(portal_runner.time, "monotonic", lambda: 0.0)
    portal_runner.run_index_walk(p, dry_run=False, max_seconds=10_000)
    assert p.calls["walk"] == ["A", "B"]


def test_index_walk_one_failed_category_stays_green_with_error_recorded(monkeypatch):
    # One category's walk raises -> the run stays rc=0 (partial failure is
    # tolerated) but the failure is COUNTED in the aggregate, and the other
    # category is still walked + its unseen rows nominated.
    cap = _nominations(monkeypatch)
    p = _FakePortal(supports_complete_walk=True, complete=True, walk_fails={"A"})
    rc, agg = portal_runner.run_index_walk(p, dry_run=False)
    assert rc == 0
    assert agg["errors"] == 1
    assert p.calls["walk"] == ["A", "B"]
    assert [c for c, _, _ in cap["queued"]] == ["B"]   # failed cat nominates nothing
    assert agg["index_pages"] == 1          # only B contributed pages
    assert sum(c["listings_to_verify"] for c in agg["by_category"]) == 2


def test_index_walk_all_categories_failed_returns_nonzero_rc(monkeypatch):
    # EVERY category failed -> the portal is fully blocked (e.g. WAF 403s the
    # runner egress); the run must go red, not record a green zero-listing walk.
    cap = _nominations(monkeypatch)
    p = _FakePortal(supports_complete_walk=True, complete=True, walk_fails={"A", "B"})
    rc, agg = portal_runner.run_index_walk(p, dry_run=False)
    assert rc != 0
    assert agg["errors"] == 2
    assert cap["queued"] == []
    assert agg["index_pages"] == 0


# --- run_detail_drain -------------------------------------------------------


def _patch_queue(monkeypatch, claim_batches):
    cap = {"complete": [], "complete_outcomes": [], "fail": [], "claim_n": [], "reclaim": 0}
    it = iter(list(claim_batches) + [[]])
    monkeypatch.setattr(
        portal_runner.db, "reclaim_stale_claims",
        lambda _c, _src: cap.__setitem__("reclaim", cap["reclaim"] + 1) or 0,
    )

    def _claim(_c, _src, n):
        cap["claim_n"].append(n)
        return next(it, [])

    monkeypatch.setattr(portal_runner.db, "claim_detail_batch", _claim)

    def _complete(_c, _src, ids, outcome="written"):
        cap["complete"].append(sorted(ids))
        cap["complete_outcomes"].append(outcome)

    monkeypatch.setattr(portal_runner.db, "complete_detail", _complete)
    monkeypatch.setattr(
        portal_runner.db, "fail_detail",
        lambda _c, _src, ids, msg, **k: cap["fail"].append(sorted(ids)),
    )
    return cap


def test_detail_drain_batches_and_completes(monkeypatch):
    cap = _patch_queue(monkeypatch, [[("1", None, None, None, None), ("2", None, None, None, None)]])
    p = _FakePortal()
    rc, agg = portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0
    assert p.calls["write"] == [["1", "2"]]
    assert cap["complete"] == [["1", "2"]]
    assert agg["listings_scraped_new"] == 2
    assert p.conn.closed


def test_detail_drain_routes_gone_and_error(monkeypatch):
    cap = _patch_queue(monkeypatch, [[("10", None, None, None, None), ("11", None, None, None, None), ("12", None, None, None, None)]])
    p = _FakePortal(fetch_kinds={"11": "gone", "12": "error"})
    rc, agg = portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert p.calls["gone"] == ["11"]
    assert p.calls["failure"] == ["12"]
    assert cap["fail"] == [["12"]]
    assert sorted(x for b in p.calls["write"] for x in b) == ["10"]
    completions = list(zip(cap["complete"], cap["complete_outcomes"]))
    assert (["11"], "gone") in completions
    assert (["10"], "written") in completions
    assert sorted(x for b in cap["complete"] for x in b) == ["10", "11"]
    assert agg["errors"] == 1 and agg["listings_inactive"] == 1


def test_detail_drain_respects_max_claims(monkeypatch):
    cap = _patch_queue(monkeypatch, [[("1", None, None, None, None), ("2", None, None, None, None)]])
    p = _FakePortal()
    portal_runner.run_detail_drain(p, 2, False, detail_workers=1, detail_rate=1.0)
    assert cap["claim_n"] == [2]


def test_detail_drain_dry_run_does_not_claim(monkeypatch):
    cap = _patch_queue(monkeypatch, [[("1", None, None, None, None)]])
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
        [("1", None, None, None, None), ("2", None, None, None, None), ("3", None, None, None, None)],
        [("4", None, None, None, None), ("5", None, None, None, None)],
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
    _patch_queue(monkeypatch, [[("1", None, None, None, None)]])
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
    cap = _patch_queue(monkeypatch, [[("1", None, None, None, None)]])
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
    _patch_queue(monkeypatch, [[("1", None, None, None, None), ("2", None, None, None, None)]])
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
    _patch_queue(monkeypatch, [[("1", None, None, None, None)]])

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
    cap = _patch_queue(monkeypatch, [[("1", None, None, None, None), ("2", None, None, None, None)]])
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
    cap = _patch_queue(monkeypatch, [[("1", None, None, None, None), ("2", None, None, None, None)]])
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
    _patch_queue(monkeypatch, [[("1", None, None, None, None)]])
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
    batches = iter([[("9", None, None, None, None)], []])
    monkeypatch.setattr(
        portal_runner.db, "claim_detail_batch", lambda *a, **k: next(batches, []))

    def _complete(_c, _src, ids, outcome="written"):
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


# --- run_phase: scrape_runs lifecycle + honest crash accounting -------------
#
# W0.2. This lifecycle used to be copy-pasted into every `*_main.py`, and the
# `finally: _finalize(run_id, {}, drain=True)` in all nine copies is why the six
# portals that crashed on 2026-08-26 recorded `scrape_runs.errors = 0`.


class _PhaseRecorder:
    """Captures what run_phase writes to scrape_runs, via the db seam."""

    def __init__(self, monkeypatch, *, run_id: int | None = 42) -> None:
        self.starts: list[tuple[str, str]] = []
        self.finals: list[tuple[int, dict[str, Any]]] = []
        self.bumps: list[tuple[int, dict[str, Any]]] = []
        self.signatures: list[tuple[str, str, str]] = []
        monkeypatch.setattr(portal_runner.db, "connect", lambda: _Conn())
        monkeypatch.setattr(
            portal_runner, "record_failure_signature",
            lambda _c, exc, *, source, lane: self.signatures.append(
                (type(exc).__name__, source, lane)),
        )
        monkeypatch.setattr(
            portal_runner.db, "scrape_run_start",
            lambda _c, run_type, source: (
                self.starts.append((run_type, source)) or run_id),
        )
        monkeypatch.setattr(
            portal_runner.db, "scrape_run_finalize",
            lambda _c, rid, **kw: self.finals.append((rid, kw)),
        )
        monkeypatch.setattr(
            portal_runner.db, "bump_scrape_run_counts",
            lambda _c, rid, **kw: self.bumps.append((rid, kw)),
        )


def test_run_phase_finalizes_a_clean_index_run(monkeypatch):
    rec = _PhaseRecorder(monkeypatch)
    rc = portal_runner.run_phase(
        _FakePortal(), "index",
        lambda portal, dry_run, **kw: (0, {"index_pages": 4, "errors": 0}), False,
    )
    assert rc == 0
    assert rec.starts == [("index", "fake")]        # source comes off the portal
    assert rec.finals[0][1]["index_pages"] == 4
    assert rec.finals[0][1]["errors"] == 0
    assert rec.bumps == []                          # nothing crashed


def test_run_phase_crash_bumps_errors_and_leaves_ended_at_unstamped(monkeypatch):
    """The regression this wave exists for: a drain that dies mid-flight must not
    be recorded as a finished run with errors=0."""
    rec = _PhaseRecorder(monkeypatch)

    def _crash(portal, dry_run, **kw):
        raise psycopg.errors.CheckViolation(
            'new row for relation "listings" violates check constraint '
            '"listings_area_basis_check"'
        )

    monkeypatch.setattr(portal_runner, "run_detail_drain", _crash)
    with pytest.raises(psycopg.errors.CheckViolation):
        portal_runner.run_phase(_FakePortal(), "detail", _crash, False)

    assert rec.bumps == [(42, {"errors": 1})]   # the run says it failed
    assert rec.finals == []                     # and never claims it completed


def test_run_phase_crash_on_the_index_lane_is_recorded_too(monkeypatch):
    rec = _PhaseRecorder(monkeypatch)

    def _crash(portal, dry_run, **kw):
        raise RuntimeError("index walk fell over")

    with pytest.raises(RuntimeError):
        portal_runner.run_phase(_FakePortal(), "index", _crash, False)
    # The index lane previously returned early from _finalize on an empty agg, so a
    # crashed walk wrote nothing at all — not even an error.
    assert rec.bumps == [(42, {"errors": 1})]
    assert rec.finals == []


def test_run_phase_records_the_crash_before_the_exception_propagates(monkeypatch):
    """Ordering matters: the process may be dying, so the error has to be on the
    row before the exception leaves run_phase."""
    _PhaseRecorder(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(
        portal_runner.db, "bump_scrape_run_counts",
        lambda _c, rid, **kw: seen.append("bumped"),
    )

    def _crash(portal, dry_run, **kw):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        portal_runner.run_phase(_FakePortal(), "detail", _crash, False)
        seen.append("propagated")
    assert seen == ["bumped"]


def test_run_phase_uses_drain_finalize_semantics_only_for_the_drain(monkeypatch):
    rec = _PhaseRecorder(monkeypatch)
    p = _FakePortal()
    monkeypatch.setattr(
        portal_runner, "run_detail_drain",
        lambda portal, dry_run, **kw: (0, {"listings_updated": 3}),
    )
    portal_runner.run_phase(
        p, "index", lambda portal, dry_run, **kw: (0, {"index_pages": 1}), False)
    portal_runner.run_phase(p, "detail", portal_runner.run_detail_drain, False)
    # The drain persists counters per chunk, so its finalize must not re-write them.
    assert [kw["bump_already_applied"] for _rid, kw in rec.finals] == [False, True]


def test_run_phase_dry_run_touches_no_scrape_run(monkeypatch):
    rec = _PhaseRecorder(monkeypatch)
    rc = portal_runner.run_phase(
        _FakePortal(), "index",
        lambda portal, dry_run, **kw: (0, {"index_pages": 2}), True)
    assert rc == 0
    assert rec.starts == [] and rec.finals == [] and rec.bumps == []


def test_run_phase_dry_run_crash_records_nothing(monkeypatch):
    rec = _PhaseRecorder(monkeypatch)

    def _crash(portal, dry_run, **kw):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        portal_runner.run_phase(_FakePortal(), "detail", _crash, True)
    assert rec.bumps == []


def test_run_phase_survives_a_failed_scrape_run_start(monkeypatch):
    """No run row (a DB hiccup at start) must not become a crash of its own."""
    rec = _PhaseRecorder(monkeypatch)
    monkeypatch.setattr(
        portal_runner.db, "scrape_run_start",
        lambda _c, run_type, source: (_ for _ in ()).throw(RuntimeError("no db")),
    )
    rc = portal_runner.run_phase(
        _FakePortal(), "index",
        lambda portal, dry_run, **kw: (0, {"index_pages": 1}), False)
    assert rc == 0
    assert rec.finals == []          # run_id is None -> nothing to finalize
    assert rec.bumps == []


def test_run_phase_crash_recording_failure_never_masks_the_real_exception(monkeypatch):
    """Bookkeeping is best-effort: if the DB is the thing that is down, the caller
    must still see the ORIGINAL error, not a secondary one from the bump."""
    _PhaseRecorder(monkeypatch)
    monkeypatch.setattr(
        portal_runner.db, "bump_scrape_run_counts",
        lambda *_a, **_k: (_ for _ in ()).throw(psycopg.OperationalError("db gone")),
    )

    def _crash(portal, dry_run, **kw):
        raise ValueError("the real problem")

    with pytest.raises(ValueError, match="the real problem"):
        portal_runner.run_phase(_FakePortal(), "detail", _crash, False)


# --- W3.1: the crash path is also the failure-signature producer -----------


def test_run_phase_crash_records_a_failure_signature_with_portal_and_lane(monkeypatch):
    """The chokepoint has portal + lane in scope; _record_run_crash never did. Without
    them an incident cannot say which portal it came from."""
    rec = _PhaseRecorder(monkeypatch)

    def _crash(portal, dry_run, **kw):
        raise psycopg.errors.CheckViolation(
            'new row for relation "listings" violates check constraint '
            '"listings_area_basis_check"'
        )

    with pytest.raises(psycopg.errors.CheckViolation):
        portal_runner.run_phase(_FakePortal(), "detail", _crash, False)
    assert rec.signatures == [("CheckViolation", "fake", "detail")]


def test_run_phase_records_a_signature_even_with_no_scrape_run_row(monkeypatch):
    """The old early-return on run_id=None meant a crash after a failed
    scrape_run_start left NO trace anywhere at all."""
    rec = _PhaseRecorder(monkeypatch)
    monkeypatch.setattr(
        portal_runner.db, "scrape_run_start",
        lambda _c, run_type, source: (_ for _ in ()).throw(RuntimeError("no db")),
    )

    def _crash(portal, dry_run, **kw):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        portal_runner.run_phase(_FakePortal(), "index", _crash, False)
    assert rec.bumps == []                                   # no row to bump
    assert rec.signatures == [("RuntimeError", "fake", "index")]


def test_a_failing_signature_write_never_masks_the_real_exception(monkeypatch):
    _PhaseRecorder(monkeypatch)
    monkeypatch.setattr(
        portal_runner, "record_failure_signature",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("incident table missing")),
    )

    def _crash(portal, dry_run, **kw):
        raise ValueError("the real problem")

    with pytest.raises(ValueError, match="the real problem"):
        portal_runner.run_phase(_FakePortal(), "detail", _crash, False)


def test_record_failure_signature_derives_the_key_from_the_text_only(monkeypatch):
    """The asymmetry the whole wave rests on: same error, different portal and lane,
    one signature."""
    seen: list[dict[str, Any]] = []
    import toolkit.ops_incidents as oi

    monkeypatch.setattr(oi, "record_failure_signature",
                        lambda _c, sig, **kw: seen.append({"sig": sig, **kw}))
    monkeypatch.setattr(oi, "actions_context",
                        lambda: (".github/workflows/x.yml", "https://gh/run/9"))
    exc = psycopg.errors.CheckViolation(
        'new row for relation "listings" violates check constraint '
        '"listings_area_basis_check"'
    )
    portal_runner.record_failure_signature(_Conn(), exc, source="mmreality", lane="detail")
    portal_runner.record_failure_signature(_Conn(), exc, source="remax", lane="index")
    assert seen[0]["sig"] == seen[1]["sig"]
    assert "listings_area_basis_check" in seen[0]["sig"]
    assert [s["origin"] for s in seen] == ["mmreality/detail", "remax/index"]
    assert seen[0]["workflow_path"] == ".github/workflows/x.yml"


def test_the_chokepoint_stamps_its_actions_run_id(monkeypatch):
    """The run id is what stops the poller counting this same crash a second time when
    it meets the concluded run 80-256 minutes later (migration 463). Off-CI it is None
    — the always-on worker's lanes have no second observer."""
    seen: list[dict[str, Any]] = []
    import toolkit.ops_incidents as oi

    monkeypatch.setattr(oi, "record_failure_signature",
                        lambda _c, sig, **kw: seen.append({"sig": sig, **kw}))
    monkeypatch.setattr(oi, "actions_context", lambda: (None, None))
    monkeypatch.setenv("GITHUB_RUN_ID", "32788072691")
    portal_runner.record_failure_signature(
        _Conn(), RuntimeError("boom"), source="mmreality", lane="detail")
    monkeypatch.delenv("GITHUB_RUN_ID")
    portal_runner.record_failure_signature(
        _Conn(), RuntimeError("boom"), source="mmreality", lane="drain")
    assert [s["run_id"] for s in seen] == [32788072691, None]


def test_run_phase_passes_run_id_and_kwargs_through_to_the_runner(monkeypatch):
    _PhaseRecorder(monkeypatch)
    seen: dict[str, Any] = {}

    def _runner(portal, dry_run, **kw):
        seen.update(kw)
        return (0, {})

    portal_runner.run_phase(
        _FakePortal(), "detail", _runner, False, max_claims=17, detail_workers=3)
    assert seen["run_id"] == 42
    assert seen["max_claims"] == 17 and seen["detail_workers"] == 3


# --- the gone-rate breaker (rule #3's last rail) ---------------------------


def test_drain_breaker_stops_flipping_when_ingest_fetches_mostly_read_gone(monkeypatch):
    """A portal answering every page with its gone signal (consent redirect,
    WAF 404s) is not the market. Ingest rows were on the index minutes ago, so
    once a majority of them read gone the run stops flipping and records the
    rest as failures to retry later."""
    ids = [str(i) for i in range(1, 26)]
    cap = _patch_queue(monkeypatch, [[(i, None, None, None, None) for i in ids]])
    p = _FakePortal(fetch_kinds={i: "gone" for i in ids})
    rc, agg = portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0
    # The 20th observation completes the sample and trips the breaker before
    # that item is routed, so 19 flipped and the remaining 6 were recorded as
    # failures to retry later.
    flipped = portal_runner._GoneRateBreaker.MIN_SAMPLE - 1
    assert len(p.calls["gone"]) == flipped
    assert len(p.calls["failure"]) == 25 - flipped
    assert agg["listings_inactive"] == flipped


def test_drain_breaker_ignores_presence_checks(monkeypatch):
    """A backlog of truly dead listings legitimately reads 100% gone; presence
    checks (priority < 0) must not trip the breaker."""
    ids = [str(i) for i in range(1, 26)]
    _patch_queue(monkeypatch, [[(i, None, None, None, None) for i in ids]])
    monkeypatch.setattr(
        portal_runner.db, "queue_priorities",
        lambda conn, source, nids: {n: portal_runner.db.QUEUE_PRIORITY_VERIFY for n in nids})
    p = _FakePortal(fetch_kinds={i: "gone" for i in ids})
    rc, agg = portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert rc == 0
    assert len(p.calls["gone"]) == 25 and p.calls["failure"] == []


def test_drain_breaker_needs_a_sample_before_it_trips(monkeypatch):
    """Nineteen gone ingest fetches in a row is still within one run's noise."""
    ids = [str(i) for i in range(1, 20)]
    _patch_queue(monkeypatch, [[(i, None, None, None, None) for i in ids]])
    p = _FakePortal(fetch_kinds={i: "gone" for i in ids})
    portal_runner.run_detail_drain(p, None, False, detail_workers=1, detail_rate=1.0)
    assert len(p.calls["gone"]) == 19 and p.calls["failure"] == []
