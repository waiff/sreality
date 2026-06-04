"""check_llm_health.py — go red (exit 1) when the LLM pipeline looks dead.

Read-only liveness probe. Condition scoring is the highest-volume LLM
consumer; if the Anthropic account runs out of credits (or the key breaks)
the batch `submit` fails and no new `llm_calls` rows are written — but the
per-listing error-swallowing in the scorers means nothing else goes red, so
an outage can stay silent for hours (it did: ~8h on 2026-06-04).

This job makes that loud: it exits non-zero when there have been NO
`llm_calls` for `--max-idle-hours` AND there is pending condition-scoring
work (active listings without a condition level) — i.e. the pipeline should
be producing calls but isn't. A failed scheduled run notifies the operator.

Needs only SUPABASE_DB_URL — deliberately NOT the Anthropic key, so it keeps
working precisely when the API is the thing that's down.

Exit codes: 0 healthy or legitimately idle · 1 STALLED (alert) · 2 misconfig.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

LOG = logging.getLogger("check_llm_health")


def assess(
    *,
    last_call_age_hours: float | None,
    pending: int,
    max_idle_hours: float,
    min_pending: int,
) -> tuple[bool, str]:
    """Return (stalled, message). stalled=True means raise the alarm (exit 1).

    No alarm when there's nothing to score (pending below the floor) — a
    quiet pipeline with no backlog is legitimately idle. Otherwise alarm if
    there have been no calls at all, or none within the idle window.
    """
    if pending < min_pending:
        return False, f"OK idle: pending={pending} < min_pending={min_pending}"
    if last_call_age_hours is None:
        return True, f"STALLED: no llm_calls on record while pending={pending}"
    if last_call_age_hours > max_idle_hours:
        return True, (
            f"STALLED: last llm_call {last_call_age_hours:.1f}h ago "
            f"(> {max_idle_hours}h) while pending={pending} — LLM pipeline "
            f"appears down (check Anthropic credit balance / API key)"
        )
    return False, (
        f"OK: last llm_call {last_call_age_hours:.1f}h ago, pending={pending}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-idle-hours", type=float,
        default=float(os.environ.get("LLM_HEALTH_MAX_IDLE_HOURS", "4")),
        help="Alert if no llm_calls within this many hours (default 4; the "
             "batch submit cadence is 3h).",
    )
    parser.add_argument(
        "--min-pending", type=int,
        default=int(os.environ.get("LLM_HEALTH_MIN_PENDING", "50")),
        help="Only alert when at least this many active listings are unscored "
             "(default 50) — avoids false alarms when there's nothing to do.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    import psycopg

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        last_call_age_hours = _last_call_age_hours(conn)
        pending = _pending_unscored(conn)

    stalled, message = assess(
        last_call_age_hours=last_call_age_hours,
        pending=pending,
        max_idle_hours=args.max_idle_hours,
        min_pending=args.min_pending,
    )
    if stalled:
        LOG.error("LLM_HEALTH %s", message)
        return 1
    LOG.info("LLM_HEALTH %s", message)
    return 0


def _last_call_age_hours(conn: Any) -> float | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXTRACT(EPOCH FROM (now() - MAX(called_at))) / 3600.0 "
            "FROM llm_calls"
        )
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _pending_unscored(conn: Any) -> int:
    """Active listings (seen within 7d) with no building condition level yet.

    Uses the derived `listings.building_condition_level` column (NULL = not
    scored) so it's a single-table count, not a join over listing_snapshots.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM listings "
            "WHERE is_active = true "
            "  AND last_seen_at > now() - interval '7 days' "
            "  AND building_condition_level IS NULL"
        )
        return int(cur.fetchone()[0])


if __name__ == "__main__":
    sys.exit(main())
