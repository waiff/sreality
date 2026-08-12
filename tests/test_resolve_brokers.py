"""Tests for scripts.resolve_brokers pure helpers.

Hermetic: only the keyset id-paging is exercised with a fake cursor; the SQL and
DB I/O are verified out-of-band via the Supabase MCP / the Actions full sweep.
The keyset chunker replaced an unbounded ``SELECT ... ORDER BY sreality_id`` that
crossed the pooler's 2-min statement timeout once four portals were attributed,
so these tests assert that EVERY page stays bounded (the regression guard).
"""

from __future__ import annotations

from typing import Any

from scripts.resolve_brokers import _BROKER_SOURCES, _broker_bearing_ids


class _KeysetCur:
    """Simulates a keyset scan over a fixed ascending id universe.

    Honours the ``sreality_id > :last`` lower bound and the ``LIMIT :lim`` page
    size, so the helper's pagination logic is exercised exactly as in Postgres.
    """

    def __init__(self, conn: "_KeysetConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_KeysetCur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        last = params.get("last")
        lim = params["lim"]
        ge = [i for i in self._conn.universe if last is None or i > last]
        self._rows = [(i,) for i in ge[:lim]]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _KeysetConn:
    def __init__(self, universe: list[int]) -> None:
        self.universe = sorted(universe)
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _KeysetCur:
        return _KeysetCur(self)


def test_keyset_returns_every_id_in_order():
    universe = [-287340, -5, -1, 7, 42, 1000, 4294963276]
    conn = _KeysetConn(universe)
    assert _broker_bearing_ids(conn, page_size=2) == universe


def test_keyset_threads_last_id_across_pages():
    conn = _KeysetConn([10, 20, 30, 40, 50])
    _broker_bearing_ids(conn, page_size=2)
    lasts = [p.get("last") for _, p in conn.executed]
    # first page has no lower bound; each subsequent page resumes after the prior
    # page's last id (keyset, not OFFSET).
    assert lasts == [None, 20, 40]


def test_every_page_is_bounded_no_unbounded_scan():
    """The bug was one unbounded ``ORDER BY`` scan. Every issued statement must
    carry a LIMIT and never the old inline ``source IN (...)`` literal."""
    conn = _KeysetConn(list(range(1, 51)))
    _broker_bearing_ids(conn, page_size=10)
    for sql, params in conn.executed:
        assert "LIMIT %(lim)s" in sql
        assert "source = ANY(%(srcs)s)" in sql
        assert "source IN (" not in sql
        assert params["srcs"] == list(_BROKER_SOURCES)


def test_keyset_terminates_on_exact_multiple():
    # A full final page is followed by one empty page that stops the loop.
    conn = _KeysetConn([1, 2, 3, 4])
    assert _broker_bearing_ids(conn, page_size=2) == [1, 2, 3, 4]
    # 2 full pages + 1 empty terminator = 3 statements.
    assert len(conn.executed) == 3


def test_keyset_terminates_on_short_page():
    # A short final page stops the loop without an extra empty query.
    conn = _KeysetConn([1, 2, 3])
    assert _broker_bearing_ids(conn, page_size=2) == [1, 2, 3]
    assert len(conn.executed) == 2


def test_keyset_empty_universe_is_single_query():
    conn = _KeysetConn([])
    assert _broker_bearing_ids(conn, page_size=100) == []
    assert len(conn.executed) == 1


# --- connection / lock resilience (the 2026-08-10 23:10 SSL-drop red) ---------


def test_release_lock_survives_a_dead_connection(caplog: Any) -> None:
    """The release runs from a `finally:`. On 2026-08-10 an SSL drop mid-rollup
    made `conn.cursor()` itself raise here, so the crash-during-cleanup REPLACED
    the real error in the traceback. It must warn and return instead."""
    import logging

    from scripts.resolve_brokers import _release_lock

    class _DeadConn:
        def cursor(self) -> Any:
            raise RuntimeError("the connection is closed")

    with caplog.at_level(logging.WARNING):
        _release_lock(_DeadConn(), "full:abc")  # must not raise
    failed = [r for r in caplog.records if "lock release failed" in r.message]
    assert failed
    # ...carrying the exception. The warning used to discard it entirely, so the
    # one scenario worth investigating produced a green run and a log line with
    # zero diagnostic content.
    assert failed[-1].exc_info is not None


def test_firm_link_takes_row_locks_in_ascending_id_order() -> None:
    """Deadlock guard: this UPDATE and the detail drain's batch upsert write the
    same `listings` rows, so this side must have ONE fixed lock order regardless
    of the plan the join would otherwise pick.

    PARTIAL and one-sided by design: the drain still locks in fetch-completion
    order and keys on a different column, so a cycle stays possible. What covers
    the residual is db.run_resilient retrying the DeadlockDetected victim on both
    sides; ordering here only makes the collision rarer."""
    from scripts.resolve_brokers import _LINK_LISTINGS_FIRM

    sql = " ".join(_LINK_LISTINGS_FIRM.format(extra="AND l.id = ANY(%(ids)s)").split())
    # MATERIALIZED, or PG12+ could inline the CTE and lose the ordered lock pass.
    assert "WITH targets AS MATERIALIZED" in sql
    assert "ORDER BY l.id FOR UPDATE OF l" in sql
    # ...and the locking pass must come before the UPDATE that re-touches the rows.
    assert sql.index("FOR UPDATE OF l") < sql.index("UPDATE listings l SET broker_firm_id")


def test_main_opens_the_connection_through_db_connect(monkeypatch: Any) -> None:
    """A 60-90 minute sweep needs db.connect()'s TCP keepalives + handshake retry,
    not a bare psycopg.connect() (which is what let a pooler recycle kill a run)."""
    import sys

    import scripts.resolve_brokers as rb

    class _DryConn:
        def __init__(self) -> None:
            self.executed: list[str] = []

        def __enter__(self) -> "_DryConn":
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def cursor(self) -> Any:
            return _DryCur(self)

    class _DryCur:
        def __init__(self, conn: _DryConn) -> None:
            self._conn = conn

        def __enter__(self) -> "_DryCur":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def execute(self, sql: str, params: Any = None) -> None:
            self._conn.executed.append(" ".join(sql.split()))

        def fetchall(self) -> list[tuple[Any, ...]]:
            return []

        def fetchone(self) -> tuple[Any, ...]:
            return (0,)

    opened: list[str] = []
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://test")
    monkeypatch.setattr(sys, "argv", ["resolve_brokers", "--dry-run"])
    monkeypatch.setattr(
        rb.db, "connect", lambda url=None, **k: opened.append(url) or _DryConn())
    assert rb.main() == 0
    assert opened == ["postgres://test"]


_BEAT = "UPDATE broker_resolution_lock SET heartbeat_at=now()"
_RELEASE = "UPDATE broker_resolution_lock SET holder=NULL"


class _ResilientCur:
    """Cursor that can inject ONE pooler drop, then serves the scripted rows."""

    def __init__(self, conn: "_ResilientConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "_ResilientCur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        import psycopg

        s = " ".join(sql.split())
        if self._conn.fail_on and self._conn.fail_on in s:
            self._conn.fail_on = None
            self._conn.broken = True
            raise psycopg.OperationalError("SSL connection has been closed unexpectedly")
        self._conn.executed.append(s)
        if s == "SELECT now()":
            self._rows = [("CUTOFF",)]
        elif "INSERT INTO broker_resolution_runs" in s and "RETURNING" in s:
            self._rows = [(1,)]
        elif "FROM dirty_broker_listings" in s and s.startswith("SELECT"):
            self._rows = [(10,), (11,)]
        else:
            self._rows = []
        self.rowcount = len(self._rows)
        if _BEAT in s:
            # The heartbeat's holder-guarded CAS matches our row (1) unless the
            # lock was declared stale and re-claimed by another run (0).
            self.rowcount = self._conn.lock_rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _ResilientConn:
    def __init__(self, name: str, fail_on: str | None = None,
                 lock_rows: int = 1) -> None:
        self.name = name
        self.fail_on = fail_on
        self.lock_rows = lock_rows
        self.broken = False
        self.closed = False
        self.executed: list[str] = []

    def cursor(self) -> _ResilientCur:
        return _ResilientCur(self)

    def transaction(self) -> Any:
        import contextlib

        return contextlib.nullcontext()

    def close(self) -> None:
        self.closed = True


def test_run_incremental_reconnects_and_rebinds_after_a_pooler_drop(monkeypatch: Any) -> None:
    """The 2026-08-10 23:10 red: an SSL drop mid-run killed the whole pass. The
    phases are idempotent, so the drop must now cost a reconnect + replay — and
    the CALLER must get the fresh connection back, or the lock release in its
    `finally:` lands on the dead socket."""
    import scripts.resolve_brokers as rb

    monkeypatch.setattr(rb.db.time, "sleep", lambda s: None)
    first = _ResilientConn("first", fail_on="INSERT INTO broker_identities")
    fresh = _ResilientConn("fresh")

    res, live = rb._run_incremental(
        first, [], [], 500, reconnect=lambda: fresh)

    assert res == {"attributed": 2, "brokers": 0}
    # the caller's handle is the REPLACEMENT, not the dead original
    assert live is fresh
    assert first.broken and first.closed
    # the dropped phase replayed in full on the new connection, and the run
    # finished there (queue rows dequeued, run row closed out)
    assert any("INSERT INTO broker_identities" in s for s in fresh.executed)
    assert any("DELETE FROM dirty_broker_listings" in s for s in fresh.executed)
    assert any("listings_attributed" in s for s in fresh.executed)


def test_heartbeat_runs_once_per_attempt_not_once_per_phase(monkeypatch: Any) -> None:
    """The beat must be the FIRST statement of every retried ATTEMPT, not a step
    of its own before each phase. A phase re-runs from the top on retry, so a beat
    outside the retried op leaves the lock unrefreshed for the whole replay — the
    2026-08-09 sweep's merge phase alone ran 8.4 min on ONE attempt, so a single
    replay used to exceed the staleness window and let a `*/10` incremental steal
    the lock out from under a live sweep."""
    import scripts.resolve_brokers as rb

    monkeypatch.setattr(rb.db.time, "sleep", lambda s: None)
    first = _ResilientConn("first", fail_on="INSERT INTO broker_identities")
    fresh = _ResilientConn("fresh")

    rb._run_incremental(first, [], [], 500, "incremental:abc",
                        reconnect=lambda: fresh)

    # the dropped attempt beat before it ran, and it was that attempt's LAST
    # statement (nothing of the phase itself landed)
    assert first.executed[-1].startswith(_BEAT)
    # ...and the replay beat AGAIN, first thing, on the FRESH connection
    assert fresh.executed[0].startswith(_BEAT)
    beats = [i for i, s in enumerate(fresh.executed) if s.startswith(_BEAT)]
    phases = ["INSERT INTO broker_identities", "UPDATE broker_identities bi SET firm_identity_id",
              "WITH targets AS MATERIALIZED", "INSERT INTO brokers",
              "DELETE FROM dirty_broker_listings"]
    # one beat per phase re-run on the fresh conn, each preceding its phase
    assert len(beats) >= len(phases)
    for needle in phases:
        at = next(i for i, s in enumerate(fresh.executed) if needle in s)
        assert any(b < at for b in beats)
    # beats are counted per ATTEMPT, not per phase: the dropped attempt of
    # resolve.attribute contributed exactly one beat of its own on top of the
    # one-per-phase set the replay produced.
    dropped = [s for s in first.executed if s.startswith(_BEAT)]
    assert len(dropped) == 1


def test_lost_lock_aborts_the_run_instead_of_resolving_concurrently(
    monkeypatch: Any,
) -> None:
    """A heartbeat CAS that matches 0 rows means our lock went stale and another
    run took it over. Continuing would resolve concurrently while _release_lock's
    own CAS silently no-ops. Fail loud — and RuntimeError is not an
    OperationalError, so db.run_resilient re-raises it immediately rather than
    replaying three more times into the race."""
    import pytest

    import scripts.resolve_brokers as rb

    monkeypatch.setattr(rb.db.time, "sleep", lambda s: None)
    stolen = _ResilientConn("stolen", lock_rows=0)

    with pytest.raises(RuntimeError, match="lock lost mid-run"):
        rb._run_incremental(stolen, [], [], 500, "incremental:abc",
                            reconnect=lambda: stolen)

    # exactly ONE beat: non-transient, so no retry churn...
    assert len([s for s in stolen.executed if s.startswith(_BEAT)]) == 1
    # ...and no attribution write landed after the lock was lost
    assert not any("INSERT INTO broker_identities" in s for s in stolen.executed)


def test_release_lock_falls_back_to_a_fresh_connection() -> None:
    """main()'s `conn` is only rebound on a NORMAL return, so a run that
    reconnected and then died non-transiently releases on the socket
    run_resilient already closed — stranding the lock for a full staleness TTL
    even though the process had a healthy path to the DB. The holder-guarded CAS
    is safe from any connection, so the release opens one of its own."""
    import scripts.resolve_brokers as rb

    class _DeadConn:
        def cursor(self) -> Any:
            raise RuntimeError("the connection is closed")

    fresh = _ResilientConn("fresh")
    rb._release_lock(_DeadConn(), "full:abc", reconnect=lambda: fresh)
    assert any(s.startswith(_RELEASE) for s in fresh.executed)
    # the release owns the connection it opened, so it must close it
    assert fresh.closed


def test_release_lock_does_not_reconnect_when_the_handle_is_live() -> None:
    """The fallback is a last resort, not a second write: a working connection
    must release on itself and never open a spare."""
    import scripts.resolve_brokers as rb

    live = _ResilientConn("live")
    opened: list[int] = []
    rb._release_lock(live, "full:abc",
                     reconnect=lambda: opened.append(1) or _ResilientConn("spare"))
    assert [s for s in live.executed if s.startswith(_RELEASE)]
    assert opened == []


def test_firm_linking_gets_its_own_floor_when_attribution_ate_the_budget(
    monkeypatch: Any,
) -> None:
    """The firm-link loop runs AFTER attribution, which routinely spends the whole
    --max-seconds budget (the 2026-08-09 and 08-10 sweeps both logged "time budget
    reached during attribution"). Guarding it with the SAME deadline trips on its
    first check and skips the global firm reconcile outright — a phase measured at
    43-186s for the full corpus, traded away on roughly half the sweeps. It gets
    _FIRM_LINK_MIN_SECONDS of its own instead, which still bounds the accumulation
    case the guard exists for."""
    import time

    import scripts.resolve_brokers as rb

    conn = _ResilientConn("full")
    all_ids = [1, 2, 3, 4, 5, 6]  # 3 chunks at batch_size=2
    attributed: list[list[int]] = []
    linked: list[list[int]] = []

    monkeypatch.setattr(rb, "_broker_bearing_ids", lambda c, n: all_ids)
    monkeypatch.setattr(rb, "_attribute",
                        lambda c, sel, params: attributed.append(params["ids"]))
    monkeypatch.setattr(rb, "_resolve_firms", lambda c, free, franchise: None)
    monkeypatch.setattr(rb, "_link_listings_firm",
                        lambda c, extra="", params=None: linked.append(params["ids"]))
    monkeypatch.setattr(rb, "_attach_singletons", lambda c: 0)
    monkeypatch.setattr(rb, "_cross_source_merge", lambda c, auto, run_id: (0, 0))
    monkeypatch.setattr(rb, "_max_id", lambda c, table: 0)
    monkeypatch.setattr(rb, "_refresh_matview", lambda c: None)
    monkeypatch.setattr(rb, "_generate_merge_candidates", lambda c: 0)

    # the budget is already spent when the sweep starts, i.e. the worst real shape
    rb._run_full(conn, [], [], [], 2, time.monotonic() - 1,
                 reconnect=lambda: conn)

    # attribution stops at its first chunk boundary, as designed...
    assert attributed == [[1, 2]]
    # ...and firm linking still walks the whole corpus on its own floor
    assert linked == [[1, 2], [3, 4], [5, 6]]
