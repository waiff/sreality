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
reaches the previous cursor, so no completed run is skipped. Crucially, it advances
ONLY on a poll that reached back past the cursor — a poll that hit the page cap
first has an uncovered window and holds the cursor instead (see _advance_cursor).

A dead poller is invisible in `workflow_failures` itself — it just stops
accumulating rows, which looks exactly like "nothing failed". The liveness signal is
the AGE of this cursor, watched by verify_pipeline's `workflow_poller_liveness`.

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
import time
import urllib.error
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
# 1,000 runs. Raised from 5 (500) in W0.2 of the reliability program: at ~100 failures/day
# and a real inter-poll gap of 80–256 min, 500 was close enough to the ceiling that the cap
# was reachable — and hitting it used to drop runs permanently (see _advance_cursor). Cost is
# bounded and cheap: at most 10 list requests per poll against the Actions API's 1,000/hour
# per-repo budget for GITHUB_TOKEN, and the loop still stops as soon as a page reaches back
# past `since`, so the extra pages are only ever fetched when they are actually needed.
MAX_PAGES = 10
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


# --- W3.1 backstop: read WHY a run went red -------------------------------
#
# `workflow_failures` stores which workflow is red and no failure reason of any kind.
# The chokepoint producer (scraper.portal_runner) covers portal crashes at t+0 with no
# API cost, but portal workflows are only ~22% of the failure corpus — CI, backfills,
# jobs and the LLM lanes are the other 78%, and reds with no Python exception at all
# (timeouts, startup_failure) have no in-process path by definition. So this lane pulls
# the failed job's log and extracts the terminal error.

MAX_LOG_FETCHES = 25          # Actions API budget guard: one poll, at most this many logs
LOG_TAIL_BYTES = 64 * 1024    # measured job logs run 27KB-172KB; a drain log is far larger
_JOB_FAIL_CONCLUSIONS = frozenset({"failure", "timed_out", "cancelled", "startup_failure"})

# Wall-clock ceiling for the whole incident pass. MAX_LOG_FETCHES alone is not a time
# budget: each fetch is up to 4 round trips (jobs list, the 302, then _fetch_blob_tail's
# two ranged reads) at API_TIMEOUT_S=30 each, so 25 of them is a ~50-minute worst case
# inside a job with `timeout-minutes: 5`. A killed job is not caught by any try/except.
INCIDENT_PASS_BUDGET_S = 90.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow, so the caller can re-request the target BARE.

    `/actions/jobs/{id}/logs` answers 302 to a SAS-signed Azure blob URL, and CPython's
    redirect handler copies every header — `Authorization: Bearer` included — onto the
    redirected request. That both leaks the GITHUB_TOKEN to a third-party host and does
    not even work: Azure answers 401 when the bearer header is present."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _gh_request(url: str, token: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def select_failed_job(jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The job whose log is worth reading, plus the step that broke (pure, testable)."""
    for job in jobs:
        if job.get("conclusion") not in _JOB_FAIL_CONCLUSIONS:
            continue
        step_name = None
        step_status = None
        for step in job.get("steps") or []:
            if step.get("conclusion") in _JOB_FAIL_CONCLUSIONS:
                step_name = step.get("name")
                step_status = step.get("conclusion")
                break
        return {
            "job_id": int(job["id"]),
            "job_name": job.get("name"),
            "step_name": step_name or job.get("name"),
            # The Actions API never exposes a process exit code; the step's conclusion
            # is the closest honest stand-in, and it is what distinguishes a timeout
            # kill from a plain non-zero exit in the fallback key.
            "exit_code": step_status or job.get("conclusion"),
        }
    return None


def _fetch_failed_job(repo: str, token: str, run_id: int) -> dict[str, Any] | None:
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs?per_page=50"
    try:
        with urllib.request.urlopen(_gh_request(url, token), timeout=API_TIMEOUT_S) as resp:
            payload = json.load(resp)
    except Exception as exc:  # noqa: BLE001 — no log is a degraded incident, not a crash
        LOG.warning("jobs fetch failed for run %d (%s)", run_id, exc)
        return None
    return select_failed_job(payload.get("jobs", []) or [])


def _fetch_blob_tail(url: str, cap_bytes: int) -> str | None:
    """Read the last `cap_bytes` of a signed blob, BARE (no auth header).

    Two requests on purpose: Azure ignores suffix ranges — `Range: bytes=-500` came back
    200 with the whole 27KB body — so the length is learned from a 1-byte closed range
    first, then a real closed range is asked for."""
    total: int | None = None
    try:
        head = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(head, timeout=API_TIMEOUT_S) as resp:
            content_range = resp.headers.get("Content-Range") or ""
            resp.read(1)
        if "/" in content_range:
            total = int(content_range.rsplit("/", 1)[1])
    except Exception as exc:  # noqa: BLE001 — 404 BlobNotFound / expired SAS are routine
        LOG.info("log blob probe failed (%s)", exc)
        return None
    if not total:
        return None
    start = max(0, total - cap_bytes)
    try:
        req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{total - 1}"})
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
            body = resp.read(cap_bytes + 1024)
    except Exception as exc:  # noqa: BLE001
        LOG.info("log blob read failed (%s)", exc)
        return None
    return body.decode("utf-8", "ignore")


def fetch_job_log(repo: str, token: str, job_id: int, *, cap_bytes: int = LOG_TAIL_BYTES) -> str | None:
    """Plain-text tail of one job's log, or None. Run-level `/runs/{id}/logs` returns a
    ZIP; only the job-level endpoint is text."""
    url = f"https://api.github.com/repos/{repo}/actions/jobs/{job_id}/logs"
    try:
        with _NO_REDIRECT_OPENER.open(_gh_request(url, token), timeout=API_TIMEOUT_S) as resp:
            return resp.read(cap_bytes).decode("utf-8", "ignore")
    except urllib.error.HTTPError as err:
        if err.code in (301, 302, 303, 307, 308):
            location = err.headers.get("Location")
            return _fetch_blob_tail(location, cap_bytes) if location else None
        LOG.info("log fetch for job %d returned HTTP %s", job_id, err.code)
        return None
    except Exception as exc:  # noqa: BLE001
        LOG.info("log fetch for job %d failed (%s)", job_id, exc)
        return None


def record_incidents(
    conn: Any, repo: str, token: str, failures: list[dict[str, Any]],
    *, budget_seconds: float = INCIDENT_PASS_BUDGET_S,
) -> dict[str, int]:
    """Give every newly recorded red a REASON, and close the ones the fleet answered.

    The API-budget saver is the RUN CLAIM (`ops_incident_runs`, migration 463), not a
    workflow→signature guess: a run the in-process producer already recorded costs zero
    fetches here and — the part that matters — cannot be counted a second time. The
    earlier `open_signature_by_workflow` reuse did neither. It was keyed on
    `workflow_path` alone with no run correlation, so it (a) still fell through to the
    unconditional `failure_count + 1` bump, double-counting every portal crash, and
    (b) folded a NEW reason into whatever incident last touched that workflow within the
    hour — a workflow that opened a CheckViolation incident at 10:00 and then timed out
    at 10:30 recorded the timeout as a CheckViolation, and the timeout's own signature
    was never derived, so it could never alert.

    `budget_seconds` bounds the pass: the caller's job has `timeout-minutes: 5` and a
    kill is not an exception, so overrunning here would cost the poller its cursor
    advance (the input-coverage guarantee W3 rests on).
    """
    from scripts import failure_signature
    from toolkit import ops_incidents

    stats = {"recorded": 0, "already_recorded": 0, "fetched": 0,
             "unreadable": 0, "skipped": 0, "expired": 0}
    if not failures:
        return {**stats, **ops_incidents.auto_resolve(conn)}

    excerpt_cap = ops_incidents.excerpt_byte_budget(conn)
    budget = MAX_LOG_FETCHES
    deadline = time.monotonic() + float(budget_seconds)

    for f in failures:
        path = f.get("workflow_path")
        run_id = f.get("run_id")
        # Claim first: it is both the dedupe and the cheapest possible skip.
        try:
            if not ops_incidents.claim_run(conn, run_id):
                stats["already_recorded"] += 1
                continue
        except Exception as exc:  # noqa: BLE001 — incidents must never break the poller
            LOG.warning("ops incident claim failed for run %s: %r", run_id, exc)
            continue
        signature: str | None = None
        excerpt: str | None = None
        if time.monotonic() >= deadline:
            # Out of wall clock, not out of API budget. Same disposition as `skipped`:
            # the run stays in workflow_failures with no incident asserting nothing.
            stats["expired"] += 1
            continue
        if budget <= 0:
            # Not "unreadable" — unread. A scoped fallback key here would open an
            # incident asserting nothing, so the run stays in workflow_failures only.
            stats["skipped"] += 1
            continue
        budget -= 1
        job = _fetch_failed_job(repo, token, run_id)
        text = fetch_job_log(repo, token, job["job_id"]) if job else None
        if text:
            stats["fetched"] += 1
            signature = failure_signature.signature_from_log(text)
            excerpt = failure_signature.excerpt_from_log(text, max_bytes=excerpt_cap)
        if signature is None:
            stats["unreadable"] += 1
            signature = failure_signature.fallback_signature(
                workflow_path=path,
                step_name=(job or {}).get("step_name"),
                exit_code=(job or {}).get("exit_code"),
            )
        try:
            ops_incidents.record_failure_signature(
                conn, signature,
                workflow_path=path,
                origin=f"actions/{f.get('conclusion')}",
                sample_run_url=f.get("html_url"),
                sample_excerpt=excerpt,
                run_id=run_id,
                run_claimed=True,
            )
            stats["recorded"] += 1
        except Exception as exc:  # noqa: BLE001 — incidents must never break the poller
            LOG.warning("ops incident write failed for run %s: %r", run_id, exc)

    return {**stats, **ops_incidents.auto_resolve(conn)}


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


def _advance_cursor(
    completions: list[datetime], *, reached_since: bool,
) -> datetime | None:
    """Where the high-water mark goes after a poll. None = leave it where it is.

    Only a poll that reached back past `since` has covered its whole window, and only
    that poll may move the cursor — to the newest completion it saw.

    When the page cap is hit first, the pages fetched are the NEWEST N runs, so every
    completion seen is *newer* than `since` and the uncovered runs are older than all
    of them. The old code advanced to `min(completions)` here, believing it was
    "crawling to oldest-seen so the gap is picked up next poll" — but the next poll
    filters on `completed_at < since`, and the skipped runs are older than the new
    cursor, so they were dropped **permanently**. That is why only 2 of the 6 portals
    that failed on 2026-08-26 were ever recorded. Holding the cursor keeps the window
    open instead: the poll re-scans, and the gap closes as soon as volume drops back
    under the page budget.
    """
    if not reached_since or not completions:
        return None
    return max(completions)


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
    newly_recorded: list[dict[str, Any]] = []
    incidents: dict[str, int] = {}
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
                if cur.rowcount:
                    inserted += cur.rowcount
                    newly_recorded.append(f)

        # The cursor advance goes FIRST, before the incident pass. record_incidents
        # talks to the Actions API and to Azure blob storage inside a job with
        # `timeout-minutes: 5`, and a job kill is not an exception the try/except below
        # can catch — so anything slow there used to cost the poller its cursor advance,
        # which is the input-coverage guarantee the whole W3 clustering rests on.
        # Incidents are best-effort bookkeeping over rows already committed above;
        # the cursor is not.
        completions = [c for c in (parse_ts(r.get("updated_at")) for r in runs) if c is not None]
        new_cursor = _advance_cursor(completions, reached_since=reached_since)
        if new_cursor is not None:
            _write_cursor(conn, new_cursor)
        elif completions:
            gap_min = (min(completions) - since).total_seconds() / 60.0
            LOG.error(
                "WORKFLOW_FAILURES page cap (%d pages / %d runs) hit before reaching the "
                "cursor: runs completed between %s and %s were NOT fetched (a %.0f-min "
                "window). Holding the cursor so they stay eligible; raise MAX_PAGES if this "
                "repeats.",
                MAX_PAGES, MAX_PAGES * PER_PAGE, since, min(completions), gap_min,
            )

        # Only NEWLY inserted rows: a re-scanned run inside the cursor overlap has
        # already had its reason recorded, and re-counting it would inflate the
        # incident's failure_count past the number of real failures. (The run claim in
        # record_incidents enforces that across producers too, not just across polls.)
        try:
            incidents = record_incidents(conn, repo, token, newly_recorded)
        except Exception as exc:  # noqa: BLE001 — the poller's own job comes first
            LOG.warning("ops incident pass failed: %r", exc)

    LOG.info(
        "WORKFLOW_FAILURES scanned=%d failed=%d inserted=%d successes_tracked=%d reached_since=%s",
        len(runs), len(failed), inserted, len(successes), reached_since,
    )
    if incidents:
        LOG.info("OPS_INCIDENTS %s", " ".join(f"{k}={v}" for k, v in sorted(incidents.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
