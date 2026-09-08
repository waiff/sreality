"""Terminate a rented GPU pod the moment it stops earning its rent.

THE EXPENSIVE BUG, 2026-09-08: the dispatchers waited `job_max_seconds +
STARTUP_GRACE_S` no matter what the pod did. A pod that died in its first seconds (the
`git clone --branch <sha>` bug, see `scripts/pod_bootstrap.py`) still billed for the
full 8,115 s window — ~$0.50 for zero vectors, and RunPod's Pod logs endpoint answers
400, so nothing in the run said why.

The runner has `SUPABASE_DB_URL`; the pod's own writes are therefore readable from the
outside. This module turns that into three terminations, all cheaper than the window:

  (a) BOOTSTRAP DEADLINE — no heartbeat at all within `bootstrap_deadline_s` (default
      20 min: clone + installs + the first weight download). This is the case that just
      cost $0.50: "the clone or the install failed".
  (b) STALL DEADLINE — the pod booted and then stopped making progress for
      `stall_deadline_s` (default 15 min). Distinguishing (a) from (b) is the whole
      reason the payload writes a heartbeat the moment Python is up, BEFORE any model
      download: otherwise "clone failed" and "weights are downloading slowly" look
      identical from here.
  (c) ALL-TERMINAL — every unit of work reached a terminal state. The job is done; the
      remaining window is pure waste.

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

DEFAULT_BOOTSTRAP_DEADLINE_S = 20 * 60.0
DEFAULT_STALL_DEADLINE_S = 15 * 60.0
DEFAULT_POLL_INTERVAL_S = 60.0


@dataclass(frozen=True)
class Progress:
    """One reading of the job's own progress record in Postgres.

    `booted` — the payload has proved Python is running (its first DB write).
    `marker` — changes iff the job advanced; compared, never interpreted.
    `terminal` — every unit of work this dispatch cares about is finished.
    `detail` — free text for the log line.
    """

    booted: bool
    marker: str
    terminal: bool = False
    detail: str = ""


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
        self.verdict: str | None = None

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
            if reading.booted and not self._booted:
                self._booted = True
                self._progress_at = elapsed_s
                self._log.info("WATCHDOG pod booted after %.0fs — %s",
                               elapsed_s, reading.detail)
            if reading.marker != self._marker:
                if self._marker is not None and self._booted:
                    self._log.info("WATCHDOG progress at %.0fs — %s",
                                   elapsed_s, reading.detail)
                self._marker = reading.marker
                if self._booted:
                    self._progress_at = elapsed_s
            if reading.terminal:
                return self._terminate("all-terminal", elapsed_s, context,
                                       reading.detail or "every arm reached a terminal "
                                       "status; the rest of the window is waste")

        if not self._booted and elapsed_s >= self._bootstrap_deadline_s:
            return self._terminate(
                "bootstrap-deadline", elapsed_s, context,
                f"no heartbeat within {self._bootstrap_deadline_s:.0f}s — the pod never "
                "reported Python running, so the clone or the install failed (RunPod's "
                "Pod logs endpoint answers 400; there is nothing else to read)")

        if (self._booted and self._progress_at is not None
                and elapsed_s - self._progress_at >= self._stall_deadline_s):
            return self._terminate(
                "stall-deadline", elapsed_s, context,
                f"booted, then no progress for {elapsed_s - self._progress_at:.0f}s "
                f"(limit {self._stall_deadline_s:.0f}s); last seen: {self._marker}")

        return None

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
