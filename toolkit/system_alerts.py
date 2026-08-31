"""In-app system-health alerts through the unified notification feed.

A `system_health` producer for `notification_dispatches` (migration 274): a red
pipeline-verification check — or a stalled LLM pipeline — inserts one append-only
dispatch row so the SPA nav bell badge lights up, reusing the whole existing
Notifications surface instead of a parallel alerting path.

Dependency-free by design (stdlib + a caller-passed psycopg connection) so both the
FastAPI service and the standalone verification script can call it.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from typing import Any


def _today_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def _system_health_channels(conn: Any) -> list[str]:
    """Operator-chosen external channels for system alerts (default [] = in-app only)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM app_settings WHERE key = 'system_health_channels'"
        )
        row = cur.fetchone()
    raw = row[0] if row and row[0] is not None else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, str) and c]


def emit_system_alert(
    conn: Any, check_key: str, message: str, *,
    day: str | None = None, dedupe_key: str | None = None,
) -> bool:
    """Insert a system_health notification_dispatches row; return whether one was inserted.

    Idempotent via `dedupe_key` + `ON CONFLICT (dedupe_key) DO NOTHING` (a repeat is a
    no-op returning False). When `dedupe_key` is not given it falls back to the legacy
    per-UTC-day key `sys:{check_key}:{day or today}` (at most one alert/day). The
    transition emitter passes an explicit edge-anchored key instead — see
    `emit_transition_alerts`.
    """
    key = dedupe_key or f"sys:{check_key}:{day or _today_utc()}"
    channels = _system_health_channels(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO notification_dispatches "
            "  (source_kind, change_kind, channel, status, message, dedupe_key, target_channels) "
            "VALUES ('system_health', 'system_alert', 'in_app', 'sent', %s, %s, %s::text[]) "
            "ON CONFLICT (dedupe_key) DO NOTHING",
            (message, key, channels),
        )
        return (cur.rowcount or 0) > 0


def _iso(run_at: _dt.datetime) -> str:
    """Stable second-resolution UTC stamp for incident-anchored dedupe keys."""
    return run_at.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_utc(value: _dt.datetime) -> _dt.datetime:
    """Tolerate a naive timestamp from a caller or a fixture; the column is timestamptz."""
    if value.tzinfo is None:
        return value.replace(tzinfo=_dt.timezone.utc)
    return value.astimezone(_dt.timezone.utc)


@dataclass(frozen=True)
class AlertPolicy:
    """The one escalation policy every producer of check results inherits (W3.4).

    `reescalate_hours` are the fixed rungs after an incident's onset; `weekly_hours`
    is the repeating rung that follows them forever (a red that nobody fixes must keep
    saying so, at a cadence that does not become wallpaper). `flap_cooldown_hours` is
    the hysteresis: a check that returns to `fail` within that window re-enters the SAME
    incident instead of opening a new one, and a recovery is only announced once the
    check has held non-fail for that long.
    """

    reescalate_hours: tuple[float, ...] = (6.0, 24.0, 72.0)
    weekly_hours: float = 168.0
    flap_cooldown_hours: float = 6.0

    @classmethod
    def from_thresholds(cls, thresholds: dict[str, Any]) -> "AlertPolicy":
        """Build from `app_settings.pipeline_check_thresholds` (scalars only —
        `verify_pipeline.load_thresholds` drops any non-scalar value from the DB merge,
        so the ladder is spelled as one key per rung and never as a JSON array)."""
        def _num(key: str, fallback: float) -> float:
            v = thresholds.get(key, fallback)
            return float(v) if isinstance(v, (int, float)) else fallback

        rungs = tuple(sorted(
            h for h in (_num("alert_reescalate_1_hours", 6.0),
                        _num("alert_reescalate_2_hours", 24.0),
                        _num("alert_reescalate_3_hours", 72.0)) if h > 0
        ))
        return cls(
            reescalate_hours=rungs,
            weekly_hours=_num("alert_reescalate_weekly_hours", 168.0),
            flap_cooldown_hours=_num("alert_flap_cooldown_hours", 6.0),
        )

    def due_rung(self, elapsed_hours: float) -> str | None:
        """The HIGHEST rung this incident has reached, or None. Only the highest is
        emitted, so an incident that predates the ladder's deploy (or a lane that was
        down for days) produces ONE alert, never a backlog of every overdue rung."""
        label: str | None = None
        for h in self.reescalate_hours:
            if elapsed_hours >= h:
                label = f"{h:g}h"
        if self.weekly_hours > 0 and elapsed_hours >= self.weekly_hours:
            label = f"w{int(elapsed_hours // self.weekly_hours)}"
        return label


@dataclass(frozen=True)
class CheckState:
    """A check's stored history, collapsed to what the ladder needs.

    `incident_started_at` is the first `fail` of the current streak *under the flap
    cooldown* — short green blips inside the window do not restart the clock, which is
    what keeps a flapping check on one incident instead of one per oscillation.
    `last_run_at` is what makes the delayed recovery fire exactly once: the incident is
    announced closed on the first run that observes the cooldown elapsed.
    """

    status: str | None
    incident_started_at: _dt.datetime | None = None
    last_fail_at: _dt.datetime | None = None
    last_run_at: _dt.datetime | None = None
    fail_runs: int = 0

    @property
    def timestamped(self) -> bool:
        """False for a state synthesised from a bare status map (legacy callers)."""
        return self.last_run_at is not None


def _collapse(rows: list[tuple[str, _dt.datetime]], cooldown_h: float) -> CheckState:
    """Newest-first (status, run_at) rows for ONE check → its CheckState.

    An unbroken run of `fail` rows is ONE incident however far apart the runs are — the
    cooldown bounds green stretches, never observation gaps. (The 6h lane really runs
    80-256 min late under the Actions throttle; a gap-based rule would restart the
    incident on a throttled run and re-alert onset every single time.)
    """
    if not rows:
        return CheckState(status=None)
    cooldown = _dt.timedelta(hours=max(cooldown_h, 0.0))
    status, last_run_at = rows[0][0], rows[0][1]
    first_fail = next((i for i, (s, _) in enumerate(rows) if s == "fail"), None)
    if first_fail is None:
        return CheckState(status=status, last_run_at=last_run_at)
    last_fail_at = rows[first_fail][1]
    started_at, anchor, runs = last_fail_at, last_fail_at, 1
    blip = False  # a non-fail row stands between `anchor` and the row being examined
    for s, at in rows[first_fail + 1:]:
        if s != "fail":
            if anchor - at > cooldown:
                break  # green for longer than the cooldown: the incident starts after it
            blip = True
            continue
        if blip and anchor - at > cooldown:
            break
        started_at, anchor, runs, blip = at, at, runs + 1, False
    return CheckState(
        status=status, incident_started_at=started_at, last_fail_at=last_fail_at,
        last_run_at=last_run_at, fail_runs=runs,
    )


def check_states(
    conn: Any, *, policy: AlertPolicy | None = None, history_days: int = 30,
) -> dict[str, CheckState]:
    """Per-check stored history (call BEFORE writing this run's rows, so it is the
    baseline this run's results are compared against).

    Bounded to `history_days` because an incident older than that has already climbed
    every rung; the index is `pipeline_check_results_key_run_idx (check_key, run_at desc)`.
    """
    pol = policy or AlertPolicy()
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=history_days)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT check_key, status, run_at FROM pipeline_check_results "
            "WHERE run_at >= %s ORDER BY check_key, run_at DESC",
            (cutoff,),
        )
        rows = cur.fetchall()
    grouped: dict[str, list[tuple[str, _dt.datetime]]] = {}
    for key, status, run_at in rows:
        grouped.setdefault(str(key), []).append((str(status), _as_utc(run_at)))
    return {k: _collapse(v, pol.flap_cooldown_hours) for k, v in grouped.items()}


def latest_statuses(conn: Any) -> dict[str, str]:
    """Status-only view of `check_states` (the ladder needs the timestamps too)."""
    return {k: s.status for k, s in check_states(conn).items() if s.status is not None}


def emit_transition_alerts(
    conn: Any,
    results: list[dict[str, Any]],
    prev_statuses: dict[str, str],
    run_at: _dt.datetime,
    *,
    states: dict[str, CheckState] | None = None,
    policy: AlertPolicy | None = None,
) -> dict[str, int]:
    """Ring the bell on INCIDENTS, not on every red run and not on every edge.

    An incident opens at a check's first `fail` and stays open while the check keeps
    failing OR flaps back to `fail` within `policy.flap_cooldown_hours`. Every key is
    anchored on that one onset timestamp, so `ON CONFLICT (dedupe_key) DO NOTHING`
    gives exactly-once semantics per incident across every lane and cadence:

      * onset        `sys:{k}:onset:{incident_start}`         — once, at the first fail.
      * re-escalation `sys:{k}:reesc:{rung}:{incident_start}` — at 6h / 24h / 72h and
        then weekly while it stays red. Only the highest DUE rung fires per run, so a
        long-running red says so periodically instead of going silent for six days
        (`property_maintenance`, red 2026-08-20..26 with one alert on day one).
      * recovery     `sys:{k}:recovery:{incident_start}`      — once, and only after the
        check has held non-fail for the cooldown. An outage that oscillates hourly
        (`llm_errors`: 114 alternating onset/recovery alerts for an outage that never
        recovered) therefore produces ONE onset and ONE recovery, not 114.

    ok↔warn never rings (warn is dashboard-only). `states` is `check_states(conn)` read
    before this run's writes; without it the emitter degrades to the pre-ladder
    edge-only behaviour keyed on `run_at`, so a caller holding only a status map keeps
    working (no ladder, since an incident's age is unknowable from a status alone).
    """
    pol = policy or AlertPolicy()
    counts = {"onset": 0, "recovery": 0, "reescalation": 0}
    now = _as_utc(run_at)
    cooldown = _dt.timedelta(hours=max(pol.flap_cooldown_hours, 0.0))
    for r in results:
        k = r["check_key"]
        curr = r["status"]
        st = (states or {}).get(k) or CheckState(status=prev_statuses.get(k))
        if curr == "fail":
            # An unbroken red continues its incident however late this run is (the lanes
            # run 80-256 min behind schedule under the Actions throttle, and a purely
            # gap-based rule would re-alert onset on every throttled run). Only a GREEN
            # stretch longer than the cooldown ends it.
            continuing = (
                st.timestamped and st.last_fail_at is not None
                and (st.status == "fail" or now - st.last_fail_at <= cooldown)
            )
            if continuing:
                start = st.incident_started_at or now
            elif not st.timestamped and st.status == "fail":
                continue  # legacy ongoing branch: prev=fail, no anchor to reason from
            else:
                start = now
            anchor = _iso(start)
            msg = r.get("message") or f"Pipeline check '{k}' failed."
            if emit_system_alert(conn, k, msg, dedupe_key=f"sys:{k}:onset:{anchor}"):
                counts["onset"] += 1
                continue
            rung = pol.due_rung((now - start).total_seconds() / 3600.0)
            if rung is None:
                continue
            hours = (now - start).total_seconds() / 3600.0
            runs = st.fail_runs + 1
            note = (
                f"⚠ Still failing ({rung}): '{k}' has been red for {hours:.0f}h "
                f"({runs} failing runs since {anchor}). {msg}"
            )
            if emit_system_alert(
                conn, k, note, dedupe_key=f"sys:{k}:reesc:{rung}:{anchor}"
            ):
                counts["reescalation"] += 1
            continue
        # Not failing: announce recovery once the incident has actually closed.
        if not st.timestamped:
            if st.status == "fail":
                note = f"✓ Recovered: '{k}' is healthy again (now {curr})."
                if emit_system_alert(
                    conn, k, note, dedupe_key=f"sys:{k}:recovery:{_iso(now)}"
                ):
                    counts["recovery"] += 1
            continue
        if st.last_fail_at is None or st.incident_started_at is None:
            continue
        closed_now = now - st.last_fail_at > cooldown
        closed_before = (
            st.last_run_at is not None and st.last_run_at - st.last_fail_at > cooldown
        )
        if not closed_now or closed_before:
            continue  # still inside the flap window, or already announced on an earlier run
        red_h = (st.last_fail_at - st.incident_started_at).total_seconds() / 3600.0
        note = (
            f"✓ Recovered: '{k}' is healthy again (now {curr}) after {red_h:.0f}h red "
            f"({st.fail_runs} failing runs since {_iso(st.incident_started_at)})."
        )
        if emit_system_alert(
            conn, k, note,
            dedupe_key=f"sys:{k}:recovery:{_iso(st.incident_started_at)}",
        ):
            counts["recovery"] += 1
    return counts


def _iso_week(run_at: _dt.datetime) -> str:
    year, week, _ = _as_utc(run_at).isocalendar()
    return f"{year}-W{week:02d}"


def emit_weekly_heartbeat(
    conn: Any,
    results: list[dict[str, Any]],
    states: dict[str, CheckState],
    run_at: _dt.datetime,
) -> bool:
    """One digest per ISO week — the operator-facing half of the dead-man switch.

    Migration 274's `emit_verification_stale_alert` catches a dead harness from INSIDE
    the database (no fresh `pipeline_check_results` row → ring). This catches the case
    that one cannot: the harness runs, the DB is fine, and the whole delivery path is
    silently broken. A heartbeat that stops arriving is itself the signal.

    Keyed `sys:heartbeat:{ISO-week}` because `verify_pipeline.yml` appends `--weekly` to
    all four of Monday's 6-hourly runs — without a week-grain key it would fire 4x.
    """
    statuses = {k: s.status for k, s in states.items() if s.status is not None}
    statuses.update({r["check_key"]: r["status"] for r in results})
    failing = sorted(k for k, s in statuses.items() if s == "fail")
    if failing:
        parts = []
        for k in failing:
            st = states.get(k)
            if st is not None and st.incident_started_at is not None:
                hours = (_as_utc(run_at) - st.incident_started_at).total_seconds() / 3600
                parts.append(f"{k} ({hours:.0f}h)")
            else:
                parts.append(k)
        note = (
            f"Weekly health digest: {len(failing)} of {len(statuses)} checks are "
            f"failing — {', '.join(parts)}."
        )
    else:
        note = (
            f"✓ Weekly health digest: all {len(statuses)} pipeline checks are healthy. "
            "This heartbeat is the proof that the alerting path itself is alive — if it "
            "stops arriving, treat the silence as a failure."
        )
    return emit_system_alert(
        conn, "weekly_heartbeat", note,
        dedupe_key=f"sys:heartbeat:{_iso_week(run_at)}",
    )
