"""record_workflow_failures.py — poll the Actions API, persist failed runs.

GitHub only emails the operator about failed SCHEDULED runs, so push-triggered
and dispatch-triggered failures stay invisible unless someone opens the
Actions tab. This poller (monitor_workflow_failures.yml, every 30 min) lists
recently completed runs, keeps the failed-ish ones, and inserts them into
`workflow_failures` (migration 178) — idempotent via ON CONFLICT (run_id) DO
NOTHING, so the 40-min lookback overlapping the 30-min cadence is harmless.
The Health page surfaces the table through `recent_workflow_failures()`.

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

ALERT_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
LOOKBACK_MINUTES = 40
PER_PAGE = 100
MAX_PAGES = 2

API_TIMEOUT_S = 30


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def select_failed_runs(
    runs: list[dict[str, Any]],
    *,
    since: datetime,
    exclude_name: str = MONITOR_WORKFLOW_NAME,
) -> list[dict[str, Any]]:
    """Pure filter over parsed `/actions/runs` JSON (no network, testable).

    Keeps runs with an alerting conclusion that completed at/after `since`
    (completion time approximated by `updated_at` — the list endpoint has no
    completed-at field), excluding the monitor workflow itself.
    """
    out: list[dict[str, Any]] = []
    for run in runs:
        if run.get("conclusion") not in ALERT_CONCLUSIONS:
            continue
        if run.get("name") == exclude_name:
            continue
        completed_at = parse_ts(run.get("updated_at"))
        if completed_at is None or completed_at < since:
            continue
        out.append(
            {
                "run_id": int(run["id"]),
                "workflow_name": run.get("name") or "(unnamed)",
                "workflow_path": run.get("path"),
                "conclusion": run["conclusion"],
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

    since = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
    runs: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        batch = _fetch_runs_page(repo, token, page)
        runs.extend(batch)
        if len(batch) < PER_PAGE:
            break
    failed = select_failed_runs(runs, since=since)
    # Successes are NOT windowed: the latest success anywhere in the page resets
    # the streak. workflow_run_health is one upserted row per workflow, so a stale
    # page-success can never regress last_success_at (greatest() guard below).
    successes = select_latest_successes(runs)

    import psycopg

    inserted = 0
    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
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

    LOG.info(
        "WORKFLOW_FAILURES scanned=%d failed=%d inserted=%d successes_tracked=%d",
        len(runs), len(failed), inserted, len(successes),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
