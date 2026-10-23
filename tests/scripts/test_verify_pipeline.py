"""Hermetic tests for scripts.verify_pipeline — the registry and the harness that measures it.

No DB: here we test the registry metadata, due selection, the registry projection, the pass's
budget (deferral) and memo, the unmeasured paths, persistence, and migration_drift's cells. Each
check's own rules are tested beside its module (tests/scripts/test_checks_*.py).
"""

from __future__ import annotations

import contextlib
from collections import Counter
import datetime as _dt
import json
import re
import pytest
from typing import Any

from pathlib import Path

from scripts.checks import clock
from scripts.checks import platform as platform_checks
from scripts.checks.platform import check_migration_drift
from scripts.migration_objects import MIGRATION_OBJECT_PROBE_SQL, SAFE_IDENT, load_migrations
from scripts.verify_pipeline import (
    CHECKS,
    THRESHOLDS,
    Check,
    run_checks,
)

T = THRESHOLDS
_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _registered(*keys: str) -> list[Check]:
    """The registered checks with these keys, in registry order."""
    return [c for c in CHECKS if c.key in keys]


def _by_key(key: str) -> Check:
    (check,) = _registered(key)
    return check


def _keys() -> list[str]:
    return [c.key for c in CHECKS]


def _hosted(host: str) -> list[str]:
    """The keys this host measures, in registry order."""
    return [c.key for c in CHECKS if c.host == host]


# The keys the worker's health lane measures (waves 3-6), in registry order; GitHub keeps two.
_WORKER_KEYS = ["location_town_coverage", "jobs", "capacity", "llm", "scraping", "publishing",
                "delisting", "coverage", "dedup", "notifications", "parsing", "data_quality",
                "text_extraction"]
_GITHUB_KEYS = ["migration_drift", "outbound_url_conformance"]


def _stub(key: str, fn: Any, *, budget_s: float = 45.0, cls: str = "alarm",
          host: str = "github-actions", cadence_s: int = 3600) -> Check:
    """An unregistered check for exercising the harness itself."""
    return Check(key, fn, "platform", cls, host, cadence_s, key, budget_s=budget_s)


def _verdict(key: str, status: str = "ok", value: Any = 0, **extra: Any) -> dict[str, Any]:
    """A measured result as every check returns one: its status rides on a judged cell."""
    cell = {"subject": key, "portal": None, "status": status, "value": value, "unit": "",
            "message": f"{key} {status}", "facts": {}}
    return {"check_key": key, "status": status, "value": value, "details": {"cells": [cell]},
            **extra}


# --- run_checks isolation --------------------------------------------------


def test_one_erroring_check_is_unknown_and_does_not_abort_the_run() -> None:
    """A check that raised measured nothing: unknown with the error kept, never a fail
    that reads as a measured Problem, and the lane goes on to the next check."""

    def ok_check(conn: Any, thresholds: Any) -> dict[str, Any]:
        return _verdict("ok_one", value=1)

    def boom_check(conn: Any, thresholds: Any) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    results = run_checks(conn=None, thresholds={},
                         checks=[_stub("ok_one", ok_check), _stub("boom", boom_check)])
    by_key = {r["check_key"]: r for r in results}
    assert by_key["ok_one"]["status"] == "ok"
    assert by_key["boom"]["status"] == "unknown"
    assert by_key["boom"]["details"]["errored"] is True
    assert by_key["boom"]["details"]["cells"] == [], "an unmeasured result carries no cell"
    assert "kaboom" in by_key["boom"]["details"]["error"]
    assert "boom" in by_key["boom"]["message"] and "kaboom" in by_key["boom"]["message"]


def test_run_checks_runs_exactly_the_checks_it_is_handed_in_order() -> None:
    def _one(key: str) -> Check:
        return _stub(key, lambda c, t: _verdict(key))

    assert run_checks(None, {}, checks=[]) == []
    out = run_checks(None, {}, checks=[_one("b"), _one("a")])
    assert [r["check_key"] for r in out] == ["b", "a"]


def test_every_result_carries_the_time_its_check_took() -> None:
    import time

    def _slow(conn: Any, thresholds: Any) -> dict[str, Any]:
        time.sleep(0.02)
        return _verdict("slow")

    def _boom(conn: Any, thresholds: Any) -> dict[str, Any]:
        raise RuntimeError("x")

    slow, boom = run_checks(None, {}, checks=[_stub("slow", _slow), _stub("boom", _boom)])
    assert isinstance(slow["details"]["elapsed_ms"], int)
    assert slow["details"]["elapsed_ms"] >= 20
    assert isinstance(boom["details"]["elapsed_ms"], int)


# --- W0.4: a lane degrades gracefully -------------------------------------


def test_results_are_persisted_as_each_check_completes() -> None:
    """The W0.4 fix: a job killed mid-lane must keep the results it already computed.
    Before this, run_checks computed ALL results before persisting ANY, so a 5-min
    timeout wrote zero rows."""
    persisted: list[str] = []

    def slow_then_boom(conn: Any, thresholds: Any) -> dict[str, Any]:
        raise RuntimeError("the lane died here")

    checks = [
        _stub("first", lambda c, t: _verdict("first", value=1)),
        _stub("second", lambda c, t: _verdict("second", value=2)),
        _stub("third", slow_then_boom),
    ]
    results = run_checks(None, {}, checks=checks,
                         on_result=lambda r: persisted.append(r["check_key"]))
    # Every result reached the sink at the moment it completed, in order.
    assert persisted == ["first", "second", "third"]
    assert [r["status"] for r in results] == ["ok", "ok", "unknown"]


def test_a_failing_sink_does_not_end_the_lane() -> None:
    checks = [
        _stub("a", lambda c, t: _verdict("a", value=1)),
        _stub("b", lambda c, t: _verdict("b", value=1)),
    ]

    def _explode(_r: dict[str, Any]) -> None:
        raise RuntimeError("insert failed")

    results = run_checks(None, {}, checks=checks, on_result=_explode)
    assert [r["check_key"] for r in results] == ["a", "b"]


def test_a_timed_out_check_is_unknown_rather_than_failing() -> None:
    """"I could not measure this" is a different claim from "this is broken". Under DB
    pressure a fail-on-timeout rule manufactures a wall of false reds at exactly the
    moment the operator needs to read the real one."""

    def _cancelled(conn: Any, thresholds: Any) -> dict[str, Any]:
        raise RuntimeError("canceling statement due to statement timeout")

    (result,) = run_checks(None, {}, checks=[_stub("slow", _cancelled)])
    assert result["status"] == "unknown"
    assert result["details"]["timed_out"] is True
    assert result["details"]["cells"] == []
    assert result["details"]["budget_seconds"] == 45.0
    assert result["details"]["elapsed_seconds"] >= 0
    assert "Not measured" in result["message"]


class _Clock:
    """A monotonic clock the test moves by hand, and every read moves on by `step`, as a real
    clock moves between two reads: on a frozen one a lone due check fit a lane of its own budget
    while the worker deferred it on every tick (wave 6). `step=0.0` only for pins to the ms."""

    def __init__(self, step: float = 1e-6) -> None:
        self.now = 1_000.0
        self.step = step

    def monotonic(self) -> float:
        self.now += self.step
        return self.now


def _on_the_clock(monkeypatch: Any, *, step: float = 1e-6) -> _Clock:
    """Point the harness's wall clock at a fake one."""
    import types

    import scripts.verify_pipeline as vp

    clock = _Clock(step)
    from scripts.checks import clock as lane_clock

    fake = types.SimpleNamespace(monotonic=clock.monotonic)
    monkeypatch.setattr(vp, "_time", fake)
    monkeypatch.setattr(lane_clock, "_time", fake)  # the lane's deadlines live in the clock module
    return clock


class _TimedConn:
    """Each statement takes its scripted seconds on the fake clock and, like Postgres, is
    cancelled once they pass the SET LOCAL statement_timeout armed before it."""

    def __init__(self, clock: _Clock, seconds: list[float]) -> None:
        self.clock = clock
        self.seconds = list(seconds)
        self.timeouts_ms: list[int] = []
        self.ran: list[str] = []

    def transaction(self) -> "_TimedConn":
        return self

    def cursor(self) -> "_TimedConn":
        return self

    def __enter__(self) -> "_TimedConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        import psycopg

        armed = re.fullmatch(r"SET LOCAL statement_timeout = '(\d+)ms'", sql)
        if armed:
            self.timeouts_ms.append(int(armed.group(1)))
            return
        took, limit = self.seconds.pop(0), self.timeouts_ms[-1] / 1000.0
        if took > limit:
            self.clock.now += limit
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
        self.clock.now += took
        self.ran.append(sql)

    def fetchall(self) -> list[Any]:
        return []


def _statements(key: str, n: int, *, budget_s: float = 45.0, cls: str = "alarm") -> Check:
    """A check that sends `n` statements through the clock's bounded helper."""

    def _fn(conn: Any, thresholds: Any) -> dict[str, Any]:
        for i in range(n):
            clock.fetchall(conn, f"select {i} -- {key}")
        return _verdict(key, value=n)

    return _stub(key, _fn, budget_s=budget_s, cls=cls)


def test_a_check_spends_its_budget_once_across_all_its_statements(monkeypatch: Any) -> None:
    """One fixed timeout per statement let a check of N statements take N budgets
    (dual_write_parity ran p50 57 s on 45 s). Each statement now gets what the check has
    LEFT, never under 1 s (0 would switch the timeout off): the first of three statements
    takes 1.5 of a 2 s budget, so the second gets the 1 s floor, is cancelled there, and the
    check stops 0.5 s past its budget instead of running on to the third."""
    import scripts.verify_pipeline as vp

    conn = _TimedConn(_on_the_clock(monkeypatch, step=0.0), [1.5, 1.5, 0.1])
    checks = [_statements("three", 3, budget_s=2.0)]
    stored: list[dict[str, Any]] = []
    (result,) = run_checks(conn, {}, checks=checks, on_result=stored.append,
                           lane_budget_s=vp.lane_budget_for(checks))
    assert conn.timeouts_ms == [2000, 1000]
    assert conn.ran == ["select 0 -- three"]
    assert result["status"] == "unknown" and result["details"]["timed_out"] is True
    assert result["details"]["budget_seconds"] == 2.0
    assert result["details"]["elapsed_seconds"] == 2.5  # >= the 1.5 s it had spent
    assert stored == [result], "a timed-out check is stored: it ran and measured nothing"


def test_an_uncapped_lane_still_caps_a_late_checks_deadline_at_its_remainder(
    monkeypatch: Any,
) -> None:
    """The canary's lane (no pass cap) keeps wave 2's rule: a check is owed its own budget only
    while the lane still has it, so the second check below, declared 45 s, starts with 2 s."""
    conn = _TimedConn(_on_the_clock(monkeypatch, step=0.0), [2.0, 0.5])
    out = run_checks(conn, {}, checks=[_statements("first", 1), _statements("second", 1)],
                     lane_budget_s=4.0)
    assert conn.timeouts_ms == [4000, 2000]
    assert [r["status"] for r in out] == ["ok", "ok"]


def test_a_capped_pass_defers_a_check_whose_budget_no_longer_fits(
    monkeypatch: Any, caplog: Any,
) -> None:
    """Wave 6: on the worker's capped pass a check started short timed out into an unknown row
    that held a 6-hourly gauge Not measured for a whole cadence. It is not started at all: no
    row, so it stays due for the next pass, and a later check that still fits runs."""
    import logging

    caplog.set_level(logging.WARNING, logger="verify_pipeline")
    conn = _TimedConn(_on_the_clock(monkeypatch), [1.9, 0.5])
    checks = [_statements("first", 1, budget_s=2.0), _statements("second", 1, budget_s=4.0),
              _statements("third", 1, budget_s=1.0)]
    stored: list[str] = []
    out = run_checks(conn, {}, checks=checks, lane_budget_s=4.0, defer_unfit=True,
                     on_result=lambda r: stored.append(r["check_key"]))
    assert [r["check_key"] for r in out] == stored == ["first", "third"]
    assert [r["status"] for r in out] == ["ok", "ok"]
    assert conn.timeouts_ms == pytest.approx([2000, 1000], abs=1), (
        "the check that runs gets its whole budget")
    assert "DEFER second: its 4s budget does not fit the 2s the pass has left" in caplog.text


@pytest.mark.parametrize("budget_s, runs", [(45.0, True), (45.5, True), (46.5, False)])
def test_a_check_fits_while_its_budget_is_within_a_second_of_what_the_pass_has_left(
    monkeypatch: Any, budget_s: float, runs: bool,
) -> None:
    """The fit is weighed before the check starts, with 1 s of slack: a check whose budget is
    its whole lane fits though the lane's clock has moved, and 1.5 s over what is left does not."""
    _on_the_clock(monkeypatch)
    out = run_checks(None, {}, checks=[_stub("one", lambda c, t: _verdict("one"),
                                             budget_s=budget_s)],
                     lane_budget_s=45.0, defer_unfit=True)
    assert [r["check_key"] for r in out] == (["one"] if runs else [])


def test_a_budget_exhausted_lane_writes_no_row_for_the_checks_it_did_not_reach(
    monkeypatch: Any, caplog: Any,
) -> None:
    """A check the lane never reached measured nothing and stores nothing, so it stays due
    and the next run (on the worker, the next minute) measures it. Its old `not_run` row
    made it look measured and pushed its next measurement a whole cadence out."""
    import logging

    clock = _on_the_clock(monkeypatch)

    def _spends(key: str, seconds: float) -> Check:
        def _fn(conn: Any, thresholds: Any) -> dict[str, Any]:
            clock.now += seconds  # Python-side time no statement_timeout can cut short
            return _verdict(key, value=1)
        return _stub(key, _fn, budget_s=2.0)

    caplog.set_level(logging.WARNING, logger="verify_pipeline")
    stored: list[str] = []
    checks = [_spends("first", 6.5), _spends("second", 0.1), _spends("third", 0.1)]
    results = run_checks(None, {}, checks=checks, lane_budget_s=6.0,
                         on_result=lambda r: stored.append(r["check_key"]))
    assert [r["check_key"] for r in results] == stored == ["first"]
    assert "not reached, so still due: second, third" in caplog.text
    # A budget already spent by the time the loop starts reaches nothing at all.
    assert run_checks(None, {}, checks=checks, lane_budget_s=-1.0,
                      on_result=lambda r: stored.append(r["check_key"])) == []
    assert stored == ["first"]


def test_lane_budget_is_reset_after_a_run() -> None:
    """State leaking between runs would silently shrink a later lane's statement
    timeout to whatever the previous run had left."""
    run_checks(None, {}, checks=[_stub("a", lambda c, t: _verdict("a"))], lane_budget_s=5.0)
    assert clock.LANE.deadline is None and clock.LANE.check_deadline is None
    assert clock.LANE.statement_timeout_ms() == clock.DEFAULT_STATEMENT_TIMEOUT_MS


def _sharing(key: str, reads: list[str], *, fails: bool = False) -> Check:
    """A check that takes the shared measure read through the pass memo, as parsing and
    data_quality do."""

    def _read() -> list[tuple[str, int]]:
        reads.append(key)
        if fails:
            raise RuntimeError("relation measure_plausibility_by_source does not exist")
        return [("sreality", 7)]

    def _fn(conn: Any, thresholds: Any) -> dict[str, Any]:
        rows = clock.PASS.memo("measure_plausibility", _read)
        return _verdict(key, value=len(rows))

    return _stub(key, _fn)


def test_one_pass_shares_its_memoised_read_and_the_next_pass_reads_again() -> None:
    """Design § 6 (wave 6): the ~12 s measure read parsing and data_quality both need is made
    once per pass, never kept across passes, and only inside one (a direct call reads)."""
    reads: list[str] = []
    checks = [_sharing("parsing", reads), _sharing("data_quality", reads)]
    first = run_checks(None, {}, checks=checks)
    assert [r["status"] for r in first] == ["ok", "ok"] and reads == ["parsing"]
    run_checks(None, {}, checks=checks)
    assert reads == ["parsing", "parsing"], "two passes rebuild the memo"
    checks[1].fn(None, {})
    checks[1].fn(None, {})
    assert reads == ["parsing", "parsing", "data_quality", "data_quality"]


def test_a_memoised_read_that_failed_fails_every_reader_of_the_pass_once() -> None:
    """The error is the read's result for the pass: the second check gets the same error
    instead of paying the read again, and both read Not measured with the reason."""
    reads: list[str] = []
    out = run_checks(None, {}, checks=[_sharing("parsing", reads, fails=True),
                                       _sharing("data_quality", reads, fails=True)])
    assert reads == ["parsing"]
    assert [r["status"] for r in out] == ["unknown", "unknown"]
    assert all("measure_plausibility_by_source" in r["details"]["error"] for r in out)


def test_each_check_runs_under_its_own_budget(monkeypatch: Any) -> None:
    """The outbound probe gets 120 s, the database checks 45 s: the statement timeout a
    check runs under is ITS budget, not one lane-wide number."""
    import scripts.verify_pipeline as vp

    _on_the_clock(monkeypatch, step=0.0)
    seen: dict[str, str] = {}

    def _peek(key: str) -> Any:
        def _fn(conn: Any, thresholds: Any) -> dict[str, Any]:
            seen[key] = clock.statement_timeout_clause()
            return _verdict(key)
        return _fn

    checks = [_stub("db", _peek("db")), _stub("probe", _peek("probe"), budget_s=120.0)]
    run_checks(None, {}, checks=checks, lane_budget_s=vp.lane_budget_for(checks))
    assert seen == {"db": "45000ms", "probe": "120000ms"}
    # Wave 6: parsing reads the active stock several ways on four default budgets; W4's push ledger
    # reads a few hundred rows on 5 s.
    assert {c.key: c.budget_s for c in CHECKS if c.budget_s != clock.CHECK_BUDGET_S} == {
        "parsing": 180.0, "outbound_url_conformance": 120.0, "notifications": 5.0}


# --- the registry ------------------------------------------------------------


def test_every_registered_check_is_declared_once_with_its_metadata() -> None:
    from scripts import verify_pipeline as vp

    keys = _keys()
    assert len(keys) == len(set(keys)) == 15
    # Waves 3-6: the worker hosts the platform, portal, dedup, parsing and quality checks; GitHub
    # keeps for good what needs a checkout (migration_drift) or an address that is not the
    # scrapers' (the outbound probe).
    assert _hosted("realtime-worker") == _WORKER_KEYS
    assert _hosted("github-actions") == _GITHUB_KEYS and not _hosted("pg_cron")
    # Wave 8: the page's four sections, in its order, and none without a check.
    assert vp.DOMAINS == ("portals", "dedup", "data_quality", "platform")
    assert {c.domain for c in CHECKS} == set(vp.DOMAINS)
    for c in CHECKS:
        assert c.label.strip(), c.key
        assert c.cls in vp.CLASSES and c.cls in {"alarm", "gauge"}, c.key
        assert c.domain in vp.DOMAINS, c.key
        assert c.host in vp.HOSTS, c.key
        assert isinstance(c.cadence_s, int) and c.cadence_s > 0, c.key
        assert c.budget_s > 0, c.key
        assert c.owner_path == "" or c.owner_path.startswith("/"), c.key
        # a body wired under the wrong key would store its rows under another check's name
        assert c.fn.__name__ == f"check_{c.key}", c.key


def test_every_check_carries_its_page_section_and_owner_page() -> None:
    """Wave 8 (D1): the page groups rows by domain (the portal matrix: the four portal checks and
    parsing; the engine; data quality; the platform) and links each to the page that owns the
    fix, '' for none. tests/test_spa_route_contract.py holds every path to an SPA route."""
    assert {c.key: (c.domain, c.owner_path) for c in CHECKS} == {
        "location_town_coverage": ("data_quality", "/location-quality"),
        "jobs": ("platform", "/settings"),
        "capacity": ("platform", ""),
        "llm": ("platform", "/costs"),
        "scraping": ("portals", "/scrapers"),
        "publishing": ("portals", "/scrapers"),
        "delisting": ("portals", "/scrapers"),
        "coverage": ("portals", "/scrapers"),
        "dedup": ("dedup", "/autodedup/progress"),
        "notifications": ("platform", "/notifications"),
        "parsing": ("portals", "/scrapers"),
        "data_quality": ("data_quality", "/scrapers"),
        "text_extraction": ("data_quality", "/costs"),
        "migration_drift": ("platform", ""),
        "outbound_url_conformance": ("data_quality", ""),
    }


def test_the_dedup_check_keeps_the_contracts_metadata() -> None:
    """Wave 5 (D1): an alarm every 10 min on the worker, labelled Engine, after coverage on the
    page, on the 45 s database budget; stale after 30 min, due after 540 s, a recovery hold of
    1,800 s."""
    from scripts.checks.dedup import check_dedup
    from toolkit.system_alerts import _hold

    c = _by_key("dedup")
    assert (c.fn, c.domain, c.cls, c.host, c.cadence_s, c.label, c.unit, c.owner_path) == (
        check_dedup, "dedup", "alarm", "realtime-worker", 600, "Engine", "cells",
        "/autodedup/progress")
    assert c.budget_s == clock.CHECK_BUDGET_S == 45.0
    assert _keys().index("dedup") == _keys().index("coverage") + 1
    assert (c.staleness_budget, c.due_after_s) == (_dt.timedelta(minutes=30), 540)
    assert _hold(c.cadence_s) == _dt.timedelta(seconds=1_800)


def test_the_wave6_checks_carry_the_contracts_metadata() -> None:
    """Wave 6 (D1): parsing is a portal alarm and the three quality checks gauges, all on the
    worker (location_town_coverage moved there from GitHub); the URL probe's page section is
    data quality now. Every one emits cells."""
    from scripts.checks import quality
    from scripts.verify_outbound_urls import PROBE_BUDGET_S, check_outbound_url_conformance

    def meta(key: str) -> tuple[Any, ...]:
        c = _by_key(key)
        return (c.fn, c.domain, c.cls, c.host, c.cadence_s, c.budget_s, c.label, c.unit,
                c.owner_path)

    worker, github = "realtime-worker", "github-actions"
    assert meta("parsing") == (quality.check_parsing, "portals", "alarm", worker, 21_600, 180.0,
                               "Parsing", "cells", "/scrapers")
    # data_quality's owner is the per-portal page: no page renders value quality yet.
    assert meta("data_quality") == (quality.check_data_quality, "data_quality", "gauge", worker,
                                    21_600, 45.0, "Data quality", "cells", "/scrapers")
    assert meta("location_town_coverage") == (
        quality.check_location_town_coverage, "data_quality", "gauge", worker, 21_600, 45.0,
        "Location coverage", "cells", "/location-quality")
    assert meta("text_extraction") == (quality.check_text_extraction, "data_quality", "gauge",
                                       worker, 3_600, 45.0, "Description reading", "cells",
                                       "/costs")
    assert meta("migration_drift") == (check_migration_drift, "platform", "alarm", github,
                                       3_600, 45.0, "Migration drift", "files", "")
    assert meta("outbound_url_conformance") == (
        check_outbound_url_conformance, "data_quality", "alarm", github, 604_800,
        PROBE_BUDGET_S, "Portal links open the right page", "cells", "")
    # parsing (6-hourly): stale after 18 h, due after 5 h 45 min, recovered after 11.5 h of OK.
    from toolkit.system_alerts import _hold

    parsing, text = _by_key("parsing"), _by_key("text_extraction")
    assert (parsing.staleness_budget, parsing.due_after_s) == (_dt.timedelta(hours=18), 20_700)
    assert _hold(parsing.cadence_s) == _dt.timedelta(hours=11, minutes=30)
    assert (text.staleness_budget, text.due_after_s) == (_dt.timedelta(hours=3), 3_240)


def test_every_worker_alarm_due_at_once_fits_the_pass_and_gauges_defer() -> None:
    """Wave 6 (D2): the thirteen worker budgets sum 680 s, past what one pass may take inside the
    watcher's 600 s in-flight line, so the pass is capped at 500 s (the worker pins the
    constant): every ALARM due at once fits (7 x 45 s + parsing's 180 s + notifications' 5 s =
    500 s, alarms run first) and a gauge that no longer fits is deferred to the next 60 s tick.
    GitHub's two checks owe 165 s."""
    from scripts import verify_pipeline as vp

    worker = [c for c in CHECKS if c.host == "realtime-worker"]
    alarms = [c for c in worker if c.cls == "alarm"]
    assert (vp.lane_budget_for(alarms), vp.lane_budget_for(worker)) == (500.0, 680.0)
    assert vp.lane_budget_for(alarms) <= 500.0 < vp.lane_budget_for(worker)
    assert vp.lane_budget_for([c for c in CHECKS if c.host == "github-actions"]) == 165.0


def test_the_first_projection_from_main_retires_the_nine_quality_keys() -> None:
    """Wave 6 (D15): parsing and the data_quality and text_extraction gauges replace eight legacy
    keys, and floor_convention goes with no successor (Q4). Their GitHub rows go at the canary's
    first projection from main, the ladder closes their open incidents as retired with no notice,
    and the worker projects the new keys on its first health pass. location_town_coverage keeps
    its key: the canary's projection leaves its GitHub row be (its retire spares every key still
    registered), and the worker's re-hosts it and restarts its declared_at."""
    from scripts import verify_pipeline as vp

    legacy = ("ppm2_median_shift", "ppm2_basis_floor_share", "ppm2_measure_coverage",
              "area_vs_usable_divergence", "field_fill_matrix", "outbound_url_coverage",
              "location_payload_shape_drift", "text_extraction_lag", "floor_convention")
    assert not set(legacy) & set(_keys())
    then = _NOW - _dt.timedelta(days=30)
    conn = _HealthChecksConn({key: {"host": "github-actions", "cadence_s": 21_600,
                                    "declared_at": then}
                              for key in (*legacy, "location_town_coverage")})
    assert vp.project_registry(conn, "github-actions", _NOW)["deleted"] == 9
    assert not set(legacy) & set(conn.table)
    assert conn.table["location_town_coverage"]["host"] == "github-actions"
    assert vp.project_registry(conn, "realtime-worker", _NOW) == {
        "upserted": len(_WORKER_KEYS), "deleted": 0}
    moved = conn.table["location_town_coverage"]
    assert (moved["host"], moved["declared_at"]) == ("realtime-worker", _NOW)
    assert {"parsing", "data_quality", "text_extraction"} <= set(conn.table)


def test_wave6_deleted_the_legacy_bodies_their_thresholds_and_shared_state() -> None:
    """What the nine retired keys took with them (D15): their bodies, the SQL mirror of a
    portal's payload reader and its remedies (rule 21: portal names in shared code), the
    plausibility cache (the pass memo replaces it), the registry constants (THRESHOLDS keys now)
    and every threshold only they read."""
    from scraper import field_census
    from scripts import verify_pipeline as vp

    for name in ("check_ppm2_median_shift", "check_ppm2_basis_floor_share",
                 "check_ppm2_measure_coverage", "check_area_vs_usable_divergence",
                 "check_field_fill_matrix", "check_outbound_url_coverage",
                 "check_location_payload_shape_drift", "check_text_extraction_lag",
                 "check_floor_convention", "SREALITY_SHAPE_CASE_SQL", "_LOCATION_SHAPE_REMEDY",
                 "_PLAUSIBILITY_CACHE", "REGISTRY_MAX_AGE_DAYS", "REGISTRY_MAX_STAGED_HOURS",
                 "_FLOOR_CONVENTION_SQL", "_LOCATION_TOWN_COVERAGE_SQL"):
        assert not hasattr(vp, name), name
    for name in ("FILL_DROP_WARN_PP", "FILL_DROP_FAIL_PP", "FILL_DROP_MIN_BASELINE",
                 "FILL_COLLAPSE_RATIO", "FILL_COLLAPSE_MIN_ROWS", "OFF_CANON_RISE_WARN_PP",
                 "OFF_CANON_RISE_FAIL_PP", "CENSUS_STALE_DAYS"):
        assert not hasattr(field_census, name), f"{name} is a THRESHOLDS key now"
    retired = ("text_extraction_lag_", "outbound_url_null_", "ppm2_", "area_divergence_",
               "estate_sum_", "location_payload_shape_drift_", "floor_convention_")
    assert not [k for k in THRESHOLDS if k.startswith(retired)]


def test_the_worker_heartbeat_is_the_watchers_to_judge_not_a_checks() -> None:
    """Wave 2: the pg_cron watcher's `worker` cell (migration 620) reads the heartbeat every
    5 min; the GitHub-hosted check sampled it every ~4.9 h, so it caught an outage of length
    D with a probability of about D / 4.9 h. The three hosts stay as they are."""
    from scripts import verify_pipeline as vp

    assert "worker_liveness" not in _keys()
    assert "worker_stale_fail_minutes" not in THRESHOLDS
    assert not hasattr(vp, "check_worker_liveness")
    assert vp.HOSTS == ("realtime-worker", "github-actions", "pg_cron")


_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def test_every_threshold_a_check_reads_is_declared_and_every_declared_one_is_read() -> None:
    """THRESHOLDS is the one home, with no override (wave 3): a key a check reads that it
    lacks is a KeyError, so that check errors into Not measured on every run, and a key no
    check names is a basis nobody is judged by (some are named in a tuple, then read)."""
    head, _, rest = (_SCRIPTS / "verify_pipeline.py").read_text().partition(
        "THRESHOLDS: dict[str, Any] = {")
    _declarations, _, tail = rest.partition("\n}\n")
    text = "\n".join([head, tail, (_SCRIPTS / "verify_outbound_urls.py").read_text(),
                      *(path.read_text() for path in sorted((_SCRIPTS / "checks").glob("*.py")))])
    read = set(re.findall(
        r"""(?:thresholds|THRESHOLDS)(?:\[|\.get\()\s*["']([A-Za-z0-9_]+)["']""", text))
    assert sorted(read - set(THRESHOLDS)) == [], "read but not declared"
    named = {key for key in THRESHOLDS if re.search(rf"""["']{key}["']""", text)}
    assert sorted(set(THRESHOLDS) - named) == [], "declared but named by no check"


def test_the_capacity_reference_is_blessed_at_most_90_days_back() -> None:
    """Design § 4: the sticky reference is a blessed date at most 90 d back, so this rail goes red
    the day it expires and forces the dated re-blessing PR (the lowest weekly p50 since an old
    date judges today's jobs against a workload and an instance they no longer have)."""
    blessed = THRESHOLDS["CAPACITY_REFERENCE_FROM"]
    today = _dt.datetime.now(_dt.timezone.utc).date()
    assert isinstance(blessed, _dt.date) and not isinstance(blessed, _dt.datetime)
    assert blessed <= today, f"CAPACITY_REFERENCE_FROM {blessed} is in the future"
    assert (today - blessed).days <= 90, (
        f"CAPACITY_REFERENCE_FROM {blessed} is {(today - blessed).days} days back, past 90: "
        "re-bless it to a measured quiet week's first day in a dated PR")


def test_the_gauges_are_the_ruled_standing_conditions_and_the_rest_are_alarms() -> None:
    """Q2 (2026-10-18): these are standing conditions, read as gauges (wave 4's coverage in
    walk_coverage's place; wave 6's data_quality, location and description-reading gauges);
    every other key — parsing, the regression detector, included — stays an alarm."""
    assert {c.key for c in CHECKS if c.cls == "gauge"} == {
        "location_town_coverage", "coverage", "data_quality", "text_extraction"}


def test_each_check_keeps_the_cadence_of_the_lane_it_ran_in() -> None:
    """The worker's alarms run every 10 minutes, capacity every 30, the coverage and description
    reading gauges hourly, parsing and the location and data-quality gauges 6-hourly; on GitHub
    migration_drift runs hourly and the URL probe weekly."""
    assert {k: _by_key(k).cadence_s for k in _WORKER_KEYS} == {
        "location_town_coverage": 21_600, "jobs": 600, "capacity": 1_800, "llm": 600,
        "scraping": 600, "publishing": 600, "delisting": 600, "coverage": 3_600, "dedup": 600,
        "notifications": 600, "parsing": 21_600, "data_quality": 21_600, "text_extraction": 3_600}
    assert {k: _by_key(k).cadence_s for k in _GITHUB_KEYS} == {
        "migration_drift": 3_600, "outbound_url_conformance": 604_800}


def test_staleness_budget_is_three_cadences_or_the_cadence_plus_githubs_delay() -> None:
    """GitHub delivers a cron up to ~13 h late, so its keys get cadence + 18 h; the worker
    and pg_cron keep time and get three cadences."""
    assert _stub("a", None, host="github-actions", cadence_s=3_600).staleness_budget \
        == _dt.timedelta(hours=19)
    assert _stub("b", None, host="realtime-worker", cadence_s=600).staleness_budget \
        == _dt.timedelta(minutes=30)
    assert _stub("c", None, host="pg_cron", cadence_s=300).staleness_budget \
        == _dt.timedelta(minutes=15)


# --- due selection -------------------------------------------------------------


class _DueConn:
    """Answers the newest-run_at-per-key read from a {key: run_at} map; a newest row older than
    its key's `declared` stamp is dropped where the statement guards on declared_at."""

    def __init__(self, newest: dict[str, Any],
                 declared: dict[str, _dt.datetime] | None = None) -> None:
        self._newest = newest
        self._declared = declared or {}
        self.executed: list[tuple[str, Any]] = []
        self._rows: list[tuple[Any, ...]] = []

    def cursor(self) -> "_DueConn":
        return self

    def __enter__(self) -> "_DueConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        guarded = "r.run_at >= coalesce(h.declared_at" in " ".join(sql.split())

        def newest(key: str) -> Any:
            at, floor = self._newest.get(key), self._declared.get(key)
            if guarded and at is not None and floor is not None and at < floor:
                return None  # the newest row predates the registration, so every row does
            return at

        self._rows = [(k, newest(k)) for k in params["keys"]]

    def fetchall(self) -> Any:
        return self._rows


_NOW = _dt.datetime(2026, 10, 18, 12, 0, tzinfo=_dt.timezone.utc)


def test_a_check_with_no_result_is_due() -> None:
    from scripts.verify_pipeline import due_checks

    for host in ("github-actions", "realtime-worker"):
        assert [c.key for c in due_checks(_DueConn({}), host, _NOW)] == _hosted(host)


def test_a_check_is_due_once_its_newest_result_is_a_cadence_old() -> None:
    from scripts.verify_pipeline import due_checks

    def ago(**kw: float) -> _dt.datetime:
        return _NOW - _dt.timedelta(**kw)

    conn = _DueConn({
        "migration_drift": ago(minutes=61),   # hourly, over a cadence old: due
        "outbound_url_conformance": ago(days=6),  # weekly: not due
    })
    assert [c.key for c in due_checks(conn, "github-actions", _NOW)] == ["migration_drift"]
    (sql, params), = conn.executed  # one round trip for the whole host
    assert "max(r.run_at)" in sql and "unnest(%(keys)s::text[])" in sql
    assert params["keys"] == _hosted("github-actions")
    worker = _DueConn({
        "capacity": ago(minutes=20),  # 30-minute: not due
        "scraping": ago(minutes=11),  # 10-minute: due
        "coverage": ago(minutes=30),  # hourly, half a cadence old: not due
        "parsing": ago(hours=5),      # 6-hourly: not due
        "data_quality": ago(hours=7),  # 6-hourly: due
    })
    due = [c.key for c in due_checks(worker, "realtime-worker", _NOW)]
    assert not {"capacity", "coverage", "parsing"} & set(due)
    assert {"scraping", "data_quality"} <= set(due)
    assert due == [k for k in _keys() if k in due], "registry order is the run order"
    assert len(due) == len(_hosted("realtime-worker")) - 3


def test_a_check_is_due_a_slack_before_its_cadence_so_an_early_run_still_measures_it() -> None:
    """Every row carries its run's start, so the next hourly run, started a minute earlier in
    its slot, found a 59-minute-old row not due: an hourly check was measured every second
    run. The slack is a tenth of the cadence, at most 15 min."""
    from scripts.verify_pipeline import due_checks

    def due(key: str, **age: float) -> bool:
        conn = _DueConn({key: _NOW - _dt.timedelta(**age)})
        host = _by_key(key).host
        return key in [c.key for c in due_checks(conn, host, _NOW)]

    assert due("migration_drift", minutes=59)
    assert due("migration_drift", minutes=54)   # 60 min less the 6-min slack, inclusive
    assert not due("migration_drift", minutes=50)
    assert due("parsing", hours=5, minutes=45)
    assert not due("parsing", hours=5, minutes=44)
    assert due("outbound_url_conformance", days=7, minutes=-10)
    assert not due("outbound_url_conformance", days=7, minutes=-16)
    assert {c.cadence_s - c.due_after_s for c in CHECKS if c.host == "github-actions"} \
        == {360, 900}
    # The worker's 60 s tick: a 10-minute check is due after 9 minutes, a 30-minute one 27,
    # the hourly gauges 54, the 6-hourly checks 5 h 45 min.
    assert {c.cadence_s - c.due_after_s for c in CHECKS if c.host == "realtime-worker"} \
        == {60, 180, 360, 900}


def test_rows_from_before_a_keys_registration_never_make_it_not_due() -> None:
    """The page and the ladder ignore rows older than declared_at, so a key whose newest row
    predates its registration read Not measured for a whole cadence before the lanes measured
    it: the weekly URL probe, last measured before wave 1, among them."""
    from scripts.verify_pipeline import due_checks

    registered = _NOW - _dt.timedelta(minutes=1)  # project_registry runs first in main()
    conn = _DueConn({"outbound_url_conformance": _NOW - _dt.timedelta(days=2),
                     "migration_drift": _NOW - _dt.timedelta(seconds=30)},
                    declared=dict.fromkeys(("outbound_url_conformance", "migration_drift"),
                                           registered))
    due = [c.key for c in due_checks(conn, "github-actions", _NOW)]
    assert "outbound_url_conformance" in due
    assert "migration_drift" not in due, "a row written since the registration still counts"
    # Wave 6's host move restarts location_town_coverage's declared_at: the worker measures it
    # in the pass that re-hosts it, its GitHub rows notwithstanding.
    moved = _DueConn({"location_town_coverage": _NOW - _dt.timedelta(hours=1)},
                     declared={"location_town_coverage": registered})
    assert "location_town_coverage" in [c.key for c in due_checks(moved, "realtime-worker", _NOW)]
    (sql, _params), = conn.executed
    assert "left join health_checks h on h.key = k.key" in " ".join(sql.split())


def test_due_selection_never_reaches_another_hosts_checks() -> None:
    """The canary never measures the worker's checks and the health lane never GitHub's: each
    host's one read names only its own keys."""
    from scripts.verify_pipeline import due_checks

    hosts = ("github-actions", "realtime-worker")
    for host, other in (hosts, hosts[::-1]):
        conn = _DueConn({})
        due = [c.key for c in due_checks(conn, host, _NOW)]
        assert not set(due) & set(_hosted(other)), host
        assert conn.executed[0][1]["keys"] == _hosted(host), host
    assert due_checks(_DueConn({}), "pg_cron", _NOW) == []


# --- the registry projection -----------------------------------------------------


class _HealthChecksConn:
    """An in-memory health_checks table behind project_registry's three statements."""

    def __init__(self, table: dict[str, dict[str, Any]] | None = None) -> None:
        self.table = {k: dict(v) for k, v in (table or {}).items()}
        self.in_transaction = False
        self.rowcount = -1
        self._rows: list[tuple[Any, ...]] = []

    def transaction(self) -> Any:
        conn = self

        class _Txn:
            def __enter__(self) -> None:
                conn.in_transaction = True

            def __exit__(self, *exc: Any) -> bool:
                conn.in_transaction = False
                return False

        return _Txn()

    def cursor(self) -> "_HealthChecksConn":
        return self

    def __enter__(self) -> "_HealthChecksConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        assert self.in_transaction, "the projection is one transaction"
        head = " ".join(sql.split())
        if head.startswith("select key, host, cadence_s, declared_at from health_checks"):
            self._rows = [(k, r["host"], r["cadence_s"], r["declared_at"])
                          for k, r in self.table.items() if k in params["keys"]]
        elif head.startswith("delete from health_checks where host = %(host)s"):
            gone = [k for k, r in self.table.items()
                    if r["host"] == params["host"] and k not in params["keys"]]
            for k in gone:
                del self.table[k]
            self.rowcount = len(gone)
        else:  # pragma: no cover - a statement the projection never sends
            raise AssertionError(sql)

    def executemany(self, sql: str, rows: list[dict[str, Any]]) -> None:
        assert self.in_transaction and "on conflict (key) do update" in sql
        for row in rows:
            self.table[row["key"]] = dict(row)

    def fetchall(self) -> Any:
        return self._rows


@pytest.mark.parametrize("host", ["github-actions", "realtime-worker"])
def test_projection_inserts_every_check_of_its_host_with_its_metadata(host: str) -> None:
    """Each host projects only its own rows, sorted by their place in the whole registry; a
    GitHub key may age its cadence plus 18 h before it reads stale, a worker key 3 cadences."""
    from scripts.verify_pipeline import project_registry

    conn = _HealthChecksConn()
    assert project_registry(conn, host, _NOW) == {"upserted": len(_hosted(host)), "deleted": 0}
    assert list(conn.table) == _hosted(host)
    for sort, c in enumerate(CHECKS):
        if c.host != host:
            continue
        row = conn.table[c.key]
        assert (row["domain"], row["class"], row["host"], row["cadence_s"]) == (
            c.domain, c.cls, host, c.cadence_s)
        assert (row["label"], row["unit"], row["owner_path"], row["sort"]) == (
            c.label, c.unit, c.owner_path, sort)
        stale_s = c.cadence_s + 18 * 3_600 if host == "github-actions" else 3 * c.cadence_s
        assert row["staleness_budget"] == _dt.timedelta(seconds=stale_s)
        assert row["declared_at"] == _NOW


def test_projection_keeps_declared_at_unless_the_host_or_cadence_changed() -> None:
    from scripts.verify_pipeline import project_registry

    then = _NOW - _dt.timedelta(days=3)
    conn = _HealthChecksConn({
        # wave 6 moved the URL probe's page section (its domain), not its host or cadence
        "outbound_url_conformance": {"host": "github-actions", "cadence_s": 604_800,
                                     "declared_at": then},
        # wave 3 moved migration_drift from 6-hourly to hourly
        "migration_drift": {"host": "github-actions", "cadence_s": 21_600, "declared_at": then},
        # wave 6 moved location_town_coverage from GitHub to the worker
        "location_town_coverage": {"host": "github-actions", "cadence_s": 21_600,
                                   "declared_at": then},
        # wave 8 moved the portal checks' page section to the matrix and gave them an owner page
        "scraping": {"host": "realtime-worker", "cadence_s": 600, "domain": "scraping",
                     "owner_path": "", "declared_at": then},
    })
    project_registry(conn, "github-actions", _NOW)
    assert conn.table["outbound_url_conformance"]["declared_at"] == then  # unchanged: kept
    assert conn.table["outbound_url_conformance"]["domain"] == "data_quality"
    assert conn.table["migration_drift"]["declared_at"] == _NOW           # cadence moved
    project_registry(conn, "realtime-worker", _NOW)
    assert conn.table["location_town_coverage"]["declared_at"] == _NOW    # host moved
    assert conn.table["location_town_coverage"]["host"] == "realtime-worker"
    scraping = conn.table["scraping"]
    assert (scraping["domain"], scraping["owner_path"], scraping["declared_at"]) == (
        "portals", "/scrapers", then)                                     # section moved: kept
    project_registry(conn, "github-actions", _NOW + _dt.timedelta(hours=1))
    assert conn.table["migration_drift"]["declared_at"] == _NOW, "a re-projection is a no-op"


def test_projection_retires_only_its_own_hosts_undeclared_keys() -> None:
    from scripts.verify_pipeline import project_registry

    conn = _HealthChecksConn({
        "outbound_url_parity": {"host": "github-actions", "cadence_s": 604_800,
                                "declared_at": _NOW},
        "health_lane": {"host": "realtime-worker", "cadence_s": 600, "declared_at": _NOW},
        "liveness": {"host": "pg_cron", "cadence_s": 300, "declared_at": _NOW},
    })
    assert project_registry(conn, "github-actions", _NOW)["deleted"] == 1
    assert "outbound_url_parity" not in conn.table
    assert {"health_lane", "liveness"} <= set(conn.table)
    assert project_registry(conn, "realtime-worker", _NOW)["deleted"] == 1
    assert "health_lane" not in conn.table and "liveness" in conn.table
    assert set(_hosted("github-actions")) <= set(conn.table), "the lane leaves GitHub's rows be"


def test_the_first_projection_from_main_retires_the_eight_legacy_keys() -> None:
    """Wave 3's jobs, capacity and llm replace eight GitHub keys. Their rows go at the canary's
    first projection from main, and the ladder then closes their open incidents as retired,
    with no notice (an open llm_errors incident included)."""
    from scripts.verify_pipeline import project_registry

    legacy = ("worker_lane_stall", "broker_resolution_freshness", "workflow_poller_liveness",
              "db_saturation", "long_open_transaction", "llm_errors", "llm_burn_rate",
              "dual_write_parity")
    conn = _HealthChecksConn({key: {"host": "github-actions", "cadence_s": 3_600,
                                    "declared_at": _NOW} for key in legacy})
    assert project_registry(conn, "github-actions", _NOW)["deleted"] == 8
    assert not set(legacy) & set(conn.table)


def test_the_first_projection_from_main_retires_the_seven_portal_keys() -> None:
    """Wave 4's scraping, publishing, delisting and coverage replace seven GitHub keys. Their rows
    go at the canary's first projection from main, the ladder closes any incident of theirs as
    retired, with no notice, and the worker projects the four on its first health pass."""
    from scripts.verify_pipeline import project_registry

    legacy = ("acquisition_lag", "property_maintenance", "walk_coverage", "walk_end_streak",
              "delist_flip_refused", "false_delist_share", "sreality_image_template")
    assert not set(legacy) & set(_keys())
    conn = _HealthChecksConn({key: {"host": "github-actions", "cadence_s": 21_600,
                                    "declared_at": _NOW} for key in legacy})
    assert project_registry(conn, "github-actions", _NOW)["deleted"] == 7
    assert not set(legacy) & set(conn.table)
    assert project_registry(conn, "realtime-worker", _NOW) == {
        "upserted": len(_WORKER_KEYS), "deleted": 0}
    assert {"scraping", "publishing", "delisting", "coverage"} <= set(conn.table)


def test_the_first_projection_from_main_retires_the_broker_rulings_key() -> None:
    """Wave 5's dedup check judges the broker suppressions as its rulings/brokers arm, every 10 min
    on the worker. The hourly GitHub row goes at the canary's first projection from main, the
    ladder closes any incident of its own as retired, with no notice, and the worker projects
    dedup on its first health pass; until that canary run both rows show on the page."""
    from scripts import verify_pipeline as vp

    assert "broker_merge_suppression" not in _keys()
    assert "broker_suppression_violations_fail" not in THRESHOLDS
    assert not hasattr(vp, "check_broker_merge_suppression")
    assert not hasattr(vp, "_BROKER_SUPPRESSION_SQL")
    conn = _HealthChecksConn({"broker_merge_suppression": {
        "host": "github-actions", "cadence_s": 3_600, "declared_at": _NOW}})
    assert vp.project_registry(conn, "realtime-worker", _NOW) == {
        "upserted": len(_WORKER_KEYS), "deleted": 0}
    assert "dedup" in conn.table and "broker_merge_suppression" in conn.table, \
        "the worker leaves GitHub's row be"
    assert vp.project_registry(conn, "github-actions", _NOW)["deleted"] == 1
    assert "broker_merge_suppression" not in conn.table


# --- persistence --------------------------------------------------------------------


class _InsertConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def cursor(self) -> "_InsertConn":
        return self

    def __enter__(self) -> "_InsertConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))


def test_a_stored_result_keeps_its_message_and_cells_inside_details() -> None:
    """The message used to be dropped on the floor: the page and the ladder read it here."""
    from scripts.verify_pipeline import insert_result

    cells = [{"subject": "bazos", "portal": "bazos", "status": "fail", "value": 3,
              "unit": "h", "message": "bazos 3 h behind", "facts": {}}]
    details = {"cells": cells, "elapsed_ms": 12}
    conn = _InsertConn()
    insert_result(conn, {"check_key": "k", "status": "fail", "value": 3.0,
                         "details": details, "message": "bazos is 3 h behind"}, _NOW)
    (sql, params), = conn.calls
    assert "INSERT INTO pipeline_check_results" in sql
    assert params[:4] == (_NOW, "k", "fail", 3.0)
    stored = json.loads(params[4])
    assert stored == {"cells": cells, "elapsed_ms": 12, "message": "bazos is 3 h behind"}
    assert "message" not in details, "the check's own dict is not mutated"


def test_a_stored_result_without_a_message_adds_none() -> None:
    from scripts.verify_pipeline import insert_result

    conn = _InsertConn()
    insert_result(conn, {"check_key": "k", "status": "ok", "value": None, "details": None},
                  _NOW)
    assert json.loads(conn.calls[0][1][4]) == {}


# --- run_due: what is due, alarms first, stored as it lands ---------------------------------


class _ResultsConn:
    """An in-memory pipeline_check_results behind due_checks' read and insert_result's write."""

    def __init__(self, newest: dict[str, _dt.datetime] | None = None) -> None:
        self.rows: list[tuple[str, _dt.datetime]] = list((newest or {}).items())
        self._out: list[tuple[Any, ...]] = []

    def cursor(self) -> "_ResultsConn":
        return self

    def __enter__(self) -> "_ResultsConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        if sql.startswith("INSERT INTO pipeline_check_results"):
            self.rows.append((params[1], params[0]))
            return
        assert "max(r.run_at)" in sql, sql
        self._out = [(k, max((at for key, at in self.rows if key == k), default=None))
                     for k in params["keys"]]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._out


def _logging_check(key: str, log: list[str], *, cls: str = "alarm",
                   host: str = "github-actions") -> Check:
    """A stub check that notes when it runs."""
    def _fn(conn: Any, thresholds: Any) -> dict[str, Any]:
        log.append(f"run:{key}")
        return _verdict(key)

    return _stub(key, _fn, cls=cls, host=host)


def test_the_harness_projects_only_hosts_that_declare_a_check(monkeypatch: Any) -> None:
    """pg_cron's registry row is the watcher's own (migration 620): projecting that host would
    retire it, and the ladder would silently close every open liveness incident as retired."""
    from scripts import verify_pipeline as vp

    assert sorted(vp._HARNESS_HOSTS) == ["github-actions", "realtime-worker"]
    with pytest.raises(ValueError, match="pg_cron"):
        vp.project_registry(object(), "pg_cron", _NOW)
    # main() is GitHub's runner only: the worker's health lane calls run_due itself.
    for host in ("pg_cron", "realtime-worker"):
        with pytest.raises(SystemExit):
            _run_main(monkeypatch, ["--host", host], [])


def test_run_due_measures_only_the_due_checks_alarms_before_gauges(monkeypatch: Any) -> None:
    """Alarms are what a red means, so a pass that runs short spends its budget on them;
    inside each class the registry order is the priority."""
    import scripts.verify_pipeline as vp

    log: list[str] = []
    monkeypatch.setattr(vp, "CHECKS", (
        _logging_check("gauge_a", log, cls="gauge"), _logging_check("alarm_a", log),
        _logging_check("fresh", log), _logging_check("gauge_b", log, cls="gauge"),
        _logging_check("alarm_b", log), _logging_check("worker", log, host="realtime-worker"),
    ))
    conn = _ResultsConn({"fresh": _NOW - _dt.timedelta(minutes=10)})
    out = vp.run_due(conn, {}, "github-actions", now=_NOW)
    assert log == ["run:alarm_a", "run:alarm_b", "run:gauge_a", "run:gauge_b"]
    assert [r["check_key"] for r in out] == ["alarm_a", "alarm_b", "gauge_a", "gauge_b"]


def test_run_due_hands_each_result_on_the_moment_it_completes(monkeypatch: Any) -> None:
    import scripts.verify_pipeline as vp

    log: list[str] = []
    monkeypatch.setattr(vp, "CHECKS", (_logging_check("a", log), _logging_check("b", log)))

    def _sink(conn: Any, result: dict[str, Any], run_at: _dt.datetime) -> None:
        assert run_at == _NOW, "every row carries its run's start"
        log.append(f"stored:{result['check_key']}")

    vp.run_due(_ResultsConn(), {}, "github-actions", now=_NOW, on_result=_sink)
    assert log == ["run:a", "stored:a", "run:b", "stored:b"]


def test_run_due_caps_the_pass_at_the_lane_budget_it_is_given(monkeypatch: Any) -> None:
    """The worker's health lane runs it under a 500 s pass (360 s in waves 4-5, 300 s in wave 3),
    deferring what no longer fits; by default (the canary) the due budgets sum, deferring none."""
    import scripts.verify_pipeline as vp

    log: list[str] = []
    monkeypatch.setattr(vp, "CHECKS", (_logging_check("a", log), _logging_check("b", log)))
    seen: list[tuple[float, bool]] = []
    real = vp.run_checks

    def _spy(conn: Any, thresholds: Any, **kw: Any) -> Any:
        seen.append((kw["lane_budget_s"], kw["defer_unfit"]))
        return real(conn, thresholds, **kw)

    monkeypatch.setattr(vp, "run_checks", _spy)
    vp.run_due(_ResultsConn(), {}, "github-actions", now=_NOW)
    vp.run_due(_ResultsConn(), {}, "github-actions", now=_NOW, lane_budget_s=10.0)
    assert seen == [(2 * clock.CHECK_BUDGET_S, False), (10.0, True)]


def test_a_capped_pass_runs_every_alarm_then_defers_the_gauge_that_no_longer_fits(
    monkeypatch: Any,
) -> None:
    """Alarms first, so the pass's cap is spent on what a red means; the gauge the rest cannot
    hold writes nothing, stays due and runs on the next pass instead of reading Not measured
    for a whole cadence."""
    import scripts.verify_pipeline as vp

    clock_ = _on_the_clock(monkeypatch)
    log: list[str] = []

    def _takes(key: str, cls: str) -> Check:
        def _fn(conn: Any, thresholds: Any) -> dict[str, Any]:
            log.append(f"run:{key}")
            clock_.now += 1.5
            return _verdict(key)
        return _stub(key, _fn, budget_s=2.0, cls=cls, host="realtime-worker", cadence_s=21_600)

    monkeypatch.setattr(vp, "CHECKS", (_takes("gauge_a", "gauge"), _takes("alarm_a", "alarm"),
                                       _takes("gauge_b", "gauge"), _takes("alarm_b", "alarm")))
    conn = _ResultsConn()
    first = vp.run_due(conn, {}, "realtime-worker", now=_NOW, lane_budget_s=5.0)
    assert [r["check_key"] for r in first] == ["alarm_a", "alarm_b", "gauge_a"]
    later = _NOW + _dt.timedelta(minutes=1)
    assert [c.key for c in vp.due_checks(conn, "realtime-worker", later)] == ["gauge_b"]
    second = vp.run_due(conn, {}, "realtime-worker", now=later, lane_budget_s=5.0)
    assert [r["check_key"] for r in second] == ["gauge_b"]
    assert log == ["run:alarm_a", "run:alarm_b", "run:gauge_a", "run:gauge_b"]


# The worker lane's pass: scraper.realtime_worker.HEALTH_PASS_BUDGET_SECONDS, pinned in its test.
_WORKER_PASS_S = 500.0


@pytest.mark.parametrize("key", ["capacity", "coverage", "text_extraction", "parsing"])
def test_a_check_due_alone_runs_on_the_workers_capped_pass(monkeypatch: Any, caplog: Any,
                                                           key: str) -> None:
    """The capped pass sizes its lane to the due budgets, so a check due alone gets a lane of its
    own budget (45 s for the capacity alarm and the two hourly gauges, 180 s for parsing). The
    lane's clock has moved by the time the check is weighed, and wave 6's first build deferred
    it on every tick until another check fell due with it. It runs, and nothing is deferred."""
    import dataclasses
    import logging

    import scripts.verify_pipeline as vp

    _on_the_clock(monkeypatch)
    caplog.set_level(logging.WARNING, logger="verify_pipeline")
    registered = _by_key(key)
    monkeypatch.setattr(vp, "CHECKS", (
        dataclasses.replace(registered, fn=lambda c, t: _verdict(key)),))
    lanes: list[float] = []
    real = vp.run_checks

    def _spy(conn: Any, thresholds: Any, **kw: Any) -> Any:
        lanes.append(kw["lane_budget_s"])
        return real(conn, thresholds, **kw)

    monkeypatch.setattr(vp, "run_checks", _spy)
    conn = _ResultsConn()
    out = vp.run_due(conn, {}, "realtime-worker", now=_NOW, lane_budget_s=_WORKER_PASS_S)
    assert lanes == [registered.budget_s], "the lane is the lone check's own budget"
    assert [r["check_key"] for r in out] == [k for k, _ in conn.rows] == [key]
    assert "DEFER" not in caplog.text


def test_a_check_the_pass_cannot_fit_is_deferred_then_measured_alone_on_the_next_pass(
    monkeypatch: Any, caplog: Any,
) -> None:
    """Two 45 s alarms due on a 60 s pass: the first spends 30 s, so the second's 45 s does not
    fit the 30 s left, slack included. It is deferred with its log line and stores nothing, so
    it stays due; alone on the next pass a minute later its lane is its own 45 s, and it runs."""
    import logging

    import scripts.verify_pipeline as vp

    clock_ = _on_the_clock(monkeypatch)
    caplog.set_level(logging.WARNING, logger="verify_pipeline")
    log: list[str] = []

    def _takes(key: str, seconds: float) -> Check:
        def _fn(conn: Any, thresholds: Any) -> dict[str, Any]:
            log.append(f"run:{key}")
            clock_.now += seconds
            return _verdict(key)
        return _stub(key, _fn, host="realtime-worker", cadence_s=600)

    monkeypatch.setattr(vp, "CHECKS", (_takes("first", 30.0), _takes("second", 1.0)))
    conn = _ResultsConn()
    first = vp.run_due(conn, {}, "realtime-worker", now=_NOW, lane_budget_s=60.0)
    assert [r["check_key"] for r in first] == [k for k, _ in conn.rows] == ["first"]
    assert ("DEFER second: its 45s budget does not fit the 30s the pass has left; due next pass"
            in caplog.text)
    later = _NOW + _dt.timedelta(minutes=1)
    assert [c.key for c in vp.due_checks(conn, "realtime-worker", later)] == ["second"]
    second = vp.run_due(conn, {}, "realtime-worker", now=later, lane_budget_s=60.0)
    assert [r["check_key"] for r in second] == ["second"]
    assert [k for k, _ in conn.rows] == ["first", "second"]
    assert log == ["run:first", "run:second"]


def test_a_restarted_lane_measures_nothing_twice(monkeypatch: Any) -> None:
    """Due-ness is read from the stored results, never held in the process, so a lane that
    restarts (a deploy, a crash, the next 60 s tick) re-measures nothing it already measured
    this cadence, and measures it again once the cadence has passed."""
    import scripts.verify_pipeline as vp

    log: list[str] = []
    monkeypatch.setattr(vp, "CHECKS", (_logging_check("a", log), _logging_check("b", log)))
    conn = _ResultsConn()
    assert [r["check_key"] for r in vp.run_due(conn, {}, "github-actions", now=_NOW)] == ["a", "b"]
    assert vp.run_due(conn, {}, "github-actions", now=_NOW + _dt.timedelta(minutes=1)) == []
    assert log == ["run:a", "run:b"]
    vp.run_due(conn, {}, "github-actions", now=_NOW + _dt.timedelta(hours=1))
    assert log == ["run:a", "run:b", "run:a", "run:b"]


def test_a_check_the_budget_did_not_reach_is_due_on_the_next_run(monkeypatch: Any) -> None:
    import scripts.verify_pipeline as vp

    clock = _on_the_clock(monkeypatch)
    log: list[str] = []

    def _overruns(conn: Any, thresholds: Any) -> dict[str, Any]:
        clock.now += 100.0  # past the whole pass budget, outside any statement
        return _verdict("slow")

    monkeypatch.setattr(vp, "CHECKS", (_stub("slow", _overruns), _logging_check("late", log)))
    conn = _ResultsConn()
    assert [r["check_key"] for r in vp.run_due(conn, {}, "github-actions", now=_NOW)] == ["slow"]
    assert [key for key, _ in conn.rows] == ["slow"] and log == []
    later = _NOW + _dt.timedelta(minutes=1)
    assert [c.key for c in vp.due_checks(conn, "github-actions", later)] == ["late"]
    assert [r["check_key"] for r in vp.run_due(conn, {}, "github-actions", now=later)] == ["late"]


# --- main(): one flow, and its exit code never reflects a check ----------------------------


class _MainConn:
    def __enter__(self) -> "_MainConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _run_main(monkeypatch: Any, argv: list[str], due: list[Check], *,
              ref: str | None = None) -> tuple[int, dict[str, Any]]:
    """main() over fakes; `ref` is GITHUB_REF_NAME (None: unset, as on a local run)."""
    import sys

    import scripts.verify_pipeline as vp

    seen: dict[str, Any] = {"order": [], "inserted": []}

    def _project(conn: Any, host: str, now: Any) -> dict[str, int]:
        seen["order"].append(f"project:{host}")
        return {"upserted": len(due), "deleted": 0}

    def _due(conn: Any, host: str, now: Any) -> list[Check]:
        seen["order"].append(f"due:{host}")
        return due

    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://unused")
    # CI's own GITHUB_REF_NAME names the PR branch, so every test pins it.
    if ref is None:
        monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    else:
        monkeypatch.setenv("GITHUB_REF_NAME", ref)
    monkeypatch.setattr(vp, "connect", lambda: _MainConn())
    monkeypatch.setattr(vp, "project_registry", _project)
    monkeypatch.setattr(vp, "due_checks", _due)
    monkeypatch.setattr(vp, "insert_result",
                        lambda conn, r, run_at: seen["inserted"].append(r["check_key"]))
    monkeypatch.setattr(sys, "argv", ["verify_pipeline", *argv])
    return vp.main(), seen


def _returning(key: str, status: str, *, cls: str = "alarm") -> Check:
    if status == "unknown":
        return _stub(key, lambda c, t: {"check_key": key, "status": status, "value": None,
                                        "details": {"cells": [], "skipped": "nothing"},
                                        "message": f"{key} {status}"}, cls=cls)
    return _stub(key, lambda c, t: _verdict(key, status, 1, message=f"{key} {status}"), cls=cls)


def test_main_projects_the_registry_then_measures_and_stores_the_due_checks(
    monkeypatch: Any, caplog: Any,
) -> None:
    import logging

    import scripts.verify_pipeline as vp

    caplog.set_level(logging.INFO, logger="verify_pipeline")
    handed: list[Any] = []

    def _reads(conn: Any, thresholds: Any) -> dict[str, Any]:
        handed.append(thresholds)
        return _verdict("t", value=1)

    code, seen = _run_main(monkeypatch, [], [_returning("a", "ok"), _returning("b", "warn"),
                                             _stub("t", _reads)])
    assert code == 0
    assert seen["order"] == ["project:github-actions", "due:github-actions"]
    assert seen["inserted"] == ["a", "b", "t"]
    assert "CHECK a status=ok value=1" in caplog.text  # failure_signature reads this line
    assert len(handed) == 1 and handed[0] is vp.THRESHOLDS, "no run-time override"


def test_only_a_run_from_main_projects_the_registry(monkeypatch: Any, caplog: Any) -> None:
    """A dispatch from a branch runs that branch's CHECKS: projecting them would delete
    main's keys from health_checks (the page serves registry rows only) until main's next
    run re-declared them. It still measures and stores."""
    import logging

    caplog.set_level(logging.INFO, logger="verify_pipeline")
    code, seen = _run_main(monkeypatch, [], [_returning("a", "ok")], ref="feature/health-w2")
    assert code == 0
    assert seen["order"] == ["due:github-actions"] and seen["inserted"] == ["a"]
    assert "not projected: this run is from feature/health-w2, not main" in caplog.text
    _, seen = _run_main(monkeypatch, [], [_returning("a", "ok")], ref="main")
    assert seen["order"] == ["project:github-actions", "due:github-actions"]


def test_main_dry_run_projects_nothing_and_stores_nothing(monkeypatch: Any) -> None:
    code, seen = _run_main(monkeypatch, ["--dry-run"], [_returning("a", "fail")])
    assert code == 0
    assert seen["order"] == ["due:github-actions"] and seen["inserted"] == []


def test_no_measured_status_reds_the_run(monkeypatch: Any) -> None:
    """The canary's assertions (scripts/health_canary.py) are the one way a monitoring run
    goes red; a measured Problem is the ladder's to tell. The exit flag is gone, so passing
    it is refused rather than silently ignored."""
    gauge = _returning("g", "fail", cls="gauge")
    assert _run_main(monkeypatch, [], [gauge, _returning("a", "fail")])[0] == 0
    assert _run_main(monkeypatch, [], [_returning("a", "warn"),
                                       _returning("b", "unknown")])[0] == 0
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, ["--exit-nonzero-on-fail"], [_returning("a", "fail")])


# --- migration_drift (scripts/checks/platform.py): merged is not applied ---------


class _DriftConn:
    """Returns a canned presence verdict per (kind, ident) probe."""

    def __init__(self, present: dict[tuple[str, str], bool], default: bool = True) -> None:
        self._present = present
        self._default = default
        self.probed: list[tuple[str, str]] = []

    def transaction(self) -> "_DriftConn":
        return self

    def cursor(self) -> "_DriftConn":
        return self

    def __enter__(self) -> "_DriftConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        if sql.startswith("SET LOCAL statement_timeout"):
            return
        self.probed = list(zip(params["kinds"], params["idents"]))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [(k, i, self._present.get((k, i), self._default)) for k, i in self.probed]


def _window() -> list[Any]:
    return load_migrations(_MIGRATIONS_DIR, newest=T["migration_drift_window"])


def _file_cells(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["subject"]: c for c in out["details"]["cells"]}


def _probeable(mig: Any) -> bool:
    return any(SAFE_IDENT.match(o.ident) for o in mig.objects)


def test_migration_drift_ok_when_everything_is_present() -> None:
    """Wave 6 (D5): one cell per file of the window, OK ones included, so a file applied late
    recovers on a measured OK and the all-applied state reads OK, never Not measured."""
    conn = _DriftConn({}, default=True)
    out = check_migration_drift(conn, T)
    cells = _file_cells(out)
    assert out["status"] == "ok" and out["value"] == 0
    assert list(cells) == [m.filename for m in _window()]
    assert conn.probed, "objects were probed"
    probed = [m.filename for m in _window() if _probeable(m)]
    assert probed and all(cells[f]["status"] == "ok" for f in probed)


def test_migration_drift_fails_when_a_migration_is_wholly_absent() -> None:
    """The 2026-08-25 signature: merged, never applied, so NONE of its objects
    exist. Nothing else in the platform noticed for 29 hours.

    The subject migration is DERIVED from the current window, like the
    half-present test below, not hardcoded: this test used to pin migration 444's
    `listings.discovered_at`, which silently stopped exercising the failure path
    the moment 25 newer migrations pushed 444 out of `migration_drift_window` —
    it then passed `default=True` for everything and asserted "fail" against an
    all-present database, so it went red for the next PR to cross that boundary
    rather than for any real drift.
    """
    migs = load_migrations(_MIGRATIONS_DIR, newest=T["migration_drift_window"])
    # The target's objects must be DISJOINT from every other in-window migration's:
    # two migrations may legitimately restate one object (468 and 471 both restate
    # llm_calls' called_for CHECK), and marking a shared object absent fails BOTH,
    # breaking the exactly-one assertion for a reason that is not drift.
    target = next(
        m for m in migs
        if m.objects and all(SAFE_IDENT.match(o.ident) for o in m.objects)
        and not any(
            {(o.kind, o.ident) for o in m.objects} & {(o.kind, o.ident) for o in other.objects}
            for other in migs if other is not m and other.objects))
    conn = _DriftConn({(o.kind, o.ident): False for o in target.objects}, default=True)
    out = check_migration_drift(conn, T)
    cells = _file_cells(out)
    assert out["status"] == "fail"
    assert out["value"] == 1
    assert cells[target.filename]["status"] == "fail"
    assert [f for f, c in cells.items() if c["status"] == "fail"] == [target.filename]


def test_migration_drift_only_warns_when_a_migration_is_half_present() -> None:
    """Half-applied is ambiguous — a partial apply, or the parser mis-read the
    file. Ambiguity warns; it must not page anyone at 3am."""
    migs = load_migrations(_MIGRATIONS_DIR, newest=T["migration_drift_window"])
    # The missing object must be declared by no other file in the window: a function that a later
    # migration restates (health_liveness: 620, 621, 633, 655) would make those files fully absent.
    declared = Counter((o.kind, o.ident) for m in migs for o in m.objects)
    multi, gone = next((m, o) for m in migs if len(m.objects) >= 2
                       for o in m.objects if declared[(o.kind, o.ident)] == 1)
    conn = _DriftConn({(gone.kind, gone.ident): False}, default=True)
    out = check_migration_drift(conn, T)
    cells = _file_cells(out)
    assert out["status"] == "warn"
    assert cells[multi.filename]["status"] == "warn"
    assert not [f for f, c in cells.items() if c["status"] == "fail"]


def test_migration_drift_reports_its_own_blind_spot(monkeypatch: Any) -> None:
    """Migrations that only drop, grant, or update data declare nothing to probe. They must be
    COUNTED and named, not silently treated as passing — the failure mode this sprint already
    hit with a test that never ran: each is a Not measured cell saying so."""
    from scripts.migration_objects import Migration, MigrationObject

    window = [Migration(700, "700_grants_only.sql", []),
              Migration(701, "701_a_table.sql", [MigrationObject("relation", "public.t")]),
              Migration(702, "702_odd_ident.sql", [MigrationObject("relation", "no such ident!")])]
    monkeypatch.setattr(platform_checks, "load_migrations", lambda *a, **k: window)
    conn = _DriftConn({}, default=True)
    out = check_migration_drift(conn, T)
    cells = _file_cells(out)
    assert {f: c["status"] for f, c in cells.items()} == {
        "700_grants_only.sql": "unknown", "701_a_table.sql": "ok", "702_odd_ident.sql": "unknown"}
    assert "Unverifiable" in cells["700_grants_only.sql"]["message"]
    assert out["status"] == "ok" and conn.probed == [("relation", "public.t")]


@pytest.mark.parametrize("folder", ["missing", "empty"])
def test_migration_drift_with_no_migration_files_is_not_measured(
    folder: str, tmp_path: Path, monkeypatch: Any,
) -> None:
    """The worker image ships no migrations/ folder. Zero files probed is zero files
    verified: it read `ok` ("All 0 checkable of the newest 0 migrations are present")."""
    target = tmp_path / "migrations"
    if folder == "empty":
        target.mkdir()
    monkeypatch.setattr(platform_checks, "_MIGRATIONS_DIR", target)
    conn = _DriftConn({}, default=True)
    out = check_migration_drift(conn, T)
    assert out["status"] == "unknown" and out["value"] is None
    assert out["details"]["skipped"] == "no migration files on this host"
    assert "Not measured" in out["message"]
    assert conn.probed == [], "nothing to probe, so no statement is sent"


def test_migration_drift_probes_only_safe_identifiers() -> None:
    """to_regclass raises on a malformed identifier instead of returning NULL,
    which would turn one odd migration into a crashed check."""
    conn = _DriftConn({}, default=True)
    check_migration_drift(conn, T)
    for _kind, ident in conn.probed:
        assert SAFE_IDENT.match(ident), ident


def test_a_lane_owes_every_check_it_runs_its_own_budget() -> None:
    from scripts import verify_pipeline as vp

    assert vp.lane_budget_for([]) == 0.0
    assert vp.lane_budget_for(CHECKS) == sum(c.budget_s for c in CHECKS)
    assert vp.lane_budget_for(_registered("jobs", "outbound_url_conformance")) == 165.0


_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"


# Checkout, setup-python and `pip install -e .` take about two minutes; the assertions, seconds.
_SETUP_AND_ASSERTIONS_S = 480


def test_the_canary_job_fits_every_github_check_due_at_once() -> None:
    """The canary may find every GitHub-hosted check due at once (after a long gap or a registry
    change), so a registry that outgrows its step must fail HERE, not go quiet in production:
    the shared 120 s budget left 7-17 of 24 checks unrun for weeks. The harness step's own
    limit makes an overrun a red step, not a silent job kill; the poller runs first under its own."""
    import yaml
    from scripts import verify_pipeline as vp

    workflow = yaml.safe_load((_WORKFLOWS / "verify_pipeline.yml").read_text())
    (job,) = workflow["jobs"].values()
    steps = {s["name"]: s for s in job["steps"] if "name" in s}
    poller, harness = steps["Record failed workflow runs"], steps["Measure the GitHub-hosted checks"]
    hosted = [c for c in CHECKS if c.host == "github-actions"]
    assert vp.lane_budget_for(hosted) + vp._JOB_HEADROOM_S <= harness["timeout-minutes"] * 60
    assert ((poller["timeout-minutes"] + harness["timeout-minutes"]) * 60 + _SETUP_AND_ASSERTIONS_S
            <= job["timeout-minutes"] * 60)


def test_no_workflow_passes_a_flag_the_harness_deleted() -> None:
    """--arm-dual-write-parity went in wave 3 with its check, --exit-nonzero-on-fail in wave 2
    (the canary's assertions red a run), --only and --weekly in wave 1. argparse refuses them,
    so a workflow still passing one would crash its harness step on every run instead of
    measuring."""
    import yaml

    runs = [step["run"] for path in sorted(_WORKFLOWS.glob("*.yml"))
            for job in (yaml.safe_load(path.read_text()).get("jobs") or {}).values()
            for step in job.get("steps") or []
            if "scripts.verify_pipeline" in step.get("run", "")]
    assert runs, "a workflow runs the harness"
    for run in runs:
        for flag in ("--arm-dual-write-parity", "--exit-nonzero-on-fail", "--only",
                     "--weekly"):
            assert not re.search(rf"{flag}(?![\w-])", run), (flag, run)


def test_the_canary_keeps_the_monitoring_concurrency_group() -> None:
    """One group, so a dispatched run waits for the scheduled one instead of measuring the
    same due checks twice beside it."""
    import yaml

    workflow = yaml.safe_load((_WORKFLOWS / "verify_pipeline.yml").read_text())
    assert workflow["concurrency"]["group"] == "pipeline-verification"


# --- the unmeasured rail (health wave 1) ----------------------------------------
#
# A measurement that did not happen is Not measured: never ok, and never a warn that the
# ladder would read as a measured Watch holding an incident open.

_UNMEASURED_MARKERS = {"timed_out", "errored", "skipped"}


def _unmeasured_results() -> list[dict[str, Any]]:
    """One result from every path that measures nothing (a check the lane budget does not
    reach, or a capped pass defers, writes no result at all, so it has none here). Each
    check module's own tests hold its unmeasured paths; these are the harness's and the two
    GitHub checks'."""
    from unittest import mock

    from scripts.verify_outbound_urls import check_outbound_url_conformance

    def _boom(conn: Any, thresholds: Any) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    def _cancelled(conn: Any, thresholds: Any) -> dict[str, Any]:
        raise RuntimeError("canceling statement due to statement timeout")

    def _uncelled(conn: Any, thresholds: Any) -> dict[str, Any]:
        return {"check_key": "uncelled", "status": "fail", "value": 3, "details": {},
                "message": "a Problem no cell carries"}

    sampled = _BoundConn({"": [(1, "remax", "byt", None, "https://www.remax-czech.cz/x/1")]})
    with mock.patch.object(platform_checks, "_MIGRATIONS_DIR",
                           Path("/no-such-checkout/migrations")):
        no_migration_files = check_migration_drift(_DriftConn({}), T)
    return [
        *run_checks(None, {}, checks=[_stub("boom", _boom), _stub("slow", _cancelled),
                                      _stub("uncelled", _uncelled)]),
        no_migration_files,
        check_outbound_url_conformance(_BoundConn(), T),
        check_outbound_url_conformance(sampled, T, budget_s=0.0),
    ]


def test_every_unmeasured_path_is_unknown_and_says_why() -> None:
    results = _unmeasured_results()
    assert len(results) == 6
    errored, timed_out = results[:2]
    assert errored["details"]["errored"] is True and "kaboom" in errored["details"]["error"]
    assert timed_out["details"]["timed_out"] is True
    for r in results:
        markers = _UNMEASURED_MARKERS & set(r["details"])
        assert markers, r["check_key"]
        assert r["status"] == "unknown", (r["check_key"], r["status"])
        assert r["value"] is None and r["message"], r["check_key"]
        assert "UNKNOWN" not in r["message"], r["check_key"]  # the page's words: Not measured
        if "skipped" in markers:  # the reason as text, never a bare flag
            assert isinstance(r["details"]["skipped"], str), r["check_key"]
            assert r["details"]["skipped"].strip(), r["check_key"]


def test_no_unmeasured_result_reads_as_a_measured_watch() -> None:
    """The rail. Before wave 1 every one of these was a `warn`, which the ladder read as a
    measured Watch: in 30 days 34 of 88 'Recovered' notices followed an unmeasured run."""
    for r in _unmeasured_results():
        assert not (r["status"] == "warn" and _UNMEASURED_MARKERS & set(r["details"])), r


def test_no_result_reaches_the_ladder_without_cells(caplog: Any) -> None:
    """Wave 6 (D4) deleted the ladder's implicit cell, the one a result with no cells used to be
    read as. A measured verdict now needs a judged cell: without one it would show on the page
    and reach no incident, so the harness stores it Not measured and logs the check's defect;
    every unmeasured result carries an empty cell list."""
    import logging

    caplog.set_level(logging.ERROR, logger="verify_pipeline")

    def _returns(status: str, cells: Any) -> Check:
        details = {} if cells is None else {"cells": cells}
        value = None if status == "unknown" else 1
        return _stub(f"{status}-{cells is None}", lambda c, t: {
            "check_key": "k", "status": status, "value": value, "details": dict(details)})

    unjudged = [{"subject": "a", "portal": None, "status": "unknown", "value": None, "unit": "",
                 "message": "", "facts": {}}]
    out = run_checks(None, {}, checks=[_returns("fail", None), _returns("warn", unjudged),
                                       _returns("ok", "a table"), _returns("unknown", None)])
    for r in out:
        assert r["status"] == "unknown" and r["value"] is None, r
        assert r["details"]["cells"] == [], r
    assert [r["details"].get("skipped") for r in out[:3]] == [
        "the check returned fail without a judged cell",
        "the check returned warn without a judged cell",
        "the check returned ok without a judged cell"]
    assert out[3]["details"]["cells"] == []
    assert caplog.text.count("without a judged cell; stored Not measured") == 3
    (kept,) = run_checks(None, {}, checks=[_stub("k", lambda c, t: _verdict("k", "fail"))])
    assert kept["status"] == "fail" and kept["details"]["cells"][0]["status"] == "fail"


# --- the checks that read on a raw cursor now spend their own wall clock (health wave 2) ---


class _BoundConn:
    """Records each statement and whether a SET LOCAL statement_timeout armed it inside the
    same transaction; answers by SQL fragment, else with no rows."""

    def __init__(self, answers: dict[str, list[Any]] | None = None) -> None:
        self._answers = answers or {}
        self.sent: list[tuple[str, bool]] = []
        self._in_transaction = False
        self._armed = False
        self._rows: list[Any] = []

    @contextlib.contextmanager
    def transaction(self) -> Any:
        self._in_transaction = True
        try:
            yield
        finally:
            self._in_transaction = self._armed = False

    def cursor(self) -> "_BoundConn":
        return self

    def __enter__(self) -> "_BoundConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        if sql.startswith("SET LOCAL statement_timeout"):
            self._armed = self._in_transaction
            return
        self.sent.append((sql, self._armed))
        self._armed = False
        self._rows = next((v for k, v in self._answers.items() if k in sql), [])

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


def test_migration_drift_sends_its_probe_on_its_own_wall_clock() -> None:
    """Wave 2: three checks read on a raw cursor, under the connection's default timeout and past
    any budget; migration_drift is the one wave 6 kept (the other two went with their keys, and
    their successors pin their own statements beside quality.py). Its probe goes through the
    clock's fetchall, armed with what the check has left."""
    conn = _BoundConn()
    _by_key("migration_drift").fn(conn, T)
    assert [sql for sql, armed in conn.sent if armed] == [MIGRATION_OBJECT_PROBE_SQL]
    assert all(armed for _, armed in conn.sent)
