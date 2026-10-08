"""Tests for scripts.recompute_property_stats pure helpers.

Hermetic: the id-batching arithmetic, the fake-conn execution order, and the
static validity of every SQL constant's `%`-placeholders are exercised here; the
SQL's runtime semantics + DB I/O are verified out-of-band via the Supabase MCP
after the migrations apply.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.recompute_property_stats import (
    _attach_stragglers,
    _batch_ranges,
    _drain_dirty,
    properties_changed,
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


def test_a_resumed_walk_starts_its_ranges_at_the_cursor():
    assert list(_batch_ranges(6000, 2000, 2001)) == [(2001, 4001), (4001, 6001)]
    assert list(_batch_ranges(6000, 2000, 6001)) == []


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


_STRAGGLERS = (lambda s: s == "SELECT id FROM listings WHERE property_id IS NULL LIMIT %(limit)s",
               [(11,), (12,)])
_BORN = (lambda s: "INSERT INTO properties" in s, [(101,), (102,)])


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


def test_attach_stragglers_births_and_recomputes_them_in_one_transaction() -> None:
    """One bare INSERT per straggler (the one birth path) and the normal recompute of the new
    properties, all-or-nothing: db.run_resilient REPLAYS this op, and a replay must find the
    stragglers unlinked, never a linked bare row Browse could show. No spatial link, no dirty
    enqueue, and no native-id backfill (migration 314's validated CHECK left it nothing)."""
    from scraper.db import NEW_SINGLETONS_SQL

    conn = _TxnMarkingConn([_STRAGGLERS, _BORN])
    assert _attach_stragglers(conn) == 2
    order = _sqls(conn)
    birth = next(i for i, s in enumerate(order) if "INSERT INTO properties" in s)
    recompute = next(i for i, s in enumerate(order) if "WITH batch AS" in s)
    assert order[0] == "BEGIN" and birth < recompute and order[-1] == "COMMIT"
    assert order[birth] == " ".join(NEW_SINGLETONS_SQL.split())
    assert conn.executed[birth][1] == {"ids": [11, 12]}
    assert conn.executed[recompute][1] == {"ids": [101, 102]}
    assert not any("ST_DWithin" in s or "INSERT INTO dirty_properties" in s
                   or "source_id_native" in s or "lock_timeout" in s for s in order)


def test_attach_without_stragglers_writes_nothing():
    conn = _FakeConn()
    assert _attach_stragglers(conn) == 0
    assert not any("INSERT INTO properties" in s or "WITH batch AS" in s for s in _sqls(conn))


def test_attach_is_bounded_and_browse_syncs_its_births(monkeypatch: Any) -> None:
    """All nine portals land new rows NULL, so one attach must take a bounded slice (a backlog
    after a worker freeze would otherwise birth, recompute and browse-sync everything in one
    transaction), and the births reach Browse on this lane's cadence, not the next rebuild."""
    import scripts.recompute_property_stats as rps

    synced: list[list[int]] = []
    monkeypatch.setattr(rps, "sync_browse_list", lambda _c, ids: synced.append(list(ids)))
    conn = _FakeConn([
        (lambda s: s.startswith("SELECT id FROM listings WHERE property_id IS NULL"), [(11,)]),
        (lambda s: "INSERT INTO properties" in s, [(101,)]),
    ])

    assert _attach_stragglers(conn, limit=1) == 1

    select = _find(conn, "SELECT id FROM listings WHERE property_id IS NULL")
    assert select is not None and select[1] == {"limit": 1}
    assert "ORDER BY" not in select[0]
    assert synced == [[101]]
    assert rps.STRAGGLER_BATCH == 2000


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

    def transaction(self) -> Any:
        conn = self

        class _Txn:
            def __enter__(self) -> Any:
                conn.executed.append(("BEGIN", None))
                return self

            def __exit__(self, *exc: Any) -> None:
                conn.executed.append(("COMMIT", None))

        return _Txn()


# One drain slice, in order: the after-step inside ONE bounded transaction under the drain's own
# lock-wait ceiling (the Browse patch in its own savepoint), then the dequeue outside it.
_SLICE = ("BEGIN", "SET LOCAL statement_timeout", "SET LOCAL lock_timeout", "WITH batch AS",
          "BEGIN", "DELETE FROM browse_list", "INSERT INTO browse_list", "COMMIT",
          "INSERT INTO dirty_broker_listings", "COMMIT", "DELETE FROM dirty_properties")


def _steps(executed: list[tuple[str, Any]]) -> list[str]:
    return [next(p for p in _SLICE if s.startswith(p)) for s, _ in executed
            if s.startswith(_SLICE)]


def test_drain_dirty_recomputes_each_batch_then_terminates():
    conn = _DrainConn([[(7, "t1"), (8, "t1")], [(9, "t2")], []])
    total = _drain_dirty(conn, batch_size=2, cutoff="CUTOFF")
    assert total == 3
    assert conn.recomputed == [[7, 8], [9]]
    # deletes are scoped to the claimed ids and the run cutoff
    assert conn.deleted == [([7, 8], "CUTOFF"), ([9], "CUTOFF")]
    # each slice: recompute under the raised ceiling and the 5 s lock wait, Browse patch, broker
    # queue, then dequeue
    assert _steps(conn.executed) == list(_SLICE) * 2
    assert "SET LOCAL lock_timeout = '5s'" in _sqls(conn)
    assert [p for s, p in conn.executed if s.startswith("INSERT INTO dirty_broker_listings")] == [
        {"ids": [7, 8]}, {"ids": [9]}]
    assert [p for s, p in conn.executed if s.startswith("DELETE FROM browse_list")] == [
        ([7, 8],), ([9],)]


def test_properties_changed_recomputes_then_patches_then_queues_brokers():
    """The identity writers' after-step is the drain's, minus the ceiling and the dequeue: it
    nests in the caller's transaction and inherits the caller's timeouts."""
    conn = _FakeConn()
    properties_changed(conn, [9, 3, None, 9, 3])
    assert _steps(conn.executed) == ["WITH batch AS", "DELETE FROM browse_list",
                                     "INSERT INTO browse_list", "INSERT INTO dirty_broker_listings"]
    assert len(conn.executed) == 4
    assert [p for _s, p in conn.executed] == [{"ids": [3, 9]}, ([3, 9],), ([3, 9],),
                                               {"ids": [3, 9]}]
    assert not any("statement_timeout" in s or "lock_timeout" in s or "dirty_properties" in s
                   for s in _sqls(conn))


def test_the_broker_mirror_stamps_statement_time_not_transaction_start():
    """Inside a merge's transaction now() is its start, which a concurrent broker pass's cutoff
    can follow; its `marked_at <= cutoff` dequeue would then drop the re-stamp."""
    from scripts.recompute_property_stats import _MIRROR_BROKER_DIRTY_SQL as sql

    assert "now()" not in sql
    assert sql.count("clock_timestamp()") == 2


def test_properties_changed_with_no_ids_runs_nothing():
    conn = _FakeConn()
    properties_changed(conn, [])
    properties_changed(conn, [None])
    assert conn.executed == []


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


def test_full_sweep_attaches_bounded_batches_until_one_comes_back_short(monkeypatch: Any) -> None:
    import scripts.recompute_property_stats as rps

    sizes = iter([rps.STRAGGLER_BATCH, rps.STRAGGLER_BATCH, 7])
    calls = _run_main(monkeypatch, [])
    assert calls == ["attach"]          # an empty backlog is one pass
    monkeypatch.setattr(rps, "_attach_stragglers", lambda c, **k: calls.append("attach") or next(sizes))
    calls.clear()
    assert rps.main() == 0
    assert calls == ["attach", "attach", "attach"]


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
        _STRAGGLERS,
        _BORN,
    ]


def test_run_incremental_pass_runs_all_phases_and_unlocks():
    from scripts.recompute_property_stats import run_incremental_pass

    conn = _FakeConn(script=_lock_script(acquired=True))
    stats = run_incremental_pass(conn, batch_size=500)
    assert stats["skipped"] is False
    # every phase of the incremental pass ran...
    assert _find(conn, "INSERT INTO properties")  # straggler attach
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
    batch loop: serves now(), the lease CAS (always granted), max(id), and the
    saved resume cursor (next_lo, runs, cycle_started_at, young) when given one."""

    def __init__(self, max_id: int, cursor: tuple[Any, ...] | None = None) -> None:
        super().__init__([
            (lambda s: s == "SELECT now()", [("CUTOFF",)]),
            (lambda s: "property_maintenance_lease" in s and "RETURNING" in s, [(1,)]),
            (lambda s: "coalesce(max(id), 0) FROM properties" in s, [(max_id,)]),
            (lambda s: s.startswith("SELECT") and "property_sweep_cursor" in s,
             [cursor] if cursor else []),
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
    # complete walk → the swept-range clear over its whole range
    assert _dirty_clears(conn) == [{"cutoff": "CUTOFF", "lo": 1, "hi": 4001}]
    # ...and the completion stamp the health check reads (O(1) liveness signal)
    stamp = _find(conn, "property_sweep_last_complete")
    assert stamp and stamp[1]["max_id"] == 4000 and stamp[1]["batches"] == 2
    # every recompute statement runs under the raised per-statement ceiling
    # (the pooler's ~2-min default killed the first post-#971 batch at 3.5min)
    ceilings = [s for s, _ in conn.executed if "SET LOCAL statement_timeout" in s]
    recomputes = [s for s, _ in conn.executed if "WITH batch AS" in s]
    assert len(ceilings) == len(recomputes) == 2
    # ...and keeps the session's lock wait: only the drain slice bounds its own
    assert not any("lock_timeout" in s for s in _sqls(conn))


def test_the_full_sweep_patches_no_browse_row(monkeypatch: Any) -> None:
    """It recomputes every property, and the */15 wholesale rebuild is already that job's
    read-model half: only the dirty drain and the identity writers patch Browse."""
    conn = _SweepConn(max_id=4000)
    assert _run_sweep(monkeypatch, conn, []) == 0
    assert not any("browse_list" in s for s in _sqls(conn))


# monotonic: started_at, _wait_lease entry anchor, batch-1 deadline check, batch-1 per-batch
# timing, batch-2 deadline check (over a 60s budget), then the elapsed stamps in logging.
_BUDGET_STOP_CLOCK = [0.0, 1.0, 5.0, 50.0, 100.0, 101.0, 102.0, 103.0]
_CURSOR_SAVE = "VALUES ('property_sweep_cursor'"
_CURSOR_DROP = "DELETE FROM app_settings WHERE key = 'property_sweep_cursor'"
_CURSOR_READ = "SELECT (value->>'next_lo')"


def _swept(conn: _FakeConn) -> list[tuple[int, int]]:
    return [(p["lo"], p["hi"]) for s, p in conn.executed if "WITH batch AS" in s]


# Pinned as text, not read from the module: psycopg ignores unused mapping keys, so a dropped
# bound would still be handed its parameter and pass every params-only assertion.
_SCOPED_CLEAR = ("DELETE FROM dirty_properties WHERE marked_at <= %(cutoff)s "
                 "AND property_id >= %(lo)s AND property_id < %(hi)s")


def _dirty_clears(conn: _FakeConn) -> list[Any]:
    clears = [(s, p) for s, p in conn.executed if s.startswith("DELETE FROM dirty_properties")]
    assert all(s == _SCOPED_CLEAR for s, _ in clears), "every sweep clear is bounded both ways"
    return [p for _, p in clears]


def test_a_fresh_cycle_stopped_by_the_budget_saves_its_cursor_and_exits_green(
    monkeypatch: Any, caplog: Any,
) -> None:
    """The first budget stop of a cycle is tolerated: exit 0 with a warning naming where the
    next run resumes, and save that cursor. It still clears dirty rows ONLY in the range it
    swept (a wider clear would erase the recompute signal for unswept ids, leaving them stale
    until the next cycle instead of healed by the next incremental pass), and stamps nothing:
    a stale stamp IS the health check's alarm condition."""
    conn = _SweepConn(max_id=6000)  # 3 batches at the default size
    rc = _run_sweep(monkeypatch, conn, ["--max-seconds", "60"], clock=list(_BUDGET_STOP_CLOCK))
    assert rc == 0
    assert "the next run resumes this cycle at id 2001" in caplog.text
    assert _swept(conn) == [(1, 2001)]
    assert _dirty_clears(conn) == [{"cutoff": "CUTOFF", "lo": 1, "hi": 2001}]
    saved = _find(conn, _CURSOR_SAVE)
    assert saved and saved[1] == {"next_lo": 2001, "cycle_started_at": "CUTOFF", "runs": 1}
    assert not _find(conn, "NOT EXISTS (SELECT 1 FROM listings")
    assert not _find(conn, "property_sweep_last_complete")
    assert _find(conn, "SET holder = NULL")


class _TxnSweepConn(_SweepConn):
    transaction = _TxnMarkingConn.transaction


def test_a_resumed_run_finishes_the_cycle_from_its_cursor(monkeypatch: Any) -> None:
    """Run 2 walks [next_lo, max_id] only and clears only that range's dirt (ids below it were
    recomputed by run 1 and may be dirty again), reconciles, stamps the cycle (runs=2, run 1's
    start) and deletes the cursor in the stamp's own transaction."""
    conn = _TxnSweepConn(max_id=6000, cursor=(2001, 1, "CYCLE", True))
    assert _run_sweep(monkeypatch, conn, []) == 0
    assert _swept(conn) == [(2001, 4001), (4001, 6001)]
    assert _dirty_clears(conn) == [{"cutoff": "CUTOFF", "lo": 2001, "hi": 6001}]
    assert _find(conn, "NOT EXISTS (SELECT 1 FROM listings")
    stamp = _find(conn, "property_sweep_last_complete")
    assert stamp and stamp[1]["runs"] == 2 and stamp[1]["cycle_started_at"] == "CYCLE"
    assert stamp[1]["max_id"] == 6000 and stamp[1]["batches"] == 2
    assert not _find(conn, _CURSOR_SAVE)
    order = _sqls(conn)
    clear = next(i for i, s in enumerate(order) if s.startswith("DELETE FROM dirty_properties"))
    stamped = next(i for i, s in enumerate(order) if "property_sweep_last_complete" in s)
    dropped = order.index(_CURSOR_DROP)
    begin = max(i for i in range(clear) if order[i] == "BEGIN")
    assert begin < clear < stamped < dropped < order.index("COMMIT", begin)
    assert _find(conn, "SET holder = NULL")


def test_a_resumed_run_stopped_by_the_budget_again_is_red_and_advances_the_cursor(
    monkeypatch: Any, caplog: Any,
) -> None:
    """A cycle that needs three or more runs is genuinely too slow: RED, with the cycle facts
    in the message, and the cursor still advanced so a run within the age limit continues."""
    conn = _SweepConn(max_id=6000, cursor=(2001, 1, "CYCLE", True))
    rc = _run_sweep(monkeypatch, conn, ["--max-seconds", "60"], clock=list(_BUDGET_STOP_CLOCK))
    assert rc == 1
    assert "three or more runs" in caplog.text and "investigate per-batch cost" in caplog.text
    assert _swept(conn) == [(2001, 4001)]
    assert _dirty_clears(conn) == [{"cutoff": "CUTOFF", "lo": 2001, "hi": 4001}]
    saved = _find(conn, _CURSOR_SAVE)
    assert saved and saved[1] == {"next_lo": 4001, "cycle_started_at": "CYCLE", "runs": 2}
    assert not _find(conn, "NOT EXISTS (SELECT 1 FROM listings")
    assert not _find(conn, "property_sweep_last_complete")
    assert _find(conn, "SET holder = NULL")


@pytest.mark.parametrize("cursor", [
    (2001, 1, "OLD", False),     # the cycle began more than _CURSOR_MAX_AGE ago
    (8001, 1, "CYCLE", True),    # past max_id
    (1, 1, "CYCLE", True),       # nothing to skip
], ids=["stale", "past-max-id", "at-id-1"])
def test_a_cursor_that_cannot_continue_its_cycle_is_ignored(
    monkeypatch: Any, caplog: Any, cursor: tuple[Any, ...],
) -> None:
    """A stale cursor would let the stamp vouch for a reconcile whose lower half is days old:
    walk from id 1 as a fresh cycle instead. The age is judged by the database's clock."""
    import scripts.recompute_property_stats as rps

    conn = _SweepConn(max_id=6000, cursor=cursor)
    assert _run_sweep(monkeypatch, conn, []) == 0
    read = _find(conn, _CURSOR_READ)
    assert read and read[1] == {"max_age": rps._CURSOR_MAX_AGE}
    assert "ignoring the saved cursor" in caplog.text
    assert _swept(conn) == [(1, 2001), (2001, 4001), (4001, 6001)]
    stamp = _find(conn, "property_sweep_last_complete")
    assert stamp and stamp[1]["runs"] == 1 and stamp[1]["cycle_started_at"] == "CUTOFF"
    assert _find(conn, _CURSOR_DROP)


def test_a_fresh_complete_walk_clears_its_whole_range_and_leaves_no_cursor(
    monkeypatch: Any,
) -> None:
    """No cursor: walk from id 1 as before. The one dirty clear covers [1, max_id + 1), which
    stands in for the deleted global `_CLEAR_DIRTY_SQL` (why that is the same clear: the comment
    on `_CLEAR_DIRTY_SWEPT_SQL`)."""
    import scripts.recompute_property_stats as rps

    conn = _SweepConn(max_id=4000)
    assert _run_sweep(monkeypatch, conn, []) == 0
    assert _swept(conn) == [(1, 2001), (2001, 4001)]
    assert not hasattr(rps, "_CLEAR_DIRTY_SQL")
    assert _dirty_clears(conn) == [{"cutoff": "CUTOFF", "lo": 1, "hi": 4001}]
    assert _find(conn, "NOT EXISTS (SELECT 1 FROM listings")
    stamp = _find(conn, "property_sweep_last_complete")
    assert stamp and stamp[1]["runs"] == 1 and stamp[1]["cycle_started_at"] == "CUTOFF"
    assert _find(conn, _CURSOR_DROP) and not _find(conn, _CURSOR_SAVE)


def test_the_cursor_survives_one_daily_gap_but_not_two() -> None:
    """The daily cron starts hours late and unevenly, so the next day's run must still find the
    cycle young enough to resume; the day after must start over at id 1."""
    import scripts.recompute_property_stats as rps

    assert 24 * 3600 < _interval_seconds(rps._CURSOR_MAX_AGE) < 48 * 3600


def test_the_dry_run_reports_the_cursor_and_writes_nothing(
    monkeypatch: Any, caplog: Any,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    conn = _SweepConn(max_id=6000, cursor=(2001, 1, "CYCLE", True))
    conn.script.append((lambda s: s.startswith("SELECT count(*)"), [(7,)]))
    assert _run_sweep(monkeypatch, conn, ["--dry-run"]) == 0
    assert "(2001, 1, 'CYCLE', True)" in caplog.text
    assert not any(s.startswith(("INSERT", "UPDATE", "DELETE")) for s in _sqls(conn))
    assert not _find(conn, "property_maintenance_lease")


def test_a_fresh_run_stopped_before_its_first_batch_is_red_and_saves_nothing(
    monkeypatch: Any, caplog: Any,
) -> None:
    """The budget clock starts before the lease wait and the straggler attach, so a backlog can
    spend it before batch 1. A cursor at id 1 is never resumed, so there is no cycle to continue:
    RED and nothing saved, or every such day would exit green."""
    conn = _SweepConn(max_id=6000)
    # monotonic: started_at, _wait_lease anchor, batch-1 deadline check (past 60s), elapsed
    rc = _run_sweep(monkeypatch, conn, ["--max-seconds", "60"], clock=[0.0, 1.0, 100.0, 101.0])
    assert rc == 1
    assert "before the first batch" in caplog.text and "resumes" not in caplog.text
    assert _swept(conn) == [] and _dirty_clears(conn) == []
    assert not _find(conn, _CURSOR_SAVE) and not _find(conn, "property_sweep_last_complete")
    assert _find(conn, "SET holder = NULL")


def test_a_cursor_at_the_last_id_still_resumes(monkeypatch: Any) -> None:
    """`next_lo <= max_id`: a stop on the batch holding only the last id continues the cycle."""
    conn = _SweepConn(max_id=6001, cursor=(6001, 1, "CYCLE", True))
    assert _run_sweep(monkeypatch, conn, []) == 0
    assert _swept(conn) == [(6001, 8001)]
    stamp = _find(conn, "property_sweep_last_complete")
    assert stamp and stamp[1]["runs"] == 2


def test_an_unreadable_cursor_costs_one_fresh_cycle_not_every_run(
    monkeypatch: Any, caplog: Any,
) -> None:
    """The Settings page edits any app_settings row as raw JSON, and the read casts in SQL. A
    value the casts reject is a DataError, which run_resilient never retries: it must start a
    fresh cycle (whose completion deletes the row), not red every run until fixed by hand."""
    import psycopg

    class _HandEditedCur(_Cur):
        def execute(self, sql: str, params: Any = None) -> None:
            if "value->>'next_lo'" in sql:
                raise psycopg.errors.InvalidTextRepresentation(
                    'invalid input syntax for type bigint: "540k"')
            super().execute(sql, params)

    class _HandEditedConn(_SweepConn):
        def cursor(self) -> Any:
            return _HandEditedCur(self)

    conn = _HandEditedConn(max_id=4000)
    assert _run_sweep(monkeypatch, conn, []) == 0
    assert "unreadable" in caplog.text and "540k" in caplog.text
    assert _swept(conn) == [(1, 2001), (2001, 4001)]
    assert _find(conn, _CURSOR_DROP)


def test_the_cursor_sql_reads_back_what_it_saves() -> None:
    """The fakes serve canned rows, not SQL, so pin the SQL's own half of the contract: the read
    names the keys the save writes, in `_resume_point`'s order; a cursor is young while its
    cycle began after now() - max_age; both rows are upserts (a resumed run that stops again
    saves over the cursor); and the stamp carries the cycle's facts."""
    import re

    import scripts.recompute_property_stats as rps

    def built(sql: str) -> dict[str, str]:
        return dict(re.findall(r"'(\w+)', %\((\w+)\)s", sql))

    read = " ".join(rps._READ_SWEEP_CURSOR_SQL.split())
    assert re.findall(r"value->>'(\w+)'", read) == [
        "next_lo", "runs", "cycle_started_at", "cycle_started_at"]
    assert "(value->>'cycle_started_at')::timestamptz > now() - %(max_age)s::interval" in read
    assert built(rps._SAVE_SWEEP_CURSOR_SQL) == {
        "next_lo": "next_lo", "cycle_started_at": "cycle_started_at", "runs": "runs"}
    stamp = built(rps._STAMP_SWEEP_COMPLETE_SQL)
    assert stamp["runs"] == "runs" and stamp["cycle_started_at"] == "cycle_started_at"
    for sql in (rps._SAVE_SWEEP_CURSOR_SQL, rps._STAMP_SWEEP_COMPLETE_SQL):
        assert "ON CONFLICT (key) DO UPDATE" in sql


class _SettingsCur(_Cur):
    """app_settings as a dict: an upsert stores the jsonb object its SQL builds from its
    ('key', %(param)s) pairs, a DELETE pops the row, and the cursor read serves back the keys
    it names, young by the fake database's clock."""

    def execute(self, sql: str, params: Any = None) -> None:
        import re

        super().execute(sql, params)
        s, rows = self._conn.executed[-1][0], self._conn.settings
        key = re.search(r"'(property_sweep_\w+)'", s)
        if key is None:
            return
        if s.startswith("INSERT INTO app_settings"):
            rows[key[1]] = {k: params[p] for k, p in re.findall(r"'(\w+)', %\((\w+)\)s", s)}
        elif s.startswith("DELETE FROM app_settings"):
            rows.pop(key[1], None)
        elif s.startswith("SELECT") and key[1] in rows:
            *values, began = (rows[key[1]].get(k) for k in re.findall(r"value->>'(\w+)'", s))
            age = (self._conn.now - began).total_seconds()
            self._rows = [(*values, age < _interval_seconds(params["max_age"]))]


class _SettingsSweepConn(_SweepConn):
    def __init__(self, max_id: int, settings: dict[str, Any], now: Any) -> None:
        super().__init__(max_id)
        self.settings, self.now = settings, now
        self.script.insert(0, (lambda s: s == "SELECT now()", [(now,)]))  # first match wins

    def cursor(self) -> Any:
        return _SettingsCur(self)


@pytest.mark.parametrize("gap_hours, resumed", [(25, True), (37, False)])
def test_the_next_run_continues_from_exactly_what_the_last_one_saved(
    monkeypatch: Any, gap_hours: int, resumed: bool,
) -> None:
    """Run 1 stops on budget; run 2 reads back the row run 1 wrote and finishes the cycle, or
    starts over at id 1 once the cycle is past _CURSOR_MAX_AGE. Either way the stamp carries
    the cycle it completed and the cursor is gone."""
    from datetime import UTC, datetime, timedelta

    settings: dict[str, Any] = {}
    start = datetime(2026, 10, 3, 10, tzinfo=UTC)
    first = _SettingsSweepConn(6000, settings, now=start)
    rc = _run_sweep(monkeypatch, first, ["--max-seconds", "60"], clock=list(_BUDGET_STOP_CLOCK))
    assert rc == 0
    assert settings == {
        "property_sweep_cursor": {"next_lo": 2001, "cycle_started_at": start, "runs": 1}}

    later = start + timedelta(hours=gap_hours)
    second = _SettingsSweepConn(6000, settings, now=later)
    assert _run_sweep(monkeypatch, second, [], clock=[0.0] * 32) == 0
    stamp = settings.pop("property_sweep_last_complete")
    assert settings == {}
    assert _swept(second)[0] == ((2001, 4001) if resumed else (1, 2001))
    assert (stamp["runs"], stamp["cycle_started_at"]) == ((2, start) if resumed else (1, later))


class _DropAfterCur(_Cur):
    """Runs the statement, then loses the reply ONCE for the first one matching
    `conn.drop_after`: a drop that may follow the server's COMMIT."""

    def execute(self, sql: str, params: Any = None) -> None:
        import psycopg

        super().execute(sql, params)
        if self._conn.drop_after and self._conn.drop_after in self._conn.executed[-1][0]:
            self._conn.drop_after = None
            raise psycopg.OperationalError("server closed the connection unexpectedly")


class _DropAfterSweepConn(_SweepConn):
    def __init__(self, max_id: int, cursor: tuple[Any, ...], drop_after: str) -> None:
        super().__init__(max_id, cursor)
        self.drop_after: str | None = drop_after

    def cursor(self) -> Any:
        return _DropAfterCur(self)


@pytest.mark.parametrize("drop_after, budget_stop", [
    (_CURSOR_READ, False), (_CURSOR_SAVE, True), (_CURSOR_DROP, False),
], ids=["read", "stop", "finalize"])
def test_a_dropped_cursor_statement_replays_with_identical_values(
    monkeypatch: Any, drop_after: str, budget_stop: bool,
) -> None:
    """Every cursor read and write runs through step(), so a drop replays the whole op. Its
    values (next_lo, runs + 1, the cycle's start) are fixed before the op, so a replay after a
    COMMIT the client never saw writes the same row again: never runs + 2."""
    import scripts.recompute_property_stats as rps

    monkeypatch.setattr(rps.db.time, "sleep", lambda s: None)
    conn = _DropAfterSweepConn(6000, cursor=(2001, 1, "CYCLE", True), drop_after=drop_after)
    argv, clock = (["--max-seconds", "60"], list(_BUDGET_STOP_CLOCK)) if budget_stop else ([], None)
    assert _run_sweep(monkeypatch, conn, argv, clock=clock) == (1 if budget_stop else 0)
    replays = 1 if drop_after == _CURSOR_READ else 2
    hi = 4001 if budget_stop else 6001
    assert _dirty_clears(conn) == [{"cutoff": "CUTOFF", "lo": 2001, "hi": hi}] * replays
    if budget_stop:
        assert [p for s, p in conn.executed if _CURSOR_SAVE in s] == [
            {"next_lo": 4001, "cycle_started_at": "CYCLE", "runs": 2}] * replays
    else:
        assert [(p["runs"], p["cycle_started_at"]) for s, p in conn.executed
                if "property_sweep_last_complete" in s] == [(2, "CYCLE")] * replays


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


# --- one property, one voice (migrations 561 + 588) --------------------------------------

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
MIGRATION_561 = MIGRATIONS / "561_one_property_view.sql"
MIGRATION_588 = MIGRATIONS / "588_canonical_order_and_portal_dates.sql"
ADVERT_FIELDS = (
    "repr_listing_id", "repr_listing_ref_id", "category_main", "category_type",
    "category_sub_cb", "subtype", "disposition", "area_m2", "current_price_czk", "condition",
    "building_condition_level", "apartment_condition_level", "furnished", "source",
)
AMENITIES = ("has_lift", "has_balcony", "has_parking", "terrace", "garage", "cellar")
PHYSICAL_FACTS = (
    "usable_area", "estate_area", "garden_area", "parking_lots", "building_type", "ownership",
    "energy_rating",
)


def _set_clause(sql: str) -> str:
    """The recompute UPDATE's SET list, up to its FROM."""
    return sql.split("UPDATE properties p SET", 1)[1].split("FROM child_agg", 1)[0]


def _rhs(set_clause: str, column: str) -> str:
    """The (single-line) expression assigned to `column`."""
    import re

    m = re.search(rf"^\s*{re.escape(column)}\s*=\s*(.+?),?$", set_clause, re.M)
    assert m, f"{column} is not assigned in the SET list"
    return m[1].strip().rstrip(",")


def _child_agg(sql: str) -> str:
    return " ".join(sql.split("child_agg AS (", 1)[1].split("\n    ),", 1)[0].split())


def test_the_one_order_is_spelled_once_in_the_function():
    """MS5: active first, then a map point, then the earliest first sighting among active
    adverts / the latest last sighting among inactive ones, then portal trust, then the id.
    The rollup reads the rank and orders nothing of its own."""
    from scripts.recompute_property_stats import _RECOMPUTE_BATCH_SQL

    body = " ".join(MIGRATION_588.read_text().split())
    assert ("order by l.is_active desc, (ll.geom is not null) desc, "
            "case when l.is_active then l.first_seen_at end, "
            "case when not l.is_active then l.last_seen_at end desc, "
            "public.source_trust_rank(l.source), l.id))::integer") in body
    sql = " ".join(_RECOMPUTE_BATCH_SQL.split())
    assert "CROSS JOIN LATERAL property_canonical_listings(b.id) o" in sql
    for gone in ("source_trust_rank", "src_rank", "DISTINCT ON", "best_area", "golden", "coalesce(c."):
        assert gone not in sql, f"a second ordering or fallback is back: {gone}"


def test_every_advert_field_comes_from_the_canonical_advert():
    """Price and area from one row and condition with both derived levels from that same row
    (rule 14): the canonical advert, never a mix. The two write-only columns are not written
    (W6 dropped them, migration 593)."""
    from scripts.recompute_property_stats import _RECOMPUTE_BATCH_SQL

    setc = _set_clause(_RECOMPUTE_BATCH_SQL)
    for column in ADVERT_FIELDS:
        assert _rhs(setc, column).startswith("c."), f"{column} must be the canonical advert's"
    for gone in ("price_per_m2_source", "distinct_site_count"):
        assert gone not in _RECOMPUTE_BATCH_SQL


def test_every_physical_fact_is_the_first_non_empty_value_in_the_same_order():
    import re

    from scripts.recompute_property_stats import _RECOMPUTE_BATCH_SQL

    setc = _set_clause(_RECOMPUTE_BATCH_SQL)
    rollup = _child_agg(_RECOMPUTE_BATCH_SQL)
    for column in PHYSICAL_FACTS:
        assert _rhs(setc, column) == f"r.{column}"
        assert re.search(
            rf"\(array_agg\(k\.{column} ORDER BY k\.canonical_rank\) "
            rf"FILTER \(WHERE k\.{column} IS NOT NULL\)\)\[1\] AS {column}", rollup,
        ), f"{column} must be the first non-empty value in the canonical order"
    assert "bool_or(k.is_active) AS is_active" in rollup, "a property is live while ANY advert is"


def test_the_six_amenities_are_a_union_over_every_advert():
    """MS6: yes when any advert says yes, active or not; no order decides an amenity."""
    from scripts.recompute_property_stats import _RECOMPUTE_BATCH_SQL

    setc = _set_clause(_RECOMPUTE_BATCH_SQL)
    rollup = _child_agg(_RECOMPUTE_BATCH_SQL)
    for column in AMENITIES:
        assert _rhs(setc, column) == f"r.{column}"
        assert f"bool_or(k.{column}) AS {column}," in rollup
        assert f"array_agg(k.{column}" not in rollup


def test_one_newest_ad_date_per_offered_portal_and_the_two_portal_lists():
    """MS19: a dated column per `PORTAL_OPTIONS` code, each added by a migration; two sorted lists."""
    import re

    from scripts.recompute_property_stats import _RECOMPUTE_BATCH_SQL
    from toolkit.filter_registry import PORTAL_OPTIONS

    codes = [o.value for o in PORTAL_OPTIONS]
    setc = _set_clause(_RECOMPUTE_BATCH_SQL)
    assert re.findall(r"^\s*newest_ad_at_(\w+) = r\.newest_ad_at_\1,$", setc, re.M) == codes
    assert len(re.findall(r"newest_ad_at_\w+ =", setc)) == len(codes)
    rollup = _child_agg(_RECOMPUTE_BATCH_SQL)
    migrations = " ".join(
        " ".join(path.read_text(encoding="utf-8").split()) for path in MIGRATIONS.glob("*.sql"))
    for code in codes:
        assert (f"max(k.first_seen_at) FILTER (WHERE k.source = '{code}') "
                f"AS newest_ad_at_{code},") in rollup
        assert f"add column if not exists newest_ad_at_{code} timestamptz" in migrations, code
    assert "array_agg(DISTINCT k.source ORDER BY k.source) AS all_sources," in rollup
    assert ("coalesce(array_agg(DISTINCT k.source ORDER BY k.source) FILTER (WHERE k.is_active), "
            "ARRAY[]::text[]) AS active_sources,") in rollup
    for column in ("all_sources", "active_sources"):
        assert _rhs(setc, column) == f"r.{column}"


def test_a_tenth_portal_is_offered_only_with_its_read_model_lines():
    """MS19 (migration 590): a PORTAL_OPTIONS code is a portal Browse can order by only when
    the latest browse_projection projects its date (after the two lists and the ad count, in
    PORTAL_OPTIONS order) and the latest rebuild_browse_list builds and renames its partial
    index. With the properties column above, a tenth portal needs all three before it can be
    offered."""
    from tests.migration_defs import latest_definition
    from tests.test_browse_read_path_guardrail import _latest_migration_defining
    from tests.test_location_w3_projection import _columns, _sql
    from toolkit.filter_registry import PORTAL_OPTIONS

    codes = [o.value for o in PORTAL_OPTIONS]
    view = _latest_migration_defining("browse_projection").name
    assert _columns(_sql(view), "browse_projection")[-(len(codes) + 3):] == [
        "all_sources", "active_sources", "source_count", *(f"newest_ad_at_{c}" for c in codes)]
    rebuild = " ".join(latest_definition("rebuild_browse_list").read_text(encoding="utf-8").split())
    for c in codes:
        assert (f"create index browse_list_next_newest_ad_at_{c}_idx on browse_list_next "
                f"(category_main, category_type, newest_ad_at_{c} desc, property_id desc) "
                f"where newest_ad_at_{c} is not null") in rebuild, c
        assert (f"alter index browse_list_next_newest_ad_at_{c}_idx rename to "
                f"browse_list_newest_ad_at_{c}_idx") in rebuild, c
    assert rebuild.count("browse_list_next_newest_ad_at_") == 2 * len(codes)


def test_the_price_history_is_the_canonical_adverts_lineage():
    """MS10: the canonical advert's same-portal predecessors' steps plus one handover per link."""
    from scripts.recompute_property_stats import _RECOMPUTE_BATCH_SQL

    sql = " ".join(_RECOMPUTE_BATCH_SQL.split())
    assert sql.startswith("WITH batch AS (")
    assert "lineage AS ( WITH RECURSIVE link AS (" in sql
    assert ("WHERE l.property_id = k.pid AND l.source = k.source "
            "AND l.last_seen_at < k.first_seen_at AND l.first_seen_at < k.first_seen_at "
            "ORDER BY l.last_seen_at DESC, l.id DESC LIMIT 1") in sql
    assert "FROM lineage g JOIN listing_price_steps ps ON ps.listing_id = g.id" in sql
    assert "FROM lineage g JOIN listing_snapshots s ON s.listing_id = g.id" in sql
    assert "FROM steps ps GROUP BY ps.pid" in sql
    assert "LEFT JOIN lineage_span cs ON cs.pid = r.pid" in sql
    assert "ps.property_id" not in sql


def test_the_canonical_handover_is_stamped_for_the_price_alerts():
    """`repr_since` moves only when the canonical advert changes from one advert to another, or
    when a property the sweep reset to no ads (`source_count` 0, which only that reset writes)
    gains one, and both alert producers count only steps after it (migration 561). The city
    figures follow the canonical advert: their stamp clears with it, or when it predates
    `repr_since`."""
    import inspect

    from api import notifications as nf
    from scripts.recompute_property_stats import _RECOMPUTE_BATCH_SQL

    setc = _set_clause(_RECOMPUTE_BATCH_SQL)
    assert _rhs(setc, "repr_since") == (
        "CASE WHEN p.repr_listing_ref_id <> c.id OR p.source_count = 0 THEN now() "
        "ELSE p.repr_since END")
    assert _rhs(setc, "city_proximity_computed_at") == (
        "CASE WHEN p.repr_listing_ref_id <> c.id OR p.source_count = 0 "
        "OR p.city_proximity_computed_at < p.repr_since "
        "THEN NULL ELSE p.city_proximity_computed_at END")
    assert ("add column if not exists repr_since timestamptz not null default '-infinity'"
            in " ".join(MIGRATION_561.read_text().split()))
    assert "ps.scraped_at > p.repr_since" in inspect.getsource(nf._recent_price_drops)
    assert "st.scraped_at > m.repr_since" in inspect.getsource(nf.match_monitored_collections_once)
    assert "p.repr_since" in nf._MONITORED_CTE


def test_a_property_with_no_ads_is_reset_to_no_ads_once():
    """The batch statement starts from the ads, so the sweep resets a childless property
    (2026-10-07: 65587, 65660 and 302257 still named ads that other properties held): inactive,
    neither handle of a canonical ad, no portals, a count of 0, every offered portal's date NULL.
    Status `active` only (a merge retires its loser), `repr_since` untouched, and only while a
    column still differs, so the daily sweep never rewrites a row it already reset."""
    import re

    from scripts.recompute_property_stats import _RECONCILE_CHILDLESS_SQL
    from toolkit.filter_registry import PORTAL_OPTIONS

    sql = " ".join(_RECONCILE_CHILDLESS_SQL.split())
    m = re.fullmatch(
        r"UPDATE properties p SET \((.+?)\) = \((.+?)\) WHERE p\.status = 'active' "
        r"AND NOT EXISTS \(SELECT 1 FROM listings l WHERE l\.property_id = p\.id\) "
        r"AND \((.+?)\) IS DISTINCT FROM \((.+?)\)", sql)
    assert m, sql
    columns, values, guarded, guard = (g.split(", ") for g in m.groups())
    assert dict(zip(columns, values, strict=True)) == {
        "is_active": "false", "repr_listing_ref_id": "NULL", "repr_listing_id": "NULL",
        "source_count": "0", "all_sources": "ARRAY[]::text[]", "active_sources": "ARRAY[]::text[]",
        **{f"newest_ad_at_{o.value}": "NULL" for o in PORTAL_OPTIONS}}
    assert guarded == [f"p.{c}" for c in columns] and guard == values
    assert "repr_since" not in sql


def test_with_no_canonical_ad_a_property_leaves_the_read_models():
    """Why the reset needs no clause of its own in Browse or the map: the projection's consumer
    rule (rule 25) reads the location row of the canonical ad, joined on `repr_listing_ref_id`,
    so a NULL never passes it, and `browse_list` and `properties_map_mv` are its rows."""
    from tests.test_browse_read_path_guardrail import _latest_migration_defining, _strip_comments

    sql = _strip_comments(_latest_migration_defining("browse_projection").read_text())
    body = " ".join(sql[sql.lower().index("view browse_projection as"):].split(";", 1)[0].split())
    assert "left join listing_location ll on ll.listing_id = p.repr_listing_ref_id" in body
    assert "(ll.geom IS NOT NULL OR ll.country_status = 'foreign')" in body


def test_the_reconcile_counts_the_properties_it_reset():
    """The count the sweep logs (`RECOMPUTE reconciled childless=`): the rows the reset wrote."""
    from scripts.recompute_property_stats import _RECONCILE_CHILDLESS_SQL, _reconcile_childless

    reset = _FakeConn(script=[(lambda s: s.startswith("UPDATE properties p SET (is_active,"),
                               [(65587,), (65660,), (302257,)])])
    assert _reconcile_childless(reset) == 3
    assert _sqls(reset) == [" ".join(_RECONCILE_CHILDLESS_SQL.split())]


def test_every_recompute_variant_carries_the_whole_statement():
    """The one/scoped variants are derived from the batch SQL by narrowing the
    batch CTE; if that ever becomes a copy, they must not lose a rule."""
    import scripts.recompute_property_stats as rps

    for sql in (rps._RECOMPUTE_BATCH_SQL, rps._RECOMPUTE_ONE_SQL,
                rps._RECOMPUTE_SCOPED_SQL):
        for rule in ("property_canonical_listings", "lineage AS (", "city_proximity_computed_at",
                     "newest_ad_at_", "active_sources"):
            assert rule in sql
        assert "price_per_m2_source_id" not in sql


def test_a_property_is_born_one_way():
    """One bare INSERT (`scraper.db.NEW_SINGLETONS_SQL`) and the normal recompute: no other
    runtime code writes a new `properties` row, so no copy of the rollup can drift."""
    root = Path(__file__).resolve().parent.parent
    writers = sorted(
        str(path.relative_to(root))
        for top in ("scraper", "scripts", "toolkit", "api", "autodedup", "location_data")
        for path in (root / top).rglob("*.py")
        if "INSERT INTO properties" in path.read_text(encoding="utf-8").replace("\n", " ")
    )
    assert writers == ["scraper/db.py"], writers
