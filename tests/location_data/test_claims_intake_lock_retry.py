"""A batch that loses a LOCK runs again; anything else still stamps the run `failed` (W12).

No database. Two `mode=full` walks died ~80 s in on 2026-09-14 (runs 34817669095,
34824922631) with `canceling statement due to lock timeout` — the intake's batches bump the
same `dirty_locations` rows the resolver's four concurrent drain slices hold, and a 5 s
ceiling behind one of those is an ordinary wait, not a fault. What these pin is that the
retry re-runs THE SAME batch (the keyset cursor has not moved, the write is idempotent), that
its counters are rolled back with the transaction, and that the two lock sqlstates are the
ONLY ones it swallows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from location_data import claims_intake, page_readers
from tests.location_data.claim_intake_fixtures import SREALITY_POST_CUTOVER, entries_for

BASE_TS = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)


def _record(listing_id: int) -> tuple[Any, ...]:
    """One scan row in the selection's column order, with no stored body."""
    return (listing_id, "sreality", f"n{listing_id}", dict(SREALITY_POST_CUTOVER), BASE_TS,
            None, False, None, None, None, 1, None)


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self._conn.dispatch(self, " ".join(sql.split()), params or {})

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result


class _Conn:
    """A keyset over `rows`, plus a claim write that raises whatever is queued for it."""

    def __init__(self, rows: list[int], fail_writes: list[BaseException] | None = None) -> None:
        self.rows = rows
        self.fail_writes = list(fail_writes or ())
        # The `after_id` EVERY scan was asked with — the whole point: a retried batch must
        # re-read the same window, never the next one.
        self.scans: list[int] = []
        self.writes = 0
        self.finished: list[dict[str, Any]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Cursor:
        return _Cursor(self)

    def dispatch(self, cur: _Cursor, sql: str, params: dict[str, Any]) -> None:
        cur._result = []
        if "set_config" in sql:
            return
        if "FROM portal_contracts WHERE source" in sql:
            cur._result = [(1, 1)]
            return
        if sql.startswith("INSERT INTO location_claim_batches"):
            cur._result = [(7,)]
            return
        if sql.startswith("UPDATE location_claim_batches"):
            self.finished.append(dict(params))
            return
        if "FROM listings l" in sql:
            self.scans.append(params["after_id"])
            rows = [r for r in self.rows if r > params["after_id"]]
            cur._result = [_record(r) for r in rows[:params["batch_size"]]]
            return
        if "INSERT INTO location_claims" in sql:
            self.writes += 1
            if self.fail_writes:
                raise self.fail_writes.pop(0)
            cur._result = [(len(params["rows"].obj), 1)]
            return
        raise AssertionError(f"unhandled SQL: {sql[:120]}")


@pytest.fixture(autouse=True)
def _stub_preconditions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claims_intake, "missing_relations", lambda conn: [])
    monkeypatch.setattr(
        claims_intake, "load_entries", lambda conn: {"sreality": entries_for("sreality")})
    monkeypatch.setattr(claims_intake, "_resume_point", lambda *a, **k: None)
    # Payload half only: no page-capable portal means no bodies pass and no R2.
    monkeypatch.setattr(page_readers, "page_entries", lambda entries, page_kind: [])


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The backoff, captured instead of slept — the retry is on the incident path."""
    recorded: list[float] = []
    monkeypatch.setattr(claims_intake.time, "sleep", recorded.append)
    return recorded


def _run(conn: _Conn, **over: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "mode": "full", "source": "sreality", "batch_size": 10, "max_seconds": None,
        "limit": None, "start_after_id": 0, "statement_timeout": 60, "dry_run": False,
        "note": None, "store": None,
    }
    kwargs.update(over)
    return claims_intake.run(conn, **kwargs)


def test_a_lost_lock_retries_the_same_batch_and_counts_it_once(
    sleeps: list[float],
) -> None:
    """One retry, the cursor unchanged, and the counters rolled back with the transaction.

    The batch had already added its three listings and their claims to `stats` before the
    write raised; keeping them would report work that was never committed."""
    clean = _run(_Conn([1, 2, 3]))
    conn = _Conn([1, 2, 3], [psycopg.errors.LockNotAvailable("lock timeout")])

    stats = _run(conn)

    assert conn.scans == [0, 0, 3], "the retry re-reads the SAME window, then the keyset ends"
    assert stats["lock_retries"] == 1
    assert clean["claims"] > 0 and clean["lock_retries"] == 0
    for counter in ("listings", "claims", "claims_payload", "claims_inserted", "enqueued"):
        assert stats[counter] == clean[counter], counter
    assert stats["outcome"] == "ok"
    assert stats["cursor_after_id"] == 3
    assert sleeps == [claims_intake.BATCH_RETRY_BACKOFF_S]


def test_a_deadlock_is_retried_too_and_the_backoff_doubles(sleeps: list[float]) -> None:
    conn = _Conn([1], [psycopg.errors.DeadlockDetected("deadlock detected"),
                       psycopg.errors.LockNotAvailable("lock timeout")])

    stats = _run(conn)

    assert stats["lock_retries"] == 2
    assert conn.scans == [0, 0, 0, 1]
    assert sleeps == [2.0, 4.0]


def test_five_consecutive_lock_failures_stamp_the_run_failed(sleeps: list[float]) -> None:
    """Never a silent skip: the run gives up on the batch by FAILING, so the workflow
    restarts and the cursor is still where the last committed batch left it."""
    conn = _Conn([1], [psycopg.errors.LockNotAvailable("lock timeout")] * 6)

    with pytest.raises(psycopg.errors.LockNotAvailable):
        _run(conn)

    assert conn.writes == claims_intake.MAX_CONSECUTIVE_LOCK_FAILURES
    assert conn.scans == [0] * claims_intake.MAX_CONSECUTIVE_LOCK_FAILURES
    assert conn.finished[-1]["outcome"] == "failed"
    assert "LockNotAvailable" in conn.finished[-1]["note"]
    # It sleeps BETWEEN attempts, not after the last one.
    assert sleeps == [2.0, 4.0, 8.0, 16.0]


@pytest.mark.parametrize("exc", [
    psycopg.errors.AdminShutdown("terminating connection"),   # 57P01
    psycopg.errors.ConnectionFailure("connection failure"),   # 08006
    psycopg.OperationalError("the socket closed"),            # no sqlstate at all
    psycopg.errors.QueryCanceled("statement timeout"),        # 57014
])
def test_everything_that_is_not_a_lock_is_still_fatal(
    exc: BaseException, sleeps: list[float],
) -> None:
    """A dead connection fails every batch the same way, so retrying it is a hot loop on a
    socket that is gone; the workflow restart is the recovery."""
    conn = _Conn([1], [exc])

    with pytest.raises(type(exc)):
        _run(conn)

    assert conn.writes == 1
    assert sleeps == []
    assert conn.finished[-1]["outcome"] == "failed"


def test_the_backoff_never_sleeps_past_the_budget(sleeps: list[float]) -> None:
    """A batch asleep through the budget is a run that stamps nothing."""
    conn = _Conn([1], [psycopg.errors.LockNotAvailable("lock timeout")])

    _run(conn, max_seconds=0.5)

    assert sleeps and sleeps[0] <= 0.5


# ---------------------------------------------------------------- the lock ceiling

def test_the_lock_timeout_is_20s_and_env_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5 s was the number two full walks died on: a queue-row bump waiting behind one of the
    drain's four concurrent slices is normal, not a fault."""
    conn = _Conn([])
    monkeypatch.delenv(claims_intake.LOCK_TIMEOUT_ENV, raising=False)
    assert claims_intake.DEFAULT_LOCK_TIMEOUT_S == 20

    def _timeouts() -> dict[str, Any]:
        seen: dict[str, Any] = {}
        cur = _Cursor(conn)
        monkeypatch.setattr(conn, "dispatch",
                            lambda c, sql, params: seen.update(params), raising=False)
        with claims_intake.guarded(conn, 600):
            pass
        del cur
        return seen

    assert _timeouts()["lock_timeout"] == "20s"
    monkeypatch.setenv(claims_intake.LOCK_TIMEOUT_ENV, "45")
    assert _timeouts()["lock_timeout"] == "45s"
    # A typo must not take the lane down, nor mean "wait for ever".
    monkeypatch.setenv(claims_intake.LOCK_TIMEOUT_ENV, "none")
    assert _timeouts()["lock_timeout"] == "20s"


# ---------------------------------------------------------------- the yield read

def test_running_batch_id_is_lane_scoped_and_ignores_a_stale_stamp() -> None:
    """The read the fast schedule yields on: the HOURLY lane's still-running batch, and only
    while its stamp is young enough to belong to a live run."""
    assert claims_intake.RUNNING_BATCH_MAX_AGE_MINUTES == 65
    sql = " ".join(claims_intake._RUNNING_BATCH_SQL.split())
    assert "lane = %(lane)s" in sql
    assert "outcome = 'running'" in sql
    assert "started_at > now() - (%(max_age_minutes)s * interval '1 minute')" in sql

    seen: dict[str, Any] = {}

    class _Yield(_Conn):
        def dispatch(self, cur: _Cursor, sql: str, params: dict[str, Any]) -> None:
            cur._result = []
            if "set_config" in sql:
                return
            seen.update(params)
            cur._result = [(1318,)] if self.rows else []

    assert claims_intake.running_batch_id(_Yield([1])) == 1318
    assert seen == {"lane": claims_intake.LANE, "max_age_minutes": 65}
    assert claims_intake.running_batch_id(_Yield([])) is None
    # Never its own lane: the fast schedule would then yield to itself for ever.
    assert claims_intake.LANE != claims_intake.FAST_LANE
