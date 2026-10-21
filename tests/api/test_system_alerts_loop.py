"""The API's system-alerts loop: one ladder pass a minute on its own connection, a failed
pass logged and survived, and the API's only background loop, under its own guard."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Iterator

import pytest

pytest.importorskip("fastapi")

from api import dependencies as deps
from api import main as api_main
from api import system_alerts_loop as loop
from toolkit import system_alerts

_CONN = object()
_ZERO = {"opened": 0, "reminded": 0, "recovered": 0, "closed_silently": 0, "notices": 0}


@contextlib.contextmanager
def _background_conn() -> Iterator[object]:
    yield _CONN


def _loop_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == loop.__name__]


def test_a_pass_reconciles_on_its_own_connection_and_logs_only_a_change(monkeypatch, caplog) -> None:
    seen: list[object] = []
    counts = dict(_ZERO)

    def fake_reconcile(conn: object) -> dict[str, int]:
        seen.append(conn)
        return dict(counts)

    monkeypatch.setattr(deps, "open_background_conn", _background_conn)
    monkeypatch.setattr(system_alerts, "reconcile", fake_reconcile)
    caplog.set_level(logging.INFO, logger=loop.__name__)
    loop._reconcile_once()
    assert seen == [_CONN]
    assert _loop_records(caplog) == []
    counts.update(opened=1, notices=1)
    loop._reconcile_once()
    assert [r.getMessage() for r in _loop_records(caplog)] == [f"system alerts: {counts}"]


@pytest.mark.parametrize("failing", ["connect", "reconcile"])
def test_a_failed_pass_is_logged_and_never_raised(monkeypatch, caplog, failing) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("pooler down")

    monkeypatch.setattr(
        deps, "open_background_conn", boom if failing == "connect" else _background_conn)
    monkeypatch.setattr(system_alerts, "reconcile", boom)
    loop._reconcile_once()
    [record] = _loop_records(caplog)
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None


def test_the_loop_passes_at_once_then_every_interval_until_stopped(monkeypatch) -> None:
    passes: list[None] = []
    monkeypatch.setattr(loop, "SYSTEM_ALERTS_INTERVAL_SECONDS", 0.01)

    async def scenario() -> None:
        stop = asyncio.Event()
        running = asyncio.get_running_loop()

        def fake_pass() -> None:
            passes.append(None)
            if len(passes) == 3:
                running.call_soon_threadsafe(stop.set)

        monkeypatch.setattr(loop, "_reconcile_once", fake_pass)
        await asyncio.wait_for(loop.system_alerts_loop(stop), timeout=5)

    asyncio.run(scenario())
    assert len(passes) == 3


def test_the_teller_is_the_apis_only_loop_and_its_guard_keeps_it_off(monkeypatch) -> None:
    """The notification producers run on the worker's lane alone (W2b): the lifespan starts
    the teller and nothing else, and HEALTH_TELLER_DISABLED (the suite's default) starts none."""
    async def idle(stop_event: asyncio.Event) -> None:
        await stop_event.wait()

    monkeypatch.setattr(loop, "system_alerts_loop", idle)
    monkeypatch.setenv("STUCK_ROW_SWEEP_DISABLED", "1")

    async def started_tasks() -> set[str]:
        async with api_main._lifespan(api_main.app):
            return {t.get_name() for t in asyncio.all_tasks()} - {asyncio.current_task().get_name()}

    monkeypatch.setenv("HEALTH_TELLER_DISABLED", "1")
    assert asyncio.run(started_tasks()) == set()
    monkeypatch.delenv("HEALTH_TELLER_DISABLED")
    assert asyncio.run(started_tasks()) == {"system-alerts"}
