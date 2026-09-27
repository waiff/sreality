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
      remaining window is pure waste. TERMINAL IS PER LAUNCH, and the other end of that
      invariant is `scripts/tagging_bakeoff_embed.py::pending_arms`, which treats a
      `failed` arm as work to do: right for a payload deciding what to claim, wrong as a
      reason to keep a pod alive. Both stay as they are. What makes a retry a NEW attempt
      is the DISPATCHER resetting the previous attempt's `failed` arms to `pending`
      before launch (`tagging_bakeoff_dispatch.reset_failed_arms`); an arm that fails
      AGAIN during this run is genuinely terminal and this case fires on it correctly.
      Without that reset, attempt 5 of run 1 read 10/10 terminal two seconds in
      (2026-09-08 (j)).
  (d) CRASH LOOP — two or more `exit=` reports from the pod's own bootstrap within the
      stall window, OR (when the lane sets `max_passes`) any report from a later pass.
      RunPod re-runs the docker start command whenever it exits, so a bootstrap that dies
      keeps dying, on the clock, and the stall rail only catches it a quarter of an hour
      later (2026-09-08 (h): ~33 min, ~$0.12). A SLOW loop never tripped the first half: on
      2026-09-27 the G1 payload was OOM-killed every ~18 min for ten passes (exits never
      15 min apart, and the bounded history dropped most of them before a poll), so the
      pass NUMBER is the rail that cannot be missed — for a lane whose bootstrap bounds its
      restarts (G1). The DB-backed lanes resume across restarts and pass None. The
      teardown prints the FIRST exit report — the original cause — because every later
      one is a symptom of the restart.
  (e) PAYLOAD GAVE UP — the bootstrap's own `payload gave-up` report (a lane's opt-in
      restart bound: `scripts/pod_bootstrap.py`). The bootstrap writes it only AFTER its
      finalize returned, so the pod has uploaded what it had and is idling; every further
      second is rent. Its `step=finalize starting` record is not the token — and while it
      is the newest word, (c) waits up to `finalize_grace_s` for the token: the finalize
      closes every arm (the all-terminal cue) a second before the bootstrap reports, and a
      poll in that second would have read a give-up as a clean finish (a green dispatch).

EVERY RULE THAT READS STEP RECORDS READS ONLY THIS POD'S (2026-09-27, GitHub run
36353934278): a resume of G1 run 2 launched pod 04lgy6nzcv1l8x and tore it down two seconds
later as a crash loop "on pass 11" — pass 11 was the PREVIOUS dispatch's pod
(udlpld675b3zwp), whose history was still the run row's `pod steps` line because the new pod
had not reported yet. The history is keyed by bake-off run, not by pod, so (d), (e), the
finalize wait and the teardown's last-step line now admit a record only when its `pod` is the
pod `run_job` launched (its `PodContext`) and, as a second guard, when its `ts` is no earlier
than the launch less `clock_skew_s`. A record that cannot prove either is not ours, and is
logged once as ignored. A lane that folds step timestamps into its marker must scope them the
same way (`own_records`; the tagging lane reads `pod_id` off this watchdog).

WHAT COUNTS AS PROGRESS IS THE CALLER'S QUESTION, not this module's: a poller returns a
`Progress` whose `marker` is any string that CHANGES when the job advances (a vector
count, a heartbeat timestamp, both). This module only compares markers to the last one
it saw. A marker built on a heartbeat timestamp keeps a crash-looping payload alive for
the whole window (2026-09-27: every restart's heartbeats read as progress), so a lane that
can count finished work — shards, vectors — should put only that in its marker once the
payload is up (the G1 lane: `units=` on the payload's alive line).

TIME IS THE `elapsed_s` THE CALLER PASSES IN — never a clock this module reads. That is
what makes every case above testable with a fake clock and a fake poller, offline, for
free.

A POLL THAT RAISES IS NOT PROGRESS. An unreadable database leaves the deadlines running
against the last CONFIRMED progress, so a broken read cannot buy the pod unlimited time
— the failure mode that matters here is paying for nothing, not stopping early.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

LOG = logging.getLogger("pod_watchdog")

# 30 min, raised from 20 on 2026-09-08 (g): the deadline now runs from the last STEP
# heartbeat rather than from launch, so it can afford to be generous with one slow step
# without letting a dead pod idle — the stall deadline catches that within 15 min.
DEFAULT_BOOTSTRAP_DEADLINE_S = 30 * 60.0
DEFAULT_STALL_DEADLINE_S = 15 * 60.0
DEFAULT_POLL_INTERVAL_S = 60.0
# A bounded bootstrap runs a failing payload at most three times (pod_bootstrap), so a
# fourth pass means something outside that bound is restarting the container. Off unless a
# lane passes it: an unbounded lane's restarts are its resume.
DEFAULT_MAX_PASSES = 3
GAVE_UP_TOKEN = "payload gave-up"
FINALIZE_TOKEN = "step=finalize starting"
# How long all-terminal waits for the give-up report once a finalize has begun; past it the
# pod ends as all-terminal anyway (a reporter that cannot write must not buy it the window).
DEFAULT_FINALIZE_GRACE_S = 300.0
# The pod's clock is not the runner's: a record this much older than the launch still counts
# as this pod's. The same grace the tagging lane gives a boot stamp.
DEFAULT_CLOCK_SKEW_S = 120.0
_PASS_RE = re.compile(r"\bpass=(\d+)\b")
# A lane may cut a record short (the tagging lane keeps 2000 chars), which leaves it invalid
# JSON; `pod` and `ts` precede the tail in `pod_report.build_record`, so they survive the cut.
_POD_FIELD_RE = re.compile(r'"pod":\s*"([^"\\]*)"')
_TS_FIELD_RE = re.compile(r'"ts":\s*"([^"\\]*)"')


def _parse_ts(text: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def record_origin(record: str) -> tuple[str, datetime | None]:
    """(pod id, timestamp) a bootstrap heartbeat record names: '' and None where it names
    neither (a pre-(h) text record, a test's bare string)."""
    try:
        data = json.loads(record)
    except ValueError:
        data = None
    if isinstance(data, dict):
        pod, ts = data.get("pod"), data.get("ts")
        return (str(pod) if pod else ""), (_parse_ts(ts) if isinstance(ts, str) else None)
    pod_m, ts_m = _POD_FIELD_RE.search(record), _TS_FIELD_RE.search(record)
    return (pod_m.group(1) if pod_m else ""), (_parse_ts(ts_m.group(1)) if ts_m else None)


def is_own_record(record: str, *, pod_id: str | None, since: datetime | None) -> bool:
    """Whether THIS dispatch's pod wrote the record: its pod id is `pod_id` and its stamp is
    not before `since` (the launch less the clock skew). Each guard applies when its side is
    known; a record that cannot prove what is asked of it is not ours."""
    if not pod_id and since is None:
        return True
    pod, ts = record_origin(record)
    if pod_id and pod != pod_id:
        return False
    return since is None or (ts is not None and ts >= since)


def own_records(records: Sequence[str], *, pod_id: str | None,
                since: datetime | None) -> list[str]:
    """The records `is_own_record` admits, in their order."""
    return [r for r in records if is_own_record(r, pod_id=pod_id, since=since)]


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
      `scripts/pod_report.py`), verbatim: the watchdog, not the lane, sorts this pod's
      records from a previous pod's. Every NEW record of this pod is logged, and two `exit=`
      records in it are a crash loop. A lane that reports only the newest record leaves
      this empty and keeps the three original rails.
    """

    booted: bool
    marker: str
    terminal: bool = False
    detail: str = ""
    step: str = ""
    steps: tuple[str, ...] = ()


class PodWatchdog:
    """A `run_job(progress=...)` hook. Returns None to keep waiting, or the reason to
    tear the pod down now.

    `launched_at` (wall clock, taken by the lane before the launch) arms the timestamp
    guard on step records; the pod-id guard arms itself from the `PodContext` every call
    carries, and `pod_id` is public so a lane's poller can scope its own reads by it."""

    def __init__(
        self,
        poll: Callable[[], Progress],
        *,
        bootstrap_deadline_s: float = DEFAULT_BOOTSTRAP_DEADLINE_S,
        stall_deadline_s: float = DEFAULT_STALL_DEADLINE_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        max_passes: int | None = None,
        finalize_grace_s: float = DEFAULT_FINALIZE_GRACE_S,
        launched_at: datetime | None = None,
        clock_skew_s: float = DEFAULT_CLOCK_SKEW_S,
        log: logging.Logger | None = None,
    ) -> None:
        self._poll = poll
        self.pod_id = ""
        self._since = (None if launched_at is None
                       else launched_at - timedelta(seconds=float(clock_skew_s)))
        self._max_passes = None if max_passes is None else int(max_passes)
        self._finalize_grace_s = float(finalize_grace_s)
        self._finalizing_at: float | None = None
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
        pod_id = getattr(context, "pod_id", None)
        if pod_id:
            self.pod_id = str(pod_id)
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
                return self._terminate(crash[0], elapsed_s, context, crash[1])
            if not reading.steps and reading.step and self._owns(reading.step):
                self.last_step = reading.step
                if reading.step != self._step:
                    # The bootstrap naming its own progress — the thing 2026-09-08 could
                    # not see at all.
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
                if (self._finalizing_at is not None
                        and elapsed_s - self._finalizing_at < self._finalize_grace_s):
                    self._log.info("WATCHDOG every arm terminal at %.0fs, but the bootstrap's "
                                   "give-up is still finalizing: waiting for its report",
                                   elapsed_s)
                    return None
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

    def _absorb_steps(self, reading: Progress, elapsed_s: float) -> tuple[str, str] | None:
        """Log every heartbeat record this poll brought that we had not seen, and answer
        the crash-loop and gave-up questions. Returns (case, teardown reason), or None.

        The records are opaque strings — this module compares them, it does not parse
        them, except for three tokens the bootstrap writes for exactly this: `exit=` (its
        EXIT trap: the bootstrap died), `pass=N` (which container run this is) and
        `payload gave-up` (its restart bound was reached) — and for the `pod` and `ts` that
        say whose they are: only this pod's records reach any of the rules below."""
        own: list[str] = []
        ignored: list[str] = []
        for record in reading.steps:
            if self._owns(record):
                own.append(record)
            elif record not in self._seen:
                self._seen.add(record)
                ignored.append(record)
        if ignored:
            self._log.info("WATCHDOG ignoring %d step record(s) not written by pod %s since "
                           "%s (a previous pod's history under the same run): newest %s",
                           len(ignored), self.pod_id or "(unknown)",
                           self._since.isoformat() if self._since else "(any time)",
                           ignored[-1][:300])
        gave_up = ""
        top_pass = 0
        for record in own:
            match = _PASS_RE.search(record)
            if match:
                top_pass = max(top_pass, int(match.group(1)))
            if record in self._seen:
                continue
            self._seen.add(record)
            # Trimmed: a record carries up to ~3000 chars of log tail, and the whole of
            # it belongs in the teardown line, not once per routine heartbeat.
            self._log.info("WATCHDOG step=%s at %.0fs", record[:400], elapsed_s)
            if GAVE_UP_TOKEN in record:
                gave_up = record
            elif FINALIZE_TOKEN in record:
                self._finalizing_at = elapsed_s
            elif "exit=" in record:
                self._exits.append((elapsed_s, record))
                if not self.first_exit:
                    self.first_exit = record
        if own:
            self._step = self.last_step = own[-1]
        cause = (f"THE FIRST FAILURE (the cause; the rest are its restarts): "
                 f"{self.first_exit[:2000]}" if self.first_exit else "")
        if gave_up:
            return ("payload-failed",
                    "the bootstrap gave the payload up (an exit 137, or its restart bound) "
                    "and its finalize has returned; nothing on this pod will move again: "
                    f"{gave_up[:2000]}" + (f" — {cause}" if cause else ""))
        if self._max_passes is not None and top_pass > self._max_passes:
            return ("crash-loop",
                    f"the pod's start command is on pass {top_pass} (limit "
                    f"{self._max_passes}): the container keeps restarting, however slowly. "
                    + cause)
        if len(self._exits) < 2:
            return None
        span = self._exits[-1][0] - self._exits[0][0]
        if span > self._stall_deadline_s:
            return None
        return ("crash-loop",
                f"the pod's bootstrap exited {len(self._exits)} times in {span:.0f}s — "
                "RunPod re-runs the start command whenever it exits, so this pod is "
                "restarting on the clock and will never boot. " + cause)

    def _owns(self, record: str) -> bool:
        return is_own_record(record, pod_id=self.pod_id or None, since=self._since)

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
