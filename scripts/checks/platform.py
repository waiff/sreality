"""Platform checks: jobs, capacity and llm (the worker's health lane), migration_drift (GitHub).

`check_jobs` judges every scheduled machine once, one cell per job: each GitHub workflow on
main by its production runs (a `schedule` run, or a `push` on main), the canary's poller that
records them, each pg_cron job, each worker lane (from the heartbeat) and each derived
artifact. `check_capacity` judges whether the database and the worker's memory keep up,
against references an incident cannot erode. `check_llm` judges whether each provider is
serving, per provider and per job, and whether fleet spend is sane. `check_migration_drift`
asks the live catalog whether the newest merged migrations exist; it reads a checkout, so it
stays on GitHub. Each check takes the harness's `thresholds`, sends every statement through the
per-check clock and imports neither the harness nor the worker; one that could measure nothing
is `unknown` with `details.skipped`, and an arm it could not read is named in
`details.not_measured` while the others are still judged.
"""

from __future__ import annotations

import datetime as _dt
import re
import statistics
from pathlib import Path
from typing import Any

from psycopg import errors as _pg_errors

from autodedup.incremental_lane import LEASE_LOST as _LEASE_LOST, STOPPING as _STOPPING
from scripts.checks.clock import fetchall, fetchone
from scripts.checks.common import (
    BEAT_FRESH_S as _BEAT_FRESH_S,
    CLOCK_SQL as _CLOCK_SQL,
    WORKER as _WORKER,
    ago as _ago,
    as_utc as _as_utc,
    cell as _cell,
    graded as _graded,
    iso as _iso,
    judged as _judged,
    now as _now,
    num as _num,
    oneline as _oneline,
    previous_result as _previous_result,
    result as _result,
)
from scripts.migration_objects import MIGRATION_OBJECT_PROBE_SQL, SAFE_IDENT, load_migrations
from scripts.record_workflow_failures import CURSOR_KEY, MONITOR_WORKFLOW_NAME

_UTC = _dt.timezone.utc
# A role without access to pg_cron's schema or tables reads no history: Not measured, never OK.
_CRON_UNREADABLE = (_pg_errors.InsufficientPrivilege, _pg_errors.InvalidSchemaName,
                    _pg_errors.UndefinedTable)
# One outage, two wordings: OpenAI's 429 says "You have no credits remaining", Anthropic's
# "credit balance" (the patterns llm_errors used, carried verbatim; auth failures match neither).
_CREDIT_ERROR_PATTERNS = ("%credit balance%", "%no credits remaining%")
# The checkout's migrations/ folder. The worker image ships none, so a run there probes nothing.
_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


# --- shared shape ------------------------------------------------------------------------


def _round(value: float | None, digits: int = 0) -> float | None:
    return None if value is None else round(value, digits)


def _hours(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds / 3600, 1)


def _stamp(at: _dt.datetime | None) -> str:
    return "none on record" if at is None else at.astimezone(_UTC).strftime("%Y-%m-%d %H:%M UTC")


# --- jobs: GitHub workflows and the poller that records them --------------------------------

_GH_CURSOR_SQL = "select (value #>> '{}')::timestamptz from app_settings where key = %(key)s"

# Production runs only: a schedule run, or a push on main (a row stored before the poller
# recorded the event, wave 3, is never a job's record). Every red after the newest success is
# one streak, since a later success would have moved last_success_at past it.
_GH_WORKFLOWS_SQL = """
select h.workflow_path, h.workflow_name, h.state, h.last_success_at, h.recent_starts,
       r.reds, r.newest_red_at, r.conclusion, r.reason, r.html_url
  from workflow_run_health h
  left join lateral (
        select count(*) as reds, max(f.run_started_at) as newest_red_at,
               (array_agg(f.conclusion order by f.run_started_at desc))[1] as conclusion,
               (array_agg(f.reason order by f.run_started_at desc))[1] as reason,
               (array_agg(f.html_url order by f.run_started_at desc))[1] as html_url
          from workflow_failures f
         where f.workflow_path = h.workflow_path
           and (f.event = 'schedule' or (f.event = 'push' and f.head_branch = 'main'))
           and f.run_started_at > coalesce(h.last_success_at, '-infinity'::timestamptz)) r
    on true
 where h.on_main
 order by h.workflow_path
"""


def _github_cells(conn: Any, thresholds: dict[str, Any], now: _dt.datetime) -> list[dict[str, Any]]:
    """The poller's cell, then one per workflow on main, judged as of the poller's cursor."""
    row = fetchone(conn, _GH_CURSOR_SQL, {"key": CURSOR_KEY})
    cursor = _as_utc(row[0]) if row else None
    stale_s = float(thresholds["jobs_poller_stale_h"]) * 3600
    if cursor is None:
        live = False
        cells = [_cell("gh/poller", "fail", "no poller cursor on record: the canary's poller step "
                       "has never completed a poll, so no GitHub run is recorded", unit="h")]
    else:
        age = (now - cursor).total_seconds()
        live = age <= stale_s
        message = (f"cursor {_ago(age)} old" if live else
                   f"its cursor is {_ago(age)} old (limit {_ago(stale_s)}): the canary's poller "
                   "step is failing, held at its page cap or not running, so no workflow's "
                   "newest runs are known")
        cells = [_cell("gh/poller", "ok" if live else "fail", message, value=round(age / 3600, 1),
                       unit="h", facts={"cursor": _iso(cursor)})]
    for workflow in fetchall(conn, _GH_WORKFLOWS_SQL):
        if workflow[1] == MONITOR_WORKFLOW_NAME:
            continue  # the canary judges itself: its red run is GitHub's email
        subject = f"gh/{Path(workflow[0]).stem}"
        if live and cursor is not None:
            cells.append(_workflow_cell(subject, workflow, thresholds, cursor))
        else:
            cells.append(_cell(subject, "unknown", "Not measured: the GitHub poller is stale, so "
                               "this workflow's newest runs are unknown",
                               facts={"workflow": workflow[1]}))
    return cells


def _workflow_cell(subject: str, row: tuple[Any, ...], thresholds: dict[str, Any],
                   as_of: _dt.datetime) -> dict[str, Any]:
    """One workflow's production record: its newest run, its red streak, its time since success."""
    _path, name, state, last_ok, recent, reds, red_at, conclusion, reason, url = row
    last_ok, red_at, reds = _as_utc(last_ok), _as_utc(red_at), int(reds or 0)
    # The poller stores scheduled starts only (a push has no cadence to be overdue against), so
    # a push-only workflow is judged by its reds alone.
    starts = sorted(s for s in (_as_utc(v) for v in recent or ()) if s is not None)
    gaps = [(b - a).total_seconds() for a, b in zip(starts, starts[1:])]
    min_starts = int(thresholds["jobs_gh_gap_min_starts"])
    median_gap = statistics.median(gaps) if gaps else None
    p90 = (statistics.quantiles(gaps, n=10, method="inclusive")[-1]
           if len(starts) >= min_starts and len(gaps) >= 2 else None)
    facts: dict[str, Any] = {
        "workflow": name, "state": state, "last_success_at": _iso(last_ok),
        "production_reds": reds, "starts": len(starts),
        "median_gap_h": _hours(median_gap), "p90_gap_h": _hours(p90)}
    if reds:
        facts.update(newest_red_at=_iso(red_at), conclusion=conclusion, reason=reason,
                     run_url=url)
    if state and state != "active":
        return _cell(subject, "ok", f"off: the workflow is {state}", facts=facts)
    if median_gap is None:
        facts["long_cycle"] = "Not measured: fewer than 2 scheduled starts recorded"
    why = f"{reason or 'reason not read'}" + (f" — {url}" if url else "")
    long_cycle_s = float(thresholds["jobs_gh_long_cycle_h"]) * 3600
    conditions: list[tuple[str, str]] = []
    if reds and median_gap is not None and median_gap >= long_cycle_s:
        conditions.append(("fail", f"its newest production run failed ({conclusion}, "
                                   f"{_stamp(red_at)}) and it starts only every "
                                   f"{_ago(median_gap)}, so one red loses a cycle: {why}"))
    if reds >= int(thresholds["jobs_gh_streak_fail"]):
        conditions.append(("fail", f"{reds} production reds in a row since its last success "
                                   f"({_stamp(last_ok)}), the newest {conclusion} at "
                                   f"{_stamp(red_at)}: {why}"))
    multiple = float(thresholds["jobs_gh_gap_p90_multiple"])
    if p90 is None:
        facts["overdue"] = f"Not measured: {len(starts)} of {min_starts} scheduled starts recorded"
    elif last_ok is None:
        facts["overdue"] = "Not measured: no production success on record"
    elif (as_of - last_ok).total_seconds() > multiple * p90:
        conditions.append(("fail", f"no successful production run for "
                                   f"{_ago((as_of - last_ok).total_seconds())}, over "
                                   f"{multiple:g}× its p90 start gap of {_ago(p90)}"))
    if reds and not conditions:
        conditions.append(("warn", f"its newest production run failed ({conclusion}, "
                                   f"{_stamp(red_at)}): {why}"))
    ok = (f"last production success {_stamp(last_ok)}" if last_ok
          else "no production run recorded yet")
    return _judged(subject, conditions, ok, value=reds, unit="reds", facts=facts)


# --- jobs: pg_cron ----------------------------------------------------------------------

# Per job, its newest 7 days of runs counted back from its newest start, so a job that stopped
# firing is still measured against its own last week. The last three, the 24 h failures and the
# newest failure are finished runs only: pg_cron marks a run cut off by a server restart failed
# and leaves its end_time NULL, which says nothing about the job.
_CRON_JOBS_SQL = """
with newest as (
    select jobid, max(start_time) as at from cron.job_run_details group by jobid),
runs as (
    select d.jobid, d.start_time, d.end_time, d.status, d.return_message,
           extract(epoch from d.start_time - lag(d.start_time)
                   over (partition by d.jobid order by d.start_time)) as gap_s,
           row_number() over (partition by d.jobid, d.end_time is null
                              order by d.start_time desc) as nth
      from cron.job_run_details d
      join newest n on n.jobid = d.jobid
     where d.start_time > n.at - interval '7 days')
select j.jobname, j.active, n.at as newest_start, count(r.start_time) as starts,
       percentile_cont(0.5) within group (order by r.gap_s) as median_gap_s,
       array_agg(r.status order by r.start_time desc)
           filter (where r.end_time is not null and r.nth <= 3) as last_3,
       count(*) filter (where r.end_time is not null and r.status = 'failed'
                          and r.start_time > now() - interval '24 hours') as failed_24h,
       max(r.start_time) filter (where r.status = 'failed' and r.end_time is not null)
           as newest_failure_at,
       (array_agg(left(r.return_message, 300) order by r.start_time desc)
           filter (where r.status = 'failed' and r.end_time is not null))[1] as newest_failure
  from cron.job j
  left join newest n on n.jobid = j.jobid
  left join runs r on r.jobid = j.jobid
 group by j.jobname, j.active, n.at
 order by j.jobname
"""

_CRON_EMPTY = "cron.job returned no rows (pg_cron's row security shows a role only its own jobs)"


def _cron_cells(conn: Any, thresholds: dict[str, Any],
                now: _dt.datetime) -> tuple[list[dict[str, Any]], str | None]:
    """One cell per pg_cron job: failures in its newest runs, and lateness on its own gaps."""
    try:
        rows = fetchall(conn, _CRON_JOBS_SQL)
    except _CRON_UNREADABLE as exc:
        return [], f"pg_cron's tables are unreadable ({_oneline(exc)})"
    if not rows:
        return [], _CRON_EMPTY
    fail_of_3 = int(thresholds["jobs_cron_fail_of_3"])
    multiple = float(thresholds["jobs_cron_gap_multiple"])
    min_starts = int(thresholds["jobs_cron_gap_min_starts"])
    cells = []
    for jobname, active, newest, starts, gap, last3, failed_24h, failed_at, failure in rows:
        subject, newest, last3 = f"cron/{jobname}", _as_utc(newest), list(last3 or [])
        starts, failed_24h, gap = int(starts or 0), int(failed_24h or 0), _num(gap)
        facts = {"active": active, "last_start": _iso(newest), "starts_7d": starts,
                 "median_gap_s": _round(gap), "last_3": last3, "failed_24h": failed_24h}
        if not active:
            cells.append(_cell(subject, "ok", "inactive (cron.job.active is false)", facts=facts))
            continue
        if newest is None:
            cells.append(_cell(subject, "unknown", "Not measured: the job has no run on record",
                               facts=facts))
            continue
        error, failed3 = _oneline(failure), last3.count("failed")
        conditions: list[tuple[str, str]] = []
        if failed3 >= fail_of_3:
            conditions.append(("fail", f"{failed3} of its last {len(last3)} runs failed: {error}"))
        elif failed_24h:
            conditions.append(("warn", f"{failed_24h} failed run{'s' * (failed_24h != 1)} in 24 h, "
                                       f"the newest at {_stamp(_as_utc(failed_at))}: {error}"))
        since = (now - newest).total_seconds()
        if starts < min_starts or not gap:
            facts["late"] = f"Not measured: {starts} of {min_starts} starts in its newest 7 days"
        elif since > multiple * gap:
            conditions.append(("fail", f"no start for {_ago(since)}, over {multiple:g}× its "
                                       f"median start gap of {_ago(gap)}"))
        ok = f"last started {_ago(since)} ago; {failed3} of its last {len(last3)} runs failed"
        cells.append(_judged(subject, conditions, ok, value=failed_24h, unit="failed runs",
                             facts=facts))
    return cells, None


# --- jobs: worker lanes -------------------------------------------------------------------

_HEARTBEAT_SQL = """
select beat_at, started_at, details from worker_heartbeats where worker = %(worker)s
"""

# scraper/realtime_worker.LANES, in its order (a check never imports the worker; a test pins the two). The worker seeds
# every lane it registers into its heartbeat at boot, so a lane named here and absent from the beat was not written by
# a worker running this code: an older process still beating (a deploy that did not replace it), which would leave the
# lane's work undone with no cell to say so (the notifications lane is the producers' one clock since W2b).
WORKER_LANES: tuple[str, ...] = (
    "probe", "drain", "images", "count_probe", "maintenance", "broker_maintenance",
    "estimation", "location_resolve", "location_intake_fast", "location_refetch",
    "sold_comps", "text_extract", "health", "autodedup", "notifications", "push", "heartbeat",
)


def _lane_cells(conn: Any, thresholds: dict[str, Any],
                now: _dt.datetime) -> tuple[list[dict[str, Any]], str | None]:
    """A cell per worker lane (the heartbeat lane is the watcher's) and its portals."""
    row = fetchone(conn, _HEARTBEAT_SQL, {"worker": _WORKER})
    if row is None:
        return [], "no realtime-worker heartbeat on record"
    beat_at, booted, details = _as_utc(row[0]), _as_utc(row[1]), row[2]
    beat_age = (now - beat_at).total_seconds() if beat_at else float("inf")
    if beat_age > _BEAT_FRESH_S or booted is None:
        return [], (f"the worker's heartbeat is {_ago(beat_age)} old; the watcher's worker cell "
                    "judges that")
    if not isinstance(details, dict):
        return [], "the heartbeat carries no lane state"
    # The previous measurement and its cells: the lane counters a flap is measured against, and
    # the portals failing then (a portal that recovers leaves the heartbeat, so its cell is
    # carried as OK).
    previous_at, previous = _previous_result(conn, "jobs")
    uptime = (now - booted).total_seconds()
    cells: list[dict[str, Any]] = []
    for lane in sorted(details):
        if lane != "heartbeat" and isinstance(details[lane], dict):
            cells += _lane(lane, details[lane], previous, thresholds, now, booted, uptime,
                           previous_at)
    cells += [_missing_lane(lane, previous) for lane in WORKER_LANES
              if lane != "heartbeat" and lane not in details]
    return cells, None


def _missing_lane(lane: str, previous: dict[str, Any]) -> dict[str, Any]:
    """A registered lane the heartbeat lacks: Watch at once, Problem when the previous measurement read it missing too.

    Once, it can be this worker's own boot (its health lane reaching the read before its first beat replaced the
    predecessor's row); two measurements apart (one cadence, 10 min, against a 30 s beat) it is a worker that lags.
    """
    subject = f"lane/{lane}"
    again = bool(((previous.get(subject) or {}).get("facts") or {}).get("missing"))
    said = ("missing: the heartbeat has no entry for it, so the beat is from a worker older than "
            "this code (a deploy it lags)")
    return _cell(subject, "fail" if again else "warn",
                 said + ("; missing at the previous measurement too" if again else ""),
                 facts={"missing": True})


def _lane(lane: str, info: dict[str, Any], previous: dict[str, Any], thresholds: dict[str, Any],
          now: _dt.datetime, booted: _dt.datetime, uptime: float,
          previous_at: _dt.datetime | None = None) -> list[dict[str, Any]]:
    """The lane's cell (the worst of its conditions) and its portals' cells."""
    subject = f"lane/{lane}"
    interval = _num(info.get("interval_s"))
    last = info.get("last") if isinstance(info.get("last"), dict) else {}
    passes, failed = int(info.get("passes") or 0), int(info.get("failed_passes") or 0)
    last_pass = _as_utc(info.get("last_pass_at"))
    facts = {"interval_s": interval, "passes": passes, "failed_passes": failed,
             "last_pass_at": _iso(last_pass), "in_flight_s": info.get("in_flight_s"),
             "failing_since": info.get("failing_since"), "last_error": info.get("last_error")}
    if "interval_s" not in info:
        # A wave-3 worker seeds every lane's interval_s (None until it wakes), so an entry with no
        # key at all was written by an older worker: the row a first deploy reads until its own
        # beat replaces it, or a predecessor still beating beside it.
        return [_cell(subject, "unknown", "Not measured: its heartbeat entry has no interval_s, "
                      "so the beat is from a worker older than wave 3", facts=facts)]
    if last.get("skipped") == "slice<=0":
        return [_cell(subject, "ok", "off: its slice is 0", facts=facts)]
    if interval is not None and interval <= 0:
        return [_cell(subject, "ok", "off: its interval is 0", facts=facts)]
    grace = float(thresholds["jobs_lane_boot_grace_s"])
    if interval is None:
        if uptime <= grace:
            return []
        return [_cell(subject, "fail", f"never woke: no interval recorded {_ago(uptime)} after "
                      "the worker started", facts=facts)]
    if uptime < grace and (last_pass is None or last_pass < booted):
        return []  # booting, and no pass in this process yet
    floor = float(thresholds["jobs_lane_floor_s"])
    bound = max(3 * interval, interval + 2 * (_num(info.get("last_duration_s")) or 0.0), floor)
    # A lane switched on since the previous measurement (it read off then) has nothing to be late
    # from until it completes a pass: its clock starts at that measurement, never at the boot
    # (autodedup came off its brake at 02:40 on 2026-10-19 and read "late ... 2.1 h" at once).
    held = previous.get(subject) or {}
    held_interval = _num((held.get("facts") or {}).get("interval_s"))
    was_off = held_interval is not None and held_interval <= 0
    reference = last_pass or (max(previous_at, booted) if was_off and previous_at else booted)
    since = (now - reference).total_seconds()
    conditions: list[tuple[str, str]] = []
    if since > bound and uptime > bound:
        conditions.append(("fail", f"late: no completed pass for {_ago(since)} (limit "
                                   f"{_ago(bound)})"))
    failing_since = _as_utc(info.get("failing_since"))
    if failing_since is not None:
        failing_for = (now - failing_since).total_seconds()
        error = _oneline(info.get("last_error")) or "no error text"
        if (interval >= float(thresholds["jobs_lane_daily_interval_s"])
                or failing_for > max(3 * interval, floor)):
            conditions.append(("fail", f"failing for {_ago(failing_for)}: {error}"))
        else:
            conditions.append(("warn", f"its newest pass failed: {error}"))
    in_flight = _num(info.get("in_flight_s"))
    if in_flight is not None and in_flight > float(thresholds["jobs_lane_inflight_fail_s"]):
        conditions.append(("fail", f"one pass in flight for {_ago(in_flight)}"))
    elif in_flight is not None and in_flight > float(thresholds["jobs_lane_inflight_warn_s"]):
        conditions.append(("warn", f"one pass in flight for {_ago(in_flight)}"))
    flap = _flap(previous.get(subject), passes, failed, thresholds)
    if flap:
        conditions.append(("warn", flap))
    # An abort (the engine's pair_budget or deadline: the pass rolled back, nothing committed) is
    # a completed pass to the heartbeat, so failed_passes never counts it: the autodedup lane
    # livelocked for hours on 2026-10-19 while this cell read OK. A refusal is a failed pass (the
    # failing rule above reads it); a failed re-cut is a completed one and is not read here. Only a
    # reason string is an abort: the other lanes' `errors` count items a completed pass failed,
    # and text_extract's `aborted` is True for a pass its own ceiling bounded. The heartbeat
    # replaces `last` when a pass completes or is refused, so one abort is read twice while the
    # next pass runs long or raises: Problem needs the previous measurement to have read an abort
    # too, and a pass to have completed since.
    aborted = _abort_reason(last.get("aborted"))
    facts["last_aborted"] = aborted
    if aborted:
        held_facts = held.get("facts") or {}
        held_aborted = _abort_reason(held_facts.get("last_aborted"))
        held_passes = _num(held_facts.get("passes"))
        if held_aborted and held_passes is not None and passes > held_passes:
            conditions.append(("fail", f"its last pass aborted: {aborted}, as had an earlier "
                                       "pass read at the previous measurement"))
        elif held_aborted and any(isinstance(c, str) and c.startswith("fail: its last pass aborted")
                                  for c in held_facts.get("conditions") or []):
            # A re-read of the same pass keeps a Problem only the abort condition itself read
            # (common.judged stores each as "<level>: <text>"): the cell's status is its worst
            # condition's, and a late or failing lane reads fail beside a single abort.
            conditions.append(("fail", f"its last pass aborted: {aborted}, after an earlier "
                                       "aborted pass"))
        else:
            conditions.append(("warn", f"its last pass aborted: {aborted}"))
    ok = f"last pass {_ago(since)} ago" if last_pass else "no completed pass yet"
    return [_judged(subject, conditions, ok, value=round(since), unit="s", facts=facts),
            *_portal_cells(lane, info, previous, max(3 * interval, floor), now)]


def _abort_reason(value: Any) -> str | None:
    """A pass's abort reason when it names one (the engine's pair_budget or deadline), else None."""
    # The engine's shutdown aborts are excluded, being no livelock: `stopping` (the worker is
    # shutting down) and `lease_lost` (its lease was released under the pass: the shutdown, or a
    # dispatch that took the holder for dead).
    if not isinstance(value, str) or value in (_STOPPING, _LEASE_LOST):
        return None
    return _oneline(value) or None


def _flap(prev: dict[str, Any] | None, passes: int, failed: int,
          thresholds: dict[str, Any]) -> str | None:
    """Half or more of the passes since this check's previous result failed, over enough passes."""
    facts = (prev or {}).get("facts") or {}
    if facts.get("passes") is None or facts.get("failed_passes") is None:
        return None
    done, broke = passes - int(facts["passes"]), failed - int(facts["failed_passes"])
    if done < 0 or broke < 0 or done + broke < int(thresholds["jobs_lane_flap_min_passes"]):
        return None  # counters reset (a boot the seed missed), or too few passes to call it
    if broke / (done + broke) < float(thresholds["jobs_lane_flap_share"]):
        return None
    return f"flapping: {broke} of {done + broke} passes failed since the previous measurement"


def _portal_cells(lane: str, info: dict[str, Any], previous: dict[str, Any], limit: float,
                  now: _dt.datetime) -> list[dict[str, Any]]:
    """Each portal failing inside the lane, and an OK for one that failed at the previous result."""
    errors = info.get("portal_errors") if isinstance(info.get("portal_errors"), dict) else {}
    cells = []
    for portal, entry in sorted(errors.items()):
        since = _as_utc(entry.get("failing_since")) if isinstance(entry, dict) else None
        if since is None:
            continue
        failing_for = (now - since).total_seconds()
        error = _oneline(entry.get("last_error")) or "no error text"
        cells.append(_cell(
            f"lane/{lane}/{portal}", "fail" if failing_for > limit else "warn",
            f"failing for {_ago(failing_for)}: {error}", value=round(failing_for), unit="s",
            portal=portal,
            facts={"failing_since": _iso(since), "last_error": entry.get("last_error")}))
    for subject, held in sorted(previous.items()):
        portal = held.get("portal")
        if subject.startswith(f"lane/{lane}/") and portal and portal not in errors:
            cells.append(_cell(subject, "ok", "not failing", value=0, unit="s", portal=portal))
    return cells


# --- jobs: derived artifacts -----------------------------------------------------------------

# GitHub delivers a scheduled run hours late (the canary's scheduled runs came a median 7.2 h
# apart, at most 13.5 h, 09-28..10-18), so an artifact a workflow produces is stale only past its
# budget plus the registry's GitHub delivery slack; every other host keeps the bare budget.
_ARTIFACTS_SQL = """
select a.name, a.producer, a.host, a.cadence, a.last_succeeded_at, a.last_rows,
       a.last_duration_ms, extract(epoch from a.staleness_budget)::float8 as budget_s,
       s.slack_s, a.is_serving,
       public.health_stale(a.last_succeeded_at,
                           a.staleness_budget + make_interval(secs => s.slack_s)) as stale
  from derived_artifacts a
  cross join lateral (
        select case when a.host = 'github-actions' then %(slack_s)s::float8 else 0 end
               as slack_s) s
 order by a.name
"""


def _artifact_cells(conn: Any, thresholds: dict[str, Any],
                    now: _dt.datetime) -> list[dict[str, Any]]:
    """One cell per derived artifact, stale past its own budget; never succeeded is unknown."""
    cells = []
    for name, producer, host, cadence, last_ok, rows, duration_ms, budget_s, slack_s, serving, \
            stale in fetchall(conn, _ARTIFACTS_SQL,
                              {"slack_s": float(thresholds["GITHUB_DELIVERY_SLACK_S"])}):
        last_ok = _as_utc(last_ok)
        facts = {"producer": producer, "host": host, "cadence": cadence,
                 "last_succeeded_at": _iso(last_ok), "last_rows": rows,
                 "last_duration_ms": duration_ms, "budget_s": budget_s, "slack_s": slack_s,
                 "is_serving": serving}
        if last_ok is None:  # health_stale reads NULL as stale; never succeeded is no verdict
            cells.append(_cell(f"artifact/{name}", "unknown", "Not measured: it has never "
                               "succeeded", facts=facts))
            continue
        age = (now - last_ok).total_seconds()
        slack = f" + {_ago(slack_s)} GitHub delivery slack" if slack_s else ""
        message = (f"stale: last succeeded {_ago(age)} ago, past its {_ago(budget_s)} budget"
                   f"{slack} ({producer}, on {host})" if stale else
                   f"last succeeded {_ago(age)} ago (budget {_ago(budget_s)}{slack})")
        cells.append(_cell(f"artifact/{name}", "fail" if stale else "ok", message,
                           value=round(age / 60), unit="min", facts=facts))
    return cells


def check_jobs(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Is every scheduled machine, on every runtime, running and succeeding? One cell per job."""
    now = _now(conn)
    cells = _github_cells(conn, thresholds, now)
    not_measured: dict[str, str] = {}
    for arm, read in (("cron", _cron_cells), ("lanes", _lane_cells)):
        found, why = read(conn, thresholds, now)
        cells += found
        if why:
            not_measured[arm] = why
    cells += _artifact_cells(conn, thresholds, now)
    return _result("jobs", cells, not_measured)


# --- capacity -------------------------------------------------------------------------------

# Per job, its finished runs' 24 h duration p50/p95, and its 7-day median start gap standing for
# its schedule's interval (no cron-expression parser is a dependency; pg_cron queues a run that
# comes due while the previous one still runs, so a saturated job's gap stretches with it).
_CAPACITY_CRON_SQL = """
with runs as (
    select d.jobid, d.start_time,
           extract(epoch from d.end_time - d.start_time) as duration_s,
           extract(epoch from d.start_time - lag(d.start_time)
                   over (partition by d.jobid order by d.start_time)) as gap_s
      from cron.job_run_details d
     where d.start_time > now() - interval '7 days')
select j.jobname, j.active, j.command,
       percentile_cont(0.5) within group (order by r.duration_s)
           filter (where r.start_time > now() - interval '24 hours') as p50_s,
       percentile_cont(0.95) within group (order by r.duration_s)
           filter (where r.start_time > now() - interval '24 hours') as p95_s,
       count(r.duration_s) filter (where r.start_time > now() - interval '24 hours')
           as finished_24h,
       percentile_cont(0.5) within group (order by r.gap_s) as median_gap_s
  from cron.job j
  left join runs r on r.jobid = j.jobid
 group by j.jobname, j.active, j.command
 order by j.jobname
"""

# The reference an incident cannot erode: each job's weekly p50s of finished runs over whole
# 7-day weeks counted from CAPACITY_REFERENCE_FROM; the week still running never counts.
_CAPACITY_REFERENCE_SQL = """
select jobname, week, percentile_cont(0.5) within group (order by duration_s) as p50_s
  from (select j.jobname,
               floor(extract(epoch from d.start_time - %(ref)s::timestamptz) / 604800) as week,
               extract(epoch from d.end_time - d.start_time) as duration_s
          from cron.job_run_details d
          join cron.job j on j.jobid = d.jobid
         where d.end_time is not null
           and d.start_time >= %(ref)s::timestamptz
           and d.start_time < %(ref)s::timestamptz
                              + floor(extract(epoch from now() - %(ref)s::timestamptz) / 604800)
                                * interval '7 days') w
 group by jobname, week
"""

# The hosts that measure through the harness (verify_pipeline._HARNESS_HOSTS, which a check never
# imports), judged apart: one host's timeouts must not hide in the other's volume. pg_cron's
# watcher keeps its own clock and never writes the harness's `timed_out`.
_HARNESS_HOSTS = ("github-actions", "realtime-worker")

_CHECK_TIMEOUTS_SQL = """
select hc.host, count(*) as results,
       count(*) filter (where r.details ? 'timed_out') as timed_out,
       array_agg(distinct hc.key order by hc.key) filter (where r.details ? 'timed_out')
           as timed_out_keys
  from health_checks hc
  join pipeline_check_results r
    on r.check_key = hc.key and r.run_at > now() - interval '24 hours'
 where hc.host = any(%(hosts)s::text[])
 group by hc.host
"""

# A session this role may not see reads back with backend_type, state and xact_start NULL, so
# it is in neither the client count nor the oldest transaction: `hidden` counts those rows.
_SERVER_SQL = """
select pg_postmaster_start_time() as started_at,
       current_setting('max_connections')::int as max_connections,
       (select count(*) from pg_stat_activity where backend_type = 'client backend') as clients,
       (select count(*) from pg_stat_activity where backend_type is null) as hidden
"""

_OLDEST_TRANSACTIONS_SQL = """
select coalesce(nullif(application_name, ''), backend_type) as who,
       coalesce(state, 'unknown') as state, extract(epoch from now() - xact_start) as age_s
  from pg_stat_activity
 where xact_start is not null and pid <> pg_backend_pid()
   and not starts_with(backend_type, 'autovacuum')
 order by xact_start
 limit 3
"""

_WORKER_MEMORY_SQL = """
select beat_at, started_at, details #>> '{autodedup,last_pass_at}' as last_pass_at,
       jsonb_typeof(details #> '{autodedup,last}') as last_kind,
       details #>> '{autodedup,last,peak_rss_mb}' as peak_rss_mb,
       details #>> '{autodedup,last,memory_limit_mb}' as memory_limit_mb
  from worker_heartbeats
 where worker = %(worker)s
"""

_STATEMENT_TIMEOUT = re.compile(
    r"^\s*set\s+statement_timeout\s*(?:=|to)\s*'?\s*(\d+(?:\.\d+)?)\s*(ms|s|min|h|d)?\s*'?",
    re.IGNORECASE)
_TIMEOUT_UNIT_S = {"ms": 0.001, "s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0}


def _statement_timeout_s(command: str | None) -> float | None:
    """A pg_cron command's `set statement_timeout` prefix in seconds (a bare number is ms)."""
    found = _STATEMENT_TIMEOUT.match(command or "")
    if not found:
        return None
    return float(found.group(1)) * _TIMEOUT_UNIT_S[(found.group(2) or "ms").lower()]


def _capacity_cron_cells(conn: Any, thresholds: dict[str, Any],
                         now: _dt.datetime) -> tuple[list[dict[str, Any]], str | None]:
    """Per pg_cron job: 24 h p50 against its sticky reference, p95 against interval and timeout."""
    ref_day: _dt.date = thresholds["CAPACITY_REFERENCE_FROM"]
    ref = _dt.datetime.combine(ref_day, _dt.time(), tzinfo=_UTC)
    try:
        rows = fetchall(conn, _CAPACITY_CRON_SQL)
        weeks = fetchall(conn, _CAPACITY_REFERENCE_SQL, {"ref": ref})
    except _CRON_UNREADABLE as exc:
        return [], f"pg_cron's tables are unreadable ({_oneline(exc)})"
    if not rows:
        return [], _CRON_EMPTY
    reference: dict[str, float] = {}
    for jobname, _week, p50 in weeks:
        if p50 is not None:
            reference[jobname] = min(float(p50), reference.get(jobname, float("inf")))
    days = (now - ref).total_seconds() / 86400
    min_days = float(thresholds["capacity_reference_min_days"])
    warn_frac, fail_frac = (float(thresholds["capacity_p95_interval_warn"]),
                            float(thresholds["capacity_p95_interval_fail"]))
    cells = []
    for jobname, active, command, p50, p95, finished, gap in rows:
        subject, timeout, ref_p50 = (f"cron/{jobname}", _statement_timeout_s(command),
                                     reference.get(jobname))
        p50, p95, gap, finished = _num(p50), _num(p95), _num(gap), int(finished or 0)
        facts = {"p50_s": _round(p50, 1), "p95_s": _round(p95, 1), "finished_24h": finished,
                 "interval_s": _round(gap), "statement_timeout_s": timeout,
                 "reference_p50_s": _round(ref_p50, 1), "reference_from": ref_day.isoformat()}
        if not active:
            cells.append(_cell(subject, "ok", "inactive (cron.job.active is false)", facts=facts))
            continue
        if not finished or p50 is None or p95 is None:
            cells.append(_cell(subject, "unknown", "Not measured: no finished run in 24 h",
                               facts=facts))
            continue
        conditions: list[tuple[str, str]] = []
        if days < min_days:
            facts["relative"] = (f"Not measured: {max(days, 0.0):.1f} of {min_days:g} days of "
                                 f"history since {ref_day}")
        elif ref_p50 is None:
            facts["relative"] = f"Not measured: no whole week of finished runs since {ref_day}"
        else:
            times = f"{p50 / ref_p50:.1f}×" if ref_p50 > 0 else "more than"
            versus = (f"24 h p50 {p50:.0f} s is {times} its reference {ref_p50:.1f} s (its lowest "
                      f"weekly p50 since {ref_day})")
            for level, multiple, floor in (
                    ("fail", float(thresholds["capacity_p50_fail_multiple"]),
                     float(thresholds["capacity_p50_fail_floor_s"])),
                    ("warn", float(thresholds["capacity_p50_warn_multiple"]),
                     float(thresholds["capacity_p50_warn_floor_s"]))):
                if p50 >= multiple * ref_p50 and p50 >= floor:
                    conditions.append((level, versus))
                    break
        if gap:
            share = p95 / gap
            if share >= warn_frac:
                conditions.append(("fail" if share >= fail_frac else "warn",
                                   f"24 h p95 {p95:.0f} s is {share:.0%} of its {gap:.0f} s "
                                   "interval"))
        if timeout and p95 >= fail_frac * timeout:
            conditions.append(("fail", f"24 h p95 {p95:.0f} s is {p95 / timeout:.0%} of its "
                                       f"{timeout:.0f} s statement_timeout"))
        cells.append(_judged(subject, conditions, f"24 h p50 {p50:.1f} s, p95 {p95:.1f} s",
                             value=_round(p50, 1), unit="s", facts=facts))
    return cells, None


def _timeouts_cells(conn: Any, thresholds: dict[str, Any]) -> list[dict[str, Any]]:
    """Per harness host, the share of its last 24 h's check results that ran out of budget."""
    read = {host: (int(total or 0), int(timed_out or 0), list(keys or []))
            for host, total, timed_out, keys in fetchall(
                conn, _CHECK_TIMEOUTS_SQL, {"hosts": list(_HARNESS_HOSTS)})}
    # 5 % and 20 % were measured on the GitHub population; the worker's cell reads the same.
    warn = float(thresholds["capacity_timeout_share_warn"])
    fail = float(thresholds["capacity_timeout_share_fail"])
    cells = []
    for host in _HARNESS_HOSTS:
        subject, (total, timed_out, keys) = f"check_timeouts/{host}", read.get(host, (0, 0, []))
        if not total:
            cells.append(_cell(subject, "unknown", f"Not measured: no check result from {host} "
                               "in 24 h", facts={"host": host}))
            continue
        share = timed_out / total
        named = f": {', '.join(keys)}" if keys else ""
        cells.append(_cell(subject, _graded(share, warn, fail),
                           f"{timed_out} of {total} check results on {host} in 24 h ran out of "
                           f"their budget ({share:.1%}){named}", value=round(share * 100, 1),
                           unit="%", facts={"host": host, "results_24h": total,
                                            "timed_out_24h": timed_out, "timed_out_keys": keys}))
    return cells


def _server_cells(conn: Any, thresholds: dict[str, Any], now: _dt.datetime) -> list[dict[str, Any]]:
    """The oldest open transaction, connections against max_connections, the last restart."""
    started, max_connections, clients, hidden = fetchone(conn, _SERVER_SQL)
    started, hidden = _as_utc(started), int(hidden or 0)
    up = (now - started).total_seconds()
    restart = _cell("db_restart", "ok", f"the database last started {_stamp(started)} "
                    f"({_ago(up)} ago)", value=round(up / 3600, 1), unit="h",
                    facts={"started_at": _iso(started)})
    if hidden:
        why = ("Not measured: pg_stat_activity hides other sessions from this role "
               f"({hidden} read without a backend_type)")
        return [_cell("open_transaction", "unknown", why, facts={"hidden_sessions": hidden}),
                _cell("connections", "unknown", why, facts={"hidden_sessions": hidden}), restart]
    oldest = fetchall(conn, _OLDEST_TRANSACTIONS_SQL)
    minutes = float(oldest[0][2]) / 60 if oldest else 0.0
    who = ", ".join(f"{name} [{state}] {float(age) / 60:.0f} min" for name, state, age in oldest)
    xact = _cell("open_transaction",
                 _graded(minutes, float(thresholds["capacity_open_xact_warn_min"]),
                         float(thresholds["capacity_open_xact_fail_min"])),
                 f"the oldest open transaction is {minutes:.0f} min old"
                 + (f" ({who})" if who else ""), value=round(minutes, 1), unit="min",
                 facts={"oldest": [{"who": n, "state": s, "age_min": round(float(a) / 60, 1)}
                                   for n, s, a in oldest]})
    share = int(clients) / int(max_connections)
    connections = _cell("connections",
                        _graded(share, float(thresholds["capacity_connections_warn"]),
                                float(thresholds["capacity_connections_fail"])),
                        f"{clients} of {max_connections} connections in use ({share:.0%})",
                        value=round(share * 100, 1), unit="%",
                        facts={"clients": int(clients), "max_connections": int(max_connections)})
    return [xact, connections, restart]


def _memory_cell(conn: Any, thresholds: dict[str, Any], now: _dt.datetime) -> dict[str, Any]:
    """The worker's peak RSS since boot against its container limit; autodedup's pass reads both."""
    row = fetchone(conn, _WORKER_MEMORY_SQL, {"worker": _WORKER})
    if row is None:
        return _cell("worker_memory", "unknown",
                     "Not measured: no realtime-worker heartbeat on record")
    beat_at, booted, last_pass, last_kind, peak, limit = row
    beat_age = (now - _as_utc(beat_at)).total_seconds()
    if beat_age > _BEAT_FRESH_S:
        return _cell("worker_memory", "unknown", f"Not measured: the worker's heartbeat is "
                     f"{_ago(beat_age)} old")
    booted, last_pass = _as_utc(booted), _as_utc(last_pass)
    if booted is None or last_pass is None or last_pass < booted:
        # The boot seed carries the previous process's `last` (the text lane reads it), and a
        # peak RSS read there is that process's, not this one's.
        return _cell("worker_memory", "unknown", "Not measured: no autodedup pass since the "
                     "worker booted", facts={"last_pass_at": _iso(last_pass), "boot": _iso(booted)})
    if last_kind != "object" or peak is None:
        return _cell("worker_memory", "unknown", "Not measured: the autodedup lane has no pass "
                     "with a memory reading on record (its pass reads the only one)")
    if limit is None or float(limit) <= 0:
        return _cell("worker_memory", "unknown",
                     "Not measured: the container reports no memory limit")
    peak, limit = float(peak), float(limit)
    share = peak / limit
    return _cell("worker_memory", _graded(share, float(thresholds["capacity_memory_warn"]),
                                          float(thresholds["capacity_memory_fail"])),
                 f"peak RSS {peak / 1024:.1f} GB of its {limit / 1024:.1f} GB limit ({share:.0%})",
                 value=round(share * 100, 1), unit="%",
                 facts={"peak_rss_mb": peak, "memory_limit_mb": limit})


def check_capacity(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Is the database, and the worker's memory, keeping up?"""
    now = _now(conn)
    cells, why = _capacity_cron_cells(conn, thresholds, now)
    cells += [*_timeouts_cells(conn, thresholds), *_server_cells(conn, thresholds, now),
              _memory_cell(conn, thresholds, now)]
    return _result("capacity", cells, {"cron": why} if why else {})


# --- llm ------------------------------------------------------------------------------------

# Every provider that ever made a call: one with none in the lookback still gets its idle cell.
_LLM_PROVIDERS_SQL = "select distinct provider from llm_cost_hour_rollup"

_LLM_CALLS_SQL = """
select provider, called_for,
       max(called_at) filter (where error is null) as last_ok_at,
       max(called_at) filter (where error is not null) as last_err_at,
       max(called_at) filter (where credit) as last_credit_at,
       count(*) filter (where credit) as credit_errors,
       array_agg(distinct model) filter (where credit) as credit_models,
       count(*) filter (where called_at > now() - interval '24 hours') as calls_24h,
       count(*) filter (where called_at > now() - interval '24 hours' and error is not null)
           as errors_24h,
       count(*) filter (where called_at > now() - interval '1 hour') as attempts_1h,
       count(*) filter (where called_at > now() - interval '1 hour' and error is null)
           as successes_1h,
       coalesce(sum(cost_usd) filter (where called_at > now() - interval '24 hours'), 0)
           as spend_24h
  from (select provider, called_for, model, called_at, error, cost_usd,
               coalesce(error ilike %(credit_a)s or error ilike %(credit_b)s, false) as credit
          from llm_calls
         where called_at > now() - make_interval(hours => %(lookback_h)s::int)) c
 group by provider, called_for
 order by provider, called_for
"""

_LLM_CREDIT_ERROR_SQL = """
select distinct on (provider) provider, called_at, called_for, model, left(error, 300) as error
  from llm_calls
 where called_at > now() - make_interval(hours => %(lookback_h)s::int)
   and (error ilike %(credit_a)s or error ilike %(credit_b)s)
 order by provider, called_at desc
"""


def _provider_cells(provider: str, rows: list[tuple[Any, ...]], credit: tuple[Any, ...] | None,
                    thresholds: dict[str, Any], now: _dt.datetime) -> list[dict[str, Any]]:
    """The provider's credit cell, then one per job calling it."""
    last_ok = max((_as_utc(r[2]) for r in rows if r[2]), default=None)
    last_err = max((_as_utc(r[3]) for r in rows if r[3]), default=None)
    last_credit = max((_as_utc(r[4]) for r in rows if r[4]), default=None)
    facts = {"last_success_at": _iso(last_ok), "last_error_at": _iso(last_err),
             "last_credit_error_at": _iso(last_credit),
             "credit_errors": sum(int(r[5] or 0) for r in rows),
             "credit_models": sorted({m for r in rows for m in (r[6] or [])}),
             "credit_jobs": sorted(r[1] for r in rows if r[5])}
    lookback = f"{int(thresholds['llm_lookback_h'])} h"
    success = (f"its newest success ({_ago((now - last_ok).total_seconds())} ago)" if last_ok
               else None)
    if last_credit is not None and (last_ok is None or last_credit > last_ok):
        at, called_for, model, text = credit or (last_credit, "", "", "")
        facts["error"] = text
        since = f"is newer than {success}" if success else f"and nothing succeeded in {lookback}"
        cells = [_cell(f"{provider}/credit", "fail", f"out of credit: its newest credit error "
                       f"({_stamp(_as_utc(at))}, {called_for} on {model}) {since}: "
                       f"{_oneline(text)}", facts=facts)]
    else:
        cells = [_cell(f"{provider}/credit", "ok", f"serving: no credit error since {success}"
                       if success else f"no credit error and no success in {lookback}",
                       facts=facts)]
    failing_now = last_err is not None and (last_ok is None or last_err > last_ok)
    return cells + [_job_cell(provider, row, failing_now, thresholds) for row in rows]


def _job_cell(provider: str, row: tuple[Any, ...], failing_now: bool,
              thresholds: dict[str, Any]) -> dict[str, Any]:
    """One job's calls to one provider: starved in the last hour, or erroring while it still is."""
    calls, errors, attempts, successes = (int(v or 0) for v in row[7:11])
    share = errors / calls if calls else 0.0
    conditions: list[tuple[str, str]] = []
    if attempts >= int(thresholds["llm_starved_min_attempts"]) and not successes:
        conditions.append(("fail", f"starved: {attempts} calls in the last hour, none succeeded"))
    if (calls >= int(thresholds["llm_error_share_min_calls"]) and failing_now
            and share >= float(thresholds["llm_error_share_warn"])):
        conditions.append(("warn", f"{share:.0%} of its {calls} calls in 24 h failed, and "
                                   f"{provider}'s newest call is an error"))
    ok = f"{calls} calls in 24 h, {errors} failed" if calls else "no call in 24 h"
    facts = {"calls_24h": calls, "errors_24h": errors, "attempts_1h": attempts,
             "successes_1h": successes, "spend_24h_usd": round(float(row[11] or 0), 2)}
    return _judged(f"{provider}/{row[1]}", conditions, ok, value=round(share * 100, 1), unit="%",
                   facts=facts)


def _spend_cell(spend: list[tuple[str, float]], thresholds: dict[str, Any]) -> dict[str, Any]:
    """Fleet spend over 24 h, with the three jobs that spent most."""
    total = sum(usd for _, usd in spend)
    warn = float(thresholds["llm_spend_24h_warn_usd"])
    fail = float(thresholds["llm_spend_24h_fail_usd"])
    top = ", ".join(f"{name} ${usd:.2f}" for name, usd in sorted(spend, key=lambda s: -s[1])[:3]
                    if usd > 0) or "none"
    status = _graded(total, warn, fail)
    limit = {"fail": f", over ${fail:.0f}", "warn": f", over ${warn:.0f}"}.get(status, "")
    return _cell("fleet/spend", status, f"${total:.2f} spent in 24 h{limit} (top: {top})",
                 value=round(total, 2), unit="USD", facts={"warn_usd": warn, "fail_usd": fail})


def check_llm(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Are LLM providers serving, per provider and per job, and is spend sane?"""
    now = _now(conn)
    params = {"lookback_h": int(thresholds["llm_lookback_h"]),
              "credit_a": _CREDIT_ERROR_PATTERNS[0], "credit_b": _CREDIT_ERROR_PATTERNS[1]}
    known = {row[0] for row in fetchall(conn, _LLM_PROVIDERS_SQL)}
    calls: dict[str, list[tuple[Any, ...]]] = {}
    for row in fetchall(conn, _LLM_CALLS_SQL, params):
        calls.setdefault(row[0], []).append(row)
    credit = {row[0]: row[1:] for row in fetchall(conn, _LLM_CREDIT_ERROR_SQL, params)}
    cells: list[dict[str, Any]] = []
    for provider in sorted(known | set(calls)):
        rows = calls.get(provider)
        if not rows:
            cells.append(_cell(provider, "ok", f"idle: no call in {params['lookback_h']} h",
                               facts={"state": "idle"}))
            continue
        cells += _provider_cells(provider, rows, credit.get(provider), thresholds, now)
    spend = [(f"{p}/{r[1]}", float(r[11] or 0)) for p, rows in calls.items() for r in rows]
    cells.append(_spend_cell(spend, thresholds))
    return _result("llm", cells, {})


# --- migration drift: merged is not applied (2026-08-25 outage) -----------------------------
#
# Applying a migration is a SEPARATE act from merging it, and nothing connected the two.
# Migration 438 merged at 17:12 and was applied 29 hours later; in between, every write on six
# portals violated a CHECK constraint the code assumed existed, and scrape_runs.errors read 0
# the whole time. CI proves a migration REPLAYS against an empty database; it says nothing
# about production. This asks the live catalog directly: for each of the newest migrations, do
# the objects it declares actually exist? The ledger (supabase_migrations.schema_migrations) is
# deliberately NOT the oracle; scripts/migration_objects.py says why. One cell per file, OK ones
# included, so a file applied late recovers on a measured OK. Unverifiable keeps its old meaning
# (no object the probe can look up); a file that only redefines objects an earlier file already
# made reads OK, not unverifiable as the design's § 4 asks: owed with the apply ledger (#1855).

_UNVERIFIABLE = ("Unverifiable: it declares no object the catalog probe can look up (only "
                 "grants, drops, data updates or cron changes, or objects a later migration "
                 "retires), so this check cannot see whether it was applied.")


def _migration_cell(filename: str, objects: list[Any],
                    present: dict[tuple[str, str], bool]) -> dict[str, Any]:
    """One migration file: applied, not applied, partly applied, or unverifiable."""
    if not objects:
        return _cell(filename, "unknown", _UNVERIFIABLE, facts={"objects": 0})
    absent = [o for o in objects if not present.get((o.kind, o.ident), False)]
    facts: dict[str, Any] = {"objects": len(objects), "absent": len(absent)}
    if not absent:
        return _cell(filename, "ok", f"Applied: all {len(objects)} of its objects exist.",
                     value=0, unit="objects absent", facts=facts)
    facts["first_absent"] = str(absent[0])
    if len(absent) == len(objects):
        return _cell(filename, "fail", (
            f"Not applied: none of its {len(objects)} objects exist ({absent[0]} absent), so "
            "code that assumes this schema fails on every write. Apply it."),
            value=len(absent), unit="objects absent", facts=facts)
    return _cell(filename, "warn", (
        f"Partly applied: {len(absent)} of its {len(objects)} objects are absent ({absent[0]}): "
        "a half-applied migration, or the object parser misread the file."),
        value=len(absent), unit="objects absent", facts=facts)


def check_migration_drift(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Are the newest merged migrations present in this database? One cell per file."""
    newest = int(thresholds["migration_drift_window"])
    migrations = load_migrations(_MIGRATIONS_DIR, newest=newest)
    if not migrations:
        detail = "no migration files on this host"
        return {
            "check_key": "migration_drift", "status": "unknown", "value": None,
            "details": {"window": newest, "skipped": detail},
            "message": (f"Migration drift is Not measured: {detail}, so no merged migration "
                        "was probed (the probe reads a repository checkout's migrations/)."),
        }

    checkable = {mig.filename: [o for o in mig.objects if SAFE_IDENT.match(o.ident)]
                 for mig in migrations}
    probes = [(o.kind, o.ident) for objects in checkable.values() for o in objects]
    present: dict[tuple[str, str], bool] = {}
    if probes:
        for kind, ident, is_present in fetchall(conn, MIGRATION_OBJECT_PROBE_SQL, {
            "kinds": [k for k, _ in probes], "idents": [i for _, i in probes],
        }):
            present[(kind, ident)] = bool(is_present)
    return _result("migration_drift",
                   [_migration_cell(mig.filename, checkable[mig.filename], present)
                    for mig in migrations], {})
