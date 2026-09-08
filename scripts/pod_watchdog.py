"""Terminate a rented GPU pod the moment it stops earning its rent.

THE EXPENSIVE BUG, 2026-09-08: the dispatchers waited `job_max_seconds +
STARTUP_GRACE_S` no matter what the pod did. A pod that died in its first seconds (the
`git clone --branch <sha>` bug, see `scripts/pod_bootstrap.py`) still billed for the
full 8,115 s window — ~$0.50 for zero vectors, and RunPod's Pod logs endpoint answers
400, so nothing in the run said why.

The runner has `SUPABASE_DB_URL`; the pod's own writes are therefore readable from the
outside. This module turns that into three terminations, all cheaper than the window:

  (a) BOOTSTRAP DEADLINE — no NEW heartbeat within `bootstrap_deadline_s` (default
      30 min) while the payload has yet to prove Python is up. Measured from the last
      heartbeat, not from launch: since 2026-09-08 (g) the bootstrap itself reports
      every step (`scripts/pod_report.py`), so a slow 2 GB torch download keeps buying
      time while a dead clone does not.
  (b) STALL DEADLINE — progress stopped for `stall_deadline_s` (default 15 min). It
      applies to a booted pod AND to a bootstrap that has already reported at least one
      step: a pod that could write and then went quiet is stalled, whatever phase it is
      in (the bootstrap repeats its current step every 300 s for exactly this reason).
      Before the first heartbeat of any kind there is nothing to compare, so (a) is the
      only rail that can fire.
  (c) ALL-TERMINAL — every unit of work reached a terminal state. The job is done; the
      remaining window is pure waste.
  (d) CRASH LOOP — two or more `exit=` reports from the pod's own bootstrap. RunPod
      re-runs the docker start command whenever it exits, so a bootstrap that dies keeps
      dying, on the clock, and the stall rail only catches it a quarter of an hour later
      (2026-09-08 (h): ~33 min, ~$0.12). The teardown prints the FIRST exit report — the
      original cause — because every later one is a symptom of the restart.

WHAT COUNTS AS PROGRESS IS THE CALLER'S QUESTION, not this module's: a poller returns a
`Progress` whose `marker` is any string that CHANGES when the job advances (a vector
count, a heartbeat timestamp, both). This module only compares markers to the last one
it saw.

TIME IS THE `elapsed_s` THE CALLER PASSES IN — never a clock this module reads. That is
what makes every case above testable with a fake clock and a fake poller, offline, for
free.

A POLL THAT RAISES IS NOT PROGRESS. An unreadable database leaves the deadlines running
against the last CONFIRMED progress, so a broken read cannot buy the pod unlimited time
— the failure mode that matters here is paying for nothing, not stopping early.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

LOG = logging.getLogger("pod_watchdog")

# 30 min, raised from 20 on 2026-09-08 (g): the deadline now runs from the last STEP
# heartbeat rather than from launch, so it can afford to be generous with one slow step
# without letting a dead pod idle — the stall deadline catches that within 15 min.
DEFAULT_BOOTSTRAP_DEADLINE_S = 30 * 60.0
DEFAULT_STALL_DEADLINE_S = 15 * 60.0
DEFAULT_POLL_INTERVAL_S = 60.0


@dataclass(frozen=True)
class Progress:
    """One reading of the job's own progress record in Postgres.

    `booted` — the payload has proved Python is running (its first DB write).
    `marker` — changes iff the job advanced; compared, never interpreted.
    `terminal` — every unit of work this dispatch cares about is finished.
    `detail` — free text for the log line.
    `step` — the bootstrap's newest step report, verbatim, when the lane wires one up.
      Logged as it changes and printed after teardown, so the GitHub run shows WHICH
      step failed and the error tail it shipped.
    `steps` — the bootstrap's whole bounded heartbeat history, oldest first (see
      `scripts/pod_report.py`). Every NEW record is logged, and two `exit=` records in it
      are a crash loop. A lane that reports only the newest record leaves this empty and
      keeps the three original rails.
    """

    booted: bool
    marker: str
    terminal: bool = False
    detail: str = ""
    step: str = ""
    steps: tuple[str, ...] = ()


class PodWatchdog:
    """A `run_job(progress=...)` hook. Returns None to keep waiting, or the reason to
    tear the pod down now."""

    def __init__(
        self,
        poll: Callable[[], Progress],
        *,
        bootstrap_deadline_s: float = DEFAULT_BOOTSTRAP_DEADLINE_S,
        stall_deadline_s: float = DEFAULT_STALL_DEADLINE_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        log: logging.Logger | None = None,
    ) -> None:
        self._poll = poll
        self._bootstrap_deadline_s = float(bootstrap_deadline_s)
        self._stall_deadline_s = float(stall_deadline_s)
        self._poll_interval_s = float(poll_interval_s)
        self._log = log or LOG
        self._last_poll_at: float | None = None
        self._marker: str | None = None
        self._progress_at: float | None = None
        self._booted = False
        self._step: str = ""
        self._seen: set[str] = set()
        self._exits: list[tuple[float, str]] = []
        self.verdict: str | None = None
        self.last_step: str = ""
        self.last_detail: str = ""
        # The first `exit=` report seen this dispatch, tail included: the cause a crash
        # loop would otherwise bury under its own restarts.
        self.first_exit: str = ""

    def __call__(self, elapsed_s: float, context: Any = None) -> str | None:
        if (self._last_poll_at is not None
                and elapsed_s - self._last_poll_at < self._poll_interval_s):
            return None
        self._last_poll_at = elapsed_s

        reading: Progress | None
        try:
            reading = self._poll()
        except Exception as exc:  # noqa: BLE001 - an unreadable DB is not progress
            self._log.warning("WATCHDOG progress read failed at %.0fs (deadlines keep "
                              "running): %s", elapsed_s, exc)
            reading = None

        if reading is not None:
            self.last_detail = reading.detail
            crash = self._absorb_steps(reading, elapsed_s)
            if crash is not None:
                return self._terminate("crash-loop", elapsed_s, context, crash)
            if reading.step:
                self.last_step = reading.step
            if not reading.steps and reading.step and reading.step != self._step:
                # The bootstrap naming its own progress — the thing 2026-09-08 could not
                # see at all.
                self._log.info("WATCHDOG step=%s at %.0fs", reading.step, elapsed_s)
                self._step = reading.step
            if reading.booted and not self._booted:
                self._booted = True
                self._progress_at = elapsed_s
                self._log.info("WATCHDOG pod booted after %.0fs — %s",
                               elapsed_s, reading.detail)
            if reading.marker != self._marker:
                # The FIRST marker is a baseline, not progress: it may be a previous
                # dispatch's rows. Every later change is progress, booted or not.
                if self._marker is not None:
                    self._log.info("WATCHDOG progress at %.0fs — %s",
                                   elapsed_s, reading.detail)
                    self._progress_at = elapsed_s
                self._marker = reading.marker
            if reading.terminal:
                return self._terminate("all-terminal", elapsed_s, context,
                                       reading.detail or "every arm reached a terminal "
                                       "status; the rest of the window is waste")

        since = self._progress_at if self._progress_at is not None else 0.0
        if not self._booted and elapsed_s - since >= self._bootstrap_deadline_s:
            last = self.last_step or "none — the bootstrap reported nothing at all"
            return self._terminate(
                "bootstrap-deadline", elapsed_s, context,
                f"no new heartbeat for {elapsed_s - since:.0f}s "
                f"(limit {self._bootstrap_deadline_s:.0f}s) and the payload never "
                "reported Python running, so the clone or the install failed; last "
                f"step: {last}")

        if (self._progress_at is not None
                and elapsed_s - self._progress_at >= self._stall_deadline_s):
            return self._terminate(
                "stall-deadline", elapsed_s, context,
                f"{'booted' if self._booted else 'bootstrapping'}, then no progress "
                f"for {elapsed_s - self._progress_at:.0f}s "
                f"(limit {self._stall_deadline_s:.0f}s); last seen: {self._marker}"
                + (f"; last step: {self.last_step}" if self.last_step else ""))

        return None

    def _absorb_steps(self, reading: Progress, elapsed_s: float) -> str | None:
        """Log every heartbeat record this poll brought that we had not seen, and answer
        the crash-loop question. Returns the teardown reason, or None.

        The records are opaque strings — this module compares them, it does not parse
        them; `exit=` is the one token it has to recognise, because that is the pod's own
        EXIT trap saying its bootstrap died."""
        for record in reading.steps:
            if record in self._seen:
                continue
            self._seen.add(record)
            # Trimmed: a record carries up to ~3000 chars of log tail, and the whole of
            # it belongs in the teardown line, not once per routine heartbeat.
            self._log.info("WATCHDOG step=%s at %.0fs", record[:400], elapsed_s)
            if "exit=" in record:
                self._exits.append((elapsed_s, record))
                if not self.first_exit:
                    self.first_exit = record
        if reading.steps:
            self._step = reading.step
        if len(self._exits) < 2:
            return None
        span = self._exits[-1][0] - self._exits[0][0]
        if span > self._stall_deadline_s:
            return None
        return (f"the pod's bootstrap exited {len(self._exits)} times in {span:.0f}s — "
                "RunPod re-runs the start command whenever it exits, so this pod is "
                "restarting on the clock and will never boot. THE FIRST FAILURE (the "
                f"cause; the rest are its restarts): {self.first_exit[:2000]}")

    def _terminate(self, case: str, elapsed_s: float, context: Any, why: str) -> str:
        self.verdict = case
        self._log.warning("WATCHDOG TERMINATING pod: case=%s elapsed=%.0fs%s — %s",
                          case, elapsed_s, _cost_note(context, elapsed_s), why)
        return f"{case}: {why}"


def _cost_note(context: Any, elapsed_s: float) -> str:
    """' spent≈$0.49 on rtx3090' when the launch reported a price, '' otherwise."""
    price = getattr(context, "cost_per_hr", None)
    gpu = getattr(context, "gpu_type_id", None)
    if price is None:
        return f" gpu={gpu}" if gpu else ""
    return f" spent≈${float(price) * elapsed_s / 3600.0:.2f} on {gpu}"
