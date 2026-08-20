"""Tests for scripts.resolve_brokers pure helpers.

Hermetic: only the keyset id-paging is exercised with a fake cursor; the SQL and
DB I/O are verified out-of-band via the Supabase MCP / the Actions full sweep.
The keyset chunker replaced an unbounded ``SELECT ... ORDER BY sreality_id`` that
crossed the pooler's 2-min statement timeout once four portals were attributed,
so these tests assert that EVERY page stays bounded (the regression guard).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

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
        self._conn.executed_with_params.append((s, params))
        if s == "SELECT now()":
            self._rows = [("CUTOFF",)]
        elif "INSERT INTO broker_resolution_runs" in s and "RETURNING" in s:
            self._rows = [(1,)]
        elif "FROM dirty_broker_listings" in s and s.startswith("SELECT"):
            self._rows = [(10,), (11,)]
        elif "SELECT key, value FROM app_settings" in s:
            self._rows = list(self._conn.settings_rows)
        elif "SELECT value FROM app_settings" in s:
            self._rows = [] if self._conn.setting_missing else [(self._conn.setting_value,)]
        elif "SELECT id, broker_id FROM broker_identities WHERE broker_id = ANY" in s:
            wanted = set(params[0])
            self._rows = [(i, b) for i, b in sorted(self._conn.broker_of.items())
                          if b in wanted]
        elif "SELECT id, broker_id FROM broker_identities" in s:
            self._rows = list(self._conn.broker_of.items())
        elif "FROM broker_merge_suppressions" in s:
            self._rows = list(self._conn.suppression_rows)
        elif "FROM broker_identity_contacts" in s:
            self._rows = list(self._conn.contact_rows)
        elif "FROM broker_identities bi" in s and "mergeable" in s:
            self._rows = list(self._conn.identity_rows)
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
        self.executed_with_params: list[tuple[str, Any]] = []
        # app_settings / broker-identity fixtures the resume-cursor and review-pair
        # paths read; harmless defaults for every other test.
        self.setting_value: Any = None
        self.setting_missing = False
        # rows of the multi-key _settings() read: (key, jsonb value)
        self.settings_rows: list[tuple[Any, ...]] = []
        self.broker_of: dict[int, int] = {}
        # the auto-merge engine's two corpus reads
        self.identity_rows: list[tuple[Any, ...]] = []
        self.contact_rows: list[tuple[Any, ...]] = []
        # active broker_merge_suppressions, as (identity_lo, identity_hi) rows
        self.suppression_rows: list[tuple[Any, ...]] = []

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
    monkeypatch.setattr(rb, "_auto_merge", lambda c, run_id: (0, 0, 0))
    monkeypatch.setattr(rb, "_max_id", lambda c, table: 0)
    monkeypatch.setattr(rb, "_refresh_matview", lambda c: None)
    monkeypatch.setattr(rb, "_generate_merge_candidates", lambda c: 0)

    # the budget is already spent when the sweep starts, i.e. the worst real shape
    rb._run_full(conn, [], [], True, 2, time.monotonic() - 1,
                 reconnect=lambda: conn)

    # attribution stops at its first chunk boundary, as designed...
    assert attributed == [[1, 2]]
    # ...and firm linking still walks the whole corpus on its own floor
    assert linked == [[1, 2], [3, 4], [5, 6]]


# --- full-sweep resume cursor + conditional dirty clear -----------------------


def _stub_full_sweep(monkeypatch: Any, all_ids: list[int],
                     cursor: int | None = None, lap_swept: int = 0,
                     lap_started_at: str | None = None,
                     merge_result: tuple[int, int, int] = (0, 0, 0)) -> list[list[int]]:
    """Neutralise every phase of _run_full except attribution; return the chunks
    attribution actually walked, in walk order.

    `merge_result` is what _auto_merge reports — the three counts the run row
    then records. Parameterised so a test can prove they are stamped in the right
    order instead of every stub returning an indistinguishable (0, 0, 0)."""
    import scripts.resolve_brokers as rb

    attributed: list[list[int]] = []
    monkeypatch.setattr(rb, "_broker_bearing_ids", lambda c, n: all_ids)
    monkeypatch.setattr(rb, "_sweep_state",
                        lambda c: (cursor, lap_swept, lap_started_at))
    monkeypatch.setattr(rb, "_attribute",
                        lambda c, sel, params: attributed.append(params["ids"]))
    monkeypatch.setattr(rb, "_resolve_firms", lambda c, free, franchise: None)
    monkeypatch.setattr(rb, "_link_listings_firm", lambda c, extra="", params=None: None)
    monkeypatch.setattr(rb, "_attach_singletons", lambda c: 0)
    monkeypatch.setattr(rb, "_auto_merge", lambda c, run_id: merge_result)
    monkeypatch.setattr(rb, "_max_id", lambda c, table: 0)
    monkeypatch.setattr(rb, "_refresh_matview", lambda c: None)
    monkeypatch.setattr(rb, "_generate_merge_candidates", lambda c: 0)
    return attributed


def _params(conn: _ResilientConn, needle: str) -> Any:
    for sql, params in conn.executed_with_params:
        if needle in sql:
            return params
    return None


def test_rotation_cursor_resumes_past_the_previous_stop_and_wraps() -> None:
    """A bare `id > cursor` resume would starve the head forever. The rotation
    walks the tail first and then wraps, so no id is ever unreachable."""
    from scripts.resolve_brokers import _rotate_from_cursor

    ids = [10, 20, 30, 40, 50]
    assert _rotate_from_cursor(ids, None) == ids
    assert _rotate_from_cursor(ids, 20) == [30, 40, 50, 10, 20]
    # a cursor at/over the corpus max wraps back to the floor
    assert _rotate_from_cursor(ids, 50) == ids
    assert _rotate_from_cursor(ids, 99) == ids
    # a cursor on an id that has since been deleted still resumes after it
    assert _rotate_from_cursor(ids, 25) == [30, 40, 50, 10, 20]
    assert _rotate_from_cursor([], 20) == []


def test_every_id_is_reachable_within_one_wrap() -> None:
    """The hard constraint: successive budget-truncated sweeps must cover the WHOLE
    corpus. Before the cursor each sweep restarted at the minimum id, so the tail
    above the break was skipped every day, forever."""
    from scripts.resolve_brokers import _rotate_from_cursor

    ids = list(range(1, 26))
    seen: set[int] = set()
    cursor: int | None = None
    for _ in range(5):  # 5 sweeps x 5 ids of budget = one full rotation
        walk = _rotate_from_cursor(ids, cursor)[:5]
        seen.update(walk)
        cursor = walk[-1]
    assert seen == set(ids)


def test_truncated_sweep_scopes_the_clear_and_withholds_the_completion_stamp(
    monkeypatch: Any,
) -> None:
    """The A2 bug in one test: the sweep broke out on --max-seconds, then wiped the
    ENTIRE dirty queue anyway — erasing the re-attribution signal for every id it
    never reached — and stamped nothing, so the run still looked green."""
    import time

    import scripts.resolve_brokers as rb

    conn = _ResilientConn("full")
    attributed = _stub_full_sweep(monkeypatch, [1, 2, 3, 4, 5, 6])

    rb._run_full(conn, [], [], True, 2, time.monotonic() - 1, reconnect=lambda: conn)

    assert attributed == [[1, 2]]
    # the clear is scoped to the swept window, NOT global
    assert not any(s == "DELETE FROM dirty_broker_listings WHERE marked_at <= %(cutoff)s"
                   for s in conn.executed)
    clear = _params(conn, "DELETE FROM dirty_broker_listings")
    assert clear == {"cutoff": "CUTOFF", "lo": 1, "hi": 2}
    # ...the resume cursor advances past the last swept id, carrying the open lap...
    cursor = _params(conn, "jsonb_build_object('last_id'")
    assert cursor == {"key": "broker_sweep_cursor", "last_id": 2,
                      "lap_swept": 2, "lap_started_at": None}
    # ...and a lap that has not gone all the way round stamps nothing.
    assert not any("completed_at" in s for s in conn.executed)


def test_lap_closes_across_truncated_sweeps_and_only_then_stamps(
    monkeypatch: Any,
) -> None:
    """The composing assertion the two halves were missing. Truncation is the
    DESIGNED steady state (the 2026-08-10 sweep attributed 480,000 of 535,007 ids
    inside its budget), so a stamp that needs one run to walk everything would never
    be written at all — a permanently amber check that never rings — while the one
    light day that did fit would age past the fail threshold two days later with the
    rotation working perfectly. Coverage is therefore tracked across runs and the
    stamp lands when the LAP closes."""
    import time

    import scripts.resolve_brokers as rb

    ids = list(range(1, 11))
    cursor: int | None = None
    lap_swept = 0
    covered: set[int] = set()
    stamps: list[Any] = []
    for _ in range(5):  # budget for one 2-id chunk per run
        conn = _ResilientConn("full")
        attributed = _stub_full_sweep(monkeypatch, ids, cursor=cursor,
                                      lap_swept=lap_swept, lap_started_at="T0")
        rb._run_full(conn, [], [], True, 2, time.monotonic() - 1, reconnect=lambda: conn)
        covered.update(i for chunk in attributed for i in chunk)
        written = _params(conn, "jsonb_build_object('last_id'")
        cursor, lap_swept = written["last_id"], written["lap_swept"]
        stamps.append(_params(conn, "completed_at"))

    assert covered == set(ids)
    # nothing stamped until the rotation had actually been all the way round...
    assert stamps[:4] == [None, None, None, None]
    assert stamps[4]["key"] == "broker_resolution_last_complete"
    assert stamps[4]["swept"] == len(ids)
    # ...and closing the lap reopens a fresh one from zero.
    assert lap_swept == 0


def test_complete_sweep_clears_globally_and_stamps_completion(monkeypatch: Any) -> None:
    """A walk that covered every id in ONE run is the only one allowed to wipe the
    whole queue (it re-attributed everything), and it closes the lap in one go."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("full")
    attributed = _stub_full_sweep(monkeypatch, [1, 2, 3, 4])

    rb._run_full(conn, [], [], True, 2, None, reconnect=lambda: conn)

    assert attributed == [[1, 2], [3, 4]]
    assert _params(conn, "DELETE FROM dirty_broker_listings") == {"cutoff": "CUTOFF"}
    stamp = _params(conn, "completed_at")
    assert stamp["key"] == "broker_resolution_last_complete" and stamp["swept"] == 4


def test_the_run_row_records_the_three_merge_counts_in_order(monkeypatch: Any) -> None:
    """auto_merges / queued_for_review / suppressed_pairs are three same-typed
    integers bound positionally. Every stub returned (0, 0, 0), so swapping any two
    of them — which silently reports the rail's work as auto-merges, or hides a
    broken rail behind a healthy-looking count — changed nothing anywhere."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("full")
    _stub_full_sweep(monkeypatch, [1, 2], merge_result=(3, 5, 7))

    stats, _ = rb._run_full(conn, [], [], True, 2, None, reconnect=lambda: conn)

    sql = next(s for s in conn.executed if "UPDATE broker_resolution_runs SET ended_at" in s)
    assert sql.index("auto_merges") < sql.index("queued_for_review") < sql.index(
        "suppressed_pairs")
    assert _params(conn, "UPDATE broker_resolution_runs SET ended_at") == (3, 5, 7, 1)
    assert (stats["auto_merges"], stats["queued"], stats["suppressed"]) == (3, 5, 7)


def test_cursor_is_written_before_the_failure_prone_tail(monkeypatch: Any) -> None:
    """The 17-25 min tail (merge, rollups, matview, candidates) dies often enough —
    an SSL drop past the retry budget, an AdminShutdown, the 110-min SIGKILL — that
    persisting the cursor behind it threw away the whole run's rotation advance and
    re-walked the identical window the next day."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("full")
    _stub_full_sweep(monkeypatch, [1, 2, 3, 4])
    monkeypatch.setattr(rb, "_refresh_matview",
                        lambda c: (_ for _ in ()).throw(RuntimeError("tail died")))

    with pytest.raises(RuntimeError):
        rb._run_full(conn, [], [], True, 2, None, reconnect=lambda: conn)

    assert _params(conn, "jsonb_build_object('last_id'")["last_id"] == 4


def test_sweep_resumes_from_the_stored_cursor(monkeypatch: Any) -> None:
    """Yesterday's truncated sweep stopped at 2; today's must start at 3 rather
    than re-walking the same head."""
    import time

    import scripts.resolve_brokers as rb

    conn = _ResilientConn("full")
    attributed = _stub_full_sweep(monkeypatch, [1, 2, 3, 4, 5, 6], cursor=2)

    rb._run_full(conn, [], [], True, 2, time.monotonic() - 1, reconnect=lambda: conn)

    assert attributed == [[3, 4]]
    assert _params(conn, "jsonb_build_object('last_id'")["last_id"] == 4


def test_wrapped_window_clears_both_arms_of_the_range(monkeypatch: Any) -> None:
    """A rotation that crossed the end of the corpus swept a window that WRAPS
    (high ids then low ones). A plain BETWEEN would delete nothing — or, with the
    bounds swapped, delete the unswept middle."""
    import time

    import scripts.resolve_brokers as rb

    conn = _ResilientConn("full")
    attributed = _stub_full_sweep(monkeypatch, [1, 2, 3, 4, 5, 6], cursor=4)

    rb._run_full(conn, [], [], True, 2, time.monotonic() - 1, reconnect=lambda: conn)

    # walk = [5, 6, 1, 2, 3, 4]; one chunk of 2 lands the window [5, 6]...
    assert attributed == [[5, 6]]
    assert _params(conn, "DELETE FROM dirty_broker_listings") == {
        "cutoff": "CUTOFF", "lo": 5, "hi": 6}
    # ...and a window that actually wraps uses the OR form, never BETWEEN.
    conn2 = _ResilientConn("full")
    _stub_full_sweep(monkeypatch, [1, 2, 3, 4, 5, 6], cursor=4)
    rb._run_full(conn2, [], [], True, 4, time.monotonic() - 1, reconnect=lambda: conn2)
    sql = next(s for s in conn2.executed if "DELETE FROM dirty_broker_listings" in s)
    assert "listing_id >= %(lo)s OR listing_id <= %(hi)s" in sql
    assert _params(conn2, "DELETE FROM dirty_broker_listings") == {
        "cutoff": "CUTOFF", "lo": 5, "hi": 2}


def test_deadline_on_the_final_chunk_is_still_a_complete_walk(monkeypatch: Any) -> None:
    """The budget can expire while finishing the LAST chunk. That swept everything,
    so it must still stamp completion — withholding it would red the freshness check
    on a sweep that did its whole job."""
    import time

    import scripts.resolve_brokers as rb

    conn = _ResilientConn("full")
    attributed = _stub_full_sweep(monkeypatch, [1, 2])

    rb._run_full(conn, [], [], True, 2, time.monotonic() - 1, reconnect=lambda: conn)

    assert attributed == [[1, 2]]
    assert _params(conn, "completed_at") is not None
    assert _params(conn, "DELETE FROM dirty_broker_listings") == {"cutoff": "CUTOFF"}


def test_sweep_state_reads_tolerate_a_missing_or_junk_setting() -> None:
    """A first run (no row), a hand-edited value, or a NULL must resume from the
    floor with a fresh lap rather than crash the daily sweep."""
    from scripts.resolve_brokers import _sweep_state

    for value, expected in [
        (None, (None, 0, None)),
        ({}, (None, 0, None)),
        ({"last_id": None}, (None, 0, None)),
        ({"last_id": "nope"}, (None, 0, None)),
        ({"last_id": "42"}, (42, 0, None)),
        ({"last_id": 42, "lap_swept": 7, "lap_started_at": "2026-08-12T04:35:00Z"},
         (42, 7, "2026-08-12T04:35:00Z")),
        ({"last_id": 42, "lap_swept": "junk", "lap_started_at": 5}, (42, 0, None)),
    ]:
        conn = _ResilientConn("cursor")
        conn.setting_value = value
        assert _sweep_state(conn) == expected
    empty = _ResilientConn("cursor")
    empty.setting_missing = True
    assert _sweep_state(empty) == (None, 0, None)


# --- applying merges at broker grain -----------------------------------------


def _merge_plan(conn: _ResilientConn) -> dict[str, Any]:
    events = _params(conn, "INSERT INTO broker_merge_events")
    update = _params(conn, "UPDATE brokers b SET status = 'merged_away'")
    return {
        "ledger": sorted(zip(events["r"], events["s"], events["i"])),
        "retired": dict(zip(update["l"], update["s"])),
        "moved": dict(zip(events["i"], events["s"])),
    }


def test_a_broker_is_retired_into_exactly_one_survivor_per_run() -> None:
    """Two components disjoint in IDENTITY space can both touch one broker: an
    already-merged broker holds several identities, and a bridge between two of its
    own can lapse (contacts are never deleted, so a value gets freq-demoted or a
    display_name changes). Keyed on the retired id, `losers` then kept whichever
    component came last while broker_merge_events had logged the broker retired into
    BOTH — a ledger the unmerge replay cannot reconcile with the surviving row."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    # broker 30 holds identities 1 and 2; component A = {1, 5}, component B = {2, 6}
    conn.broker_of = {1: 30, 5: 10, 2: 30, 6: 20}
    assert rb._apply_merges(conn, [[1, 5], [2, 6]]) == (2, 0)

    plan = _merge_plan(conn)
    # one survivor for the whole broker component, and 30 is retired ONCE
    assert plan["retired"] == {20: 10, 30: 10}
    assert sorted(plan["ledger"]) == [(20, 10, 6), (30, 10, 1), (30, 10, 2)]
    # ...and the ledger agrees with the brokers UPDATE on every retired broker
    for retired_id, survivor_id, _ in plan["ledger"]:
        assert plan["retired"][retired_id] == survivor_id


def test_a_retired_broker_keeps_no_identities() -> None:
    """The strictly more reachable sibling of the same gap, and it needs only ONE
    new edge: the identity UPDATE moved just the component's identities while the
    brokers UPDATE retired the whole broker, so a broker could end up merged_away
    while still holding one. _BROKER_ROLLUP only touches status='active', so that
    identity's rollups freeze, the dossier 404s it, and the next sweep can elect the
    merged_away broker as a SURVIVOR. api/broker_review.py::merge_brokers always
    moved the losers' whole identity set; this is the divergent path."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    # broker 40 holds identities 2 and 3; only 2 is bridged to broker 10's identity 1
    conn.broker_of = {1: 10, 2: 40, 3: 40}
    assert rb._apply_merges(conn, [[1, 2]]) == (1, 0)

    plan = _merge_plan(conn)
    assert plan["retired"] == {40: 10}
    # identity 3 rides along even though no edge named it
    assert plan["moved"] == {2: 10, 3: 10}


def test_merge_components_are_unioned_transitively_in_broker_space() -> None:
    """A chain A-B, B-C over identities of three brokers is ONE merge, not two
    overlapping ones — otherwise the middle broker is both a survivor and a loser."""
    import scripts.resolve_brokers as rb

    assert rb._broker_components([[1, 2], [3, 4]], {1: 5, 2: 7, 3: 7, 4: 9}) == [[5, 7, 9]]
    # ...and genuinely disjoint components stay separate, lowest id surviving each
    assert rb._broker_components(
        [[1, 2], [3, 4]], {1: 5, 2: 7, 3: 8, 4: 9}) == [[5, 7], [8, 9]]
    # a component already on one broker is not a merge (idempotent re-run)
    assert rb._broker_components([[1, 2]], {1: 5, 2: 5}) == []
    # an identity with no broker contributes nothing
    assert rb._broker_components([[1, 2]], {1: 5}) == []


def test_a_component_chaining_two_groups_is_logged(caplog: Any) -> None:
    """Merging at broker grain widens what one run can fuse: decide_merges caps a
    group at MAX_AUTO_MERGE_COMPONENT identities, but two capped groups chained
    through a broker that holds an identity in each now apply as ONE merge, and the
    sweep log's `auto` is a bare count that cannot tell that from a pair merge. The
    chaining edge is a merge already recorded, so it is not auto-merging on evidence
    it lacks — but the chain is unbounded, so it must be visible in the log the
    operator reads rather than only in the leaderboard moving."""
    import logging

    import scripts.resolve_brokers as rb

    # broker 7 holds identity 2 (group A) and identity 3 (group B) — one component
    with caplog.at_level(logging.WARNING, logger="resolve_brokers"):
        assert rb._broker_components(
            [[1, 2], [3, 4]], {1: 5, 2: 7, 3: 7, 4: 9}) == [[5, 7, 9]]
    assert "chains 2 auto-merge groups: 3 brokers onto survivor 5" in caplog.text

    # ...and an ordinary single-group component stays quiet
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="resolve_brokers"):
        assert rb._broker_components([[1, 2]], {1: 5, 2: 7}) == [[5, 7]]
    assert caplog.text == ""


def test_apply_merges_skips_a_group_already_on_one_broker() -> None:
    """Idempotence: a re-run after a committed apply must write nothing."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    conn.broker_of = {1: 10, 2: 10}
    assert rb._apply_merges(conn, [[1, 2]]) == (0, 0)
    assert not any("broker_merge_events" in s for s in conn.executed)
    assert rb._apply_merges(conn, []) == (0, 0)


# --- the apply-time suppression backstop (migration 401) ----------------------
#
# NOTE: _ResilientConn enforces no CHECK / FK / UNIQUE constraint (see
# [[adversarial-review-fake-conn-db-constraints]]), so these tests assert the PLAN
# — which components apply and what the ledger arrays carry — not that the DB would
# accept it. The schema-replay job (migrations.yml) PREPAREs the statements.


def test_a_component_that_would_reunite_a_suppressed_pair_is_dropped_whole(
    caplog: Any,
) -> None:
    """The transitive chain decide_merges structurally cannot see. It removes the
    suppressed EDGE (A,B), but _broker_components then fuses components through any
    broker holding an identity in both — so A and B still land on one broker via C,
    re-creating exactly the merge the operator undid, with no suppressed edge
    anywhere in the input. The backstop drops the whole component and says so."""
    import logging

    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    # identity 1 -> broker 10 (A), 2 -> broker 20 (B), 3 -> broker 30 (C);
    # corroborated edges A-C and C-B, with (1, 2) suppressed.
    conn.broker_of = {1: 10, 2: 20, 3: 30}
    with caplog.at_level(logging.WARNING, logger="resolve_brokers"):
        assert rb._apply_merges(conn, [[1, 3], [2, 3]],
                                suppressed_pairs={(1, 2)}) == (0, 1)
    assert not any("broker_merge_events" in s for s in conn.executed)
    assert "identities 1/2 are an active broker_merge_suppressions pair" in caplog.text


def test_the_group_the_pure_layer_still_emits_is_dropped_here() -> None:
    """The other half of the responsibility split (see the resolver's
    test_removing_an_edge_does_not_stop_the_group_forming_around_it): decide_merges
    emits [A, B, C] even with (A, B) suppressed, because it only removes the EDGE.
    The backstop is what actually refuses it."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    conn.broker_of = {1: 10, 2: 20, 3: 30}
    assert rb._apply_merges(conn, [[1, 2, 3]], suppressed_pairs={(1, 2)}) == (0, 1)
    assert not any("broker_merge_events" in s for s in conn.executed)


def test_an_already_co_located_suppressed_pair_does_not_block_the_merge() -> None:
    """The backstop fires on NEW co-location only. If the two identities already
    share a broker (a stale suppression, or one lifted out of band), dropping the
    component would freeze unrelated merges forever without fixing anything."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    # identities 1 and 2 are BOTH on broker 10 already; 3 is on broker 20
    conn.broker_of = {1: 10, 2: 10, 3: 20}
    assert rb._apply_merges(conn, [[1, 3]], suppressed_pairs={(1, 2)}) == (1, 0)
    plan = _merge_plan(conn)
    assert plan["retired"] == {20: 10}


def test_an_unrelated_component_still_applies_alongside_a_dropped_one() -> None:
    """Suppression is per component, not a kill switch for the whole run."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    conn.broker_of = {1: 10, 2: 20, 3: 30, 4: 40, 5: 50}
    merged, dropped = rb._apply_merges(conn, [[1, 3], [2, 3], [4, 5]],
                                       suppressed_pairs={(1, 2)})
    assert (merged, dropped) == (1, 1)
    assert _merge_plan(conn)["retired"] == {50: 40}


def test_no_suppressions_is_the_unchanged_hot_path() -> None:
    """The table starts empty and stays tiny; an empty set must not change a single
    merge decision (the rail is opt-in per pair, never a global damper)."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    conn.broker_of = {1: 10, 2: 20}
    assert rb._apply_merges(conn, [[1, 2]], suppressed_pairs=set()) == (1, 0)
    assert rb._apply_merges(_fresh_merge_conn({1: 10, 2: 20}), [[1, 2]]) == (1, 0)


def _fresh_merge_conn(broker_of: dict[int, int]) -> _ResilientConn:
    conn = _ResilientConn("merge")
    conn.broker_of = broker_of
    return conn


def test_the_sweep_loads_active_suppressions_and_passes_them_both_ways(
    monkeypatch: Any,
) -> None:
    """One indexed SELECT per sweep, and the SAME set reaches the pure decision AND
    the apply-time backstop — the two halves of the rail must never disagree about
    what the operator rejected."""
    import scripts.resolve_brokers as rb

    seen: dict[str, Any] = {}
    monkeypatch.setattr(rb.R, "decide_merges",
                        lambda i, c, **kw: seen.update(decide=kw["suppressed_pairs"])
                        or rb.R.MergeDecision([[1, 2]], [], [(1, 2)]))
    monkeypatch.setattr(rb, "_apply_merges",
                        lambda c, g, **kw: seen.update(apply=kw["suppressed_pairs"]) or (0, 1))
    monkeypatch.setattr(rb, "_queue_review_pairs", lambda c, p, i, bv, r: 0)
    monkeypatch.setattr(rb, "_suppressed_pairs", lambda c: {(1, 2)})

    conn = _ResilientConn("merge")
    conn.identity_rows = [(1, "sreality", "Jan Novak", 10, True),
                          (2, "idnes", "Novak Jan", 10, True)]
    conn.contact_rows = [(1, "email", "a@x.cz"), (2, "email", "a@x.cz")]
    auto, queued, suppressed = rb._auto_merge(conn, run_id=5)
    assert seen["decide"] == seen["apply"] == {(1, 2)}
    # the run's suppressed_pairs is edge-level suppressions PLUS whole components
    # the backstop dropped — a rail that only counted one of the two would report a
    # silent zero on exactly the transitive case it exists for
    assert (auto, queued, suppressed) == (0, 0, 2)


def test_the_suppression_load_reads_only_active_rows() -> None:
    """Lifting never deletes (the lift columns are the audit trail), so the query
    that feeds the rail has to filter or every overridden NO would come back."""
    import scripts.resolve_brokers as rb

    assert "broker_merge_suppressions" in rb._SUPPRESSED_PAIRS_SQL
    assert "lifted_at IS NULL" in rb._SUPPRESSED_PAIRS_SQL
    conn = _ResilientConn("merge")
    assert rb._suppressed_pairs(conn) == set()


def test_the_suppression_load_returns_the_pairs_it_read() -> None:
    """The whole rail is one set: a loader that returns an empty set no matter what
    the table holds disables auto-merge suppression completely and every other test
    here still passes (they pass their own set in). This is the only assertion that
    the DB rows become that set — including the int() coercion, because psycopg
    hands back whatever the driver decided the bigint was."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    conn.suppression_rows = [("1", "2"), (3, 4)]
    assert rb._suppressed_pairs(conn) == {(1, 2), (3, 4)}


def test_a_suppression_written_mid_sweep_still_blocks_the_apply() -> None:
    """The merge step runs ~8.4 min and the set is snapshotted at its top, so an
    operator NO landing while it runs would be applied straight over. _apply_merges
    re-reads the active set inside its own write transaction and unions it in."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    conn.broker_of = {1: 10, 2: 20}
    conn.suppression_rows = [(1, 2)]        # landed after the sweep's own load
    assert rb._apply_merges(conn, [[1, 2]], suppressed_pairs=set()) == (0, 1)
    assert not any("broker_merge_events" in s for s in conn.executed)
    # ...and the read happens in the same transaction as the writes it guards
    order = [s for s in conn.executed]
    assert any("FROM broker_merge_suppressions" in s for s in order)


def test_the_events_insert_pins_every_column_to_its_projection() -> None:
    """bridge_kind/bridge_value are two same-typed text columns fed from two
    same-typed arrays: swapping them survives pytest AND the PREPARE gate, and every
    auto-merge from then on records the phone number as the contact KIND."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    conn.broker_of = {1: 10, 2: 20}
    rb._apply_merges(conn, [[1, 2]], group_bridges={(1, 2): ("email", "jan@x.cz")})
    sql = next(s for s in conn.executed if "INSERT INTO broker_merge_events" in s)
    columns = ("(merge_group_id, survivor_broker_id, retired_broker_id, identity_id, "
               "prev_broker_id, reason, source, bridge_kind, bridge_value)")
    projection = "SELECT g, s, r, i, r, n, 'auto', k, v"
    unnest = ("FROM unnest(%(g)s::uuid[], %(s)s::bigint[], %(r)s::bigint[], "
              "%(i)s::bigint[], %(n)s::text[], %(k)s::text[], %(v)s::text[]) "
              "AS d(g, s, r, i, n, k, v)")
    assert columns in sql and projection in sql and unnest in sql
    assert sql.index(columns) < sql.index(projection) < sql.index(unnest)


def test_only_the_bridged_identities_carry_the_stamp() -> None:
    """The merge unit is the BROKER, so a loser's whole identity set moves — but the
    bridge explains only the identities of the group that produced it. Stamping the
    rest would invent evidence the future remax validation would then audit against."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    # broker 20 holds identity 2 (bridged to 1) AND identity 3 (carried along)
    conn.broker_of = {1: 10, 2: 20, 3: 20}
    assert rb._apply_merges(conn, [[1, 2]],
                            group_bridges={(1, 2): ("email", "jan@x.cz")}) == (1, 0)
    events = _params(conn, "INSERT INTO broker_merge_events")
    stamped = dict(zip(events["i"], events["k"]))
    assert stamped == {2: "email", 3: None}


def test_the_bridge_lookup_does_not_rescan_the_group_list_per_component() -> None:
    """The stamping pass was two nested scans over every group for every component
    (0.02s -> 7.02s at 5,000 groups). The map is built once; this pins the OUTPUT of
    the cheap form on a fan-out big enough that the quadratic one is unusable."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    groups = [[i, i + 1] for i in range(1, 4000, 2)]
    conn.broker_of = {i: i for g in groups for i in g}
    merged, dropped = rb._apply_merges(
        conn, groups, group_bridges={(1, 2): ("email", "jan@x.cz")})
    assert (merged, dropped) == (len(groups), 0)
    events = _params(conn, "INSERT INTO broker_merge_events")
    # exactly the one group with a bridge is stamped, out of 2,000
    assert [k for k in events["k"] if k] == ["email"]


def test_an_unambiguous_auto_merge_records_the_contact_that_caused_it() -> None:
    """bridge_kind/bridge_value are NULL on all 7,689 live rows, so the future remax
    validation has no evidence trail. Stamp the simple dominant case; anything
    ambiguous stays NULL rather than naming a contact that may not be the reason."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    conn.broker_of = {1: 10, 2: 20}
    assert rb._apply_merges(conn, [[1, 2]],
                            group_bridges={(1, 2): ("email", "jan@re-max.cz")}) == (1, 0)
    events = _params(conn, "INSERT INTO broker_merge_events")
    assert events["k"] == ["email"] and events["v"] == ["jan@re-max.cz"]

    plain = _fresh_merge_conn({1: 10, 2: 20})
    assert rb._apply_merges(plain, [[1, 2]]) == (1, 0)
    assert _params(plain, "INSERT INTO broker_merge_events")["k"] == [None]


def test_a_chained_component_is_not_stamped_with_one_groups_bridge() -> None:
    """Two groups fused in broker space is exactly the case where no single contact
    explains the merge (_broker_components' documented widening)."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    conn.broker_of = {1: 10, 2: 30, 3: 30, 4: 20}
    assert rb._apply_merges(conn, [[1, 2], [3, 4]],
                            group_bridges={(1, 2): ("email", "a@x.cz"),
                                           (3, 4): ("phone", "420600111222")})[0] == 2
    assert set(_params(conn, "INSERT INTO broker_merge_events")["k"]) == {None}


# --- review pairs reach the operator queue -----------------------------------


def test_review_pairs_are_persisted_as_broker_merge_candidates() -> None:
    """They were computed and discarded every sweep (9,377/day at the 2026-08-12
    review), so the conservative auto-merge guard's only output never reached the
    operator."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    conn.broker_of = {1: 100, 2: 200, 3: 300}
    identities = {
        1: rb.R.Identity(1, "sreality", "Jan Novák"),
        2: rb.R.Identity(2, "idnes", "Jan Novak"),
        3: rb.R.Identity(3, "remax", None),
    }

    bridges = {(1, 2): {"phone:+420111"}, (2, 3): {"email:a@x.cz"}}
    assert rb._queue_review_pairs(conn, [(1, 2), (2, 3)], identities, bridges,
                                  run_id=7) == 2
    sql = next(s for s in conn.executed if "broker_merge_candidates" in s)
    assert "'contact_bridge_review'" in sql
    # the SAME gate the existing name_firm populator uses: a resolved group is
    # never revived by regeneration
    assert "ON CONFLICT (group_key) DO UPDATE" in sql
    assert "WHERE broker_merge_candidates.status = 'proposed'" in sql
    params = _params(conn, "broker_merge_candidates")
    # identity ids are mapped to BROKER ids (the grain the operator merges at)
    assert params["lo"] == [100, 200] and params["hi"] == [200, 300]
    assert params["gk"] == ["contactbridge:100:200", "contactbridge:200:300"]
    # the bridging contact rides along: without it the card shows the operator two
    # names and no reason the engine hesitated, which is not a reviewable decision
    assert json.loads(params["ev"][0])["bridges"] == ["phone:+420111"]


def test_review_pairs_drop_the_oversized_component_expansion() -> None:
    """decide_merges downgrades a component larger than MAX_AUTO_MERGE_COMPONENT by
    expanding it PAIRWISE — n(n-1)/2 rows for a chain it already called
    untrustworthy. Persisting those would bury the queue (9,377 decided pairs on the
    2026-08-12 sweep) under one-click merges of agents joined only through a shared
    switchboard several hops away. The component's real edges still land."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    conn.broker_of = {1: 100, 2: 200, 3: 300}
    identities = {i: rb.R.Identity(i, "s", None) for i in (1, 2, 3)}

    # 1-2 and 2-3 are real edges; 1-3 is only the expansion's transitive closure
    bridges = {(1, 2): {"phone:+420111"}, (2, 3): {"phone:+420111"}}
    assert rb._queue_review_pairs(conn, [(1, 2), (1, 3), (2, 3)], identities,
                                  bridges, run_id=1) == 2
    params = _params(conn, "broker_merge_candidates")
    assert params["gk"] == ["contactbridge:100:200", "contactbridge:200:300"]


def test_review_pair_group_keys_are_idempotent_across_sweeps() -> None:
    """Re-running the sweep must not accumulate duplicate proposals for one pair —
    the key is the unordered BROKER pair, so pair order and identity-pair
    multiplicity both collapse onto one group_key."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    conn.broker_of = {1: 200, 2: 100, 3: 100}
    identities = {i: rb.R.Identity(i, "s", None) for i in (1, 2, 3)}

    # (1,2) and (1,3) both resolve to the broker pair {100, 200}
    bridges = {(1, 2): {"phone:+420111"}, (1, 3): {"phone:+420111"}}
    assert rb._queue_review_pairs(conn, [(1, 2), (1, 3)], identities, bridges,
                                  run_id=1) == 1
    params = _params(conn, "broker_merge_candidates")
    assert params["gk"] == ["contactbridge:100:200"]
    assert params["lo"] == [100] and params["hi"] == [200]


def test_review_pairs_skip_identities_without_a_distinct_broker() -> None:
    """An unattributed identity has no broker to propose, and a pair already on one
    broker is not a merge candidate — either would be a junk row the operator has
    to dismiss by hand."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    conn.broker_of = {1: 100, 2: 100}  # 3 has no broker
    identities = {i: rb.R.Identity(i, "s", None) for i in (1, 2, 3)}
    bridges = {(1, 2): {"phone:+420111"}, (1, 3): {"phone:+420111"}}

    assert rb._queue_review_pairs(conn, [(1, 2), (1, 3)], identities, bridges,
                                  run_id=1) == 0
    assert not any("broker_merge_candidates" in s for s in conn.executed)
    assert rb._queue_review_pairs(conn, [], identities, bridges, run_id=1) == 0


def test_auto_merge_queues_review_pairs_after_applying_merges(
    monkeypatch: Any,
) -> None:
    """Order matters: _apply_merges re-points broker_identities.broker_id, so a pair
    read BEFORE it could propose a broker that no longer survives."""
    import scripts.resolve_brokers as rb

    calls: list[str] = []
    seen: dict[str, Any] = {}
    monkeypatch.setattr(rb.R, "decide_merges",
                        lambda i, c, **kw: rb.R.MergeDecision([[1, 2]], [(1, 2)]))
    monkeypatch.setattr(rb, "_apply_merges",
                        lambda c, g, **kw: calls.append("apply") or (1, 0))
    monkeypatch.setattr(rb, "_retire_dead_candidates",
                        lambda c, by: calls.append(f"retire:{by}") or 0)
    monkeypatch.setattr(rb, "_queue_review_pairs",
                        lambda c, p, i, bv, r: calls.append("queue")
                        or seen.update(bridges=bv) or len(p))

    conn = _ResilientConn("merge")
    conn.identity_rows = [(1, "sreality", "Jan Novak", 10, True),
                          (2, "idnes", "Novak Jan", 10, True)]
    conn.contact_rows = [(1, "email", "a@x.cz"), (2, "email", "a@x.cz")]
    auto, queued, suppressed = rb._auto_merge(conn, run_id=5)

    assert calls == ["apply", "retire:auto:sweep", "queue"]
    # queued_for_review keeps meaning "pairs DECIDED", unchanged by persistence
    assert (auto, queued, suppressed) == (1, 1, 0)
    # the bridge index is keyed the same way decide_merges normalises a pair
    assert seen["bridges"] == {(1, 2): {"email:a@x.cz"}}


def test_sweep_retires_proposals_whose_brokers_no_longer_survive(
    monkeypatch: Any,
) -> None:
    """Overlapping pairs are the norm at these volumes: merging {A,B} retires B, and
    the {B,C} proposal keyed on it can then only ever answer 409 'fewer than two of
    the given brokers are active'. Neither generator revisits a key it does not
    re-propose, so without this the dead rows accumulate forever in the 100-row
    page the operator actually looks at."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("full")
    _stub_full_sweep(monkeypatch, [1, 2])

    rb._run_full(conn, [], [], True, 2, None, reconnect=lambda: conn)

    sql = next(s for s in conn.executed if "UPDATE broker_merge_candidates" in s)
    assert "status = 'proposed'" in sql
    assert "b.status = 'active'" in sql and ") < 2" in sql


# --- C1: registry-driven attribution + CZ-scoped rollups ----------------------


class _AttributeCur:
    """Records every statement `_attribute` issues over one chunk."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self) -> "_AttributeCur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((" ".join(sql.split()), params))


class _AttributeConn:
    def __init__(self) -> None:
        self.cur = _AttributeCur()

    def cursor(self) -> _AttributeCur:
        return self.cur


def test_attribute_runs_every_registered_statement_once_per_chunk() -> None:
    """The dispatcher was 16 hand-written `cur.execute` lines; onboarding a portal
    meant remembering to add 2-4 more. It is now driven by the registry, so a
    config row that lands is a statement that runs."""
    from toolkit.broker_sources import attribution_statements

    from scripts.resolve_brokers import _attribute

    conn = _AttributeConn()
    _attribute(conn, "l.id = ANY(%(ids)s)", {"ids": [7, 8]})

    assert len(conn.cur.executed) == len(attribution_statements())
    assert all(p == {"ids": [7, 8]} for _, p in conn.cur.executed)
    # Every statement is bound to the chunk — none scans the whole corpus.
    assert all("%(ids)s" in s for s, _ in conn.cur.executed)
    assert all("{sel}" not in s for s, _ in conn.cur.executed)
    for source in _BROKER_SOURCES:
        assert any(f"l.source = '{source}'" in s for s, _ in conn.cur.executed)


def test_the_sweep_id_scan_covers_every_registered_source() -> None:
    """The full sweep enumerates ids by source; a portal missing here is never
    reconciled by the daily sweep no matter what _attribute would do with it."""
    conn = _KeysetConn([1, 2, 3])
    _broker_bearing_ids(conn, page_size=10)
    assert conn.executed[0][1]["srcs"] == list(_BROKER_SOURCES)


def test_broker_rollup_writes_cz_counts_from_the_domestic_predicate() -> None:
    """D4: the leaderboard's stored counts. Two idnes syndication feeds carry ~26k
    foreign listings and ranked #1 and #2 nationally, 8x the busiest genuinely
    Czech broker, because these columns counted every attributed row."""
    from scripts.resolve_brokers import _BROKER_ROLLUP, _DOMESTIC

    sql = " ".join(_BROKER_ROLLUP.format(bscope="").split())
    assert _DOMESTIC == "l.obec_id IS NOT NULL"
    # The predicate is bound at import, not left for the caller to remember.
    assert "{domestic}" not in sql
    for col in ("cz_listing_count", "cz_property_count",
                "cz_active_listing_count", "cz_active_property_count"):
        assert f"{col} = coalesce(ls.cz_" in sql
    assert sql.count(f"FILTER (WHERE {_DOMESTIC}") == 4
    # ...and the unscoped columns still count everything (rule #3: scope, not delete).
    assert "listing_count = coalesce(ls.lc, 0)" in sql
    assert "active_property_count = coalesce(ls.apc, 0)" in sql


def test_manual_merge_recompute_writes_the_cz_columns_too() -> None:
    """api.broker_review imports the same constant for its post-merge recompute;
    if it wrote only the unscoped counts, a merged broker's ranking would silently
    fall back to zero until the next daily sweep."""
    from api.broker_review import _BROKER_ROLLUP as imported

    from scripts.resolve_brokers import _BROKER_ROLLUP

    assert imported is _BROKER_ROLLUP
    assert "cz_active_property_count" in imported.format(
        bscope="AND broker_id = ANY(%(bids)s)")


# --- the unified engine: inputs, kill switch, evidence, card hygiene ----------


def test_the_engine_reads_the_whole_corpus_unfiltered() -> None:
    """Both maps the rule consults are corpus-wide statements — which names a contact
    belongs to, how many firms a name appears at — so a WHERE clause on either read
    does not shrink the work, it changes the verdict. (The deleted CTEs did exactly
    that: they pre-filtered to frequency-1 contacts and the guard then never saw the
    six duplicates that made one agent's own e-mail look shared.)"""
    import scripts.resolve_brokers as rb

    identities = " ".join(rb._MERGE_IDENTITIES_SQL.split())
    assert "WHERE" not in identities.upper()
    assert "coalesce(b.primary_firm_id, fi.firm_id)" in identities
    assert "b.status IS DISTINCT FROM 'merged_away'" in identities
    contacts = " ".join(rb._MERGE_CONTACTS_SQL.split())
    assert contacts == "SELECT broker_identity_id, kind, value FROM broker_identity_contacts"


def test_identity_rows_carry_firm_and_mergeability_into_the_rule(
    monkeypatch: Any,
) -> None:
    """A dropped firm_id silently disables path B for that identity; a dropped
    mergeable flag lets a merged-away broker be elected a survivor again."""
    import scripts.resolve_brokers as rb

    seen: dict[str, Any] = {}
    monkeypatch.setattr(rb.R, "decide_merges",
                        lambda i, c, **kw: seen.update(ids=i, contacts=c)
                        or rb.R.MergeDecision())

    conn = _ResilientConn("merge")
    conn.identity_rows = [(1, "sreality", "Jan Novak", 10, True),
                          (2, "idnes", "Novak Jan", None, False)]
    conn.contact_rows = [(1, "email", "a@x.cz"), (999, "email", "orphan@x.cz")]
    assert rb._auto_merge(conn, run_id=1) == (0, 0, 0)

    assert seen["ids"] == [rb.R.Identity(1, "sreality", "Jan Novak", 10, True),
                           rb.R.Identity(2, "idnes", "Novak Jan", None, False)]
    # a contact row whose identity the other statement did not return is dropped,
    # not crashed on: the two reads are separate statements over a live corpus
    assert seen["contacts"] == [rb.R.Contact(1, "email", "a@x.cz")]


def test_the_kill_switch_defaults_to_on_and_only_an_explicit_false_stops_it() -> None:
    """Absent means ON — the engine is the designed behaviour, and a row that has to
    exist for it to run is a row someone deletes."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("settings")
    assert rb._settings(conn) == ([], [], True)                       # no rows at all
    for value, expected in [(False, False), ("false", False), (True, True),
                            ("true", True), (None, True)]:
        conn.settings_rows = [(rb._AUTO_MERGE_ENABLED_KEY, value)]
        assert rb._settings(conn)[2] is expected
    conn.settings_rows = [("broker_free_email_domains", ["Gmail.com"]),
                          ("broker_franchise_domains", ["RE-MAX.cz"])]
    assert rb._settings(conn) == (["gmail.com"], ["re-max.cz"], True)


def test_the_sweep_skips_the_merge_step_when_the_switch_is_off(
    monkeypatch: Any,
) -> None:
    """Off must mean the step does not run at all — and the run row must record
    zeros rather than carrying the previous run's counts."""
    import scripts.resolve_brokers as rb

    calls: list[str] = []
    conn = _ResilientConn("full")
    _stub_full_sweep(monkeypatch, [1, 2])
    monkeypatch.setattr(rb, "_auto_merge",
                        lambda c, run_id: calls.append("merge") or (7, 3, 1))

    stats, _ = rb._run_full(conn, [], [], False, 2, None, reconnect=lambda: conn)
    assert calls == []
    assert (stats["auto_merges"], stats["queued"], stats["suppressed"]) == (0, 0, 0)
    assert _params(conn, "UPDATE broker_resolution_runs SET ended_at")[:3] == (0, 0, 0)

    # ...and the control: with the switch on, the same sweep runs it
    conn2 = _ResilientConn("full")
    stats2, _ = rb._run_full(conn2, [], [], True, 2, None, reconnect=lambda: conn2)
    assert calls == ["merge"]
    assert (stats2["auto_merges"], stats2["queued"], stats2["suppressed"]) == (7, 3, 1)


def test_the_events_reason_records_the_evidence_path_per_group() -> None:
    """reason was the hardcoded 'contact_bridge' on every auto row, which now would
    be a lie half the time: a name_firm group has no contact behind it at all."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    conn.broker_of = {1: 10, 2: 20}
    rb._apply_merges(conn, [[1, 2]], group_reasons={(1, 2): rb.R.REASON_NAME_FIRM})
    assert _params(conn, "INSERT INTO broker_merge_events")["n"] == ["name_firm"]

    # a component chaining two groups carries EVERY path that contributed
    chained = _fresh_merge_conn({1: 10, 2: 30, 3: 30, 4: 20})
    rb._apply_merges(chained, [[1, 2], [3, 4]],
                     group_reasons={(1, 2): rb.R.REASON_CONTACT_NAME,
                                    (3, 4): rb.R.REASON_NAME_FIRM})
    assert set(_params(chained, "INSERT INTO broker_merge_events")["n"]) == {
        "contact_name+name_firm"}

    # a caller that passes no reasons gets the neutral fallback, not invented evidence
    plain = _fresh_merge_conn({1: 10, 2: 20})
    rb._apply_merges(plain, [[1, 2]])
    assert _params(plain, "INSERT INTO broker_merge_events")["n"] == ["auto_merge"]


def test_stale_cards_are_retired_the_moment_the_merges_land() -> None:
    """An auto-merged group leaves its old review card at 'proposed' with one
    surviving broker behind it: the UI renders it thin and the merge button can only
    answer 409. The sweep's end-of-run backstop is the same statement under a
    different actor, so the ledger says which pass closed a card."""
    import scripts.resolve_brokers as rb

    conn = _ResilientConn("merge")
    assert rb._retire_dead_candidates(conn, rb._AUTO_MERGE_RETIRE_ACTOR) == 0
    sql, params = next((sql, p) for sql, p in conn.executed_with_params
                       if "UPDATE broker_merge_candidates" in sql)
    assert params == {"by": "auto:sweep"}
    assert "status = 'proposed'" in sql
    assert "b.status = 'active'" in sql and ") < 2" in sql
    assert rb._SWEEP_RETIRE_ACTOR != rb._AUTO_MERGE_RETIRE_ACTOR


def test_shared_contacts_are_built_only_for_the_pairs_being_written() -> None:
    """The card's evidence is the contacts BOTH identities carry. Built per pair —
    an all-pairs index over the corpus is quadratic in every role inbox's carriers."""
    import scripts.resolve_brokers as rb

    contacts = [rb.R.Contact(1, "email", "a@x.cz"), rb.R.Contact(2, "email", "a@x.cz"),
                rb.R.Contact(2, "phone", "420600111222"),
                rb.R.Contact(3, "email", "b@x.cz")]
    assert rb._shared_contacts(contacts, [(2, 1)]) == {(1, 2): {"email:a@x.cz"}}
    assert rb._shared_contacts(contacts, [(1, 3)]) == {}   # nothing in common
    assert rb._shared_contacts(contacts, []) == {}
