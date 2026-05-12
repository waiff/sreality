"""Sweep estimation_runs left in `status='running'` past a cutoff.

When the Railway container restarts mid-run, a `_finish_agent_run_in_bg`
task dies silently and its row stays `status='running'` forever. Same
thing can happen if a worker process is killed by OOM or by an
unhandled exception that escapes the bg wrapper's last-ditch handler.

Run this script manually (or wire it into an Actions schedule later)
to flip those rows to `status='failed'` so they fall out of the
"is anything pending?" polling on the SPA's list page.

Cutoff defaults to 10 minutes — comfortably longer than the longest
agent loop limit currently in use (rental_estimator_full_v1 caps at
240s). Pass a different value via `--minutes N`.

Usage:
    SUPABASE_DB_URL=... python scripts/sweep_stuck_running_runs.py
    SUPABASE_DB_URL=... python scripts/sweep_stuck_running_runs.py --minutes 30
    SUPABASE_DB_URL=... python scripts/sweep_stuck_running_runs.py --dry-run

Exit status is zero in all cases (including no rows swept) so the
script is safe to wire into a cron without alerting noise.
"""

from __future__ import annotations

import argparse
import sys

from scraper import db


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--minutes", type=int, default=10,
        help="age cutoff in minutes (default: 10)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="report what would be swept but do not write",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    cutoff = f"{args.minutes} minutes"
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, created_at, source, input_sreality_id "
                "FROM estimation_runs "
                "WHERE status = 'running' "
                "  AND created_at < now() - %s::interval "
                "ORDER BY created_at",
                (cutoff,),
            )
            rows = cur.fetchall()

        if not rows:
            print("swept: 0 (no rows past cutoff)")
            return 0

        print(f"found {len(rows)} stuck run(s) older than {cutoff}:")
        for rid, created, source, sid in rows:
            print(f"  id={rid} created={created.isoformat()} "
                  f"source={source} sreality_id={sid}")

        if args.dry_run:
            print("dry-run: nothing written")
            return 0

        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "UPDATE estimation_runs "
                "SET status = 'failed', "
                "    error_message = 'lost: container restarted or "
                "background task crashed' "
                "WHERE status = 'running' "
                "  AND created_at < now() - %s::interval",
                (cutoff,),
            )
            print(f"swept: {cur.rowcount}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
