"""record_workflow_failures.py — poll the Actions API, persist failed runs.

GitHub only emails the operator about failed SCHEDULED runs, so push- and
dispatch-triggered failures stay invisible unless someone opens the Actions
tab. This poller (monitor_workflow_failures.yml) lists recently completed runs,
keeps the failed-ish ones, and inserts them into `workflow_failures` (migration
178) — idempotent via ON CONFLICT (run_id) DO NOTHING. The Health page surfaces
the table.

Windowing is a HIGH-WATER-MARK CURSOR (`app_settings.workflow_failures_cursor`),
not a fixed lookback. The monitor's cron is `*/30` but the GitHub Actions throttle
runs it 80–256 min apart, so the old fixed 40-min lookback silently dropped every
red run that completed in the uncovered gap (13 liveness reds → only 2 recorded).
The cursor advances to the newest run seen each poll and pages back until it
reaches the previous cursor, so no completed run is skipped.

`cancelled` is recorded ONLY when the run ran at least
`CANCELLED_MIN_DURATION_MINUTES`: a `timeout-minutes` kill (which GitHub reports as
`cancelled`) runs to its budget, whereas a `cancel-in-progress` supersession is
killed in seconds. Without this gate, enabling cancel-in-progress anywhere would
flood the table with superseded runs.

Needs GITHUB_REPOSITORY + GITHUB_TOKEN (the default Actions token with
`actions: read`) + SUPABASE_DB_URL.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

LOG = logging.getLogger("record_workflow_failures")

# Must match the workflow's `name:` — the poller never records its own runs,
# or one red poll would keep re-alarming the surface it feeds.
MONITOR_WORKFLOW_NAME = "Monitoring: workflow failures"

# Always-record conclusions. `cancelled` is handled conditionally (duration gate).
ALERT_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
CANCELLED_MIN_DURATION_MINUTES = 8   # >= this ⇒ a timeout-minutes kill, not a supersession
BOOTSTRAP_MINUTES = 120              # first-ever run: how far back to seed the cursor
CURSOR_OVERLAP_MINUTES = 5           # re-scan slightly before the cursor (ON CONFLICT makes it safe)
PER_PAGE = 100
MAX_PAGES = 5                        # 500 runs — ample for the real 80–256-min gap; crawls if exceeded
CURSOR_KEY = "workflow_failures_cursor"

API_TIMEOUT_S = 30


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_minutes(run: dict[str, Any]) -> float | None:
    """Run wall-clock, using `updated_at` as the completion proxy (the list endpoint
    has no completed-at field)."""
    started = parse_ts(run.get("run_started_at"))
    completed = parse_ts(run.get("updated_at"))
    if started is None or completed is None:
        return None
    return (completed - started).total_seconds() / 60.0


def select_failed_runs(
    runs: list[dict[str, Any]],
    *,
    since: datetime,
    exclude_name: str = MONITOR_WORKFLOW_NAME,
    cancelled_min_duration: float = CANCELLED_MIN_DURATION_MINUTES,
    started_check: Any = None,
) -> list[dict[str, Any]]:
    """Pure filter over parsed `/actions/runs` JSON (no network, testable).

    Keeps runs completed at/after `since` (completion ≈ `updated_at`) with an alerting
    conclusion, excluding the monitor workflow. `cancelled` is kept only when the run ran
    >= `cancelled_min_duration` minutes — a timeout-minutes kill, not a quick supersession.

    `started_check(run_id) -> bool` closes the duration gate's blind spot: a superseded run
    that sat QUEUED >= 8 min before cancel-in-progress killed it never ran at all, yet its
    run_started_at (= queue entry) makes the duration look like a timeout kill (8 of 10
    RealityMix/iDNES drain cancels on 2026-07-11 were falsely recorded this way). When
    provided, a long-looking cancelled run is kept only if at least one JOB actually
    started; None (e.g. in pure tests) keeps the duration-only behavior.
    """
    out: list[dict[str, Any]] = []
    for run in runs:
        if run.get("name") == exclude_name:
            continue
        completed_at = parse_ts(run.get("updated_at"))
        if completed_at is None or completed_at < since:
            continue
        conclusion = run.get("conclusion")
        if conclusion in ALERT_CONCLUSIONS:
            pass
        elif conclusion == "cancelled":
            dur = _duration_minutes(run)
            if dur is None or dur < cancelled_min_duration:
                continue  # supersession / quick cancel — not a real failure
            if started_check is not None and not started_check(int(run["id"])):
                continue  # never-started queued run superseded — not a failure
        else:
            continue
        out.append(
            {
                "run_id": int(run["id"]),
                "workflow_name": run.get("name") or "(unnamed)",
                "workflow_path": run.get("path"),
                "conclusion": conclusion,
                "run_started_at": parse_ts(run.get("run_started_at")),
                "html_url": run.get("html_url"),
            }
        )
    return out


def select_latest_successes(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Latest SUCCESS run per workflow path (no network, testable).

    Feeds workflow_run_health so the streak resets when a job recovers. Keyed on
    the stable `.path`; runs without a path or a success conclusion are skipped.
    """
    best: dict[str, dict[str, Any]] = {}
    for run in runs:
        if run.get("conclusion") != "success":
            continue
        path = run.get("path")
        if not path:
            continue
        started = parse_ts(run.get("run_started_at"))
        cur = best.get(path)
        if cur is None or (
            started is not None
            and (cur["last_success_at"] is None or started > cur["last_success_at"])
        ):
            best[path] = {
                "workflow_path": path,
                "workflow_name": run.get("name") or "(unnamed)",
                "last_success_run_id": int(run["id"]),
                "last_success_at": started,
            }
    return list(best.values())


def _fetch_runs_page(repo: str, token: str, page: int) -> list[dict[str, Any]]:
    url = (
        f"https://api.github.com/repos/{repo}/actions/runs"
        f"?status=completed&per_page={PER_PAGE}&page={page}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
        payload = json.load(resp)
    return payload.get("workflow_runs", []) or []


def _run_started_any_job(repo: str, token: str, run_id: int) -> bool:
    """Did any job of this run actually START? A cancel-in-progress supersession of a
    still-QUEUED run has an empty jobs list (or jobs with no started_at) — only called for
    the rare cancelled-and-long candidates, so the extra API cost is a handful per poll.
    On API failure, err on the side of recording (True) — a dropped real timeout-kill is
    worse than one noisy row."""
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs?per_page=50"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
            payload = json.load(resp)
    except Exception as exc:  # noqa: BLE001 — network flake must not drop a real failure
        LOG.warning("jobs fetch failed for run %d (%s); recording anyway", run_id, exc)
        return True
    jobs = payload.get("jobs", []) or []
    return any(j.get("started_at") for j in jobs)


def _read_cursor(conn: Any) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("SELECT value #>> '{}' FROM app_settings WHERE key = %s", (CURSOR_KEY,))
        row = cur.fetchone()
    return parse_ts(row[0]) if row and row[0] else None


def _write_cursor(conn: Any, ts: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value, updated_at) "
            "VALUES (%s, to_jsonb(%s::text), now()) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()",
            (CURSOR_KEY, ts.isoformat()),
        )


def _fetch_since_cursor(
    repo: str, token: str, since: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    """Page newest-first until a page reaches older than `since` (full coverage) or the
    page cap. Returns (runs, reached_since)."""
    runs: list[dict[str, Any]] = []
    reached_since = False
    for page in range(1, MAX_PAGES + 1):
        batch = _fetch_runs_page(repo, token, page)
        if not batch:
            reached_since = True
            break
        runs.extend(batch)
        page_oldest = min(
            (parse_ts(r.get("updated_at")) for r in batch if parse_ts(r.get("updated_at"))),
            default=None,
        )
        if len(batch) < PER_PAGE or (page_oldest is not None and page_oldest < since):
            reached_since = True
            break
    return runs, reached_since


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not repo or not token or not db_url:
        print(
            "ERROR: GITHUB_REPOSITORY, GITHUB_TOKEN and SUPABASE_DB_URL must be set.",
            file=sys.stderr,
        )
        return 2

    import psycopg

    inserted = 0
    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        now = datetime.now(timezone.utc)
        cursor_ts = _read_cursor(conn) or (now - timedelta(minutes=BOOTSTRAP_MINUTES))
        since = cursor_ts - timedelta(minutes=CURSOR_OVERLAP_MINUTES)

        runs, reached_since = _fetch_since_cursor(repo, token, since)
        failed = select_failed_runs(
            runs, since=since,
            started_check=lambda run_id: _run_started_any_job(repo, token, run_id))
        # Successes are NOT windowed: the latest success anywhere in the pages resets the
        # streak. workflow_run_health is one upserted row per workflow, so a stale
        # page-success can never regress last_success_at (greatest() guard below).
        successes = select_latest_successes(runs)

        with conn.cursor() as cur:
            for s in successes:
                cur.execute(
                    "INSERT INTO workflow_run_health "
                    "  (workflow_path, workflow_name, last_success_at, "
                    "   last_success_run_id, updated_at) "
                    "VALUES (%s, %s, %s, %s, now()) "
                    "ON CONFLICT (workflow_path) DO UPDATE SET "
                    "  workflow_name = excluded.workflow_name, "
                    "  last_success_at = greatest("
                    "    workflow_run_health.last_success_at, excluded.last_success_at), "
                    "  last_success_run_id = CASE "
                    "    WHEN excluded.last_success_at >= "
                    "         coalesce(workflow_run_health.last_success_at, '-infinity'::timestamptz) "
                    "    THEN excluded.last_success_run_id "
                    "    ELSE workflow_run_health.last_success_run_id END, "
                    "  updated_at = now()",
                    (
                        s["workflow_path"],
                        s["workflow_name"],
                        s["last_success_at"],
                        s["last_success_run_id"],
                    ),
                )
            for f in failed:
                cur.execute(
                    "INSERT INTO workflow_failures "
                    "  (run_id, workflow_name, workflow_path, conclusion, "
                    "   run_started_at, html_url) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (run_id) DO NOTHING",
                    (
                        f["run_id"],
                        f["workflow_name"],
                        f["workflow_path"],
                        f["conclusion"],
                        f["run_started_at"],
                        f["html_url"],
                    ),
                )
                inserted += cur.rowcount

        # Advance the cursor. Full coverage → jump to the newest completion; if the page cap
        # was hit first, crawl to the oldest seen so the uncovered gap is picked up next poll.
        completions = [c for c in (parse_ts(r.get("updated_at")) for r in runs) if c is not None]
        if completions:
            new_cursor = max(completions) if reached_since else min(completions)
            if not reached_since:
                LOG.warning(
                    "WORKFLOW_FAILURES page cap (%d) hit before reaching the cursor; "
                    "crawling to oldest-seen %s", MAX_PAGES, new_cursor,
                )
            _write_cursor(conn, new_cursor)

    LOG.info(
        "WORKFLOW_FAILURES scanned=%d failed=%d inserted=%d successes_tracked=%d reached_since=%s",
        len(runs), len(failed), inserted, len(successes), reached_since,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
