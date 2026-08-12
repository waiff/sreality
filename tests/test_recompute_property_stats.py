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
    assert stats == {"skipped": True, "attached": 0, "recomputed": 0}
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
               clock: list[float] | None = None) -> int:
    import sys

    import scripts.recompute_property_stats as rps

    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://test")
    monkeypatch.setattr(sys, "argv", ["recompute_property_stats", *argv])
    # See _run_main: the sweep opens its connection via scraper.db.connect now.
    monkeypatch.setattr(rps.db, "connect", lambda *a, **k: conn)
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
    assert any("lease release failed" in r.message for r in caplog.records)


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
    # under the wire runs its full retried worst case past it).
    in_flight_batch_s = 10 * 60 * rps._BATCH_RESILIENT_ATTEMPTS
    assert timeout_s >= rps._MAX_BUDGET_SECONDS + in_flight_batch_s + 120

    # The dispatch default must be dispatchable — i.e. at or under the clamp,
    # never silently truncated to it. (YAML 1.1 parses the bare key `on` as the
    # boolean True, hence the two-key lookup.)
    triggers = wf.get("on", wf.get(True))
    default_budget = float(
        triggers["workflow_dispatch"]["inputs"]["max_seconds"]["default"])
    assert default_budget <= rps._MAX_BUDGET_SECONDS
