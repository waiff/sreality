"""check_llm_health.py — go red (exit 1) when the LLM pipeline looks dead.

Read-only liveness probe. Condition scoring is the highest-volume LLM
consumer; if the Anthropic account runs out of credits (or the key breaks)
the batch `submit` fails and no new `llm_calls` rows are written — but the
per-listing error-swallowing in the scorers means nothing else goes red, so
an outage can stay silent for hours (it did: ~8h on 2026-06-04).

This job makes that loud: it exits non-zero when there have been NO
`llm_calls` for `--max-idle-hours` AND there is pending condition-scoring
work (active listings in the operator-enabled kraje without a condition
level) — i.e. the pipeline should be producing calls but isn't. A failed
scheduled run notifies the operator.

A second, condition-specific probe guards against green-masking: unrelated
agent/summarize traffic keeps the global max(called_at) fresh while the
condition batch pipeline is dead (it did: the 413 outage from 2026-06-04).
With pending work, no `called_for='score_listing_condition'` row within
`--condition-max-idle-hours` (default 8 — the 3h submit cadence plus batch
turnaround) is stalled regardless of other traffic.

A THIRD probe — independent of pending work — catches the outage the first two
missed: recorded `llm_calls` FAILURE rows (`error IS NOT NULL`, migration 259).
When condition scoring is quiet the pending-gated checks never fire, so an
exhausted-credit / dead-key outage stayed GREEN for ~8h (2026-07-01) even though
every paid path — dedup vision, estimations, summaries — was 400-ing. Now a
credit-balance error alarms immediately, and >= `--min-failures` generic
failures in the window alarms too. LLMClient records the failure row on every
provider exception; this check keys off it with no Anthropic key of its own.

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
    condition_call_age_hours: float | None,
    pending: int,
    max_idle_hours: float,
    condition_max_idle_hours: float,
    min_pending: int,
    recent_failures: int = 0,
    credit_exhausted: bool = False,
    min_failures: int = 3,
) -> tuple[bool, str]:
    """Return (stalled, message). stalled=True means raise the alarm (exit 1).

    A provider OUTAGE (exhausted credit / dead key / 5xx) is checked FIRST and is
    INDEPENDENT of pending work — the blind spot that let an 8h credit outage stay green
    (condition scoring was quiet, so the pending-gated checks never fired even though every
    LLM call was 400-ing). Recorded llm_calls failure rows (migration 259) make it visible.

    Otherwise: no alarm when there's nothing to score (pending below the floor) — a quiet
    pipeline with no backlog is legitimately idle — else alarm if there have been no calls at
    all, none within the idle window, or (even with fresh unrelated traffic) no condition
    call within its own wider window.
    """
    if credit_exhausted:
        return True, (
            f"STALLED: LLM calls failing with credit-balance errors "
            f"({recent_failures} failures in the last {max_idle_hours:.0f}h) — the Anthropic "
            f"account is out of credit (Plans & Billing). Every paid LLM path is down."
        )
    if recent_failures >= min_failures:
        return True, (
            f"STALLED: {recent_failures} LLM calls failed in the last {max_idle_hours:.0f}h "
            f"(>= {min_failures}) — the LLM provider is erroring / down (check the key + "
            f"credit balance + provider status)."
        )
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
    if condition_call_age_hours is None:
        return True, (
            f"STALLED: no score_listing_condition llm_calls on record while "
            f"pending={pending} — condition pipeline down despite other LLM "
            f"traffic (check the batch submit/ingest workflow runs)"
        )
    if condition_call_age_hours > condition_max_idle_hours:
        return True, (
            f"STALLED: last score_listing_condition call "
            f"{condition_call_age_hours:.1f}h ago (> {condition_max_idle_hours}h) "
            f"while pending={pending} — condition pipeline down despite other "
            f"LLM traffic (check the batch submit/ingest workflow runs)"
        )
    return False, (
        f"OK: last llm_call {last_call_age_hours:.1f}h ago, last condition "
        f"call {condition_call_age_hours:.1f}h ago, pending={pending}"
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
        "--condition-max-idle-hours", type=float,
        default=float(os.environ.get("LLM_HEALTH_CONDITION_MAX_IDLE_HOURS", "8")),
        help="Alert if no score_listing_condition llm_calls within this many "
             "hours while work is pending (default 8; covers the 3h submit "
             "cadence plus batch turnaround) — catches a dead condition "
             "pipeline that fresh unrelated LLM traffic would green-mask.",
    )
    parser.add_argument(
        "--min-pending", type=int,
        default=int(os.environ.get("LLM_HEALTH_MIN_PENDING", "50")),
        help="Only alert when at least this many active listings are unscored "
             "(default 50) — avoids false alarms when there's nothing to do.",
    )
    parser.add_argument(
        "--min-failures", type=int,
        default=int(os.environ.get("LLM_HEALTH_MIN_FAILURES", "3")),
        help="Alert (independent of pending work) when at least this many llm_calls have "
             "FAILED within --max-idle-hours (default 3) — a provider outage. A single "
             "credit-balance error alarms immediately regardless of this floor.",
    )
    parser.add_argument(
        "--notify", action="store_true",
        help="On a STALLED assessment, also ring the in-app bell "
             "(emit_system_alert 'llm_health') before exiting non-zero.",
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
        condition_call_age_hours = _last_call_age_hours(
            conn, called_for="score_listing_condition",
        )
        pending = _pending_unscored(conn)
        recent_failures, credit_exhausted = _recent_failures(
            conn, hours=args.max_idle_hours,
        )

    stalled, message = assess(
        last_call_age_hours=last_call_age_hours,
        condition_call_age_hours=condition_call_age_hours,
        pending=pending,
        max_idle_hours=args.max_idle_hours,
        condition_max_idle_hours=args.condition_max_idle_hours,
        min_pending=args.min_pending,
        recent_failures=recent_failures,
        credit_exhausted=credit_exhausted,
        min_failures=args.min_failures,
    )
    if stalled:
        LOG.error("LLM_HEALTH %s", message)
        if args.notify:
            _notify_stalled(db_url, message)
        return 1
    LOG.info("LLM_HEALTH %s", message)
    return 0


def _notify_stalled(db_url: str, message: str) -> None:
    """Ring the in-app bell for a stalled pipeline. Best-effort — a notify failure
    must never mask the exit-1 signal that GitHub already surfaces."""
    import psycopg

    from toolkit.system_alerts import emit_system_alert

    try:
        with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
            emit_system_alert(conn, "llm_health", message)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("LLM_HEALTH notify failed: %s", exc)


def _recent_failures(conn: Any, *, hours: float) -> tuple[int, bool]:
    """(count of FAILED llm_calls within `hours`, whether any is a credit-balance error).

    Failures = rows with `error IS NOT NULL` (migration 259). A provider outage is thus visible
    without the Anthropic key — the health check keeps working precisely when the API is down.
    The ILIKE wildcard lives in the BOUND VALUE, never in the SQL string, so no bare `%` sits
    next to a `%s` param (the psycopg format-char trap).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(*) FILTER (WHERE error ILIKE %s) "
            "FROM llm_calls "
            "WHERE error IS NOT NULL AND called_at > now() - (interval '1 hour' * %s)",
            ("%credit balance%", hours),
        )
        row = cur.fetchone()
    total = int(row[0]) if row and row[0] is not None else 0
    credit = bool(row[1]) if row and row[1] is not None else False
    return total, credit


def _last_call_age_hours(conn: Any, *, called_for: str | None = None) -> float | None:
    sql = (
        "SELECT EXTRACT(EPOCH FROM (now() - MAX(called_at))) / 3600.0 "
        "FROM llm_calls"
    )
    params: tuple[Any, ...] = ()
    if called_for is not None:
        sql += " WHERE called_for = %s"
        params = (called_for,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _pending_unscored(conn: Any) -> int:
    """Active listings (seen within 7d) with no building condition level yet.

    Uses the derived `listings.building_condition_level` column (NULL = not
    scored) so it's a single-table count, not a join over listing_snapshots.

    Mirrors the scorer's kraj scope (app_settings.
    condition_scoring_enabled_region_ids): listings outside the enabled
    kraje — or with region_id NULL — are parked, not pending, so they must
    never read as a stall. Empty list = scoring paused = nothing pending.
    Propagated siblings drop out via the levels-NULL predicate.
    """
    from scripts.backfill_condition_scores import _enabled_region_ids

    region_ids = _enabled_region_ids(conn)
    if not region_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM listings "
            "WHERE is_active = true "
            "  AND last_seen_at > now() - interval '7 days' "
            "  AND building_condition_level IS NULL "
            "  AND region_id = ANY(%s::bigint[])",
            (region_ids,),
        )
        return int(cur.fetchone()[0])


if __name__ == "__main__":
    sys.exit(main())
