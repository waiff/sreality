"""scripts/pod_watchdog.py — the three cheap terminations, with a fake clock and a fake
poller. No pod, no database, no network, no dollar.

The case that pays for this file: on 2026-09-08 a pod that died in its first seconds was
billed for the full 8,115 s wait window. Every test below is either "tear it down sooner"
or its indispensable twin, "do NOT tear down a pod that is working".
"""

from __future__ import annotations

from scripts.pod_watchdog import PodWatchdog, Progress


class _Poller:
    """A scripted sequence of readings; the last one repeats forever."""

    def __init__(self, readings: list[Progress]) -> None:
        self._readings = readings
        self.calls = 0

    def __call__(self) -> Progress:
        self.calls += 1
        return self._readings[min(self.calls, len(self._readings)) - 1]


def _run(watchdog: PodWatchdog, elapsed: list[float]) -> tuple[str | None, float | None]:
    """Feed a fake clock; return (stop reason, the elapsed at which it fired)."""
    for now in elapsed:
        stop = watchdog(now)
        if stop:
            return stop, now
    return None, None


DEAD = Progress(booted=False, marker="0|")
BOOTED = Progress(booted=True, marker="0|2026-09-08T10:00:00+00:00")


def test_a_pod_that_never_boots_is_terminated_at_the_bootstrap_deadline():
    poller = _Poller([DEAD])
    watchdog = PodWatchdog(poller, bootstrap_deadline_s=1200, stall_deadline_s=900,
                           poll_interval_s=60)
    stop, at = _run(watchdog, [60 * i for i in range(1, 40)])
    assert watchdog.verdict == "bootstrap-deadline"
    assert at == 1200
    assert "clone or the install failed" in stop


def test_a_booted_pod_that_stops_progressing_is_terminated_at_the_stall_deadline():
    # Two vector batches, then nothing: the pod is alive and useless.
    poller = _Poller([
        Progress(booted=True, marker="500|t1"),
        Progress(booted=True, marker="1000|t2"),
        Progress(booted=True, marker="1000|t2"),
    ])
    watchdog = PodWatchdog(poller, bootstrap_deadline_s=1200, stall_deadline_s=900,
                           poll_interval_s=60)
    stop, at = _run(watchdog, [60 * i for i in range(1, 60)])
    assert watchdog.verdict == "stall-deadline"
    # Last progress was the second poll (120s); the stall window runs from there.
    assert at == 120 + 900
    assert "no progress for" in stop


def test_a_pod_making_progress_is_never_terminated():
    poller = _Poller([Progress(booted=True, marker=f"{n * 500}|t{n}")
                      for n in range(1, 200)])
    watchdog = PodWatchdog(poller, bootstrap_deadline_s=1200, stall_deadline_s=900,
                           poll_interval_s=60)
    stop, _at = _run(watchdog, [60 * i for i in range(1, 150)])
    assert stop is None and watchdog.verdict is None


def test_slow_but_real_progress_beats_the_stall_deadline():
    # One batch every 14 minutes with a 15-minute deadline: still working, still paid for.
    readings = []
    for minute in range(0, 200):
        readings.append(Progress(booted=True, marker=f"{minute // 14}|t"))
    poller = _Poller(readings)
    watchdog = PodWatchdog(poller, bootstrap_deadline_s=1200, stall_deadline_s=900,
                           poll_interval_s=60)
    stop, _at = _run(watchdog, [60 * i for i in range(1, 120)])
    assert stop is None


def test_every_arm_terminal_ends_the_wait_immediately():
    poller = _Poller([
        Progress(booted=True, marker="1000|t1"),
        Progress(booted=True, marker="10800|t2", terminal=True,
                 detail="10800 vectors, arms 10/10 terminal"),
    ])
    watchdog = PodWatchdog(poller, bootstrap_deadline_s=1200, stall_deadline_s=900,
                           poll_interval_s=60)
    stop, at = _run(watchdog, [60 * i for i in range(1, 40)])
    assert watchdog.verdict == "all-terminal"
    assert at == 120 and "10/10 terminal" in stop


def test_a_boot_on_the_deadline_poll_itself_is_a_reprieve_not_a_teardown():
    # 19 silent polls, then the boot stamp arrives on the poll AT the 20-minute
    # deadline. The reading is evaluated before the deadline, so the pod lives — and the
    # stall clock then runs from the boot, not from launch.
    poller = _Poller([DEAD] * 19 + [Progress(booted=True, marker="1|t")])
    watchdog = PodWatchdog(poller, bootstrap_deadline_s=1200, stall_deadline_s=900,
                           poll_interval_s=60)
    stop, at = _run(watchdog, [60 * i for i in range(1, 40)])
    assert watchdog.verdict == "stall-deadline"
    assert at == 1200 + 900


def test_the_database_is_polled_on_its_own_interval_not_on_every_pod_poll():
    # run_job polls RunPod every 30s; the progress question is asked once a minute.
    poller = _Poller([Progress(booted=True, marker="1|t")])
    watchdog = PodWatchdog(poller, bootstrap_deadline_s=1200, stall_deadline_s=900,
                           poll_interval_s=60)
    for now in [30 * i for i in range(1, 21)]:  # 10 minutes at 30s
        watchdog(now)
    assert poller.calls == 10


def test_an_unreadable_database_is_not_progress():
    # A read that raises must not buy the pod an unlimited extension — the failure that
    # matters here is paying for nothing.
    def _boom() -> Progress:
        raise RuntimeError("connection refused")

    watchdog = PodWatchdog(_boom, bootstrap_deadline_s=1200, stall_deadline_s=900,
                           poll_interval_s=60)
    stop, at = _run(watchdog, [60 * i for i in range(1, 40)])
    assert watchdog.verdict == "bootstrap-deadline" and at == 1200


def test_the_termination_log_prices_the_decision(caplog):
    class _Ctx:
        pod_id = "u1yvcktjn6dbrt"
        gpu_type_id = "rtx3090"
        cost_per_hr = 0.22

    watchdog = PodWatchdog(_Poller([DEAD]), bootstrap_deadline_s=1200,
                           stall_deadline_s=900, poll_interval_s=60)
    with caplog.at_level("WARNING"):
        for now in [60 * i for i in range(1, 25)]:
            if watchdog(now, _Ctx()):
                break
    assert "case=bootstrap-deadline" in caplog.text
    assert "spent≈$0.07" in caplog.text and "rtx3090" in caplog.text
