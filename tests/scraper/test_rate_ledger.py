"""Hermetic tests for scraper.rate_ledger: the DB-backed shared politeness
ledger (migration 268).

The DB is a fake that RECORDS every statement + params and EMULATES the
portal_rate_state row the way the SQL would (window = max(frontier, now),
frontier += N slots, decay/penalty on penalty_factor), so two limiter
instances sharing one fake behave like two runtimes sharing one budget.
Time is frozen via the same time-module patch tests/test_rate_limit.py uses
(no wall-clock passes; sleeps are recorded).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

import scraper.rate_limit as rl
from scraper import portal_runner
from scraper.rate_ledger import (
    DEFAULT_LEASE_N,
    LedgerRateLimiter,
    build_rate_limiter,
)
from scraper.rate_limit import RateLimiter


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


def _patch_time(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> list[float]:
    sleeps: list[float] = []
    # rl.time IS the stdlib module, shared with scraper.rate_ledger — one patch
    # freezes both.
    monkeypatch.setattr(rl.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(rl.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


class _Ledger:
    """Emulated portal_rate_state + statement recorder shared by fake conns."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.rows: dict[str, dict[str, float]] = {}
        self.executed: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_on: set[str] = set()
        self.connects = 0

    def connect(self) -> "_Conn":
        self.connects += 1
        return _Conn(self)


class _Cur:
    def __init__(self, ledger: _Ledger) -> None:
        self._l = ledger
        self._result: Any = None

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def close(self) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        led = self._l
        low = sql.lower()
        if "penalized_at" in low:
            kind = "penalize"
        elif "returning" in low:
            kind = "lease"
        else:
            kind = "insert"
        led.executed.append((kind, sql, dict(params or {})))
        if kind in led.fail_on:
            raise RuntimeError(f"db down during {kind}")
        assert params is not None
        now = led.clock.monotonic()
        if kind == "insert":
            led.rows.setdefault(params["source"], {
                "next_slot_at": now,
                "interval_ms": params["interval_ms"],
                "penalty_factor": 1.0,
            })
            self._result = None
        elif kind == "lease":
            row = led.rows[params["source"]]
            start = max(row["next_slot_at"], now)
            slot = params["interval_ms"] * row["penalty_factor"] / 1000.0
            row["next_slot_at"] = start + params["n"] * slot
            row["interval_ms"] = params["interval_ms"]
            row["penalty_factor"] = max(
                1.0, row["penalty_factor"] * params["decay"])
            self._result = (max(start - now, 0.0), slot)
        else:
            row = led.rows[params["source"]]
            pf = min(params["cap"], row["penalty_factor"] * params["mult"])
            row["penalty_factor"] = pf
            row["next_slot_at"] = (
                max(row["next_slot_at"], now) + row["interval_ms"] * pf / 1000.0
            )
            self._result = None

    def fetchone(self) -> Any:
        return self._result


class _Conn:
    def __init__(self, ledger: _Ledger) -> None:
        self._ledger = ledger
        self.closed = False

    def cursor(self) -> _Cur:
        return _Cur(self._ledger)

    def close(self) -> None:
        self.closed = True


def _kinds(ledger: _Ledger) -> list[str]:
    return [k for k, _, _ in ledger.executed]


# --- lease mechanics ---

def test_first_acquire_lazily_creates_row_and_leases(monkeypatch):
    clock = _Clock()
    _patch_time(monkeypatch, clock)
    ledger = _Ledger(clock)
    lim = LedgerRateLimiter("srealitka", 2.0, lease_n=4, connect=ledger.connect)
    lim.acquire()

    assert _kinds(ledger) == ["insert", "lease"]
    _, insert_sql, insert_params = ledger.executed[0]
    assert "on conflict (source) do nothing" in insert_sql.lower()
    assert insert_params == {"source": "srealitka", "interval_ms": 500}
    _, lease_sql, lease_params = ledger.executed[1]
    assert "greatest(next_slot_at, now())" in lease_sql
    assert "make_interval" in lease_sql
    assert lease_params == {
        "source": "srealitka", "interval_ms": 500, "n": 4, "decay": 0.9,
    }


def test_one_lease_covers_n_acquires_paced_by_slot_width(monkeypatch):
    clock = _Clock()
    sleeps = _patch_time(monkeypatch, clock)
    ledger = _Ledger(clock)
    lim = LedgerRateLimiter("x", 2.0, lease_n=4, connect=ledger.connect)
    for _ in range(4):
        lim.acquire()
    # One round trip for four requests; local pacing spaces them 0.5s apart
    # (frozen clock, so waits stack like the plain limiter's).
    assert _kinds(ledger).count("lease") == 1
    assert sleeps == [0.5, 1.0, 1.5]

    lim.acquire()  # 5th -> a second lease, starting at the advanced frontier
    assert _kinds(ledger).count("lease") == 2
    # The shared frontier moved 4 slots (to t+2.0), so slot 5 starts there.
    assert sleeps[-1] == pytest.approx(2.0)


def test_n_slots_advance_shared_frontier_for_the_next_runtime(monkeypatch):
    clock = _Clock()
    sleeps = _patch_time(monkeypatch, clock)
    ledger = _Ledger(clock)
    a = LedgerRateLimiter("x", 1.0, lease_n=3, connect=ledger.connect)
    b = LedgerRateLimiter("x", 1.0, lease_n=3, connect=ledger.connect)

    a.acquire()             # A leases slots [t, t+3)
    assert sleeps == []     # A's first slot starts immediately
    b.acquire()             # B ("the other runtime") leases [t+3, t+6)
    assert sleeps == [pytest.approx(3.0)]
    assert ledger.rows["x"]["next_slot_at"] == pytest.approx(1000.0 + 6.0)


def test_healthy_leases_decay_penalty_toward_floor(monkeypatch):
    clock = _Clock()
    _patch_time(monkeypatch, clock)
    ledger = _Ledger(clock)
    lim = LedgerRateLimiter("x", 1.0, lease_n=1, connect=ledger.connect)
    lim.acquire()
    ledger.rows["x"]["penalty_factor"] = 4.0  # as if another runtime got 429'd
    lim.acquire()
    # The lease consumed the shared penalty (slot width 4x) and decayed it.
    assert lim.interval == pytest.approx(4.0)
    assert ledger.rows["x"]["penalty_factor"] == pytest.approx(3.6)
    for _ in range(60):
        lim.acquire()
    assert ledger.rows["x"]["penalty_factor"] == pytest.approx(1.0)


# --- penalty propagation ---

def test_penalize_writes_shared_penalty_and_drops_leased_window(monkeypatch):
    clock = _Clock()
    _patch_time(monkeypatch, clock)
    ledger = _Ledger(clock)
    lim = LedgerRateLimiter("x", 1.0, lease_n=10, connect=ledger.connect)
    lim.acquire()
    lim.penalize()

    kind, _, params = ledger.executed[-1]
    assert kind == "penalize"
    assert params == {"source": "x", "mult": 2.0, "cap": 8.0}
    assert ledger.rows["x"]["penalty_factor"] == pytest.approx(2.0)

    before = _kinds(ledger).count("lease")
    lim.acquire()  # remaining 9 leased slots were dropped -> re-lease, widened
    assert _kinds(ledger).count("lease") == before + 1
    assert lim.interval == pytest.approx(2.0)  # slot width now 2x base


def test_shared_penalty_is_capped(monkeypatch):
    clock = _Clock()
    _patch_time(monkeypatch, clock)
    ledger = _Ledger(clock)
    lim = LedgerRateLimiter("x", 1.0, lease_n=1, connect=ledger.connect)
    lim.acquire()
    for _ in range(10):
        lim.penalize()
    assert ledger.rows["x"]["penalty_factor"] == pytest.approx(8.0)


def test_penalty_reaches_the_other_runtime_on_its_next_lease(monkeypatch):
    clock = _Clock()
    _patch_time(monkeypatch, clock)
    ledger = _Ledger(clock)
    a = LedgerRateLimiter("x", 1.0, lease_n=1, connect=ledger.connect)
    b = LedgerRateLimiter("x", 1.0, lease_n=1, connect=ledger.connect)
    a.acquire()
    a.penalize()
    clock.now += 100.0  # idle past the pushed frontier
    b.acquire()
    assert b.interval == pytest.approx(2.0)  # B leased at the shared 2x width


# --- DB-failure posture ---

def test_db_failure_falls_back_to_local_for_the_rest_of_the_run(
    monkeypatch, caplog,
):
    clock = _Clock()
    sleeps = _patch_time(monkeypatch, clock)
    ledger = _Ledger(clock)
    ledger.fail_on = {"insert"}
    lim = LedgerRateLimiter("x", 2.0, lease_n=5, connect=ledger.connect)
    with caplog.at_level(logging.WARNING, logger="scraper.rate_ledger"):
        for _ in range(3):
            lim.acquire()
    # One warning, one connection attempt — then the DB is never touched again.
    assert sum(
        "ledger unavailable" in r.getMessage() for r in caplog.records) == 1
    assert ledger.connects == 1
    assert len(ledger.executed) == 1
    # Local pacing still holds the portal's rate (politeness never lapses).
    assert sleeps == [0.5, 1.0]


def test_connect_failure_falls_back_too(monkeypatch):
    clock = _Clock()
    sleeps = _patch_time(monkeypatch, clock)

    def _connect() -> Any:
        raise RuntimeError("pooler down")

    lim = LedgerRateLimiter("x", 4.0, lease_n=5, connect=_connect)
    for _ in range(3):
        lim.acquire()
    assert sleeps == [0.25, 0.5]


def test_penalize_db_failure_falls_back_and_still_penalizes_locally(monkeypatch):
    clock = _Clock()
    _patch_time(monkeypatch, clock)
    ledger = _Ledger(clock)
    lim = LedgerRateLimiter("x", 1.0, lease_n=2, connect=ledger.connect)
    lim.acquire()
    ledger.fail_on = {"penalize"}
    lim.penalize()
    # Fallback restored the local adaptive baseline AND applied the penalty.
    assert lim.interval == pytest.approx(2.0)
    prev_statements = len(ledger.executed)
    lim.acquire()  # decays 2.0 -> 1.8 toward the restored local base, no DB
    assert len(ledger.executed) == prev_statements  # no lease attempts anymore
    lim.penalize()
    assert lim.interval == pytest.approx(3.6)  # local adaptive widening works


# --- factory + runner wiring ---

def test_factory_off_yields_plain_rate_limiter():
    lim = build_rate_limiter("x", 2.0, False)
    assert type(lim) is RateLimiter


def test_factory_on_yields_ledger_limiter_without_touching_db():
    lim = build_rate_limiter("x", 2.0, True)
    assert isinstance(lim, LedgerRateLimiter)
    assert lim._conn is None  # connection is lazy — construction is DB-free


def test_factory_validates_lease_n():
    with pytest.raises(ValueError):
        LedgerRateLimiter("x", 1.0, lease_n=0, connect=lambda: None)


class _WiringPortal:
    source = "fake"
    index_rate = 1.5
    supports_complete_walk = False

    def categories(self) -> list[Any]:
        return []

    def make_client(self, limiter: Any) -> Any:
        return object()

    def connect_index(self) -> Any:
        class _C:
            def __enter__(self) -> "_C":
                return self

            def __exit__(self, *exc: Any) -> None:
                return None

        return _C()

    def claimable_count(self, conn: Any) -> int:
        return 0


def test_runner_builds_limiter_through_the_factory(monkeypatch):
    calls: list[tuple[str, float, bool]] = []

    def _recorder(source: str, rate: float, shared: bool, **kw: Any) -> RateLimiter:
        calls.append((source, rate, shared))
        return RateLimiter(rate)

    monkeypatch.setattr(portal_runner, "build_rate_limiter", _recorder)
    portal = _WiringPortal()
    portal_runner.run_index_walk(portal, dry_run=True)
    portal_runner.run_detail_drain(portal, 0, True, 1, 2.5)
    assert calls == [("fake", 1.5, False), ("fake", 2.5, False)]

    calls.clear()
    portal.shared_rate_limiter = True  # what each *_main sets from PortalLimits
    portal_runner.run_index_walk(portal, dry_run=True)
    assert calls == [("fake", 1.5, True)]


def test_default_lease_n_amortizes_the_round_trip():
    assert DEFAULT_LEASE_N == 20
