"""Tests for scripts.recompute_property_stats pure helpers.

Hermetic: the id-batching arithmetic, the fake-conn execution order, and the
static validity of every SQL constant's `%`-placeholders are exercised here; the
SQL's runtime semantics + DB I/O are verified out-of-band via the Supabase MCP
after the migrations apply.
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts.recompute_property_stats import (
    _attach_stragglers,
    _batch_ranges,
    _drain_dirty,
)


_INTERVAL_UNITS = {"s": 1, "sec": 1, "second": 1, "seconds": 1,
                   "min": 60, "mins": 60, "minute": 60, "minutes": 60,
                   "h": 3600, "hour": 3600, "hours": 3600}


def _interval_seconds(interval: str) -> int:
    """Parse the PG interval literals this module ships as constants
    (`"10min"`, `"15 minutes"`) so the sizing guards can DERIVE their arithmetic
    from them instead of restating the number and drifting."""
    import re

    m = re.fullmatch(r"(\d+)\s*([a-z]+)", interval.strip())
    assert m, f"unparseable interval literal {interval!r}"
    return int(m[1]) * _INTERVAL_UNITS[m[2]]


def test_interval_parser_reads_both_spellings_used_in_the_module():
    assert _interval_seconds("10min") == 600
    assert _interval_seconds("15 minutes") == 900
    assert _interval_seconds("2h") == 7200


def test_empty_when_no_properties():
    assert list(_batch_ranges(0, 2000)) == []


def test_invalid_batch_size_yields_nothing():
    assert list(_batch_ranges(100, 0)) == []


def test_half_open_ranges_cover_exact_multiple():
    assert list(_batch_ranges(4, 2)) == [(1, 3), (3, 5)]


def test_last_range_overshoots_to_cover_remainder():
    assert list(_batch_ranges(5, 2)) == [(1, 3), (3, 5), (5, 7)]


def test_every_id_lands_in_exactly_one_range():
    max_id, batch = 71_556, 2000
    seen = 0
    for lo, hi in _batch_ranges(max_id, batch):
        # half-open [lo, hi); count the ids in [lo, min(hi-1, max_id)]
        seen += min(hi - 1, max_id) - lo + 1
    assert seen == max_id


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        for predicate, rows in self._conn.script:
            if predicate(s):
                self._rows = list(rows)
                self.rowcount = len(rows)
                return
        self._rows = []
        self.rowcount = 0

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, script: list[tuple[Any, list[tuple[Any, ...]]]] | None = None) -> None:
        self.script = script or []
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> "_FakeTxn":
        return _FakeTxn()


class _FakeTxn:
    def __enter__(self) -> "_FakeTxn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _sqls(conn: _FakeConn) -> list[str]:
    return [e[0] for e in conn.executed]


def _find(conn: _FakeConn, needle: str) -> tuple[str, Any] | None:
    return next((e for e in conn.executed if needle in e[0]), None)


def test_attach_stragglers_singletons_only_no_spatial_link():
    """Stragglers become singletons; the old geo spatial-link step is gone.

    Matching is the out-of-band street+disposition dedup engine's job, so
    attach must NOT run any ST_DWithin probe or enqueue dirty_properties — it
    only inserts a singleton per unlinked listing and links it.
    """
    conn = _FakeConn()
    _attach_stragglers(conn)
    order = _sqls(conn)
    insert = next(i for i, s in enumerate(order) if "INSERT INTO properties" in s)
    link = next(i for i, s in enumerate(order) if "p.repr_listing_ref_id = l.id" in s)
    assert insert < link
    assert not any("ST_DWithin" in s for s in order)
    assert not any("INSERT INTO dirty_properties" in s for s in order)


def test_attach_stragglers_full_runs_native_id_backfill():
    conn = _FakeConn()
    _attach_stragglers(conn)
    assert any("source_id_native = sreality_id::text" in s for s in _sqls(conn))


def test_attach_stragglers_incremental_skips_native_id_backfill():
    """The */5 incremental pass must not scan the whole listings table for the
    one-time native-id backfill; the daily full sweep handles it."""
    conn = _FakeConn()
    _attach_stragglers(conn, skip_native_backfill=True)
    order = _sqls(conn)
    assert not any("source_id_native = sreality_id::text" in s for s in order)
    # still inserts singletons even when the backfill is skipped
    assert any("INSERT INTO properties" in s for s in order)


class _TxnMarkingConn(_FakeConn):
    """_FakeConn that records BEGIN/COMMIT markers, so a test can assert which
    statements share one transaction."""

    def transaction(self) -> Any:
        conn = self

        class _Txn:
            def __enter__(self) -> Any:
                conn.executed.append(("BEGIN", None))
                return self

            def __exit__(self, *exc: Any) -> None:
                conn.executed.append(("COMMIT", None))

        return _Txn()


def test_attach_stragglers_insert_and_link_are_one_transaction() -> None:
    """The INSERT and the LINK are only JOINTLY idempotent: committing the
    INSERT alone leaves listings still unlinked, so the next attempt inserts a
    SECOND singleton each and orphans one. db.run_resilient now REPLAYS this op
    on a transient error, so the pair must be all-or-nothing — and the
    whole-table native-id backfill must stay outside that transaction."""
    conn = _TxnMarkingConn()
    _attach_stragglers(conn)
    order = _sqls(conn)
    backfill = next(i for i, s in enumerate(order)
                    if "source_id_native = sreality_id::text" in s)
    begin = order.index("BEGIN")
    insert = next(i for i, s in enumerate(order) if "INSERT INTO properties" in s)
    link = next(i for i, s in enumerate(order) if "p.repr_listing_ref_id = l.id" in s)
    commit = order.index("COMMIT")
    assert backfill < begin < insert < link < commit


class _DrainCur:
    def __init__(self, conn: "_DrainConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_DrainCur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if s.startswith("DELETE FROM dirty_properties"):
            self._conn.deleted.append((params["ids"], params["cutoff"]))
            self._rows = []
        elif "SELECT property_id, marked_at FROM dirty_properties" in s:
            self._rows = self._conn.batches.pop(0) if self._conn.batches else []
        elif "WITH batch AS" in s:  # scoped recompute
            self._conn.recomputed.append(params["ids"])
            self._rows = []
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _DrainConn:
    def __init__(self, batches: list[list[tuple[Any, ...]]]) -> None:
        self.batches = list(batches)
        self.executed: list[tuple[str, Any]] = []
        self.recomputed: list[list[int]] = []
        self.deleted: list[tuple[list[int], Any]] = []

    def cursor(self) -> _DrainCur:
        return _DrainCur(self)

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()


def test_drain_dirty_recomputes_each_batch_then_terminates():
    conn = _DrainConn([[(7, "t1"), (8, "t1")], [(9, "t2")], []])
    total = _drain_dirty(conn, batch_size=2, cutoff="CUTOFF")
    assert total == 3
    assert conn.recomputed == [[7, 8], [9]]
    # deletes are scoped to the claimed ids and the run cutoff
    assert conn.deleted == [([7, 8], "CUTOFF"), ([9], "CUTOFF")]
    # scoped recomputes run under the raised per-statement ceiling too
    assert sum("SET LOCAL statement_timeout" in s for s, _ in conn.executed) == 2


def test_drain_dirty_empty_queue_is_noop():
    conn = _DrainConn([[]])
    assert _drain_dirty(conn, 100, "C") == 0
    assert conn.recomputed == []
    assert conn.deleted == []


class _MainConn(_FakeConn):
    """Context-manager conn for driving main(): serves the cutoff SELECT and
    the maintenance lease CAS (acquired), records the rest."""

    def __init__(self) -> None:
        super().__init__([
            (lambda s: s == "SELECT now()", [("CUTOFF",)]),
            (lambda s: "property_maintenance_lease" in s and "RETURNING" in s, [(1,)]),
        ])

    def __enter__(self) -> "_MainConn":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def _run_main(monkeypatch: Any, argv: list[str]) -> list[str]:
    import sys

    import scripts.recompute_property_stats as rps

    calls: list[str] = []
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://test")
    monkeypatch.setattr(sys, "argv", ["recompute_property_stats", *argv])
    # Patch db.connect, not sys.modules["psycopg"]: main() now opens the
    # connection through scraper.db.connect (keepalives + handshake retry), which
    # captured psycopg at ITS import time, so a sys.modules swap no longer
    # intercepts it — it would reach the network and burn the retry budget.
    monkeypatch.setattr(rps.db, "connect", lambda *a, **k: _MainConn())
    monkeypatch.setattr(
        rps, "_attach_stragglers", lambda c, **k: calls.append("attach") or 0)
    monkeypatch.setattr(
        rps, "_drain_dirty",
        lambda c, bs, cutoff, renew=None: calls.append("drain") or 0)
    monkeypatch.setattr(rps, "_reconcile_childless", lambda c: 0)
    monkeypatch.setattr(rps, "_max_property_id", lambda c: 0)
    assert rps.main() == 0
    return calls


def test_incremental_runs_attach_then_drain(monkeypatch: Any) -> None:
    """--incremental (the */5 cron) runs attach -> dirty drain, in that order,
    O(changes) each pass."""
    assert _run_main(monkeypatch, ["--incremental"]) == ["attach", "drain"]


def test_full_mode_skips_the_dirty_drain(monkeypatch: Any) -> None:
    """The daily full sweep recomputes every property instead of draining the queue."""
    calls = _run_main(monkeypatch, [])
    assert calls == ["attach"]


def test_every_resolved_sql_constant_has_valid_placeholders():
    """All `*_SQL` attributes — including the `.replace()`-derived executors —
    must pass psycopg's placeholder parser.

    The fakes above record SQL without parsing it (which is why a prose `~2%` in
    `_RECOMPUTE_BATCH_SQL` once shipped green and broke property maintenance +
    every merge). This module is uniquely exposed: `_RECOMPUTE_ONE_SQL` and
    `_RECOMPUTE_SCOPED_SQL` are derived from `_RECOMPUTE_BATCH_SQL` at import
    time, so they can't be statically inspected — only validated after they
    resolve. The repo-wide AST guard (tests/test_sql_placeholders.py) covers the
    base constants; this covers the derived family that actually executes.
    """
    import scripts.recompute_property_stats as rps

    split = pytest.importorskip("psycopg._queries")._split_query
    names = [n for n in dir(rps) if n.endswith("_SQL") and isinstance(getattr(rps, n), str)]
    assert {"_RECOMPUTE_BATCH_SQL", "_RECOMPUTE_ONE_SQL", "_RECOMPUTE_SCOPED_SQL"} <= set(names)
    for name in names:
        split(getattr(rps, name).encode())  # raises ProgrammingError on a bad `%`


# --- run_incremental_pass (the shared GH-cron / worker-lane implementation) ----


def _lock_script(acquired: bool):
    """FakeConn script: answer the lease CAS (RETURNING a row iff acquired),
    the cutoff now(), and the dirty claim (empty queue) so a pass runs
    end-to-end without a database."""
    return [
        (lambda s: "property_maintenance_lease" in s and "RETURNING" in s,
         [(1,)] if acquired else []),
        (lambda s: s == "SELECT now()", [("2026-07-08T00:00:00+00:00",)]),
        (lambda s: "FROM dirty_properties" in s and "SELECT" in s, []),
    ]


def test_run_incremental_pass_runs_all_phases_and_unlocks():
    from scripts.recompute_property_stats import run_incremental_pass

    conn = _FakeConn(script=_lock_script(acquired=True))
    stats = run_incremental_pass(conn, batch_size=500)
    assert stats["skipped"] is False
    sqls = _sqls(conn)
    # every phase of the incremental pass ran...
    assert _find(conn, "INSERT INTO properties")  # straggler attach
    assert not any("source_id_native = sreality_id" in s for s in sqls)  # skip legacy backfill
    assert _find(conn, "FROM dirty_properties")  # dirty drain claim
    # ...and the lease was released even on the happy path.
    assert _find(conn, "SET holder = NULL")


def test_run_incremental_pass_skips_when_lease_held():
    from scripts.recompute_property_stats import run_incremental_pass

    conn = _FakeConn(script=_lock_script(acquired=False))
    stats = run_incremental_pass(conn, batch_size=500)
    assert stats == {
        "skipped": True, "attached": 0,
        "estimations_bound": 0, "recomputed": 0,
    }
    # NOTHING ran: no attach, no recompute — and no release either
    # (we never held the lease; clearing it would release someone else's).
    sqls = _sqls(conn)
    assert not any("INSERT INTO properties" in s for s in sqls)
    assert not any("SET holder = NULL" in s for s in sqls)


def test_run_incremental_pass_unlocks_on_failure():
    from scripts.recompute_property_stats import run_incremental_pass

    class _Boom(_FakeConn):
        def cursor(self):
            cur = super().cursor()
            orig = cur.execute

            def execute(sql, params=None):
                if "INSERT INTO properties" in sql:
                    raise RuntimeError("boom")
                return orig(sql, params)

            cur.execute = execute  # type: ignore[method-assign]
            return cur

    conn = _Boom(script=_lock_script(acquired=True))
    with pytest.raises(RuntimeError):
        run_incremental_pass(conn, batch_size=500)
    assert _find(conn, "SET holder = NULL")


# --- lease heartbeat + bounded wait (the 2026-08-06 strand incident) ----------


def test_drain_dirty_renews_lease_once_per_slice():
    """A long drain (post-freeze backlog, nine-portal enqueue) must heartbeat
    its 15-min lease per claimed slice instead of silently outliving it."""
    renewals: list[int] = []
    conn = _DrainConn([[(7, "t1"), (8, "t1")], [(9, "t2")], []])
    total = _drain_dirty(conn, batch_size=2, cutoff="C",
                         renew=lambda: renewals.append(1))
    assert total == 3
    # one renewal per claim attempt (two full slices + the terminating empty one)
    assert len(renewals) == 3


def test_renew_lease_raises_when_lost():
    """Renewal missing = the TTL expired mid-work and another writer holds the
    lease — continuing would recompute concurrently, so it must abort."""
    from scripts.recompute_property_stats import _renew_lease

    conn = _FakeConn(script=_lock_script(acquired=False))
    with pytest.raises(RuntimeError, match="lease lost"):
        _renew_lease(conn, "full:x")


def test_wait_lease_is_bounded_by_wall_clock(monkeypatch: Any):
    """A dispatched sweep must fail RED against a stuck lease, not burn its
    whole job budget at 10s CAS intervals recomputing nothing (observed
    2026-08-06 08:08: 30 min in _wait_lease, 0 rows). The bound is WALL time —
    slow CAS round trips (the degraded-DB case) count against it, not just
    the sleeps."""
    import itertools

    import scripts.recompute_property_stats as rps

    monkeypatch.setattr(rps.time, "sleep", lambda s: None)
    # Each monotonic() call advances 20s: two CAS attempts (~40s of simulated
    # round-trip wall time) blow a 30s budget even though sleep() was free.
    ticks = itertools.count(start=0, step=20)
    monkeypatch.setattr(rps.time, "monotonic", lambda: float(next(ticks)))
    conn = _FakeConn(script=_lock_script(acquired=False))
    with pytest.raises(RuntimeError, match="failing RED"):
        rps._wait_lease(conn, "full:x", rps._LEASE_TTL, max_wait_seconds=30.0)


def test_lease_ttl_is_short_everywhere():
    """The 3h full-sweep grant is what turned every timeout kill into a
    multi-hour maintenance freeze — a strand must now cost minutes. If this
    needs raising, renew more often instead."""
    import scripts.recompute_property_stats as rps

    assert rps._LEASE_TTL == "15 minutes"
    assert not hasattr(rps, "_FULL_SWEEP_LEASE")


# --- full-sweep budget clean-stop --------------------------------------------


class _SweepConn(_FakeConn):
    """Context-manager conn driving main()'s full sweep without stubbing the
    batch loop: serves now(), the lease CAS (always granted), and max(id)."""

    def __init__(self, max_id: int) -> None:
        super().__init__([
            (lambda s: s == "SELECT now()", [("CUTOFF",)]),
            (lambda s: "property_maintenance_lease" in s and "RETURNING" in s, [(1,)]),
            (lambda s: "coalesce(max(id), 0) FROM properties" in s, [(max_id,)]),
        ])

    def __enter__(self) -> "_SweepConn":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def _run_sweep(monkeypatch: Any, conn: _SweepConn, argv: list[str],
               clock: list[float] | None = None,
               then: list[Any] | None = None) -> int:
    import sys

    import scripts.recompute_property_stats as rps

    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://test")
    monkeypatch.setattr(sys, "argv", ["recompute_property_stats", *argv])
    # See _run_main: the sweep opens its connection via scraper.db.connect now.
    # `then` makes db.connect a QUEUE rather than a constant, so reconnect()
    # hands back a genuinely DIFFERENT connection (the same-object stub could
    # never prove the rebind).
    queue = [conn, *(then or [])]
    monkeypatch.setattr(rps.db, "connect",
                        lambda *a, **k: queue.pop(0) if len(queue) > 1 else queue[0])
    monkeypatch.setattr(rps.signal, "signal", lambda *a: None)
    if clock is not None:
        ticks = iter(clock)
        monkeypatch.setattr(rps.time, "monotonic", lambda: next(ticks))
    return rps.main()


def test_full_sweep_renews_lease_every_batch(monkeypatch: Any) -> None:
    conn = _SweepConn(max_id=4000)  # 2 batches at the default size
    assert _run_sweep(monkeypatch, conn, []) == 0
    grants = [s for s, _ in conn.executed
              if "property_maintenance_lease" in s and "RETURNING" in s]
    # initial acquisition + one renewal per batch
    assert len(grants) == 1 + 2
    # complete walk → global dirty clear, no swept-range scope
    cleared = _find(conn, "DELETE FROM dirty_properties")
    assert cleared and "property_id <" not in cleared[0]
    # ...and the completion stamp the health check reads (O(1) liveness signal)
    stamp = _find(conn, "property_sweep_last_complete")
    assert stamp and stamp[1]["max_id"] == 4000 and stamp[1]["batches"] == 2
    # every recompute statement runs under the raised per-statement ceiling
    # (the pooler's ~2-min default killed the first post-#971 batch at 3.5min)
    ceilings = [s for s, _ in conn.executed if "SET LOCAL statement_timeout" in s]
    recomputes = [s for s, _ in conn.executed if "WITH batch AS" in s]
    assert len(ceilings) == len(recomputes) == 2


def test_full_sweep_budget_exhaustion_is_red_and_scopes_the_dirty_clear(
    monkeypatch: Any,
) -> None:
    """Stopping early must (a) exit RED — GH reports a timeout kill as
    `cancelled` which alerts nobody, an explicit failure emails — and (b) clear
    dirty rows ONLY below the high-water mark: the global clear would erase the
    recompute signal for unswept ids, leaving them stale until the next full
    sweep instead of healed by the next incremental pass."""
    conn = _SweepConn(max_id=6000)  # 3 batches at the default size
    # monotonic: started_at, _wait_lease entry anchor, batch-1 deadline check,
    # batch-1 per-batch timing, batch-2 deadline check (over budget), then the
    # elapsed stamps in logging.
    clock = [0.0, 1.0, 5.0, 50.0, 100.0, 101.0, 102.0, 103.0]
    rc = _run_sweep(monkeypatch, conn, ["--max-seconds", "60"], clock=clock)
    assert rc == 1
    recomputes = [p for s, p in conn.executed if "WITH batch AS" in s]
    assert [(p["lo"], p["hi"]) for p in recomputes] == [(1, 2001)]
    cleared = _find(conn, "DELETE FROM dirty_properties")
    assert cleared and "property_id < %(hi)s" in cleared[0]
    assert cleared[1] == {"cutoff": "CUTOFF", "hi": 2001}
    # incomplete walk must NOT reconcile childless, claim a full clear, or
    # stamp completion — a stale stamp IS the health check's alarm condition
    assert not _find(conn, "NOT EXISTS (SELECT 1 FROM listings")
    assert not _find(conn, "property_sweep_last_complete")
    # the lease is still released
    assert _find(conn, "SET holder = NULL")


class _FlakyCur(_Cur):
    """Fails the FIRST recompute statement with a transient drop, then behaves."""

    def execute(self, sql: str, params: Any = None) -> None:
        import psycopg

        s = " ".join(sql.split())
        if "WITH batch AS" in s and self._conn.fail_once:
            self._conn.fail_once = False
            raise psycopg.OperationalError("SSL connection has been closed unexpectedly")
        super().execute(sql, params)


class _FlakySweepConn(_SweepConn):
    def __init__(self, max_id: int) -> None:
        super().__init__(max_id)
        self.fail_once = True

    def cursor(self) -> Any:
        return _FlakyCur(self)


def test_batch_retry_renews_the_lease_on_every_attempt(monkeypatch: Any) -> None:
    """A retried batch can occupy 2 x _BATCH_STATEMENT_TIMEOUT (20 min), past
    the 15-min _LEASE_TTL. So the renewal must be the first statement of the
    RETRIED op — renewing once per loop iteration would let the replay outlive
    its own lease, handing maintenance to another writer mid-batch and reding
    the sweep on the next renewal."""
    import scripts.recompute_property_stats as rps

    monkeypatch.setattr(rps.db.time, "sleep", lambda s: None)
    conn = _FlakySweepConn(max_id=2000)  # exactly one batch
    assert _run_sweep(monkeypatch, conn, []) == 0
    order = _sqls(conn)
    grants = [i for i, s in enumerate(order)
              if "property_maintenance_lease" in s and "RETURNING" in s]
    recomputes = [i for i, s in enumerate(order) if "WITH batch AS" in s]
    # acquisition + one renewal per ATTEMPT (2), not per loop iteration (1)
    assert len(grants) == 3
    # the failed attempt never recorded its statement; the replay did, and its
    # own renewal came first
    assert len(recomputes) == 1
    assert grants[-1] < recomputes[-1]
    # the sweep still completed normally on the replay
    assert _find(conn, "property_sweep_last_complete")


class _DroppingCur(_Cur):
    """Fails the FIRST recompute statement with a drop that KILLS the connection,
    so run_resilient takes its reconnect arm rather than replaying in place."""

    def execute(self, sql: str, params: Any = None) -> None:
        import psycopg

        s = " ".join(sql.split())
        if "WITH batch AS" in s and self._conn.fail_once:
            self._conn.fail_once = False
            self._conn.broken = True
            raise psycopg.OperationalError("SSL connection has been closed unexpectedly")
        super().execute(sql, params)


class _DroppingSweepConn(_SweepConn):
    def __init__(self, max_id: int, fail_once: bool = False) -> None:
        super().__init__(max_id)
        self.fail_once = fail_once
        self.broken = False
        self.closed = False

    def cursor(self) -> Any:
        return _DroppingCur(self)

    def close(self) -> None:
        self.closed = True


def test_full_sweep_finishes_on_the_fresh_conn_after_a_pooler_drop(
    monkeypatch: Any,
) -> None:
    """`step`'s `nonlocal conn` rebind is what carries a reconnect through the
    REST of the sweep. Nothing exercised it: the existing flaky fake never sets
    `broken`, so run_resilient always took the same-connection arm. If the rebind
    ever regresses the symptom is silent — the finalize + `_release_lease` land on
    a dead socket, the release is swallowed into a WARNING by design, and the run
    exits GREEN with the lease stranded for a full TTL."""
    import scripts.recompute_property_stats as rps

    monkeypatch.setattr(rps.db.time, "sleep", lambda s: None)
    first = _DroppingSweepConn(max_id=4000, fail_once=True)  # 2 batches
    fresh = _DroppingSweepConn(max_id=4000)

    assert _run_sweep(monkeypatch, first, [], then=[fresh]) == 0

    # the dead original is closed by run_resilient and never written to again
    assert first.broken and first.closed
    assert not _find(first, "property_sweep_last_complete")
    assert not _find(first, "SET holder = NULL")
    # ...and the whole remaining sweep ran on the replacement: the replayed batch,
    # the SECOND batch (proving the rebind outlived one step()), the completion
    # stamp, and — load-bearing — the lease release from main()'s `finally:`.
    recomputes = [p for s, p in fresh.executed if "WITH batch AS" in s]
    assert [(p["lo"], p["hi"]) for p in recomputes] == [(1, 2001), (2001, 4001)]
    assert _find(fresh, "property_sweep_last_complete")
    assert _find(fresh, "SET holder = NULL")


# --- connection / lease resilience + budget headroom --------------------------


def test_release_lease_survives_a_dead_connection(caplog: Any) -> None:
    """Both callers release from a `finally:`; a dead connection makes even
    `conn.cursor()` raise, which would bury the real failure under a
    crash-during-cleanup. Warn and move on — the renewed TTL is the guarantee."""
    import logging

    from scripts.recompute_property_stats import _release_lease

    class _DeadConn:
        def cursor(self) -> Any:
            raise RuntimeError("the connection is closed")

    with caplog.at_level(logging.WARNING):
        _release_lease(_DeadConn(), "full:abc")  # must not raise
    failed = [r for r in caplog.records if "lease release failed" in r.message]
    assert failed
    # ...carrying the exception: the warning used to discard it entirely, leaving
    # a green run and a log line with zero diagnostic content.
    assert failed[-1].exc_info is not None


def test_release_lease_falls_back_to_a_fresh_connection() -> None:
    """`nonlocal conn` narrows this but cannot close it: when run_resilient
    exhausts its budget on a DROPPED socket it closes both the original and its
    replacement, so no live handle survives for the release. The holder-guarded
    CAS is safe from any connection, so open one rather than strand the lease for
    a whole TTL and freeze every maintenance lane."""
    from scripts.recompute_property_stats import _release_lease

    class _DeadConn:
        def cursor(self) -> Any:
            raise RuntimeError("the connection is closed")

    class _ClosableConn(_FakeConn):
        closed = False

        def close(self) -> None:
            self.closed = True

    fresh = _ClosableConn()
    _release_lease(_DeadConn(), "full:abc", reconnect=lambda: fresh)
    assert _find(fresh, "SET holder = NULL")
    # the release owns the connection it opened, so it must close it
    assert fresh.closed


def test_release_lease_does_not_reconnect_when_the_handle_is_live() -> None:
    """The fallback is a last resort, not a second write."""
    from scripts.recompute_property_stats import _release_lease

    live = _FakeConn()
    opened: list[int] = []
    _release_lease(live, "full:abc",
                   reconnect=lambda: opened.append(1) or _FakeConn())
    assert _find(live, "SET holder = NULL")
    assert opened == []


def test_workflow_timeout_covers_the_budget_ceiling() -> None:
    """`_MAX_BUDGET_SECONDS` and the workflow's `timeout-minutes` are ONE
    decision: the clean-stop only beats the runner's SIGKILL while the outer
    timeout leaves room for the budget PLUS one in-flight batch (bounded by
    `_BATCH_STATEMENT_TIMEOUT`) plus prelude/finalize. Raising one alone
    re-creates the silent `cancelled` this script exists to eliminate."""
    from pathlib import Path

    import scripts.recompute_property_stats as rps

    yaml = pytest.importorskip("yaml")
    root = Path(__file__).resolve().parent.parent
    wf = yaml.safe_load(
        (root / ".github/workflows/recompute_property_stats.yml").read_text())
    timeout_s = wf["jobs"]["recompute"]["timeout-minutes"] * 60
    # One in-flight batch = _BATCH_STATEMENT_TIMEOUT x its retry budget (the
    # deadline is only checked at a batch boundary, so a batch that starts just
    # under the wire runs its full retried worst case past it). DERIVED from the
    # constant, not the literal 10 it happens to hold: with the number hardcoded,
    # raising _BATCH_STATEMENT_TIMEOUT to 30min left this guard passing while the
    # real worst case (9720s) had already outgrown the 7800s backstop — a SIGKILL
    # mid-sweep, reported by GH as `cancelled`, emailing nobody.
    batch_ceiling_s = _interval_seconds(rps._BATCH_STATEMENT_TIMEOUT)
    in_flight_batch_s = batch_ceiling_s * rps._BATCH_RESILIENT_ATTEMPTS
    assert timeout_s >= rps._MAX_BUDGET_SECONDS + in_flight_batch_s + 120
    # The module's other, previously unguarded invariant (see the comment above
    # _BATCH_STATEMENT_TIMEOUT): renewal fires as the first statement of each
    # batch ATTEMPT, so one statement's ceiling is the longest possible renewal
    # gap and must stay comfortably under the lease TTL.
    assert batch_ceiling_s < _interval_seconds(rps._LEASE_TTL)

    # The dispatch default must be dispatchable — i.e. at or under the clamp,
    # never silently truncated to it. (YAML 1.1 parses the bare key `on` as the
    # boolean True, hence the two-key lookup.)
    triggers = wf.get("on", wf.get(True))
    default_budget = float(
        triggers["workflow_dispatch"]["inputs"]["max_seconds"]["default"])
    assert default_budget <= rps._MAX_BUDGET_SECONDS


# --- late-binding estimation identity resolution ------------------------------


def test_incremental_pass_binds_pending_estimation_listing_ids():
    """The pass stamps input_listing_id on runs created before their subject
    listing was scraped. Every estimation read path now keys solely on that
    surrogate, so an unbound run belongs to no listing page until this runs."""
    from scripts.recompute_property_stats import run_incremental_pass

    conn = _FakeConn(script=_lock_script(acquired=True))
    stats = run_incremental_pass(conn, batch_size=500)
    assert stats["skipped"] is False
    assert "estimations_bound" in stats
    found = _find(conn, "UPDATE estimation_runs")
    assert found is not None, "late-binding UPDATE did not run"
    sql = found[0]
    assert "SET input_listing_id = cand.listing_id" in sql
    # One-way and idempotent: the IS NULL guard is repeated in the UPDATE's own
    # WHERE, not only in the CTE, so a bound run can never be re-pointed.
    assert sql.count("er.input_listing_id IS NULL") >= 2
    # Never stamps a NULL over a NULL.
    assert "cand.listing_id IS NOT NULL" in sql


def test_late_binding_is_not_fuzzy():
    """A wrong attribution silently credits a paid estimate to the wrong flat,
    which is strictly worse than leaving it unattached. The resolver matches the
    unique sreality_id only — no URL arm, no normalisation, no ILIKE, and no
    ORDER BY ... LIMIT 1 'pick the best' tie-break."""
    import scripts.recompute_property_stats as rps

    conn = _FakeConn(script=_lock_script(acquired=True))
    rps.run_incremental_pass(conn, batch_size=500)
    sql = _find(conn, "UPDATE estimation_runs")[0]
    assert "l.sreality_id = er.input_sreality_id" in sql
    for banned in ("ILIKE", "input_url", "similarity(", "lower("):
        assert banned not in sql, f"late binding must not use {banned}"


# --- one measure, one row (migration 424) -------------------------------------


def _set_clause(sql: str) -> str:
    """The recompute UPDATE's SET list, up to its FROM."""
    return sql.split("UPDATE properties p SET", 1)[1].split("FROM child_agg", 1)[0]


def _rhs(set_clause: str, column: str) -> str:
    """The (single-line) expression assigned to `column`."""
    import re

    m = re.search(rf"^\s*{re.escape(column)}\s*=\s*(.+?),?$", set_clause, re.M)
    assert m, f"{column} is not assigned in the SET list"
    return m[1].strip().rstrip(",")


def test_the_denominator_is_read_from_the_same_row_as_the_numerator():
    """properties.current_price_czk is the representative child's price, so
    properties.area_m2 — what every per-m2 consumer divides it by — must be that
    same child's area. Independent rollups made a merged property's ratio divide
    one portal's price by another portal's area."""
    from scripts.recompute_property_stats import _RECOMPUTE_BATCH_SQL

    setc = _set_clause(_RECOMPUTE_BATCH_SQL)
    assert _rhs(setc, "current_price_czk").startswith("r.")
    assert _rhs(setc, "area_m2").startswith("coalesce(r.area_m2"), (
        "the area must lead with the representative child; the group-best area "
        "is a fallback, not the definition"
    )


def test_the_denominator_never_goes_back_through_the_golden_record():
    """The golden-record CTE picks each field's best value from whichever child
    happens to have one. area_m2 must not go back through it — that is exactly
    how the denominator split away from the numerator."""
    from scripts.recompute_property_stats import _RECOMPUTE_BATCH_SQL

    assert "g.area_m2" not in _RECOMPUTE_BATCH_SQL, (
        "g.area_m2 re-introduces a field-by-field pick of the per-m2 denominator"
    )


def test_usable_area_is_left_on_its_own_golden_record_pick():
    """usable_area is NOT the per-m2 denominator, and it is a live Browse +
    Watchdog filter column (browse_list.usable_area -> usable_area_min/max_filter,
    the matcher's min/max_usable_area). Binding it to the representative child
    would NULL it for every property whose repr carries an area but no
    usable_area, silently narrowing saved filters. W3 changes the denominator,
    not this."""
    from scripts.recompute_property_stats import _RECOMPUTE_BATCH_SQL

    setc = _set_clause(_RECOMPUTE_BATCH_SQL)
    assert _rhs(setc, "usable_area") == "g.usable_area"


def test_best_area_is_left_joined_so_an_area_less_property_still_updates():
    """No child reports an area -> best_area has no row for that property. An
    inner join would drop it out of the UPDATE entirely, silently freezing
    is_active and every other rolled-up column."""
    import re

    from scripts.recompute_property_stats import _RECOMPUTE_BATCH_SQL

    assert "LEFT JOIN best_area" in _RECOMPUTE_BATCH_SQL
    assert not re.search(r"(?<!LEFT )JOIN best_area", _RECOMPUTE_BATCH_SQL)


def test_the_area_fallback_is_the_pre_change_pick_verbatim():
    """The fallback exists so no property loses an area it already had. It must
    stay restricted to children that carry an area and ordered by trust alone —
    widening it to usable-area-carrying rows makes the best row's area NULL and
    drops the property out of every area filter."""
    from scripts.recompute_property_stats import _RECOMPUTE_BATCH_SQL

    body = " ".join(
        _RECOMPUTE_BATCH_SQL.split("best_area AS (", 1)[1].split("),", 1)[0].split()
    )
    assert "WHERE k.area_m2 IS NOT NULL" in body
    assert "ORDER BY k.property_id, k.src_rank," in body


def test_the_basis_stamp_names_the_row_the_price_came_from():
    """price_per_m2_source_listing_id must be built from the representative
    child's own price + area, so a non-NULL stamp always means one row backed
    both halves of the measure."""
    from scripts.recompute_property_stats import _RECOMPUTE_BATCH_SQL

    setc = _set_clause(_RECOMPUTE_BATCH_SQL)
    assert (
        "price_per_m2_source_id(r.price_czk, r.area_m2, r.listing_ref_id)"
        in " ".join(setc.split())
    )


def test_every_recompute_variant_writes_the_stamp():
    """The one/scoped variants are derived from the batch SQL by narrowing the
    batch CTE; if that ever becomes a copy, they must not lose the measure."""
    import scripts.recompute_property_stats as rps

    for sql in (rps._RECOMPUTE_BATCH_SQL, rps._RECOMPUTE_ONE_SQL,
                rps._RECOMPUTE_SCOPED_SQL):
        assert "price_per_m2_source_listing_id" in sql


def test_every_singleton_creation_path_stamps_the_basis():
    """Four writers create a property from ONE listing (straggler attach,
    insert-time singleton, the cheap singleton rollup, the unmerge split). A
    singleton's price and area are trivially one row's, so each stamps the basis
    rather than leaving it NULL until the next maintenance pass — and each calls
    the shared price_per_m2_source_id() instead of restating its validity bound."""
    import inspect

    from scraper import db
    from scripts.recompute_property_stats import _ATTACH_INSERT_SQL
    from toolkit.property_identity import _SPLIT_INSERT_ONE_SQL

    sources = {
        "attach": _ATTACH_INSERT_SQL,
        "split": _SPLIT_INSERT_ONE_SQL,
        "singleton": inspect.getsource(db._create_singleton_property),
        "cheap_rollup": inspect.getsource(db._cheap_property_rollup),
    }
    for name, sql in sources.items():
        assert "price_per_m2_source_listing_id" in sql, f"{name} drops the stamp"
        assert "price_per_m2_source_id(" in sql, f"{name} restates the validity bound"
