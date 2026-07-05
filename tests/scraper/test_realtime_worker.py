"""Hermetic tests for the always-on realtime worker (Wave C-3): the env gate,
the generic lane loop (interval<=0 idle, pass-crash survival, lane restart),
probe/drain pass filtering, and the heartbeat upsert shape. No network, no DB —
run_index_probe / run_detail_drain / db.connect are monkeypatched.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from scraper import realtime_worker as rw
from scraper.portal import default_config


def _probe_agg(**over: Any) -> dict[str, Any]:
    agg = {
        "probe_pages": 1, "index_pages": 1, "listings_found_new": 2,
        "listings_enqueued": 2, "early_stopped": 1, "errors": 0,
        "by_category": [], "rc": 0,
    }
    agg.update(over)
    return agg


def _drain_agg(**over: Any) -> dict[str, Any]:
    agg = {
        "index_pages": 0, "listings_found_new": 3, "listings_scraped_new": 3,
        "listings_updated": 1, "listings_inactive": 0, "images_discovered": 5,
        "errors": 0, "by_category": [], "rc": 0,
    }
    agg.update(over)
    return agg


# --- env gate ----------------------------------------------------------------


def test_main_exits_zero_when_disabled(monkeypatch):
    monkeypatch.delenv(rw.ENABLE_ENV, raising=False)
    monkeypatch.setattr(rw.asyncio, "run", lambda *_: pytest.fail(
        "asyncio.run must not be reached when the worker ships dark"))
    assert rw.main() == 0


def test_main_runs_when_enabled(monkeypatch):
    monkeypatch.setenv(rw.ENABLE_ENV, "1")
    ran = []

    async def fake_amain() -> int:
        ran.append(1)
        return 0

    monkeypatch.setattr(rw, "_amain", fake_amain)
    assert rw.main() == 0
    assert ran == [1]


# --- generic lane loop ---------------------------------------------------------


def test_lane_loop_runs_pass_and_stops_cleanly():
    async def scenario() -> list[int]:
        stop = asyncio.Event()
        calls: list[int] = []

        async def run_pass() -> None:
            calls.append(1)
            stop.set()

        await rw._lane_loop(
            "t", stop, lambda: 300, run_pass, default_interval=300)
        return calls

    assert asyncio.run(scenario()) == [1]


def test_lane_loop_interval_zero_idles_without_pass():
    async def scenario() -> list[int]:
        stop = asyncio.Event()
        calls: list[int] = []

        async def run_pass() -> None:
            calls.append(1)

        task = asyncio.create_task(rw._lane_loop(
            "t", stop, lambda: 0, run_pass,
            default_interval=300, idle_seconds=0.01))
        await asyncio.sleep(0.05)
        stop.set()
        await task
        return calls

    assert asyncio.run(scenario()) == []


def test_lane_loop_pass_exception_does_not_kill_the_lane():
    async def scenario() -> list[int]:
        stop = asyncio.Event()
        calls: list[int] = []

        async def run_pass() -> None:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            stop.set()

        await rw._lane_loop(
            "t", stop, lambda: 0.01, run_pass, default_interval=0.01)
        return calls

    assert len(asyncio.run(scenario())) == 2


def test_lane_loop_falls_back_when_interval_read_fails():
    async def scenario() -> list[int]:
        stop = asyncio.Event()
        calls: list[int] = []

        def read_interval() -> float:
            raise RuntimeError("db down")

        async def run_pass() -> None:
            calls.append(1)
            stop.set()

        await rw._lane_loop(
            "t", stop, read_interval, run_pass, default_interval=300)
        return calls

    assert asyncio.run(scenario()) == [1]


def test_supervised_restarts_a_crashed_lane(monkeypatch):
    monkeypatch.setattr(rw, "LANE_RESTART_SECONDS", 0.01)

    async def scenario() -> list[int]:
        stop = asyncio.Event()
        calls: list[int] = []

        async def lane() -> None:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("lane died")

        await rw._supervised("t", lane, stop)
        return calls

    assert len(asyncio.run(scenario())) == 2


# --- probe pass ----------------------------------------------------------------


def test_probe_pass_filters_disabled_and_proxied(monkeypatch):
    monkeypatch.delenv("SCRAPER_PROXY_URL", raising=False)
    monkeypatch.setattr(rw, "_PROXY_WARNED", set())
    monkeypatch.setattr(
        rw, "_read_disabled_sources", lambda: {"bazos", "remax"})
    ran: list[str] = []

    def fake_probe(source: str) -> dict[str, Any]:
        ran.append(source)
        return _probe_agg()

    monkeypatch.setattr(rw, "_run_probe_sync", fake_probe)
    state = rw._new_state()

    async def scenario() -> None:
        await rw._probe_pass(asyncio.Event(), state)

    asyncio.run(scenario())
    # ceskereality is proxied (USE_PROXY) and SCRAPER_PROXY_URL is unset.
    assert ran == ["bezrealitky", "idnes", "maxima", "realitymix"]
    probe = state["lanes"]["probe"]
    assert probe["passes"] == 1
    assert probe["last"]["portals"] == 4
    assert probe["last"]["skipped"] == 3
    assert probe["last"]["enqueued"] == 8


def test_probe_pass_includes_proxied_portal_when_env_set(monkeypatch):
    monkeypatch.setenv("SCRAPER_PROXY_URL", "http://proxy.example:1080")
    monkeypatch.setattr(rw, "_read_disabled_sources", lambda: set())
    ran: list[str] = []
    monkeypatch.setattr(
        rw, "_run_probe_sync", lambda s: ran.append(s) or _probe_agg())
    state = rw._new_state()
    asyncio.run(rw._probe_pass(asyncio.Event(), state))
    assert ran == list(rw.REALTIME_SOURCES)


def test_probe_pass_portal_error_never_ends_the_pass(monkeypatch):
    monkeypatch.setenv("SCRAPER_PROXY_URL", "http://proxy.example:1080")
    monkeypatch.setattr(rw, "_read_disabled_sources", lambda: set())
    ran: list[str] = []

    def fake_probe(source: str) -> dict[str, Any]:
        ran.append(source)
        if source == "idnes":
            raise RuntimeError("portal blocked")
        return _probe_agg()

    monkeypatch.setattr(rw, "_run_probe_sync", fake_probe)
    state = rw._new_state()
    asyncio.run(rw._probe_pass(asyncio.Event(), state))
    assert ran == list(rw.REALTIME_SOURCES)
    assert state["lanes"]["probe"]["last"]["errors"] == 1
    assert state["lanes"]["probe"]["last"]["portals"] == len(rw.REALTIME_SOURCES) - 1


# --- drain pass ----------------------------------------------------------------


def test_drain_pass_serves_only_claimable_registry_sources(monkeypatch):
    monkeypatch.setenv("SCRAPER_PROXY_URL", "http://proxy.example:1080")
    monkeypatch.setattr(rw, "_read_drain_slice", lambda: 200)
    monkeypatch.setattr(rw, "_read_drain_disabled_sources", lambda: set())
    monkeypatch.setattr(rw, "_claimable_by_source", lambda: {
        "idnes": 5, "sreality": 5000, "bazos": 0, "ceskereality": 7,
    })
    ran: list[tuple[str, int]] = []

    def fake_drain(source: str, max_claims: int) -> dict[str, Any]:
        ran.append((source, max_claims))
        return _drain_agg()

    monkeypatch.setattr(rw, "_run_drain_sync", fake_drain)
    state = rw._new_state()
    asyncio.run(rw._drain_pass(asyncio.Event(), state))
    # sreality has claimable rows but is outside the worker registry (its own
    # */15 Actions split covers it); bazos has none.
    assert ran == [("ceskereality", 200), ("idnes", 200)]
    drain = state["lanes"]["drain"]
    assert drain["last"]["sources"] == 2
    assert drain["last"]["new"] == 6


def test_drain_pass_skips_proxied_source_without_env(monkeypatch):
    monkeypatch.delenv("SCRAPER_PROXY_URL", raising=False)
    monkeypatch.setattr(rw, "_PROXY_WARNED", set())
    monkeypatch.setattr(rw, "_read_drain_slice", lambda: 50)
    monkeypatch.setattr(rw, "_read_drain_disabled_sources", lambda: set())
    monkeypatch.setattr(
        rw, "_claimable_by_source", lambda: {"ceskereality": 7, "remax": 3})
    ran: list[tuple[str, int]] = []
    monkeypatch.setattr(
        rw, "_run_drain_sync",
        lambda s, n: ran.append((s, n)) or _drain_agg())
    asyncio.run(rw._drain_pass(asyncio.Event(), rw._new_state()))
    assert ran == [("remax", 50)]


def test_drain_pass_filters_drain_disabled_source(monkeypatch):
    """The per-source drain kill-switch skips a source with claimable rows,
    counting it under `skipped` (the proxy-outage freeze-the-queue lever)."""
    monkeypatch.setenv("SCRAPER_PROXY_URL", "http://proxy.example:1080")
    monkeypatch.setattr(rw, "_read_drain_slice", lambda: 100)
    monkeypatch.setattr(
        rw, "_read_drain_disabled_sources", lambda: {"ceskereality"})
    monkeypatch.setattr(rw, "_claimable_by_source", lambda: {
        "ceskereality": 9, "idnes": 5, "remax": 3,
    })
    ran: list[tuple[str, int]] = []
    monkeypatch.setattr(
        rw, "_run_drain_sync",
        lambda s, n: ran.append((s, n)) or _drain_agg())
    state = rw._new_state()
    asyncio.run(rw._drain_pass(asyncio.Event(), state))
    assert ran == [("idnes", 100), ("remax", 100)]
    assert state["lanes"]["drain"]["last"]["skipped"] == 1
    assert state["lanes"]["drain"]["last"]["sources"] == 2


def test_drain_pass_slice_zero_skips_entirely(monkeypatch):
    monkeypatch.setattr(rw, "_read_drain_slice", lambda: 0)
    monkeypatch.setattr(rw, "_claimable_by_source", lambda: pytest.fail(
        "slice<=0 must skip the pass before the queue count"))
    state = rw._new_state()
    asyncio.run(rw._drain_pass(asyncio.Event(), state))
    assert "drain" not in state["lanes"]


# --- images pass -----------------------------------------------------------------


def _images_agg(**over: Any) -> dict[str, Any]:
    agg = {"images_stored": 4, "by_category": {}, "stopped_suspicious": False}
    agg.update(over)
    return agg


def test_images_pass_runs_capped_active_only_download(monkeypatch):
    monkeypatch.setattr(rw, "_read_images_slice", lambda: 123)
    monkeypatch.setattr(rw.image_storage, "is_configured", lambda: True)
    ran: list[int] = []
    monkeypatch.setattr(
        rw, "_run_images_sync", lambda cap: ran.append(cap) or _images_agg())
    state = rw._new_state()
    asyncio.run(rw._images_pass(asyncio.Event(), state))
    assert ran == [123]
    images = state["lanes"]["images"]
    assert images["passes"] == 1
    assert images["last"] == {
        "downloaded": 4, "stopped_suspicious": False, "cap": 123,
    }


def test_images_pass_slice_zero_skips_entirely(monkeypatch):
    monkeypatch.setattr(rw, "_read_images_slice", lambda: 0)
    monkeypatch.setattr(rw.image_storage, "is_configured", lambda: pytest.fail(
        "slice<=0 must skip the pass before touching R2 config"))
    state = rw._new_state()
    asyncio.run(rw._images_pass(asyncio.Event(), state))
    assert "images" not in state["lanes"]


# --- dedup pass ------------------------------------------------------------------


def test_dedup_lane_registered_and_dark_by_default():
    # Ships DARK: the interval default is 0, so the lane idles until the operator flips
    # realtime_dedup_interval_seconds. The lane must be registered in _amain's list.
    assert rw.DEDUP_INTERVAL_DEFAULT == 0
    src = inspect.getsource(rw._amain)
    assert '("dedup"' in src


def test_dedup_pass_records_merge_counters(monkeypatch):
    def fake_sync() -> dict[str, Any]:
        return {"dirty_claimed": 5, "dirty_cleared": 3, "auto_phash": 2,
                "auto_visual": 1, "queued": 0, "truncated": 0, "vision_errors": 0}

    monkeypatch.setattr(rw, "_dedup_sync", fake_sync)
    state = rw._new_state()
    asyncio.run(rw._dedup_pass(asyncio.Event(), state))
    dedup = state["lanes"]["dedup"]
    assert dedup["passes"] == 1
    assert dedup["last"] == {
        "claimed": 5, "cleared": 3, "auto_phash": 2, "auto_visual": 1,
        "queued": 0, "truncated": 0, "vision_errors": 0,
    }


def test_dedup_pass_empty_queue_records_zero(monkeypatch):
    # run_realtime_dirty_pass returns None on an empty queue -> the lane records a
    # zero-claim pass (heartbeat visibility) without a run row.
    monkeypatch.setattr(rw, "_dedup_sync", lambda: None)
    state = rw._new_state()
    asyncio.run(rw._dedup_pass(asyncio.Event(), state))
    assert state["lanes"]["dedup"]["last"] == {"claimed": 0, "empty": True}


def test_dedup_sync_max_seconds_from_interval(monkeypatch):
    # max_seconds = min(cap, max(60, interval*2)); the pass delegates to the engine's
    # run_realtime_dirty_pass with the worker-configured budgets and runner='worker'.
    monkeypatch.setattr(rw, "_read_dedup_interval", lambda: 90)
    monkeypatch.setattr(rw, "_read_dedup_budgets", lambda: (150, 6, 3))
    monkeypatch.setattr(rw.db, "connect", lambda: _FakeConn())
    captured: dict[str, Any] = {}

    import scripts.dedup_engine as eng

    def fake_pass(conn, *, max_dirty, compare_budget, floor_plan_budget,
                  max_seconds, runner="worker"):
        captured.update(max_dirty=max_dirty, compare_budget=compare_budget,
                        floor_plan_budget=floor_plan_budget, max_seconds=max_seconds,
                        runner=runner)
        return {"dirty_claimed": 0}

    monkeypatch.setattr(eng, "run_realtime_dirty_pass", fake_pass)
    rw._dedup_sync()
    assert captured["max_dirty"] == 150
    assert captured["compare_budget"] == 6
    assert captured["floor_plan_budget"] == 3
    assert captured["max_seconds"] == 180.0   # interval 90 * 2, under the 240 cap
    assert captured["runner"] == "worker"


def test_images_pass_without_r2_logs_once_and_idles(monkeypatch, caplog):
    """No R2 env vars → the lane idles (never calls the downloader), records a
    skipped heartbeat pass, and warns exactly once per process — the proxy-skip
    posture."""
    monkeypatch.setattr(rw, "_read_images_slice", lambda: 500)
    monkeypatch.setattr(rw.image_storage, "is_configured", lambda: False)
    monkeypatch.setattr(rw, "_R2_WARNED", set())
    monkeypatch.setattr(rw, "_run_images_sync", lambda cap: pytest.fail(
        "downloader must not run without R2 config"))
    state = rw._new_state()

    with caplog.at_level("WARNING", logger="scraper.realtime_worker"):
        asyncio.run(rw._images_pass(asyncio.Event(), state))
        asyncio.run(rw._images_pass(asyncio.Event(), state))

    images = state["lanes"]["images"]
    assert images["passes"] == 2
    assert images["last"] == {"downloaded": 0, "skipped_no_r2": True}
    warnings = [r for r in caplog.records if "R2 env vars unset" in r.message]
    assert len(warnings) == 1


def test_run_images_sync_reuses_main_machinery_active_only(monkeypatch):
    from scraper import main as scraper_main

    seen: dict[str, Any] = {}

    def fake_run(max_downloads, workers, active_only=False, **kw):
        seen.update(
            max_downloads=max_downloads, workers=workers, active_only=active_only,
        )
        return _images_agg()

    monkeypatch.setattr(scraper_main, "_run_image_downloads", fake_run)
    assert rw._run_images_sync(250)["images_stored"] == 4
    assert seen == {
        "max_downloads": 250, "workers": rw.IMAGES_WORKERS, "active_only": True,
    }


# --- heartbeat -----------------------------------------------------------------


class _FakeCursor:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self._calls = calls

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        self._calls.append((sql, params))


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.calls)

    def close(self) -> None:
        self.closed = True


def test_heartbeat_upserts_latest_wins_row(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(rw.db, "connect", lambda: conn)
    state = rw._new_state()
    rw._record_pass(state, "probe", {"portals": 4})

    rw._beat_sync(state)

    assert conn.closed
    sql, params = conn.calls[0]
    assert "INSERT INTO worker_heartbeats" in sql
    assert "ON CONFLICT (worker) DO UPDATE" in sql
    assert params["worker"] == rw.WORKER_NAME
    assert params["started_at"] == state["started_at"]
    assert isinstance(params["details"], Jsonb)
    assert params["details"].obj["probe"]["last"] == {"portals": 4}


# --- registry ------------------------------------------------------------------


def test_registry_is_internally_consistent():
    assert set(rw.REALTIME_SOURCES) == set(rw._CLIENT_CLASSES)
    assert set(rw._PORTAL_CLASSES) == set(rw.REALTIME_SOURCES) - {"bazos"}


@pytest.mark.parametrize("source", rw.REALTIME_SOURCES)
def test_build_portal_from_baked_config(source):
    portal = rw._build_portal(source, default_config(source))
    assert portal.source == source
    assert hasattr(portal, "shared_rate_limiter")


# --- count-probe lane (sreality) -----------------------------------------------


class _CountCur:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __enter__(self) -> "_CountCur":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return self._rows


class _CountConn:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def cursor(self) -> _CountCur:
        return _CountCur(self._rows)

    def close(self) -> None:
        return None


def test_count_probe_sync_flags_changes_beyond_jitter(monkeypatch):
    from scraper import main as scraper_main

    monkeypatch.setattr(scraper_main, "CATEGORIES", ((1, 1), (2, 1), (3, 1)))
    totals = {(1, 1): 105, (2, 1): 51, (3, 1): 200}

    class _Client:
        def __init__(self, cm: int, ct: int) -> None:
            self.cm, self.ct = cm, ct

        def probe_result_size(self) -> int:
            return totals[(self.cm, self.ct)]

    monkeypatch.setattr(
        scraper_main, "_build_client", lambda cm, ct, limiter=None: _Client(cm, ct))
    # prior: (1,1)=100 -> +5 beyond jitter=changed; (2,1)=50 -> +1 within jitter=NOT;
    # (3,1) absent -> first sighting, recorded but never flagged.
    monkeypatch.setattr(rw.db, "connect", lambda: _CountConn([(1, 1, 100), (2, 1, 50)]))
    captured: list[Any] = []
    monkeypatch.setattr(rw, "_upsert_count_state", lambda rows: captured.append(rows))

    agg = rw._count_probe_sync()
    assert agg["pairs"] == 3
    assert agg["errors"] == 0
    assert [(c["cm"], c["ct"], c["old"], c["new"]) for c in agg["changed"]] == [
        (1, 1, 100, 105)]
    assert captured == [[(1, 1, 105, True), (2, 1, 51, False), (3, 1, 200, False)]]


def test_count_probe_sync_counts_errors_and_none_totals(monkeypatch):
    from scraper import main as scraper_main

    monkeypatch.setattr(scraper_main, "CATEGORIES", ((1, 1), (2, 1), (3, 1)))

    class _Client:
        def __init__(self, cm: int, ct: int) -> None:
            self.cm = cm

        def probe_result_size(self):
            if self.cm == 2:
                raise RuntimeError("http 500")
            if self.cm == 3:
                return None  # API withheld the total
            return 100

    monkeypatch.setattr(
        scraper_main, "_build_client", lambda cm, ct, limiter=None: _Client(cm, ct))
    monkeypatch.setattr(rw.db, "connect", lambda: _CountConn([]))
    monkeypatch.setattr(rw, "_upsert_count_state", lambda rows: None)

    agg = rw._count_probe_sync()
    # (1,1) recorded (first sighting, not flagged); (2,1) raised; (3,1) None -> both errors
    assert agg["pairs"] == 1
    assert agg["errors"] == 2
    assert agg["changed"] == []


def test_maybe_dispatch_no_change_short_circuits(monkeypatch):
    monkeypatch.setattr(rw, "_read_count_dispatch_enabled", lambda: pytest.fail(
        "must short-circuit before reading the enabled flag on no change"))
    assert rw._maybe_dispatch_index_walk([], 600) == {
        "dispatched": False, "reason": "no_change"}


def test_maybe_dispatch_disabled_records_only(monkeypatch):
    monkeypatch.setattr(rw, "_read_count_dispatch_enabled", lambda: False)
    monkeypatch.setattr(rw, "_post_workflow_dispatch", lambda t: pytest.fail(
        "disabled must not POST"))
    assert rw._maybe_dispatch_index_walk([{"cm": 1, "ct": 1}], 600) == {
        "dispatched": False, "reason": "disabled"}


def test_maybe_dispatch_no_token_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(rw, "_read_count_dispatch_enabled", lambda: True)
    monkeypatch.setattr(rw, "_dispatch_token", lambda: None)
    monkeypatch.setattr(rw, "_DISPATCH_WARNED", set())
    with caplog.at_level("WARNING", logger="scraper.realtime_worker"):
        rw._maybe_dispatch_index_walk([{"cm": 1, "ct": 1}], 600)
        out = rw._maybe_dispatch_index_walk([{"cm": 1, "ct": 1}], 600)
    assert out["reason"] == "no_token" and out["dispatched"] is False
    assert len([r for r in caplog.records if "no token env" in r.message]) == 1


def test_maybe_dispatch_skips_fresh_walk(monkeypatch):
    monkeypatch.setattr(rw, "_read_count_dispatch_enabled", lambda: True)
    monkeypatch.setattr(rw, "_dispatch_token", lambda: "tok")
    monkeypatch.setattr(rw, "_seconds_since_last_sreality_index_walk", lambda: 120.0)
    monkeypatch.setattr(rw, "_post_workflow_dispatch", lambda t: pytest.fail(
        "a fresh walk must not re-dispatch"))
    out = rw._maybe_dispatch_index_walk([{"cm": 1, "ct": 1}], 600)
    assert out["dispatched"] is False and out["reason"] == "fresh_walk"


def test_maybe_dispatch_triggers_when_stale_and_enabled(monkeypatch):
    monkeypatch.setattr(rw, "_read_count_dispatch_enabled", lambda: True)
    monkeypatch.setattr(rw, "_dispatch_token", lambda: "tok")
    monkeypatch.setattr(rw, "_seconds_since_last_sreality_index_walk", lambda: 5000.0)
    posted: list[str] = []
    monkeypatch.setattr(rw, "_post_workflow_dispatch", lambda t: posted.append(t) or True)
    out = rw._maybe_dispatch_index_walk([{"cm": 1, "ct": 1}], 600)
    assert out == {"dispatched": True, "reason": "triggered"}
    assert posted == ["tok"]


def test_maybe_dispatch_triggers_when_no_prior_walk(monkeypatch):
    # age None (no sreality index walk ever) -> not fresh -> dispatch
    monkeypatch.setattr(rw, "_read_count_dispatch_enabled", lambda: True)
    monkeypatch.setattr(rw, "_dispatch_token", lambda: "tok")
    monkeypatch.setattr(rw, "_seconds_since_last_sreality_index_walk", lambda: None)
    monkeypatch.setattr(rw, "_post_workflow_dispatch", lambda t: True)
    assert rw._maybe_dispatch_index_walk([{"cm": 1, "ct": 1}], 600)["dispatched"] is True


def test_post_workflow_dispatch_204_true_else_false(monkeypatch):
    import requests

    class _Resp:
        def __init__(self, code: int) -> None:
            self.status_code = code
            self.text = "body"

    calls: list[Any] = []
    monkeypatch.setattr(
        requests, "post", lambda url, **kw: calls.append((url, kw)) or _Resp(204))
    assert rw._post_workflow_dispatch("tok") is True
    assert f"{rw.DISPATCH_WORKFLOW}/dispatches" in calls[0][0]
    assert calls[0][1]["json"] == {"ref": rw.DISPATCH_REF}
    assert calls[0][1]["headers"]["Authorization"] == "Bearer tok"

    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp(422))
    assert rw._post_workflow_dispatch("tok") is False


def test_count_probe_pass_records_and_reports_dispatch(monkeypatch):
    monkeypatch.setattr(rw, "_count_probe_sync", lambda: {
        "pairs": 3, "changed": [{"cm": 1, "ct": 1, "old": 100, "new": 110}], "errors": 0})
    monkeypatch.setattr(rw, "_read_count_probe_interval", lambda: 600)
    monkeypatch.setattr(rw, "_maybe_dispatch_index_walk", lambda changed, cooldown: {
        "dispatched": True, "reason": "triggered"})
    state = rw._new_state()
    asyncio.run(rw._count_probe_pass(asyncio.Event(), state))
    cp = state["lanes"]["count_probe"]
    assert cp["passes"] == 1
    assert cp["last"] == {"pairs": 3, "changed": 1, "errors": 0, "dispatched": True}


def test_count_probe_pass_no_change_skips_dispatch(monkeypatch):
    monkeypatch.setattr(rw, "_count_probe_sync", lambda: {
        "pairs": 3, "changed": [], "errors": 0})
    monkeypatch.setattr(rw, "_maybe_dispatch_index_walk", lambda *a, **k: pytest.fail(
        "no change -> the dispatch decision must be skipped entirely"))
    state = rw._new_state()
    asyncio.run(rw._count_probe_pass(asyncio.Event(), state))
    assert state["lanes"]["count_probe"]["last"] == {
        "pairs": 3, "changed": 0, "errors": 0, "dispatched": False}
