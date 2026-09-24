"""The realtime worker's `autodedup` lane: THE engine's real-time shadow pass, run from the
always-on worker (AUTODEDUP rollout §7.3).

Two halves. The lane's own contract — dark until `app_settings.realtime_autodedup_enabled`,
fail-safe when the settings read raises, a clamped claim, a hard deadline, a refusal recorded
rather than raised, heartbeat counters on every path — is pinned against stubs. And the pass
itself is driven END TO END through `autodedup.incremental_lane.run_incremental` over the
engine's own Postgres stand-in (`tests/autodedup/fake_pg.py`, which raises on any statement it
does not know), seeded the way `test_rt_gate` seeds it: so "the same code the GitHub lane runs"
and "writes only schema autodedup" are measured, not asserted by inspection. No network, no DB.
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
from autodedup.incremental import Limits
from autodedup.incremental_lane import (
    ENV_FLAG,
    LANE_NAME,
    LEASE_TTL_S,
    STATEMENT_TIMEOUT_MS,
    run_incremental,
)
from autodedup.incremental_sql import RT_LEASE_RELEASE_SQL, RT_STORE_PRESENT_SQL
from scraper import realtime_worker as rw
from scripts.verify_pipeline import DEFAULT_THRESHOLDS
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.test_rt_gate import _seed, world  # noqa: F401 - `world` is a fixture

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / (
    "557_realtime_autodedup_lane_settings.sql")


@pytest.fixture(autouse=True)
def _fresh_lane_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-process guards reset per test, and the workflow's variable ABSENT — the worker has
    no repository variable, so every pass here must run without it."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    monkeypatch.setattr(rw, "_AUTODEDUP_PASS_LOCK", threading.Lock())
    monkeypatch.setattr(rw, "_AUTODEDUP_WEDGE_LOGGED", False)
    monkeypatch.setattr(rw, "_AUTODEDUP_DARK_LOGGED", False)
    monkeypatch.setattr(rw, "_AUTODEDUP_STORE_WARNED", False)
    monkeypatch.setattr(rw, "_AUTODEDUP_LAST_OUTCOME", None)


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

    def fake(conn_factory: Any, args: Any, out_dir: Path, *, enabled: Any = None) -> Any:
        seen.update(args=dict(args), enabled=enabled, out_dir=out_dir,
                    out_dir_existed=Path(out_dir).is_dir(), conn=conn_factory())
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
    }
    out.update(over)
    return out


# ------------------------------------------------------------------ registration + dark gate


def test_the_lane_is_registered_dark_and_fail_safe() -> None:
    # default_interval=0: _lane_loop keeps it when the settings read RAISES, and the flag
    # lives inside that read — a pooler blip must not run a pass of a lane left dark.
    lane = inspect.getsource(rw._amain).split('("autodedup"', 1)[1].split(")),", 1)[0]
    assert "_read_autodedup_interval" in lane
    assert "default_interval=0" in lane


def test_the_interval_is_zero_until_the_flag_is_on_and_says_so_once(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    _settings(monkeypatch)
    with caplog.at_level(logging.INFO, logger="scraper.realtime_worker"):
        assert rw._read_autodedup_interval() == 0
        assert rw._read_autodedup_interval() == 0
    assert sum("AUTODEDUP lane dark" in r.message for r in caplog.records) == 1

    _settings(monkeypatch, realtime_autodedup_enabled=False,
              realtime_autodedup_interval_seconds=15)
    assert rw._read_autodedup_interval() == 0, "an explicit false is off too"

    _settings(monkeypatch, realtime_autodedup_enabled=True)
    assert rw._read_autodedup_interval() == rw.AUTODEDUP_INTERVAL_DEFAULT == 60
    _settings(monkeypatch, realtime_autodedup_enabled=True,
              realtime_autodedup_interval_seconds=15)
    assert rw._read_autodedup_interval() == 15
    _settings(monkeypatch, realtime_autodedup_enabled=True,
              realtime_autodedup_interval_seconds=0)
    assert rw._read_autodedup_interval() == 0, "interval 0 idles an enabled lane"

    # Switched off again: said once more, because it is news again.
    caplog.clear()
    _settings(monkeypatch)
    with caplog.at_level(logging.INFO, logger="scraper.realtime_worker"):
        rw._read_autodedup_interval()
    assert sum("AUTODEDUP lane dark" in r.message for r in caplog.records) == 1


def test_a_dark_lane_opens_no_connection_and_runs_no_pass(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Provably inert: the real reader, the real loop, the flag absent."""
    _settings(monkeypatch)
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


def test_the_seeded_settings_ship_the_lane_dark() -> None:
    """The rows exist so /settings can flip them (PUT 404s on an absent key); the flag is
    seeded OFF and an operator's value is never overwritten."""
    sql = MIGRATION.read_text(encoding="utf-8")
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert "'realtime_autodedup_enabled',\n    'false'::jsonb" in body
    assert "'realtime_autodedup_interval_seconds',\n    '60'::jsonb" in body
    assert "'realtime_autodedup_max_listings',\n    '100'::jsonb" in body
    assert "on conflict (key) do nothing" in body
    assert not re.search(r"\b(create|alter|drop|delete|update|truncate)\b", body, re.I)
    for key in (rw.AUTODEDUP_LANE_SETTING, rw.AUTODEDUP_INTERVAL_SETTING,
                rw.AUTODEDUP_MAX_LISTINGS_SETTING):
        assert f"'{key}'" in body


# ------------------------------------------------------------------------------ the bounds


def test_the_claim_cap_is_clamped_to_the_engines_own_default(
        monkeypatch: pytest.MonkeyPatch) -> None:
    assert rw.AUTODEDUP_MAX_LISTINGS_CEILING == Limits().max_listings
    for stored, expected in ((None, rw.AUTODEDUP_MAX_LISTINGS_DEFAULT), (0, 1), (-5, 1),
                             (37, 37), (10_000, rw.AUTODEDUP_MAX_LISTINGS_CEILING)):
        _settings(monkeypatch, realtime_autodedup_max_listings=stored)
        assert rw._read_autodedup_max_listings() == expected


def test_the_deadline_sits_inside_every_bound_around_it() -> None:
    """Deadline plus ONE statement at the engine's statement_timeout must end before the
    stall monitor warns, before the lane abandons the pass, and before the engine's lease
    expires — or the GH lane could take the lease while a pass here is still writing."""
    worst = rw.AUTODEDUP_PASS_DEADLINE_SECONDS + STATEMENT_TIMEOUT_MS / 1000.0
    assert worst < DEFAULT_THRESHOLDS["worker_lane_stall_warn_seconds"]
    assert worst < rw.LANE_PASS_TIMEOUT_SECONDS
    assert worst < LEASE_TTL_S


def test_the_cap_and_the_switch_reach_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _Conn()
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: conn)
    _settings(monkeypatch, realtime_autodedup_max_listings=7)
    seen = _stub_engine(monkeypatch, _summary())

    last = rw._autodedup_sync()

    # The cap and nothing else: no scope, settings or model — a pass runs under what its
    # generation was seeded with, and the engine refuses a differing argument.
    assert seen["args"] == {"max_listings": "7"}
    assert seen["enabled"] is True, "the lane's own switch stands in for the repo variable"
    assert seen["out_dir_existed"]
    assert not Path(seen["out_dir"]).exists(), "the summary file lives for the pass only"
    assert isinstance(seen["conn"], rw._DeadlineConnection)
    assert conn.executed == [RT_STORE_PRESENT_SQL]
    assert conn.closed
    assert last == {
        "ran": True, "claimed": 12, "scored": 40, "grouped": 3, "skipped": 0, "errors": 0,
        "cap": 7, "seconds": last["seconds"], "held": 1, "retired": 0,
        "latency_p50_s": 310.2, "latency_p95_s": 355.0, "bound_by": "count",
    }


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


@pytest.mark.parametrize("reason", ["leased", "unseeded", "dark"])
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
    stop the event loop — every lane of the worker — so the lane records it instead."""
    monkeypatch.setattr(rw.db, "connect", lambda *a, **k: _Conn())
    _settings(monkeypatch)
    _stub_engine(monkeypatch, SystemExit("PARITY GATE: 3 breaches " + "x" * 1000))
    state = rw._new_state()

    with caplog.at_level(logging.WARNING, logger="scraper.realtime_worker"):
        asyncio.run(rw._autodedup_pass(asyncio.Event(), state))
        asyncio.run(rw._autodedup_pass(asyncio.Event(), state))

    lane = state["lanes"]["autodedup"]
    assert lane["passes"] == 2 and lane["failed_passes"] == 0
    assert lane["last"]["errors"] == 1 and lane["last"]["ran"] is False
    assert lane["last"]["refused"].startswith("PARITY GATE")
    assert len(lane["last"]["refused"]) == rw.AUTODEDUP_REASON_CHARS
    assert lane["last"]["deadline_exceeded"] is False
    # Said on the transition, not once a minute for as long as the refusal stands.
    assert sum("AUTODEDUP lane pass stopped" in r.message for r in caplog.records) == 1
    Jsonb(rw._lane_snapshot(state["lanes"]))


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


def test_the_lane_never_reaches_the_merge_chokepoint() -> None:
    src = "".join(inspect.getsource(fn) for fn in (
        rw._autodedup_sync, rw._autodedup_pass, rw._autodedup_outcome, rw._DeadlineConnection))
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


def test_one_worker_pass_is_the_engines_pass(world, tmp_path, monkeypatch) -> None:  # noqa: F811
    """With the workflow's variable absent, the worker's switch alone opens a pass, and the
    pass is the engine's: it claims, decides, moves its cursors under the shared lease, frees
    the lease, and every row it writes is in schema autodedup."""
    conn, artifact = world
    _seed(conn, artifact, tmp_path)
    before = len(conn.statements)
    _worker_on(monkeypatch, conn)

    last = rw._autodedup_sync()

    assert last["ran"] is True and last["errors"] == 0 and last["skipped"] == 0, last
    assert last["claimed"] > 0 and last["cap"] == rw.AUTODEDUP_MAX_LISTINGS_DEFAULT
    assert conn.cursors, "the pass moved the engine's own watermark"
    assert conn.lease[LANE_NAME]["expires_at"] <= conn.now, "the lease was released"
    targets = _write_targets(conn.statements[before:])
    assert targets, "the pass wrote nothing"
    assert all(t.startswith("autodedup.") for t in targets), sorted(targets)
    assert conn.statements_in_tx, "the pass ran as the engine's one transaction"


def test_the_cap_binds_the_engines_claim(world, tmp_path, monkeypatch) -> None:  # noqa: F811
    conn, artifact = world
    _seed(conn, artifact, tmp_path)
    _worker_on(monkeypatch, conn, realtime_autodedup_max_listings=1)

    last = rw._autodedup_sync()

    assert last["ran"] is True and last["cap"] == 1
    assert last["claimed"] == 1 and last["bound_by"] == "count"


def test_the_worker_and_the_workflow_never_pass_at_once(
        world, tmp_path, monkeypatch) -> None:  # noqa: F811
    """One lease row, by name: while the GH lane holds it the worker is a green skip."""
    from datetime import timedelta

    conn, artifact = world
    _seed(conn, artifact, tmp_path)
    conn.lease[LANE_NAME] = {"holder": "gh-runner:1:1",
                             "expires_at": conn.now + timedelta(minutes=10)}
    cursors = {k: dict(v) for k, v in conn.cursors.items()}
    _worker_on(monkeypatch, conn)

    last = rw._autodedup_sync()

    assert (last["skipped"], last["reason"], last["errors"]) == (1, "leased", 0)
    assert conn.cursors == cursors
    assert conn.lease[LANE_NAME]["holder"] == "gh-runner:1:1"


def test_the_deadline_rolls_the_pass_back_and_frees_the_lease(
        world, tmp_path, monkeypatch) -> None:  # noqa: F811
    """Tripped INSIDE the engine's transaction: the rollback leaves the store as it was, and
    the lease release — the one exempt statement — still goes out."""
    conn, artifact = world
    _seed(conn, artifact, tmp_path)
    cursors = {k: dict(v) for k, v in conn.cursors.items()}
    pairs = dict(conn.pairs)
    rolled_back = conn.rolled_back

    def clock() -> float:
        return 10.0 if conn.in_transaction and len(conn.statements_in_tx) >= 5 else 0.0

    guarded = rw._DeadlineConnection(conn, 1.0, exempt=(RT_LEASE_RELEASE_SQL,), clock=clock)
    with pytest.raises(rw._AutodedupDeadline):
        run_incremental(lambda: guarded, {}, tmp_path, enabled=True)

    assert guarded.tripped
    assert conn.rolled_back == rolled_back + 1
    assert conn.cursors == cursors and conn.pairs == pairs
    assert conn.lease[LANE_NAME]["expires_at"] <= conn.now


def test_a_pass_past_its_deadline_is_an_error_not_a_crash(
        world, tmp_path, monkeypatch) -> None:  # noqa: F811
    conn, artifact = world
    _seed(conn, artifact, tmp_path)
    cursors = {k: dict(v) for k, v in conn.cursors.items()}
    monkeypatch.setattr(rw, "AUTODEDUP_PASS_DEADLINE_SECONDS", -1.0)
    _worker_on(monkeypatch, conn)

    last = rw._autodedup_sync()

    assert last["errors"] == 1 and last["deadline_exceeded"] is True
    assert "deadline" in last["refused"]
    assert conn.cursors == cursors


def test_the_workflow_path_keeps_its_own_gate(tmp_path, monkeypatch) -> None:
    """`enabled` is a keyword the worker passes; the workflow passes nothing and still needs
    the repository variable."""
    def never() -> Any:
        raise AssertionError("a dark pass must not open a connection")

    assert run_incremental(never, {}, tmp_path)["skipped"] == "dark"
    assert run_incremental(never, {}, tmp_path, enabled=False)["skipped"] == "dark"
    monkeypatch.setenv(ENV_FLAG, "true")
    assert run_incremental(never, {}, tmp_path, enabled=False)["skipped"] == "dark", (
        "a caller's own switch, when given, is the one that counts")
