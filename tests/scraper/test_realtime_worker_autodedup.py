"""The realtime worker's `autodedup` lane: THE engine's one path, run from the always-on worker
(E914).

Two halves. The lane's own contract — one integer, `app_settings.realtime_autodedup_interval_seconds`,
as cadence and kill switch (seeded 0), fail-safe when the settings read raises, a refusal
recorded rather than raised, heartbeat counters on every path — is pinned against stubs. And the
pass itself is driven END TO END through `autodedup.incremental_lane.run_incremental` over the
engine's own Postgres stand-in (`tests/autodedup/fake_pg.py`, which raises on any statement it
does not know), seeded from its `public` the way production is (A10): so "the engine bounds its
own time" and "a closed scope writes only schema autodedup" are measured, not asserted by
inspection. No network, no DB.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import threading
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from autodedup import incremental_lane
from autodedup.incremental_lane import (
    LANE_NAME,
    LEASE_TTL_S,
    PASS_BUDGET_S,
    PASS_DEADLINE_S,
    STATEMENT_TIMEOUT_MS,
    run_incremental,
)
from autodedup.incremental_sql import RT_STORE_PRESENT_SQL
from scraper import realtime_worker as rw
from scripts.verify_pipeline import DEFAULT_THRESHOLDS
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.lane_world import seed_lane
from tests.autodedup.lane_world import world as lane_world

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / (
    "557_realtime_autodedup_lane_settings.sql")


@pytest.fixture(autouse=True)
def _fresh_lane_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-process guards reset per test."""
    monkeypatch.setattr(rw, "_AUTODEDUP_PASS_LOCK", threading.Lock())
    monkeypatch.setattr(rw, "_AUTODEDUP_WEDGE_LOGGED", False)
    monkeypatch.setattr(rw, "_AUTODEDUP_STORE_WARNED", False)
    monkeypatch.setattr(rw, "_AUTODEDUP_LAST_OUTCOME", None)


@pytest.fixture()
def world() -> FakePg:
    return lane_world()


def _settings(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    """`app_settings` as a dict: an absent key reads as the row being absent."""
    monkeypatch.setattr(rw, "_read_setting", lambda key: values.get(key))


class _Conn:
    """A connection whose only statement is the store-presence probe."""

    def __init__(self, present: Any = True) -> None:
        self.present = present
        self.closed = False
        self.executed: list[str] = []

    def cursor(self) -> "_Cur":
        return _Cur(self)

    def close(self) -> None:
        self.closed = True


class _Cur:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.executed.append(sql)

    def fetchall(self) -> list[tuple]:
        return [(self.conn.present,)]


def _stub_engine(monkeypatch: pytest.MonkeyPatch, result: Any) -> dict[str, Any]:
    """Replace the engine's pass; capture what the lane handed it."""
    seen: dict[str, Any] = {}

    def fake(conn_factory: Any, **kwargs: Any) -> Any:
        seen.update(kwargs=dict(kwargs), conn=conn_factory())
        seen["calls"] = seen.get("calls", 0) + 1
        if callable(result) and not isinstance(result, BaseException):
            outcome = result(seen["calls"])
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(incremental_lane, "run_incremental", fake)
    return seen


def _summary(**over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "counts": {"claimed": 12, "pairs_scored": 40, "clusters_written": 3, "held": 1,
                   "retired": 0},
        "aborted": "",
        "latency_s": {"n": 12, "p50": 310.2, "p95": 355.0},
        "claim_bound": {"bound_by": "count", "limit": 100},
        "reconcile": {"counts": {"applied": 2}},
    }
    out.update(over)
    return out


# ------------------------------------------------------------------ registration + dark gate


def test_the_lane_is_registered_dark_and_fail_safe() -> None:
    # default_interval=0: _lane_loop keeps it when the settings read RAISES, and the interval
    # is the kill switch — a pooler blip must not run a pass of a lane left dark.
    lane = inspect.getsource(rw._amain).split('("autodedup"', 1)[1].split(")),", 1)[0]
    assert "_read_autodedup_interval" in lane
    assert "default_interval=0" in lane


def test_the_interval_is_the_only_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """One integer is cadence AND kill switch, the worker's convention: an absent row reads as
    0, and no other row — the deleted `realtime_autodedup_enabled` included — can open it."""
    read: list[str] = []

    def setting(values: dict[str, Any]) -> Any:
        def fake(key: str) -> Any:
            read.append(key)
            return values.get(key)
        return fake

    for values, expected in (({}, 0), ({"realtime_autodedup_interval_seconds": 0}, 0),
                             ({"realtime_autodedup_interval_seconds": 60}, 60),
                             ({"realtime_autodedup_interval_seconds": "junk"}, 0),
                             ({"realtime_autodedup_enabled": True}, 0)):
        monkeypatch.setattr(rw, "_read_setting", setting(values))
        assert rw._read_autodedup_interval() == expected, values
    assert set(read) == {rw.AUTODEDUP_INTERVAL_SETTING}


def test_a_dark_lane_opens_no_connection_and_runs_no_pass(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Provably inert: the real reader, the real loop, the seeded 0."""
    _settings(monkeypatch, realtime_autodedup_interval_seconds=0)
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: pytest.fail(
        "a dark lane must not open a connection"))
    monkeypatch.setattr(rw, "_autodedup_sync", lambda: pytest.fail(
        "a dark lane must not run a pass"))
    state = rw._new_state()

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(rw._lane_loop(
            "autodedup", stop, rw._read_autodedup_interval,
            lambda: rw._autodedup_pass(stop, state), state,
            default_interval=0, idle_seconds=0.01))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(scenario())
    assert "autodedup" not in state["lanes"]


def test_the_seeded_setting_ships_the_lane_dark() -> None:
    """ONE row, so /settings can set it (PUT 404s on an absent key); seeded 0 = stopped, and
    an operator's value is never overwritten."""
    sql = MIGRATION.read_text(encoding="utf-8")
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert f"'{rw.AUTODEDUP_INTERVAL_SETTING}',\n  '0'::jsonb" in body
    assert re.findall(r"'realtime_\w+'", body) == [f"'{rw.AUTODEDUP_INTERVAL_SETTING}'"]
    assert "on conflict (key) do nothing" in body
    assert not re.search(r"\b(create|alter|drop|delete|update|truncate)\b", body, re.I)


def test_the_settings_row_says_the_lane_merges() -> None:
    """W5 (E911): /settings is where the lane is turned on, and 557's description still said it
    "never merges anything". 568 rewrites the DESCRIPTION only — never an operator's value, and
    it says the interval stays 0 until the W5 seed and the gates (review A2)."""
    sql = (MIGRATION.parent / "568_autodedup_one_lane_interval_description.sql").read_text(
        encoding="utf-8")
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert "set description =" in body and "MERGES" in body
    assert "sensible running value" not in body and "G1-G3" in body and "rt_seed" in body
    assert f"where key = '{rw.AUTODEDUP_INTERVAL_SETTING}'" in body
    assert not re.search(r"\bset\s+value\b|,\s*value\s*=", body, re.I), "the value is untouched"
    assert not re.search(r"\b(create|alter|drop|delete|insert|truncate)\b", body, re.I)


# ------------------------------------------------------------------------------ the bounds


def test_the_engines_deadline_sits_inside_every_bound_around_it() -> None:
    """E913: the engine bounds its own time. Its deadline plus ONE statement at its
    statement_timeout must end before the stall monitor warns, before the lane abandons the
    pass, and before the lease expires — or a seed could take the lease mid-write."""
    worst = PASS_DEADLINE_S + STATEMENT_TIMEOUT_MS / 1000.0
    assert worst < DEFAULT_THRESHOLDS["worker_lane_stall_warn_seconds"]
    assert worst < rw.LANE_PASS_TIMEOUT_SECONDS
    assert worst < LEASE_TTL_S
    assert PASS_BUDGET_S * 2 <= PASS_DEADLINE_S, "claims are sized well inside the deadline"


def test_the_lane_hands_the_engine_its_connection_and_nothing_else(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """No argument, no wrapper: the pass runs under the scope and scorer its generation was
    seeded with, and under the engine's own deadline (E913, E914)."""
    conn = _Conn()
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: conn)
    _settings(monkeypatch)
    seen = _stub_engine(monkeypatch, _summary())

    last = rw._autodedup_sync()

    assert seen["kwargs"] == {} and seen["conn"] is conn
    assert conn.executed == [RT_STORE_PRESENT_SQL]
    assert conn.closed
    assert last == {
        "ran": True, "claimed": 12, "scored": 40, "grouped": 3, "merged": 2, "skipped": 0,
        "errors": 0, "seconds": last["seconds"], "held": 1, "retired": 0, "reconcile": "ran",
        "deadline_exceeded": False, "latency_p50_s": 310.2, "latency_p95_s": 355.0,
        "bound_by": "count",
    }
    for gone in ("AUTODEDUP_PASS_DEADLINE_SECONDS", "AUTODEDUP_PASS_BUDGET_SECONDS",
                 "_AUTODEDUP_BACKOFF", "_DeadlineConnection", "_AutodedupDeadline"):
        assert not hasattr(rw, gone), gone


# --------------------------------------------------------------- every path is a heartbeat


def test_an_absent_store_skips_every_tick_with_one_warning(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    conn = _Conn(present=None)  # to_regclass answers NULL, never an error, on a missing schema
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: conn)
    _settings(monkeypatch)
    monkeypatch.setattr(incremental_lane, "run_incremental", lambda *a, **k: pytest.fail(
        "no pass without the store"))

    with caplog.at_level(logging.WARNING, logger="scraper.realtime_worker"):
        first = rw._autodedup_sync()
        second = rw._autodedup_sync()

    assert first["skipped"] == 1 and first["reason"] == "store_absent"
    assert first["errors"] == 0 and first["ran"] is False
    assert second["reason"] == "store_absent"
    assert conn.closed
    assert sum("store (migrations 539/540) is absent" in r.message
               for r in caplog.records) == 1


@pytest.mark.parametrize("reason", ["leased", "unseeded"])
def test_the_engines_green_skips_are_skips_not_errors(
        monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: _Conn())
    _settings(monkeypatch)
    _stub_engine(monkeypatch, {"skipped": reason, "reason": "why", "spent_usd": 0.0})

    last = rw._autodedup_sync()

    assert (last["ran"], last["skipped"], last["errors"]) == (False, 1, 0)
    assert last["reason"] == reason and last["detail"] == "why"


def test_a_refusal_is_recorded_never_raised(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The engine refuses by raising SystemExit. Carried out of asyncio.to_thread it would
    stop the event loop — every lane of the worker — so the lane records it instead, and
    counts it where a lane's raised passes are counted."""
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: _Conn())
    _settings(monkeypatch)
    _stub_engine(monkeypatch, SystemExit("STORAGE: over the budget " + "x" * 1000))
    state = rw._new_state()

    with caplog.at_level(logging.WARNING, logger="scraper.realtime_worker"):
        asyncio.run(rw._autodedup_pass(asyncio.Event(), state))
        asyncio.run(rw._autodedup_pass(asyncio.Event(), state))

    lane = state["lanes"]["autodedup"]
    assert lane["failed_passes"] == 2 and lane["last_failure_at"]
    assert lane.get("passes", 0) == 0, "a refused pass is not a completed one"
    assert lane["started_at"] is None
    assert lane["last"]["errors"] == 1 and lane["last"]["ran"] is False
    assert lane["last"]["refused"].startswith("STORAGE")
    assert len(lane["last"]["refused"]) == rw.AUTODEDUP_REASON_CHARS
    # Said on the transition, not once a minute for as long as the refusal stands.
    assert sum("AUTODEDUP lane pass stopped" in r.message for r in caplog.records) == 1
    Jsonb(rw._lane_snapshot(state["lanes"]))


def test_a_completed_pass_after_a_refusal_keeps_the_failure_count(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: _Conn())
    _settings(monkeypatch)
    _stub_engine(monkeypatch, lambda n: SystemExit("STORAGE") if n == 1 else _summary())
    state = rw._new_state()

    asyncio.run(rw._autodedup_pass(asyncio.Event(), state))
    asyncio.run(rw._autodedup_pass(asyncio.Event(), state))

    lane = state["lanes"]["autodedup"]
    assert lane["passes"] == 1 and lane["failed_passes"] == 1
    assert lane["last"]["ran"] is True and lane["last"]["errors"] == 0


def test_an_aborted_pass_ran_but_counts_as_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A neighbourhood whose pair set fits not even a one-listing claim: nothing was written,
    # and the engine calls that a block worth an operator's eye.
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: _Conn())
    _settings(monkeypatch)
    _stub_engine(monkeypatch, _summary(aborted="max_pairs: wanted 180000 > 150000"))

    last = rw._autodedup_sync()

    assert last["ran"] is True and last["errors"] == 1
    assert last["aborted"].startswith("max_pairs")


def test_any_other_failure_is_the_lanes_failed_pass_and_closes_the_connection(
        monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _Conn()
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: conn)
    _settings(monkeypatch)
    _stub_engine(monkeypatch, RuntimeError("pooler went away"))

    with pytest.raises(RuntimeError):
        rw._autodedup_sync()
    assert conn.closed
    assert rw._AUTODEDUP_PASS_LOCK.acquire(blocking=False), "the lock was not released"
    rw._AUTODEDUP_PASS_LOCK.release()


def test_an_abandoned_pass_is_never_overlapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: pytest.fail(
        "a second pass must not open a connection while the first still holds the lock"))
    assert rw._AUTODEDUP_PASS_LOCK.acquire(blocking=False)
    try:
        last = rw._autodedup_sync()
    finally:
        rw._AUTODEDUP_PASS_LOCK.release()
    assert last["skipped"] == 1 and last["reason"] == "previous_pass_running"


def test_a_stopping_worker_opens_no_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rw, "_autodedup_sync", lambda: pytest.fail("no pass while stopping"))
    stop = asyncio.Event()
    stop.set()
    state = rw._new_state()
    asyncio.run(rw._autodedup_pass(stop, state))
    assert "autodedup" not in state["lanes"]


def test_the_worker_merges_only_through_the_engine() -> None:
    """The lane's merges are the engine's reconcile (A9), which merges through THE chokepoint;
    the worker itself never names it."""
    src = "".join(inspect.getsource(fn) for fn in (
        rw._autodedup_sync, rw._autodedup_pass, rw._autodedup_outcome))
    for forbidden in ("property_identity", "merge_properties", "operator_state"):
        assert forbidden not in src


# ------------------------------------------------------------- the engine, driven for real


def _worker_on(monkeypatch: pytest.MonkeyPatch, conn: FakePg, **values: Any) -> None:
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: conn)
    _settings(monkeypatch, **values)


_WRITE_TARGET = re.compile(r"\b(?:insert\s+into|update|delete\s+from)\s+([\w.]+)", re.I)


def _write_targets(statements: list[str]) -> set[str]:
    """Every table a statement writes, CTE writes included: `update ... set` in a
    select-list never matches, because a real UPDATE target is followed by `set`."""
    targets: set[str] = set()
    for sql in statements:
        for match in _WRITE_TARGET.finditer(sql):
            rest = sql[match.end():].lstrip().lower()
            if match.group(0).lower().startswith("update") and not (
                    rest.startswith("set") or re.match(r"\w+\s+set\b", rest)):
                continue
            targets.add(match.group(1).lower())
    return targets


def test_one_worker_pass_is_the_engines_pass(world, tmp_path, monkeypatch) -> None:
    """The worker's interval alone opens a pass, and the pass is the engine's: it claims,
    decides, moves its cursors under the lease, frees the lease, and — during the build and
    with the scope row closed — every row it writes is in schema autodedup."""
    seed_lane(world, tmp_path)
    _worker_on(monkeypatch, world)
    for expected in ("bootstrap", "scope_closed"):
        before = len(world.statements)

        last = rw._autodedup_sync()

        assert last["ran"] is True and last["errors"] == 0 and last["skipped"] == 0, last
        assert last["reconcile"] == expected and last["merged"] == 0
        assert world.cursors, "the pass moved the engine's own watermark"
        assert world.lease[LANE_NAME]["expires_at"] <= world.now, "the lease was released"
        targets = _write_targets(world.statements[before:])
        assert targets, "the pass wrote nothing"
        assert all(t.startswith("autodedup.") for t in targets), sorted(targets)
        assert world.statements_in_tx, "the pass ran as the engine's one transaction"


def test_a_worker_pass_inside_a_seed_is_a_green_skip(world, tmp_path, monkeypatch) -> None:
    """The seed holds the lane's own lease through its transaction: a worker pass that fires
    mid-seed skips, and writes nothing into the generation the seed is emptying."""
    seed_lane(world, tmp_path / "first")
    _worker_on(monkeypatch, world)
    during: list[dict[str, Any]] = []
    original = incremental_lane.cut_calibration

    def pass_mid_seed(conn: Any, *args: Any, **kwargs: Any) -> Any:
        during.append(rw._autodedup_sync())
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(incremental_lane, "cut_calibration", pass_mid_seed)
    out = seed_lane(world, tmp_path / "again", fresh="true")

    assert during and (during[0]["skipped"], during[0]["reason"]) == (1, "leased")
    assert out["reset"]["rt_lease"] == 0
    assert world.lease[LANE_NAME]["expires_at"] <= world.now, "the seed freed the lease"
    after = rw._autodedup_sync()
    assert after["ran"] is True and after["errors"] == 0, after


def test_another_writer_holding_the_lease_is_a_green_skip(world, tmp_path, monkeypatch) -> None:
    """One lease row, by name: while a seed or a dispatched apply/unapply holds it, the worker
    is a green skip."""
    from datetime import timedelta

    seed_lane(world, tmp_path)
    world.lease[LANE_NAME] = {"holder": "gh-apply:1:1",
                              "expires_at": world.now + timedelta(minutes=10)}
    cursors = {k: dict(v) for k, v in world.cursors.items()}
    _worker_on(monkeypatch, world)

    last = rw._autodedup_sync()

    assert (last["skipped"], last["reason"], last["errors"]) == (1, "leased", 0)
    assert world.cursors == cursors
    assert world.lease[LANE_NAME]["holder"] == "gh-apply:1:1"


def test_a_pass_past_its_own_deadline_rolls_back_and_halves_its_rate(
        world, tmp_path, monkeypatch) -> None:
    """E913: the engine stops itself between steps; its one transaction rolls back (nothing
    written, no cursor moved), the lease is freed and the next claim is half the size. To the
    worker it is a pass that ran and counts as an error, never a crash."""
    seed_lane(world, tmp_path)
    cursors = {k: dict(v) for k, v in world.cursors.items()}
    rolled_back = world.rolled_back
    monkeypatch.setattr(incremental_lane, "PASS_DEADLINE_S", -1.0)
    _worker_on(monkeypatch, world)

    last = rw._autodedup_sync()

    assert last["ran"] is True and last["errors"] == 1 and last["deadline_exceeded"] is True
    assert last["aborted"] == "deadline"
    assert world.rolled_back == rolled_back + 1
    assert world.cursors == cursors and not world.pairs and not world.rt_fp
    rate = world.settings[incremental_lane.pass_rate_key(incremental_lane.GENERATION)]
    assert rate == pytest.approx(incremental_lane.PASS_RATE_PER_S / 2)
    assert world.lease[LANE_NAME]["expires_at"] <= world.now
    assert last["reconcile"] == "pass_deadline"


def test_a_claim_the_budget_cut_still_measures_the_rate(world, tmp_path) -> None:
    """A rate halved to a one-listing claim must be measurable again, or the lane would claim
    one listing a pass for ever: a claim the time budget cut is a measurement at any size."""
    seed_lane(world, tmp_path)
    key = incremental_lane.pass_rate_key(incremental_lane.GENERATION)
    world.settings[key] = 0.0001

    out = run_incremental(lambda: world)

    assert out["claim_bound"]["bound_by"] == "time" and out["counts"]["claimed"] == 1
    assert world.settings[key] > 0.0001
