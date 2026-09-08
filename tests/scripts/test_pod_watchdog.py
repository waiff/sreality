"""scripts/pod_watchdog.py — the three cheap terminations, with a fake clock and a fake
poller. No pod, no database, no network, no dollar.

The case that pays for this file: on 2026-09-08 a pod that died in its first seconds was
billed for the full 8,115 s wait window. Every test below is either "tear it down sooner"
or its indispensable twin, "do NOT tear down a pod that is working".
"""

from __future__ import annotations

import json

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


def test_a_slow_bootstrap_that_keeps_reporting_steps_is_not_a_dead_pod():
    # The 2026-09-08 (g) case: fetch/uv/venv/torch/repo take longer than the bootstrap
    # deadline between them, but each one reports. A 2 GB torch download must not be
    # confused with a dead clone — that confusion is what this whole fix is about.
    readings = [Progress(booted=False, marker=f"0|t{minute // 10}",
                         step=f"step={minute // 10}")
                for minute in range(0, 200)]
    watchdog = PodWatchdog(_Poller(readings), bootstrap_deadline_s=1200,
                           stall_deadline_s=900, poll_interval_s=60)
    stop, _at = _run(watchdog, [60 * i for i in range(1, 60)])
    assert stop is None and watchdog.verdict is None


def test_a_bootstrap_that_reported_and_then_went_silent_is_still_torn_down():
    # Two step heartbeats, then nothing: the pod proved it can write and stopped. The
    # beat inside the bootstrap repeats every 300s, so silence here is death, not work.
    poller = _Poller([
        Progress(booted=False, marker="0|t1", step="step=fetch ok"),
        Progress(booted=False, marker="0|t2", step="step=uv ok"),
        Progress(booted=False, marker="0|t2", step="step=uv ok"),
    ])
    watchdog = PodWatchdog(poller, bootstrap_deadline_s=1800, stall_deadline_s=900,
                           poll_interval_s=60)
    stop, at = _run(watchdog, [60 * i for i in range(1, 60)])
    assert watchdog.verdict == "stall-deadline"
    assert at == 120 + 900
    assert "step=uv ok" in stop


def test_the_bootstrap_deadline_still_fires_when_nothing_ever_reports():
    # A pod whose first command dies before the reporter exists has no heartbeat at all.
    watchdog = PodWatchdog(_Poller([DEAD]), bootstrap_deadline_s=1200,
                           stall_deadline_s=900, poll_interval_s=60)
    stop, at = _run(watchdog, [60 * i for i in range(1, 40)])
    assert watchdog.verdict == "bootstrap-deadline" and at == 1200
    assert "reported nothing at all" in stop


def test_each_new_step_is_logged_and_the_last_one_is_kept_for_the_dispatcher(caplog):
    poller = _Poller([
        Progress(booted=False, marker="0|t1", step="step=fetch ok"),
        Progress(booted=False, marker="0|t2", step="step=torch ok"),
        Progress(booted=False, marker="0|t3",
                 step='{"msg": "exit=1 step=repo", "tail": "no matching distribution"}'),
    ])
    watchdog = PodWatchdog(poller, bootstrap_deadline_s=1800, stall_deadline_s=900,
                           poll_interval_s=60)
    with caplog.at_level("INFO"):
        _run(watchdog, [60, 120, 180])
    assert "step=step=fetch ok" in caplog.text and "step=torch ok" in caplog.text
    # What the dispatcher prints after teardown: the shipped error, not a guess.
    assert "no matching distribution" in watchdog.last_step


# --- the crash loop (2026-09-08 (h)) ------------------------------------------------


def _rec(msg: str, tail: str = "") -> str:
    record = {"ts": "2026-09-08T18:30:00+00:00", "msg": msg}
    if tail:
        record["tail"] = tail
    return json.dumps(record)


def test_two_exit_reports_are_a_crash_loop_and_the_first_one_is_the_verdict(caplog):
    # RunPod re-runs the docker start command whenever it exits, so a bootstrap that
    # dies keeps dying. On 2026-09-08 that burned 33 minutes at the stall deadline, and
    # the report naming the cause was the one the loop overwrote.
    first = _rec("pass=1 exit=1 step=torch", tail="OSError: No space left on device")
    poller = _Poller([
        Progress(booted=False, marker="0|t1", step=_rec("pass=1 step=venv ok"),
                 steps=(_rec("pass=1 step=venv ok"),)),
        Progress(booted=False, marker="0|t2", step=first,
                 steps=(_rec("pass=1 step=venv ok"), first)),
        Progress(booted=False, marker="0|t3", step=_rec("pass=2 exit=3 step=fetch"),
                 steps=(_rec("pass=1 step=venv ok"), first,
                        _rec("pass=2 step=deps ok"),
                        _rec("pass=2 exit=3 step=fetch",
                             tail="error: remote origin already exists"))),
    ])
    watchdog = PodWatchdog(poller, bootstrap_deadline_s=1800, stall_deadline_s=900,
                           poll_interval_s=60)
    with caplog.at_level("INFO"):
        stop, at = _run(watchdog, [60 * i for i in range(1, 40)])
    assert watchdog.verdict == "crash-loop"
    assert at == 180                                   # the poll that saw the second exit
    assert "exited 2 times" in stop
    # The FIRST failure is the cause; every later one is a symptom of the restart.
    assert "No space left on device" in stop
    assert "already exists" not in stop
    assert watchdog.first_exit == first


def test_every_new_record_is_logged_not_only_the_latest(caplog):
    # A 60s poll against a bootstrap that reports every few seconds sees several new
    # records at once; logging only the newest throws the rest away.
    records = tuple(_rec(f"pass=1 step=s{i} ok") for i in range(4))
    poller = _Poller([Progress(booted=False, marker="0|t1", step=records[-1],
                              steps=records)])
    watchdog = PodWatchdog(poller, bootstrap_deadline_s=1800, stall_deadline_s=900,
                           poll_interval_s=60)
    with caplog.at_level("INFO"):
        _run(watchdog, [60, 120])
    for i in range(4):
        assert f"step=s{i} ok" in caplog.text
    # And not again on the next poll: a record is logged once.
    assert caplog.text.count("step=s0 ok") == 1


def test_one_exit_report_alone_is_not_a_crash_loop():
    # A single failing pass is the ordinary case the other rails already cover; only the
    # repetition proves the container is restarting.
    died = _rec("pass=1 exit=1 step=torch")
    poller = _Poller([
        Progress(booted=False, marker="0|t1", step=_rec("pass=1 step=venv ok"),
                 steps=(_rec("pass=1 step=venv ok"),)),
        Progress(booted=False, marker="0|t2", step=died,
                 steps=(_rec("pass=1 step=venv ok"), died)),
    ])
    watchdog = PodWatchdog(poller, bootstrap_deadline_s=1800, stall_deadline_s=900,
                           poll_interval_s=60)
    stop, at = _run(watchdog, [60 * i for i in range(1, 40)])
    assert watchdog.verdict == "stall-deadline"      # not crash-loop
    assert at == 120 + 900


def test_a_lane_that_reports_only_the_newest_record_keeps_the_old_rails():
    # The pre-(h) note shape, and any lane that never wires a history: `steps` is empty
    # and nothing about the three original terminations changes.
    poller = _Poller([Progress(booted=False, marker="0|t1", step="step=uv ok"),
                      Progress(booted=False, marker="0|t2", step="step=venv ok"),
                      Progress(booted=False, marker="0|t2", step="step=venv ok")])
    watchdog = PodWatchdog(poller, bootstrap_deadline_s=1800, stall_deadline_s=900,
                           poll_interval_s=60)
    stop, at = _run(watchdog, [60 * i for i in range(1, 40)])
    assert watchdog.verdict == "stall-deadline" and at == 120 + 900
    assert "step=venv ok" in stop


def test_the_default_bootstrap_deadline_is_generous_enough_for_a_slow_install():
    # It runs from the last step heartbeat now, so the cost of a large value is bounded
    # by the stall deadline, not by it.
    from scripts.pod_watchdog import DEFAULT_BOOTSTRAP_DEADLINE_S

    assert DEFAULT_BOOTSTRAP_DEADLINE_S == 1800


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
