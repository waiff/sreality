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
import gc
import inspect
import io
import json
import logging
import os
import re
import socket
import sys
import threading
import weakref
from datetime import timedelta
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
    monkeypatch.setattr(rw, "_AUTODEDUP_PASS_LOCK", rw._PassLock("autodedup"))
    monkeypatch.setattr(rw, "_AUTODEDUP_STORE_WARNED", False)
    monkeypatch.setattr(rw, "_AUTODEDUP_LAST_OUTCOME", None)
    monkeypatch.setattr(rw, "_AUTODEDUP_HOLDER", None)
    monkeypatch.setattr(rw, "_AUTODEDUP_CONN", None)
    monkeypatch.setattr(rw, "_AUTODEDUP_STOPPING", threading.Event())


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
        "peak_rss_mb": 412.5,
        "rss_mb": 388.0,
        "memo_entries": {"text_facts": 812, "body_align": 64, "shingles": 30, "tokens": 2048},
        "claim_cap": {"cap": 500, "reason": "rt_seed", "limit_mb": 8192.0},
        "predecessor_released": None,
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
    """No switch, no wrapper: the pass runs under the scope and scorer its generation was
    seeded with, and under the engine's own deadline (E913, E914). Handed in beside the
    connection: a factory for a FRESH, bounded connect (E930: the halving a RAISED pass writes
    may not have a live connection of its own), E941's two — the holder the pass takes its
    lease under, named by the worker so its shutdown can release that row, and the shutdown
    itself as `stopping` — E948's boot second, which names a dead predecessor's lease, and the
    container's memory limit, read for this pass, which resets a claim cap halved under a
    smaller one (E948b)."""
    conn = _Conn()
    connects: list[dict[str, Any]] = []
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: connects.append(dict(k)) or conn)
    monkeypatch.setattr(rw, "_memory_limit_mb", lambda *_a, **_k: 8192.0)
    readings = iter([290.0, 301.5])
    monkeypatch.setattr(rw, "_return_memory", lambda: next(readings))
    _settings(monkeypatch)
    seen = _stub_engine(monkeypatch, _summary())

    last = rw._autodedup_sync()

    assert set(seen["kwargs"]) == {"fresh_conn", "holder", "stopping", "booted_epoch",
                                   "memory_limit_mb"}
    assert seen["conn"] is conn
    assert isinstance(seen["kwargs"]["holder"], str) and seen["kwargs"]["holder"]
    assert seen["kwargs"]["stopping"] == rw._AUTODEDUP_STOPPING.is_set
    assert seen["kwargs"]["booted_epoch"] == rw._BOOTED_EPOCH
    assert seen["kwargs"]["memory_limit_mb"] == 8192.0
    assert int(seen["kwargs"]["holder"].rsplit(":", 1)[1]) >= rw._BOOTED_EPOCH, (
        "this process's own holder is never older than its boot")
    assert rw._AUTODEDUP_HOLDER is None and rw._AUTODEDUP_CONN is None, "cleared after"
    seen["kwargs"]["fresh_conn"]()
    assert connects == [{}, {"attempts": 1,
                             "connect_timeout": rw.AUTODEDUP_RESCUE_CONNECT_TIMEOUT_SECONDS}]
    assert conn.executed == [RT_STORE_PRESENT_SQL]
    assert conn.closed
    assert last == {
        "ran": True, "claimed": 12, "scored": 40, "grouped": 3, "merged": 2, "skipped": 0,
        "errors": 0, "seconds": last["seconds"], "held": 1, "retired": 0, "reconcile": "ran",
        "deadline_exceeded": False, "latency_p50_s": 310.2, "latency_p95_s": 355.0,
        "bound_by": "count", "reconcile_groups": 0, "reconcile_seconds": None,
        "reconcile_refused": 0, "reconcile_failed": 0, "reconcile_waiting": 0,
        "reconcile_skipped_at_apply": 0, "reconcile_quarantined": 0, "reconcile_deferred": 0,
        "must_link_dissolved": 0,
        # the plan's silent outcomes (2026-10-07): settled groups and standing refusals by reason
        "reconcile_settled": 0, "reconcile_skipped_by_reason": {},
        # E941: the process's peak memory at the pass's end, carried from the engine's summary
        "peak_rss_mb": 412.5,
        # E948: what it holds now, the container's limit (read for this pass, E948b), the
        # claim cap (and, E948b, the limit it records), and the lease of a dead predecessor it
        # released (none)
        "rss_mb": 388.0, "memory_limit_mb": 8192.0,
        "claim_cap": {"cap": 500, "reason": "rt_seed", "limit_mb": 8192.0},
        "predecessor_released": None,
        # E949: the engine's body and token memos at the pass's end, and the RSS once freed
        # memory was handed back, before the pass began and as it ended
        "memo_entries": {"text_facts": 812, "body_align": 64, "shingles": 30, "tokens": 2048},
        "rss_at_start_mb": 290.0, "rss_after_trim_mb": 301.5,
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
    assert rw._AUTODEDUP_PASS_LOCK.try_enter(), "the lock was not released"
    rw._AUTODEDUP_PASS_LOCK.release()


def test_an_abandoned_pass_is_never_overlapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """E949: the skip hands nothing back and reads no RSS; the pass still running on its own
    thread does both."""
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: pytest.fail(
        "a second pass must not open a connection while the first still holds the lock"))
    order = _hand_back(monkeypatch)
    assert rw._AUTODEDUP_PASS_LOCK.try_enter()
    try:
        last = rw._autodedup_sync()
    finally:
        rw._AUTODEDUP_PASS_LOCK.release()
    assert last["skipped"] == 1 and last["reason"] == "previous_pass_running"
    assert order == [] and not {"rss_at_start_mb", "rss_after_trim_mb"} & set(last)


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
        rw._autodedup_sync, rw._autodedup_run, rw._return_memory, rw._autodedup_pass,
        rw._autodedup_outcome))
    for forbidden in ("property_identity", "property_carriers", "_merge_pair"):
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
        assert last["peak_rss_mb"] > 0, "the engine measured the process's memory (E941)"
        if sys.platform.startswith("linux"):
            assert last["rss_mb"] > 0, "and what it holds now (E948)"
        assert last["memory_limit_mb"] == rw._memory_limit_mb()
        assert last["predecessor_released"] is None and last["claim_cap"]["cap"] == 500
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



def test_the_heartbeat_says_what_the_reconcile_did_and_did_not_do(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Review A12/B3: the groups the reconcile read and its seconds (G2 reads the rate with
    them in), what the chokepoint or an error refused, what waits on an unread block — and
    why it did not run at all, logged once on the change."""
    conn = _Conn()
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: conn)
    _settings(monkeypatch)
    _stub_engine(monkeypatch, _summary(reconcile={
        "seconds": 4.2, "counts": {"groups": 9, "applied": 3, "refused": 1, "failed": 2,
                                   "block_not_fully_read": 4, "skipped_at_apply": 1,
                                   "quarantined": 1, "deferred_run_cap": 5}}))
    last = rw._autodedup_sync()
    assert (last["reconcile_groups"], last["reconcile_seconds"], last["merged"]) == (9, 4.2, 3)
    assert (last["reconcile_refused"], last["reconcile_failed"], last["reconcile_waiting"],
            last["reconcile_skipped_at_apply"], last["reconcile_quarantined"],
            last["reconcile_deferred"]) == (1, 2, 4, 1, 1, 5)

    _stub_engine(monkeypatch, _summary(reconcile={
        "skipped": incremental_lane.SEED_MISMATCH,
        "reason": "autodedup.settings rt_seed_version:rt is None: re-seed"}))
    monkeypatch.setattr(rw, "_AUTODEDUP_LAST_OUTCOME", None)
    with caplog.at_level(logging.INFO, logger=rw.LOG.name):
        last = rw._autodedup_sync()
        rw._autodedup_note(last)
    assert last["reconcile"] == "seed_version" and "re-seed" in last["reconcile_reason"]
    assert any("reconcile: seed_version" in r.getMessage() for r in caplog.records)


def test_the_pass_reads_peak_memory_in_mib_from_getrusage(monkeypatch: pytest.MonkeyPatch) -> None:
    """E941: ru_maxrss is KiB on Linux (bytes on macOS); the summary and the heartbeat carry
    MiB, and the stage gates read it against half the container's limit."""
    from types import SimpleNamespace

    asked: list[int] = []

    def getrusage(who: int) -> SimpleNamespace:
        asked.append(who)
        return SimpleNamespace(ru_maxrss=1_572_864)

    fake = SimpleNamespace(RUSAGE_SELF=0, getrusage=getrusage)
    monkeypatch.setattr(incremental_lane, "resource", fake)
    monkeypatch.setattr(incremental_lane.sys, "platform", "linux")
    assert incremental_lane.peak_rss_mb() == 1536.0 and asked == [0]
    monkeypatch.setattr(incremental_lane.sys, "platform", "darwin")
    assert incremental_lane.peak_rss_mb() == 1.5
    monkeypatch.setattr(incremental_lane, "resource", None)
    assert incremental_lane.peak_rss_mb() is None


# ------------------------------------------- E948: a dead predecessor, and the memory beside it


def _stranded_by_my_predecessor(world: FakePg) -> str:
    """The 10-10 row: a live lease of this hostname and pid, from a second before this boot."""
    dead = f"{socket.gethostname()}:{os.getpid()}:{rw._BOOTED_EPOCH - 106}"
    world.lease[LANE_NAME] = {"holder": dead, "expires_at": world.now + timedelta(minutes=38)}
    return dead


def _released_lines(caplog: pytest.LogCaptureFixture, dead: str) -> int:
    return sum(f"a predecessor of this container died holding autodedup.rt_lease: {dead}; "
               "released" in record.getMessage() for record in caplog.records)


def test_a_restart_in_place_frees_the_lane_its_dead_predecessor_stranded(
        world, tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    """2026-10-10: the process died holding the lease and Railway restarted it in place, so
    every pass of the new process skipped "leased" until the TTL. Now the first pass releases
    that lease, says so once, claims under half the cap, and the lane is its own again."""
    seed_lane(world, tmp_path)
    dead = _stranded_by_my_predecessor(world)
    _worker_on(monkeypatch, world)

    with caplog.at_level(logging.WARNING):
        last = rw._autodedup_sync()
        again = rw._autodedup_sync()

    assert (last["ran"], last["skipped"], last["errors"]) == (True, 0, 0), last
    assert last["predecessor_released"] == dead
    assert last["claim_cap"]["cap"] == 250 and dead in last["claim_cap"]["reason"]
    assert again["predecessor_released"] is None and again["claim_cap"]["cap"] == 250
    assert _released_lines(caplog, dead) == 1


def test_a_pass_that_refuses_after_the_release_still_logs_it(
        world, tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    """A refusal reaches the worker as SystemExit, whose text is all the heartbeat keeps: the
    release is logged by the engine when it happens, so it is on record on this path too."""
    seed_lane(world, tmp_path)
    dead = _stranded_by_my_predecessor(world)
    _worker_on(monkeypatch, world)

    def refuses(*_args: Any, **_kwargs: Any) -> Any:
        raise incremental_lane.RetireRefusal("the drift sweep would retire too much")

    monkeypatch.setattr(incremental_lane, "run_pass_bounded", refuses)
    with caplog.at_level(logging.WARNING):
        last = rw._autodedup_sync()

    assert (last["errors"], last["refused"]) == (1, "the drift sweep would retire too much")
    assert _released_lines(caplog, dead) == 1
    assert world.lease[LANE_NAME]["expires_at"] <= world.now, "and the pass gave its own back"


@pytest.mark.parametrize("readings", [[22_888.2, 22_888.2], [7_629.4, 22_888.2, 22_888.2]])
def test_a_bigger_container_resets_the_claim_cap_and_the_heartbeat_says_why(
        world, tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture,
        readings: list[float]) -> None:
    """E948b: the worker reads its container's limit every pass and hands it to the engine.
    2026-10-10's row, as E948 left it at the floor under the 8 GB container (no limit), meets
    the 24 GB one, with or without a pass under 8 GB first and with no restart in between: the
    first pass under 24 GB claims under the whole `max_listings` again, the heartbeat's
    `claim_cap` says why, and the line is logged once."""
    seed_lane(world, tmp_path)
    key = incremental_lane.claim_cap_key("rt")
    left_by_e948 = {"cap": 25,
                    "reason": "e320caaea376:1:1791637542 died before 2026-10-10T13:08:24Z"}
    world.settings[key] = dict(left_by_e948)
    _worker_on(monkeypatch, world)
    pending = list(readings)
    monkeypatch.setattr(rw, "_memory_limit_mb", lambda *_a, **_k: pending.pop(0))

    with caplog.at_level(logging.WARNING):
        beats = [rw._autodedup_sync() for _ in readings]

    *before, last, again = beats
    for beat in before:
        assert beat["memory_limit_mb"] == 7_629.4
        assert beat["claim_cap"] == {**left_by_e948, "limit_mb": 7_629.4}, "stamped, kept"
    assert (last["ran"], last["errors"], last["memory_limit_mb"]) == (True, 0, 22_888.2), last
    assert (last["claim_cap"]["cap"], last["claim_cap"]["limit_mb"]) == (500, 22_888.2)
    assert last["claim_cap"]["reason"].startswith("memory limit grew 7,629 → 22,888 MB at ")
    assert again["claim_cap"] == last["claim_cap"] == world.settings[key]
    assert sum("the claim cap reset from 25 to 500 (E948b)" in record.getMessage()
               for record in caplog.records) == 1


def test_a_skip_names_the_predecessor_it_released_or_left_to_its_ttl(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: _Conn())
    _settings(monkeypatch)
    _stub_engine(monkeypatch, {"skipped": "leased", "reason": "a seed took it",
                               "predecessor_released": "host:1:1", "predecessor_kept": None,
                               "spent_usd": 0.0})

    last = rw._autodedup_sync()

    assert (last["skipped"], last["reason"], last["predecessor_released"]) == (
        1, "leased", "host:1:1")
    assert "predecessor_kept" not in last

    _stub_engine(monkeypatch, {"skipped": "leased", "reason": "at the floor",
                               "predecessor_released": None, "predecessor_kept": "host:1:2",
                               "spent_usd": 0.0})
    last = rw._autodedup_sync()

    assert (last["detail"], last["predecessor_kept"]) == ("at the floor", "host:1:2")
    assert "predecessor_released" not in last


def test_the_containers_memory_limit_is_read_from_its_cgroup(tmp_path: Path) -> None:
    """E948 (S0's owed reading): cgroup v2 `memory.max`, else v1's file; unlimited or unread is
    None, never a number nobody set, and the read never raises."""
    v2 = tmp_path / "v2"
    v2.mkdir()
    (v2 / "memory.max").write_text("3221225472\n")
    assert rw._memory_limit_mb(str(v2)) == 3072.0
    (v2 / "memory.max").write_text("max\n")
    assert rw._memory_limit_mb(str(v2)) is None
    v1 = tmp_path / "v1"
    (v1 / "memory").mkdir(parents=True)
    (v1 / "memory" / "memory.limit_in_bytes").write_text("1073741824\n")
    assert rw._memory_limit_mb(str(v1)) == 1024.0
    (v1 / "memory" / "memory.limit_in_bytes").write_text("9223372036854771712\n")
    assert rw._memory_limit_mb(str(v1)) is None, "v1's unlimited"
    assert rw._memory_limit_mb(str(tmp_path / "absent")) is None
    (tmp_path / "unreadable" / "memory.max").mkdir(parents=True)
    assert rw._memory_limit_mb(str(tmp_path / "unreadable")) is None


def test_the_pass_reads_its_resident_memory_from_statm(tmp_path: Path) -> None:
    statm = tmp_path / "statm"
    statm.write_text("250000 131072 2000 300 0 140000 0\n")
    page = os.sysconf("SC_PAGE_SIZE")
    assert incremental_lane.rss_mb(str(statm)) == round(131072 * page / 1_048_576.0, 1)
    statm.write_text("garbage\n")
    assert incremental_lane.rss_mb(str(statm)) is None
    assert incremental_lane.rss_mb(str(tmp_path / "absent")) is None


def test_the_heartbeat_counts_the_same_rulings_the_engine_cannot_honour(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """E926: a closure of the operator's `same` rulings the invariants dissolved is re-read every
    pass while it stands, so the heartbeat says so every pass, beside the reconcile counters."""
    conn = _Conn()
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: conn)
    _settings(monkeypatch)
    summary = _summary()
    summary["counts"] = {**summary["counts"], "must_link_dissolved": 2}
    _stub_engine(monkeypatch, summary)
    assert rw._autodedup_sync()["must_link_dissolved"] == 2


# ------------------------------------------------- E941: a deploy releases the lease at once


RAILWAY_WORKER = Path(__file__).resolve().parents[2] / "railway.worker.json"
# E949: `env` sets the arena cap and execs python, which stays the container's PID 1.
WORKER_START_COMMAND = "env MALLOC_ARENA_MAX=2 python -m scraper.realtime_worker"


def test_the_shutdown_signal_is_the_one_path_that_releases(monkeypatch) -> None:
    """SIGTERM and SIGINT set the loop's stop event, as before, and start the release."""
    src = inspect.getsource(rw._amain)
    assert "loop.add_signal_handler(sig, _on_stop_signal, stop_event)" in src
    assert "signal.SIGTERM, signal.SIGINT" in src
    stop = asyncio.Event()
    rw._on_stop_signal(stop)
    assert stop.is_set() and rw._AUTODEDUP_STOPPING.is_set()


def test_the_drain_window_fits_the_release_and_ends_before_the_watchdog() -> None:
    """Railway's default is 0 s between SIGTERM and SIGKILL, so no handler would run. The
    window holds the release's one bounded connect and statement, and stays under the
    watchdog's liveness bound, which a drain must never reach: the heartbeat lane stops at
    the signal like every lane."""
    deploy = json.loads(RAILWAY_WORKER.read_text(encoding="utf-8"))["deploy"]
    draining = deploy["drainingSeconds"]
    attempts = rw.AUTODEDUP_RELEASE_CONNECT_ATTEMPTS
    worst = (rw.AUTODEDUP_CANCEL_TIMEOUT_SECONDS
             + attempts * rw.AUTODEDUP_RESCUE_CONNECT_TIMEOUT_SECONDS
             + (attempts - 1) * rw.AUTODEDUP_RELEASE_RETRY_DELAY_SECONDS)
    assert attempts >= 2, "one dropped handshake must not cost the release"
    assert worst < draining, "the cancel and both attempts fit the drain (one address)"
    assert draining < rw.LIVENESS_BOUND_SECONDS
    assert deploy["startCommand"] == WORKER_START_COMMAND


def test_a_shutdown_releases_the_lease_of_the_pass_in_flight_on_a_fresh_connection(
        monkeypatch) -> None:
    from autodedup import rt_lease
    from datetime import timedelta

    world = FakePg()
    world.lease[LANE_NAME] = {"holder": "host:1:1", "expires_at": world.now + timedelta(hours=1)}
    connects: list[dict[str, Any]] = []
    other = world.other_session()
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: connects.append(dict(k)) or other)
    monkeypatch.setattr(rw, "_AUTODEDUP_HOLDER", "host:1:1")

    thread = rw._autodedup_release_on_stop()
    assert thread is not None and not thread.daemon, "the exit waits for the one connect"
    thread.join(5)

    assert rt_lease.current(world)["live"] is False
    assert connects == [{"attempts": rw.AUTODEDUP_RELEASE_CONNECT_ATTEMPTS,
                         "retry_delay": rw.AUTODEDUP_RELEASE_RETRY_DELAY_SECONDS,
                         "connect_timeout": rw.AUTODEDUP_RESCUE_CONNECT_TIMEOUT_SECONDS}]
    assert other.closed and rw._AUTODEDUP_STOPPING.is_set()
    assert rw._autodedup_release_on_stop() is None, "a second signal starts nothing"
    assert len(connects) == 1


def test_the_shutdown_release_never_ends_another_writers_lease(monkeypatch) -> None:
    """A seed or dispatch may hold the row by the time the signal's release runs. The fake
    reads the holder predicate from RT_LEASE_RELEASE_SQL itself, so dropping `and holder =`
    from the statement fails this test: the seed's live row would be ended."""
    from autodedup import rt_lease
    from autodedup.incremental_sql import RT_LEASE_RELEASE_SQL
    from datetime import timedelta

    world = FakePg()
    world.lease[LANE_NAME] = {"holder": "rt_seed:gh:7:7",
                              "expires_at": world.now + timedelta(hours=1)}
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: world.other_session())

    assert rw._autodedup_release_lease("host:1:1") is True
    assert RT_LEASE_RELEASE_SQL in world.statements, "the release ran"
    row = rt_lease.current(world)
    assert (row["holder"], row["live"]) == ("rt_seed:gh:7:7", True)


def test_a_shutdown_with_no_pass_in_flight_opens_no_connection(monkeypatch) -> None:
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: pytest.fail("nothing to release"))
    assert rw._autodedup_release_on_stop() is None
    assert rw._AUTODEDUP_STOPPING.is_set()


def test_the_shutdown_release_never_raises(monkeypatch, caplog) -> None:
    """Best effort on a path where a raise helps nobody: the lease then ends by its TTL."""

    def refused(*_a: Any, **_k: Any) -> Any:
        raise ConnectionError("pooler down")

    monkeypatch.setattr(rw.db, "connect", refused)
    with caplog.at_level(logging.WARNING, logger="scraper.realtime_worker"):
        assert rw._autodedup_release_lease("host:1:1") is False

    class _Broken(_Conn):
        def cursor(self) -> Any:
            raise RuntimeError("server closed the connection")

    broken = _Broken()
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: broken)
    assert rw._autodedup_release_lease("host:1:1") is False
    assert broken.closed
    assert sum("could not release autodedup.rt_lease" in r.getMessage()
               for r in caplog.records) >= 1


def test_a_deploy_mid_pass_frees_the_lease_and_the_pass_commits_nothing(
        world, tmp_path, monkeypatch) -> None:
    """END TO END through the engine: the signal arrives while the pass decides. Its lease is
    released at once on a fresh connection, so the next worker's pass need not wait for the
    TTL; the pass reaches its fence, finds the lease gone and rolls back; the heartbeat says
    so; and the next worker can take the lease."""
    from autodedup import rt_lease

    seed_lane(world, tmp_path)
    cursors = {k: dict(v) for k, v in world.cursors.items()}
    other = world.other_session()
    monkeypatch.setattr(rw.db, "connect",
                        lambda *a, **k: other if k.get("attempts") else world)
    _settings(monkeypatch)
    original = incremental_lane.run_pass_bounded
    released: list[str] = []

    def sigterm_mid_pass(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        released.append(str(rw._AUTODEDUP_HOLDER))
        thread = rw._autodedup_release_on_stop()
        assert thread is not None
        thread.join(5)
        return result

    monkeypatch.setattr(incremental_lane, "run_pass_bounded", sigterm_mid_pass)

    last = rw._autodedup_sync()

    assert (last["ran"], last["errors"], last["aborted"]) == (True, 1, "lease_lost")
    assert last["reconcile"] == "pass_lease_lost" and last["claimed"] == 0
    assert world.cursors == cursors and not world.rt_fp and not world.pairs
    assert rt_lease.current(world)["live"] is False
    assert rt_lease.take(world, "next-worker:1:2", 2_400), "the next worker need not wait"
    assert released and released[0] != "None" and rw._AUTODEDUP_HOLDER is None


def test_a_deploy_while_the_pass_decides_stops_it_at_its_next_checkpoint(
        world, tmp_path, monkeypatch) -> None:
    """END TO END: the signal lands during the pass's first fact read. The pass stops at the
    next checkpoint and rolls back on its own connection, releasing its lease itself; the
    signal's own release on a fresh connection is the backstop and changes nothing more."""
    from autodedup import rt_lease

    seed_lane(world, tmp_path)
    cursors = {k: dict(v) for k, v in world.cursors.items()}
    other = world.other_session()
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: other if k.get("attempts") else world)
    _settings(monkeypatch)
    original = incremental_lane.SqlFacts.facts
    threads: list = []

    def sigterm_during_the_first_read(self, *args: Any, **kwargs: Any) -> Any:
        if not threads:
            threads.append(rw._autodedup_release_on_stop())
            threads[0].join(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(incremental_lane.SqlFacts, "facts", sigterm_during_the_first_read)

    last = rw._autodedup_sync()

    assert (last["ran"], last["errors"], last["aborted"]) == (True, 1, "stopping")
    assert last["reconcile"] == "pass_stopping"
    assert world.cursors == cursors and not world.rt_fp
    assert rt_lease.current(world)["live"] is False
    assert rt_lease.take(world, "next-worker:1:2", 2_400), "the next worker need not wait"


class _PassConn:
    """The pass's connection as the signal thread sees it: `cancel_safe` is all it calls."""

    def __init__(self, order: list[str], fails: bool = False) -> None:
        self.order, self.fails, self.timeouts = order, fails, []

    def cancel_safe(self, *, timeout: float) -> None:
        self.order.append("cancel")
        self.timeouts.append(timeout)
        if self.fails:
            raise RuntimeError("CancellationTimeout")


@pytest.mark.parametrize("fails", [False, True])
def test_the_signal_cancels_the_running_statement_then_releases(monkeypatch, fails) -> None:
    """A statement already running (up to 120 s) is cancelled inside the drain — first, bounded
    — and the release follows whatever the cancel did: it never stops the release."""
    from autodedup import rt_lease
    from datetime import timedelta

    world = FakePg()
    world.lease[LANE_NAME] = {"holder": "host:1:1", "expires_at": world.now + timedelta(hours=1)}
    order: list[str] = []
    pass_conn = _PassConn(order, fails=fails)
    monkeypatch.setattr(rw, "_AUTODEDUP_HOLDER", "host:1:1")
    monkeypatch.setattr(rw, "_AUTODEDUP_CONN", pass_conn)
    monkeypatch.setattr(rw.db, "connect",
                        lambda *a, **k: order.append("connect") or world.other_session())

    thread = rw._autodedup_release_on_stop()
    thread.join(5)

    assert order == ["cancel", "connect"]
    assert pass_conn.timeouts == [rw.AUTODEDUP_CANCEL_TIMEOUT_SECONDS]
    assert rt_lease.current(world)["live"] is False


class _CancellableSession:
    """The pass's session over the shared world: SIGTERM lands while `target` runs, the signal
    thread's `cancel_safe` cancels it, and the statement raises as Postgres would."""

    def __init__(self, world: FakePg, target: str) -> None:
        self.world, self.target, self.cancelled, self.signalled = world, target, False, False

    def cursor(self) -> Any:
        return _CancellableCursor(self)

    def transaction(self) -> Any:
        return self.world.transaction()

    def cancel_safe(self, *, timeout: float) -> None:
        self.cancelled = True

    def close(self) -> None:
        return None


class _CancellableCursor:
    def __init__(self, session: _CancellableSession) -> None:
        self.session, self.inner = session, session.world.cursor()

    def __enter__(self) -> "_CancellableCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        import psycopg

        if sql == self.session.target and not self.session.signalled:
            self.session.signalled = True
            rw._autodedup_release_on_stop().join(5)        # SIGTERM mid-statement
            if self.session.cancelled:
                raise psycopg.errors.QueryCanceled("canceling statement due to user request")
        self.inner.execute(sql, params)
        self.description = getattr(self.inner, "description", None)

    def executemany(self, sql: str, seq: Any) -> None:
        self.inner.executemany(sql, seq)

    def fetchall(self) -> list[tuple]:
        return self.inner.fetchall()


def test_a_statement_running_at_the_signal_is_cancelled_and_the_pass_rolls_back(
        world, tmp_path, monkeypatch) -> None:
    """END TO END: the cancel aborts the statement in flight; the pass takes the raise path
    (E930's halving skipped: the worker is stopping), rolls back and releases its lease."""
    import psycopg
    from autodedup import rt_lease
    from autodedup.export_sql import COHORT_LISTINGS_SQL

    seed_lane(world, tmp_path)
    cursors = {k: dict(v) for k, v in world.cursors.items()}
    session = _CancellableSession(world, COHORT_LISTINGS_SQL)
    monkeypatch.setattr(rw.db, "connect",
                        lambda *a, **k: world.other_session() if k.get("attempts") else session)
    _settings(monkeypatch)

    with pytest.raises(psycopg.errors.QueryCanceled):
        rw._autodedup_sync()

    assert session.signalled and session.cancelled
    assert world.cursors == cursors and not world.rt_fp
    assert rt_lease.current(world)["live"] is False
    rate = incremental_lane.pass_rate_key(incremental_lane.GENERATION)
    assert world.settings_by[rate].endswith(":halved_ahead"), "no halving after the signal"


# ------------------------------------ E949: what a pass frees goes back before the next pass


class _Glibc:
    """glibc as the worker loads it: the trim, and whether the pass lock was held during it."""

    def __init__(self, order: list[str]) -> None:
        self.order = order

    def malloc_trim(self, pad: int) -> int:
        held = rw._AUTODEDUP_PASS_LOCK._lock.locked()
        self.order.extend([f"malloc_trim({pad})", "locked" if held else "unlocked"])
        return 1


def _hand_back(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The worker's two calls replaced in its own module (`_collect_garbage`, `_load_library`),
    never the process's `gc` or `ctypes`."""
    order: list[str] = []

    def load(name: str) -> _Glibc:
        order.append(f"CDLL({name})")
        return _Glibc(order)

    monkeypatch.setattr(rw, "_collect_garbage", lambda *_a: order.append("gc.collect") or 0)
    monkeypatch.setattr(rw, "_load_library", load)
    monkeypatch.setattr(incremental_lane, "rss_mb", lambda *_a: order.append("rss") or 301.5)
    return order


HAND_BACK = ["gc.collect", "CDLL(libc.so.6)", "malloc_trim(0)", "locked", "rss"]


@pytest.mark.parametrize("outcome", ["ran", "refused", "leased", "store_absent", "raised"])
def test_every_pass_hands_back_freed_memory_as_it_begins_and_before_its_lock_is_released(
        monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    """The pass ran on any of the executor's 32 threads and glibc kept what it freed in that
    thread's arena (three passes on three threads: 234, 456, 678 MiB). So every pass that holds
    the lock collects, trims and reads its RSS twice: before the engine is called, and as it
    ends, before the next pass may enter. A raised pass's end runs it too, but with the raise
    still in flight, its traceback holding the pass's frames: that memory goes at the next
    pass's start (`test_a_raised_pass_is_handed_back_before_the_next_pass_reaches_the_engine`)."""
    order = _hand_back(monkeypatch)
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: _Conn(
        present=None if outcome == "store_absent" else True))
    _settings(monkeypatch)
    _stub_engine(monkeypatch, {
        "ran": _summary(), "refused": SystemExit("STORAGE: over the budget"),
        "leased": {"skipped": "leased", "reason": "a seed", "spent_usd": 0.0},
        "store_absent": _summary(), "raised": RuntimeError("pooler went away")}[outcome])
    engine = incremental_lane.run_incremental

    def entered(*args: Any, **kwargs: Any) -> Any:
        order.append("engine")
        return engine(*args, **kwargs)

    monkeypatch.setattr(incremental_lane, "run_incremental", entered)

    if outcome == "raised":
        with pytest.raises(RuntimeError):
            rw._autodedup_sync()
    else:
        last = rw._autodedup_sync()
        assert (last["rss_at_start_mb"], last["rss_after_trim_mb"]) == (301.5, 301.5)

    called = [] if outcome == "store_absent" else ["engine"]
    assert order == HAND_BACK + called + HAND_BACK
    assert rw._AUTODEDUP_PASS_LOCK.try_enter(), "the lock was released after the trim"
    rw._AUTODEDUP_PASS_LOCK.release()


class _WorkingSet:
    """What a pass holds when a statement of it times out: facts, fingerprints, pairs."""

    def __init__(self) -> None:
        self.rows = ["w" * 700 + str(i) for i in range(1000)]


class _StatementTimeout(Exception):
    pass


@pytest.mark.parametrize("kept_by_its_frame", [False, True], ids=["no-cycle", "own-cycle"])
def test_a_raised_pass_is_handed_back_before_the_next_pass_reaches_the_engine(
        world, tmp_path, monkeypatch, kept_by_its_frame: bool) -> None:
    """E930's path (a statement timeout, a terminated backend) END TO END, through the lane
    loop: the raise reaches it with a traceback holding every frame of the pass, so the
    hand-back at that pass's end, which runs while the raise is in flight, reaches none of it.
    Automatic collection is off, as on CPython 3.12 when no full collection is due, and the
    lane logs to a stream, as in production: pytest's own capture would keep the record, and
    with it the raise. `no-cycle`: the engine no longer keeps the raise it noted (`original`),
    so the loop's drop of the raise frees the pass at once. `own-cycle`: a frame that keeps its
    own raise outlives the drop, and the next pass's first hand-back is the collection that
    frees it. Either way nothing of the raised pass is alive when the next pass reaches the
    engine."""
    seed_lane(world, tmp_path)
    _worker_on(monkeypatch, world)
    logged = io.StringIO()
    monkeypatch.setattr(rw.LOG, "handlers", [logging.StreamHandler(logged)])
    monkeypatch.setattr(rw.LOG, "propagate", False)
    held: list[weakref.ref[_WorkingSet]] = []
    alive_after_drop: list[bool] = []
    alive_at_next: list[bool] = []
    engine, scoring = incremental_lane.run_incremental, incremental_lane.run_pass_bounded
    stop = asyncio.Event()
    state = rw._new_state()

    def times_out(*args: Any, **kwargs: Any) -> Any:
        if held:
            return scoring(*args, **kwargs)
        working = _WorkingSet()
        held.append(weakref.ref(working))
        try:
            raise _StatementTimeout("canceling statement due to statement timeout")
        except _StatementTimeout as exc:
            # kept: this frame -> the raise -> its traceback -> this frame
            kept = exc if kept_by_its_frame else None  # noqa: F841
            raise

    def interval() -> float:
        if held:  # the loop has logged the raised pass and dropped its raise
            alive_after_drop.append(held[0]() is not None)
        return 0.01

    async def lane() -> None:
        loop = asyncio.get_running_loop()

        def entered(*args: Any, **kwargs: Any) -> Any:
            if held:
                alive_at_next.append(held[0]() is not None)
                loop.call_soon_threadsafe(stop.set)
            return engine(*args, **kwargs)

        monkeypatch.setattr(incremental_lane, "run_incremental", entered)
        await rw._lane_loop("autodedup", stop, interval,
                            lambda: rw._autodedup_pass(stop, state), state,
                            default_interval=0.01)

    monkeypatch.setattr(incremental_lane, "run_pass_bounded", times_out)
    gc.collect()
    gc.disable()
    try:
        asyncio.run(lane())
    finally:
        gc.enable()

    assert "autodedup lane pass failed" in logged.getvalue() and "_StatementTimeout" in (
        logged.getvalue())
    assert state["lanes"]["autodedup"]["failed_passes"] == 1
    assert alive_after_drop == [kept_by_its_frame]
    assert alive_at_next == [False], "the raised pass's working set reached the next pass"
    assert state["lanes"]["autodedup"]["last"]["ran"] is True


def _no_glibc(name: str) -> Any:
    raise OSError(f"{name}: cannot open shared object file")


class _NoTrim:
    """A C library without glibc's `malloc_trim`."""


@pytest.mark.parametrize("load", [_no_glibc, lambda name: _NoTrim()],
                         ids=["no-glibc", "no-malloc_trim"])
def test_without_glibc_the_hand_back_is_a_no_op_and_never_raises(
        monkeypatch: pytest.MonkeyPatch, load: Any) -> None:
    monkeypatch.setattr(rw, "_load_library", load)
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: _Conn())
    _settings(monkeypatch)
    _stub_engine(monkeypatch, _summary())

    last = rw._autodedup_sync()

    assert last["ran"] is True and last["errors"] == 0
    if sys.platform.startswith("linux"):
        assert last["rss_at_start_mb"] > 0 and last["rss_after_trim_mb"] > 0, (
            "the RSS is read all the same")
    assert rw._AUTODEDUP_PASS_LOCK.try_enter()
    rw._AUTODEDUP_PASS_LOCK.release()
    monkeypatch.setitem(sys.modules, "autodedup.incremental_lane", None)
    assert rw._return_memory() is None, "no engine to read it from: None, never a raise"


def test_the_heartbeat_carries_both_hand_backs_beside_the_memos_before_the_end(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: _Conn())
    readings = iter([290.0, 301.5])
    monkeypatch.setattr(rw, "_return_memory", lambda: next(readings))
    _settings(monkeypatch)
    _stub_engine(monkeypatch, _summary())
    state = rw._new_state()

    asyncio.run(rw._autodedup_pass(asyncio.Event(), state))

    last = state["lanes"]["autodedup"]["last"]
    assert (last["rss_at_start_mb"], last["rss_mb"], last["rss_after_trim_mb"]) == (
        290.0, 388.0, 301.5)
    assert last["memo_entries"] == {
        "text_facts": 812, "body_align": 64, "shingles": 30, "tokens": 2048}
    Jsonb(rw._lane_snapshot(state["lanes"]))


def test_a_real_pass_leaves_no_body_behind_and_reads_its_rss_after_the_trim(
        world, tmp_path, monkeypatch) -> None:
    """END TO END through the engine: what the pass read of a body, and the tokens it hashed,
    are reported, then gone; the RSS is read once each hand-back ran."""
    from autodedup import body_align, text_facts
    from tests.autodedup.lane_world import DESCRIPTION

    seed_lane(world, tmp_path)
    _worker_on(monkeypatch, world)
    original = incremental_lane.run_pass_bounded

    def reads_a_body(*args: Any, **kwargs: Any) -> Any:
        text_facts.printed_floors(DESCRIPTION, True)
        body_align.tokens(DESCRIPTION)
        return original(*args, **kwargs)

    monkeypatch.setattr(incremental_lane, "run_pass_bounded", reads_a_body)

    last = rw._autodedup_sync()

    assert last["ran"] is True and last["errors"] == 0, last
    assert set(last["memo_entries"]) == {"text_facts", "body_align", "shingles", "tokens"}
    assert last["memo_entries"]["text_facts"] >= 1 and last["memo_entries"]["body_align"] >= 1
    assert last["memo_entries"]["tokens"] >= 1
    assert incremental_lane.memo_entries() == dict.fromkeys(last["memo_entries"], 0)
    if sys.platform.startswith("linux"):
        assert last["rss_at_start_mb"] > 0 and last["rss_after_trim_mb"] > 0


def test_the_worker_starts_with_two_malloc_arenas_and_python_as_its_pid_1() -> None:
    """Every thread of the worker allocates from one of two glibc arenas, so what one pass
    freed is the next pass's to reuse whichever thread runs it (three passes on three threads:
    230, 449, 451 MiB capped, against 230, 449, 668 uncapped). Railway runs a Dockerfile
    service's start command in exec form, with no shell, so a bare `MALLOC_ARENA_MAX=2 python
    ...` would be taken for the program and the worker would never start: `env` sets the
    variable and execs python, which stays PID 1 for E941's SIGTERM handler. The API's image
    and its start are untouched."""
    command = json.loads(RAILWAY_WORKER.read_text(encoding="utf-8"))["deploy"]["startCommand"]
    assert command == WORKER_START_COMMAND
    words = command.split()
    assert words[0] == "env" and words[1] == "MALLOC_ARENA_MAX=2"
    assert words[2:] == ["python", "-m", "scraper.realtime_worker"], "env execs python itself"
    assert not set(words) & {"sh", "bash", "-c", "&&", ";", "|"}, "no shell before python"
    for name in ("railway.json", "Dockerfile"):
        assert "MALLOC_ARENA_MAX" not in (RAILWAY_WORKER.parent / name).read_text(
            encoding="utf-8"), name
