"""verify_pipeline.py — scheduled pipeline-health harness.

Computes a fixed set of pipeline-health metrics (dedup debt, eligibility funnel,
merge latency, engine cycle health, LLM error rate, a weekly precision sample),
writes one `pipeline_check_results` row per check, and rings the in-app bell on
STATE TRANSITIONS only (toolkit.system_alerts.emit_transition_alerts): once when a
check goes red, once when it recovers — not on every red run.

Born from the 2026-07 incident: the dedup/scrape pipeline stalled silently for two
days (Anthropic credit exhaustion; 38k+ failed LLM calls) with no in-app signal,
while ~39,376 suspect unmerged byt pairs of "dedup debt" sat invisible. This job
makes both loud and durable.

Each check is isolated (one failing check writes a `fail` row with the error in
`details`, never kills the run). Thresholds live in
`app_settings.pipeline_check_thresholds` with the code defaults below as fallbacks.

    python -m scripts.verify_pipeline            # compute + write + alert
    python -m scripts.verify_pipeline --dry-run  # compute + log only, no writes
    python -m scripts.verify_pipeline --weekly   # also emit the precision sample

Needs only SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
from typing import Any, Callable

from scraper.db import connect
from toolkit.listing_identity import R2_CARRIERS as _PARITY_CARRIERS
from toolkit.system_alerts import emit_transition_alerts, latest_statuses

LOG = logging.getLogger("verify_pipeline")

# Code fallbacks. The pipeline_check_thresholds seed (migration 274) is merged OVER
# these in load_thresholds, so a key present here but not in the DB seed (e.g.
# llm_silence_fail_hours, added with the WS4 alerting rebuild) is served from this
# default until a future seed migration includes it.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "street_debt_price_pct": 1.0,
    "street_debt_warn": 30000,
    "street_debt_fail": 45000,
    "geo_debt_area_pct": 20,
    "geo_debt_price_pct": 5,
    "merge_p95_warn_hours": 24,
    "unpublished_overdue_fail": 1,
    "cycle_stall_fail_hours": 12,
    "dirty_age_p95_warn_hours": 6,
    "candidate_age_p95_warn_days": 14,
    "llm_error_rate_warn": 0.2,
    "llm_silence_fail_hours": 4,
    "llm_spend_24h_warn_usd": 90,
    "llm_spend_24h_fail_usd": 150,
    "db_cron_fail_rate_fail": 0.5,
    "worker_stale_fail_minutes": 5,
    "verification_stale_hours": 24,
    "precision_sample_n": 15,
}

_NONBYT = ("dum", "pozemek", "komercni", "ostatni")


# --- pure status derivation (unit-tested without a DB) ---------------------


def _worst(statuses: list[str]) -> str:
    for s in ("fail", "warn", "ok"):
        if s in statuses:
            return s
    return "ok"


def _status_for_street_debt(count: int, thresholds: dict[str, Any]) -> str:
    if count > thresholds["street_debt_fail"]:
        return "fail"
    if count > thresholds["street_debt_warn"]:
        return "warn"
    return "ok"


def _status_for_merge_latency(
    p95_hours: float | None, thresholds: dict[str, Any],
) -> str:
    if p95_hours is not None and p95_hours > thresholds["merge_p95_warn_hours"]:
        return "warn"
    return "ok"


def _status_for_cycle(
    *, has_row: bool, updated_age_hours: float | None, thresholds: dict[str, Any],
) -> str:
    """Fail on a STALLED cursor, not a slow one. The street backstop scan never completes a
    full-market cycle (~2 weeks at throughput) — that's the expected steady state for a large
    market, so alarming on cycle AGE was structurally-always-red and unactionable. What IS
    actionable is the cursor going idle: dedup_scan_state.updated_at advances on every run, so a
    long gap means the lane stopped running. Cycle age is reported as a gauge, not a fail driver
    (raising throughput is a capacity decision, not an incident)."""
    if not has_row:
        return "warn"  # engine has never established a scan cycle
    if updated_age_hours is None:
        return "warn"  # row exists but no progress timestamp
    if updated_age_hours > thresholds["cycle_stall_fail_hours"]:
        return "fail"  # cursor idle → the backstop scan is stalled
    return "ok"


def _status_for_dirty(p95_hours: float | None, thresholds: dict[str, Any]) -> str:
    if p95_hours is not None and p95_hours > thresholds["dirty_age_p95_warn_hours"]:
        return "warn"
    return "ok"


def _status_for_candidates(p95_days: float | None, thresholds: dict[str, Any]) -> str:
    if p95_days is not None and p95_days > thresholds["candidate_age_p95_warn_days"]:
        return "warn"
    return "ok"


def _status_for_llm_errors(
    per_called_for: list[dict[str, Any]],
    credit_live: bool,
    currently_failing: bool,
    thresholds: dict[str, Any],
) -> tuple[str, list[str]]:
    """Return (status, offending called_for keys), gated on LIVE state.

    The old check failed on ANY credit-balance error in a trailing 24h window, so it kept
    screaming "everything is down" for up to a day after the account was topped up (it fired
    ~22h post-recovery on 2026-07-09). Now a red state requires the outage to be LIVE —
    `currently_failing` = the most recent llm_call is a failure (healthy traffic since the
    last error clears it within minutes). Credit exhaustion (`credit_live`) is the
    unconditional fail; otherwise a called_for erroring >warn_rate over >=20 calls fails only
    while still live."""
    if credit_live:
        return "fail", []
    if not currently_failing:
        return "ok", []
    warn_rate = thresholds["llm_error_rate_warn"]
    offenders = [
        c["called_for"]
        for c in per_called_for
        if c["total"] >= 20 and c["total"] > 0 and c["errors"] / c["total"] > warn_rate
    ]
    return ("fail" if offenders else "ok"), offenders


def _status_for_llm_silence(hours: float | None, fail_hours: float) -> str:
    """Fail when the newest llm_call is older than `fail_hours` (or there are none at all)."""
    if hours is None or hours > fail_hours:
        return "fail"
    return "ok"


def _status_for_burn(spend_24h: float, warn_usd: float, fail_usd: float) -> str:
    """Credit-depletion early warning: the account has run dry four times in a week
    (Jul 3-10) because paid dedup-vision burn (~$75-100/day, cost-mix-driven — Jul 9 had
    FEWER calls than Jul 8 yet 40% higher cost) silently outpaces manual top-ups. Balance
    isn't queryable via API, so trailing-24h SPEND is the runway proxy: warn = top-up
    cadence risk, fail = runaway burn worth an email before the hard gate hits."""
    if spend_24h > fail_usd:
        return "fail"
    if spend_24h > warn_usd:
        return "warn"
    return "ok"


_MIN_CRON_RUNS = 3  # ignore jobs with too few finished runs to judge a rate


def _status_for_cron(
    jobs: list[dict[str, Any]], fail_rate: float,
) -> tuple[str, list[str]]:
    """Fail (naming the offenders) when any pg_cron job's failure rate over the window
    exceeds `fail_rate` with >= _MIN_CRON_RUNS finished runs. This is the DB-saturation
    signal: the fleet's heaviest jobs (health-matview refresh, browse-list rebuild) tip
    over the pooler statement_timeout en masse when the DB is overloaded, and nothing
    watched them (the 2026-07 incident surfaced as ~8 unrelated red workflows instead)."""
    offenders = []
    for j in jobs:
        finished = j["ok"] + j["failed"]
        if finished >= _MIN_CRON_RUNS and j["failed"] / finished > fail_rate:
            offenders.append(f"{j['jobname']} {j['failed']}/{finished}")
    return ("fail" if offenders else "ok"), offenders


def _status_for_worker(
    ages: list[tuple[str, float]], stale_minutes: float,
) -> tuple[str, list[str]]:
    """Fail when any heartbeating worker's last beat is older than `stale_minutes`. An
    EMPTY list is ok (no worker deployed — not this check's job to demand one); the
    realtime worker beats ~every 30s, so 5 min = 10 missed beats = down. `worker_heartbeats`
    is written every 30s and, until now, read by nothing — a dead worker (it owns the
    latency-critical loops) produced no signal at all."""
    stale = [f"{w} ({age:.0f}m)" for (w, age) in ages if age > stale_minutes]
    return ("fail" if stale else "ok"), stale


# --- thresholds ------------------------------------------------------------


def load_thresholds(conn: Any) -> dict[str, Any]:
    """app_settings.pipeline_check_thresholds merged over the code defaults, so a
    missing key (or a whole missing row) always resolves to the seeded default."""
    merged = dict(DEFAULT_THRESHOLDS)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM app_settings WHERE key = 'pipeline_check_thresholds'"
        )
        row = cur.fetchone()
    raw = row[0] if row and row[0] is not None else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, (int, float)):
                merged[k] = v
    return merged


def _fetchone(conn: Any, sql: str, params: Any = None) -> tuple[Any, ...] | None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '10min'")
        cur.execute(sql, params or ())
        return cur.fetchone()


def _fetchall(conn: Any, sql: str, params: Any = None) -> list[tuple[Any, ...]]:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '10min'")
        cur.execute(sql, params or ())
        return cur.fetchall()


# --- checks ----------------------------------------------------------------

# A pair of cross-source LISTINGS is suspect dedup debt when it shares the dedup
# key, is price-comparable, isn't contradicted, and its two properties aren't
# already merged or operator-dismissed. Counted at the PROPERTY-pair grain.
_STREET_DEBT_SQL = """
with pairs as (
  select l1.property_id as pa, l2.property_id as pb,
         l1.sreality_id as sa, l2.sreality_id as sb
  from listings l1
  join listings l2
    on l1.obec_id = l2.obec_id
   and l1.street_name_key = l2.street_name_key
   and l1.disposition = l2.disposition
   and l1.source <> l2.source
   and l1.property_id < l2.property_id
  where l1.is_active and l2.is_active
    and l1.street is not null and l1.street <> '' and l1.disposition is not null
    and l2.street is not null and l2.street <> ''
    and l1.obec_id is not null and l1.street_name_key is not null
    and l1.price_czk is not null and l2.price_czk is not null
    and abs(l1.price_czk - l2.price_czk)
        <= (%(price_pct)s / 100.0) * greatest(l1.price_czk, l2.price_czk)
    and not (l1.house_number is not null and l2.house_number is not null
             and lower(trim(l1.house_number)) <> lower(trim(l2.house_number)))
    and not (l1.floor is not null and l2.floor is not null
             and abs(l1.floor - l2.floor) >= 2)
),
filtered as (
  select pr.pa, pr.pb, min(pr.sa) as sa, min(pr.sb) as sb
  from pairs pr
  join properties p1 on p1.id = pr.pa and p1.status = 'active'
  join properties p2 on p2.id = pr.pb and p2.status = 'active'
  where not exists (
    select 1 from property_identity_candidates c
    where c.left_property_id = least(pr.pa, pr.pb)
      and c.right_property_id = greatest(pr.pa, pr.pb)
      and c.status = 'dismissed'
  )
  group by pr.pa, pr.pb
)
select
  (select count(*) from filtered) as cnt,
  coalesce((
    select jsonb_agg(jsonb_build_object(
             'property_a', pa, 'property_b', pb,
             'sreality_a', sa, 'sreality_b', sb))
    from (select * from filtered order by pa, pb limit 20) s
  ), '[]'::jsonb) as samples
"""


def check_street_debt(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    row = _fetchone(conn, _STREET_DEBT_SQL, {"price_pct": thresholds["street_debt_price_pct"]})
    count = int(row[0]) if row and row[0] is not None else 0
    samples = row[1] if row and row[1] is not None else []
    status = _status_for_street_debt(count, thresholds)
    return {
        "check_key": "street_debt",
        "status": status,
        "value": count,
        "details": {
            "suspect_pairs": count,
            "price_pct": thresholds["street_debt_price_pct"],
            "warn_at": thresholds["street_debt_warn"],
            "fail_at": thresholds["street_debt_fail"],
            "samples": samples,
        },
        "message": (
            f"Street-keyed dedup debt is {count:,} suspect cross-source byt/property "
            f"pairs (> fail threshold {int(thresholds['street_debt_fail']):,}). The dedup "
            f"engine is falling behind the market inflow."
        ),
    }


_GEO_DEBT_SQL = """
with pairs as (
  select l1.property_id as pa, l2.property_id as pb,
         l1.sreality_id as sa, l2.sreality_id as sb
  from listings l1
  join listings l2
    on l1.obec_id = l2.obec_id
   and round(st_y(l1.geom::geometry)::numeric, 4) = round(st_y(l2.geom::geometry)::numeric, 4)
   and round(st_x(l1.geom::geometry)::numeric, 4) = round(st_x(l2.geom::geometry)::numeric, 4)
   and l1.category_type = l2.category_type
   and l1.source <> l2.source
   and l1.property_id < l2.property_id
  where l1.is_active and l2.is_active
    and l1.category_main = any(%(nonbyt)s) and l2.category_main = any(%(nonbyt)s)
    and l1.geom is not null and l2.geom is not null and l1.obec_id is not null
    and l1.price_czk is not null and l2.price_czk is not null
    and abs(l1.price_czk - l2.price_czk)
        <= (%(price_pct)s / 100.0) * greatest(l1.price_czk, l2.price_czk)
    and coalesce(l1.area_m2, l1.estate_area, l1.usable_area) is not null
    and coalesce(l2.area_m2, l2.estate_area, l2.usable_area) is not null
    and abs(coalesce(l1.area_m2, l1.estate_area, l1.usable_area)
            - coalesce(l2.area_m2, l2.estate_area, l2.usable_area))
        <= (%(area_pct)s / 100.0) * greatest(
             coalesce(l1.area_m2, l1.estate_area, l1.usable_area),
             coalesce(l2.area_m2, l2.estate_area, l2.usable_area))
),
filtered as (
  select pr.pa, pr.pb, min(pr.sa) as sa, min(pr.sb) as sb
  from pairs pr
  join properties p1 on p1.id = pr.pa and p1.status = 'active'
  join properties p2 on p2.id = pr.pb and p2.status = 'active'
  where not exists (
    select 1 from property_identity_candidates c
    where c.left_property_id = least(pr.pa, pr.pb)
      and c.right_property_id = greatest(pr.pa, pr.pb)
      and c.status = 'dismissed'
  )
  group by pr.pa, pr.pb
)
select
  (select count(*) from filtered) as cnt,
  coalesce((
    select jsonb_agg(jsonb_build_object(
             'property_a', pa, 'property_b', pb,
             'sreality_a', sa, 'sreality_b', sb))
    from (select * from filtered order by pa, pb limit 20) s
  ), '[]'::jsonb) as samples
"""


def check_geo_debt(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    row = _fetchone(conn, _GEO_DEBT_SQL, {
        "nonbyt": list(_NONBYT),
        "price_pct": thresholds["geo_debt_price_pct"],
        "area_pct": thresholds["geo_debt_area_pct"],
    })
    count = int(row[0]) if row and row[0] is not None else 0
    samples = row[1] if row and row[1] is not None else []
    # value-only: a trend baseline (warn/fail on a rising count) needs history this
    # check does not yet have, so the status stays 'ok' and the number is recorded
    # for the /health sparkline until a threshold is calibrated.
    return {
        "check_key": "geo_debt",
        "status": "ok",
        "value": count,
        "details": {
            "suspect_pairs": count,
            "area_pct": thresholds["geo_debt_area_pct"],
            "price_pct": thresholds["geo_debt_price_pct"],
            "note": "value-only; trend-based thresholds are future work",
            "samples": samples,
        },
    }


_FUNNEL_SQL = """
select
  l.source,
  count(*) as total_active,
  count(*) filter (where l.street is not null and l.street <> '') as with_street,
  count(*) filter (where l.disposition is not null) as with_disposition,
  count(*) filter (where l.geom is not null) as with_geom,
  count(*) filter (where l.obec_id is not null) as with_obec,
  count(*) filter (where l.street is not null and l.street <> ''
                     and l.disposition is not null) as street_eligible,
  count(*) filter (where l.geom is not null
                     and l.category_main = any(%(nonbyt)s)) as geo_eligible
from listings l
where l.is_active = true
group by l.source
order by total_active desc
"""


def _pct(num: int, den: int) -> float:
    return round(100.0 * num / den, 2) if den else 0.0


def check_eligibility_funnel(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    rows = _fetchall(conn, _FUNNEL_SQL, {"nonbyt": list(_NONBYT)})
    per_source: list[dict[str, Any]] = []
    tot_active = tot_street_elig = 0
    for (source, total, w_street, w_disp, w_geom, w_obec, street_elig, geo_elig) in rows:
        total = int(total)
        tot_active += total
        tot_street_elig += int(street_elig)
        per_source.append({
            "source": source,
            "total_active": total,
            "pct_street": _pct(int(w_street), total),
            "pct_disposition": _pct(int(w_disp), total),
            "pct_geom": _pct(int(w_geom), total),
            "pct_obec": _pct(int(w_obec), total),
            "pct_street_eligible": _pct(int(street_elig), total),
            "pct_geo_eligible": _pct(int(geo_elig), total),
        })
    overall = _pct(tot_street_elig, tot_active)
    return {
        "check_key": "eligibility_funnel",
        "status": "ok",
        "value": overall,
        "details": {"overall_street_eligible_pct": overall,
                    "total_active": tot_active, "per_source": per_source},
    }


# Joins on left_listing_id/right_listing_id (the R2 surrogate, migrations 322 + 353
# backfill), NOT left_sreality_id/right_sreality_id: once Gate-2 flips, a merge
# involving a brand-new non-sreality-portal listing carries sreality_id=NULL on both
# the listing and the audit row, and a sreality_id-keyed join would silently drop it
# from the p50/p95 sample forever — a shrinking, growingly-unrepresentative latency
# gate that still reports green. The surrogate is populated for every row already
# (backfilled for legacy rows, stamped at insert by the engine/operator writers for
# new ones) except the ~4.5k historical self-paired audit rows the backfill
# deliberately skips (migration 353) — excluding those is correct, not a blind spot.
_MERGE_LATENCY_SQL = """
select
  percentile_cont(0.5)  within group (order by hrs) as p50,
  percentile_cont(0.95) within group (order by hrs) as p95,
  count(*) as n,
  (select count(*) from dedup_pair_audit d0
     where d0.outcome = 'merged' and d0.source = 'engine'
       and d0.run_at > now() - interval '7 days'
       and (d0.left_listing_id is null or d0.right_listing_id is null)) as excluded_n
from (
  select extract(epoch from (d.run_at - least(l1.first_seen_at, l2.first_seen_at))) / 3600.0 as hrs
  from dedup_pair_audit d
  join listings l1 on l1.id = d.left_listing_id
  join listings l2 on l2.id = d.right_listing_id
  where d.outcome = 'merged' and d.source = 'engine'
    and d.run_at > now() - interval '7 days'
    and d.left_listing_id is not null and d.right_listing_id is not null
) t
where hrs is not null and hrs >= 0
"""


def check_merge_latency(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    row = _fetchone(conn, _MERGE_LATENCY_SQL)
    p50 = float(row[0]) if row and row[0] is not None else None
    p95 = float(row[1]) if row and row[1] is not None else None
    n = int(row[2]) if row and row[2] is not None else 0
    excluded = int(row[3]) if row and row[3] is not None else 0
    status = _status_for_merge_latency(p95, thresholds)
    return {
        "check_key": "merge_latency",
        "status": status,
        "value": round(p95, 2) if p95 is not None else None,
        "details": {
            "p50_hours": round(p50, 2) if p50 is not None else None,
            "p95_hours": round(p95, 2) if p95 is not None else None,
            "merges_7d": n,
            "excluded_no_listing_id_7d": excluded,
            "warn_at_hours": thresholds["merge_p95_warn_hours"],
        },
    }


_ENGINE_HEALTH_SQL = """
select
  (select last_cycle_completed_at from dedup_scan_state where lane = 'street') as last_completed,
  (select cycle_started_at        from dedup_scan_state where lane = 'street') as cycle_started,
  (select updated_at              from dedup_scan_state where lane = 'street') as street_updated,
  (select (count(*) > 0) from dedup_scan_state where lane = 'street') as has_row,
  (select percentile_cont(0.95) within group (order by extract(epoch from (now() - marked_at)) / 3600.0)
     from dedup_dirty_properties) as dirty_p95_hours,
  (select percentile_cont(0.95) within group (order by extract(epoch from (now() - created_at)) / 86400.0)
     from property_identity_candidates where status = 'proposed') as cand_p95_days,
  (select count(*) from dedup_dirty_properties) as dirty_n,
  (select count(*) from property_identity_candidates where status = 'proposed') as proposed_n
"""


def _age_hours(ts: Any) -> float | None:
    if ts is None:
        return None
    now = _dt.datetime.now(_dt.timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    return (now - ts).total_seconds() / 3600.0


def check_engine_health(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    row = _fetchone(conn, _ENGINE_HEALTH_SQL)
    (last_completed, cycle_started, street_updated, has_row,
     dirty_p95, cand_p95, dirty_n, proposed_n) = (
        row if row else (None, None, None, False, None, None, 0, 0)
    )
    completed_age = _age_hours(last_completed)
    started_age = _age_hours(cycle_started)
    updated_age = _age_hours(street_updated)
    cycle_status = _status_for_cycle(
        has_row=bool(has_row), updated_age_hours=updated_age, thresholds=thresholds,
    )
    dirty_p95_h = float(dirty_p95) if dirty_p95 is not None else None
    cand_p95_d = float(cand_p95) if cand_p95 is not None else None
    dirty_status = _status_for_dirty(dirty_p95_h, thresholds)
    cand_status = _status_for_candidates(cand_p95_d, thresholds)
    status = _worst([cycle_status, dirty_status, cand_status])

    # Name only the component(s) actually degraded; keep the real-time-vs-backstop distinction
    # explicit so a stalled backstop scan is never read as "new listings aren't being deduped".
    issues: list[str] = []
    if cycle_status == "fail":
        issues.append(
            f"the street backstop scan is STALLED — its cursor hasn't advanced in "
            f"{updated_age:.1f}h (real-time cross-portal merges still flow via the worker; "
            "this is the market-wide catch-up scan that's stuck)"
            if updated_age is not None else "the street backstop scan has no progress signal"
        )
    elif cycle_status == "warn":
        issues.append("the dedup engine has not yet established a scan cycle")
    if dirty_status == "warn":
        issues.append(f"the dirty queue is aging (p95 {dirty_p95_h:.1f}h > "
                      f"{thresholds['dirty_age_p95_warn_hours']}h)")
    if cand_status == "warn":
        issues.append(f"the /dedup review queue is aging (p95 {cand_p95_d:.1f}d > "
                      f"{thresholds['candidate_age_p95_warn_days']}d)")
    if status == "ok":
        message = (
            f"Dedup engine healthy (street cursor advanced "
            f"{updated_age:.1f}h ago" + (f", dirty p95 {dirty_p95_h:.1f}h" if dirty_p95_h is not None else "") + ")."
        )
    else:
        message = "Dedup engine: " + "; ".join(issues) + "."
    return {
        "check_key": "engine_health",
        "status": status,
        "value": round(updated_age, 2) if updated_age is not None else None,
        "details": {
            "cycle_status": cycle_status,
            "street_cursor_updated_age_hours": round(updated_age, 2) if updated_age is not None else None,
            "cycle_stall_fail_hours": thresholds["cycle_stall_fail_hours"],
            "last_cycle_completed_at": last_completed.isoformat() if last_completed else None,
            "cycle_started_age_hours": round(started_age, 2) if started_age is not None else None,
            "dirty_status": dirty_status,
            "dirty_queue_n": int(dirty_n or 0),
            "dirty_age_p95_hours": round(dirty_p95_h, 2) if dirty_p95_h is not None else None,
            "dirty_age_p95_warn_hours": thresholds["dirty_age_p95_warn_hours"],
            "candidate_status": cand_status,
            "proposed_candidates_n": int(proposed_n or 0),
            "candidate_age_p95_days": round(cand_p95_d, 2) if cand_p95_d is not None else None,
            "candidate_age_p95_warn_days": thresholds["candidate_age_p95_warn_days"],
        },
        "message": message,
    }


_LLM_ERRORS_SQL = """
select called_for,
       count(*) as total,
       count(*) filter (where error is not null) as errors
from llm_calls
where called_at > now() - interval '24 hours'
group by called_for
order by total desc
"""

_LLM_CREDIT_SQL = """
select count(*) from llm_calls
where called_at > now() - interval '24 hours' and error ilike %s
"""

# Liveness: is the provider failing RIGHT NOW? Compares the newest failure vs the newest
# success (a success after the last error means recovered). `min_ok_at` bounds staleness so
# a lone error hours ago with no traffic since doesn't read as a live outage.
_LLM_LIVENESS_SQL = """
select
  max(called_at) filter (where error is not null) as last_err_at,
  max(called_at) filter (where error is null) as last_ok_at,
  max(called_at) filter (where error ilike %s) as last_credit_err_at,
  now() - interval '90 minutes' as min_live_at
from llm_calls
where called_at > now() - interval '24 hours'
"""


def check_llm_errors(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    rows = _fetchall(conn, _LLM_ERRORS_SQL)
    credit_row = _fetchone(conn, _LLM_CREDIT_SQL, ("%credit balance%",))
    credit_errors = int(credit_row[0]) if credit_row and credit_row[0] is not None else 0

    live = _fetchone(conn, _LLM_LIVENESS_SQL, ("%credit balance%",))
    last_err_at, last_ok_at, last_credit_err_at, min_live_at = (
        (live[0], live[1], live[2], live[3]) if live else (None, None, None, None)
    )
    # Currently failing = newest call is a failure AND that failure is recent (not stale).
    currently_failing = bool(
        last_err_at is not None
        and (last_ok_at is None or last_err_at > last_ok_at)
        and (min_live_at is None or last_err_at > min_live_at)
    )
    credit_live = bool(
        currently_failing
        and last_credit_err_at is not None
        and (last_ok_at is None or last_credit_err_at > last_ok_at)
    )

    per_called_for: list[dict[str, Any]] = []
    tot = err = 0
    for (called_for, total, errors) in rows:
        total, errors = int(total), int(errors)
        tot += total
        err += errors
        per_called_for.append({
            "called_for": called_for, "total": total, "errors": errors,
            "rate": round(errors / total, 4) if total else 0.0,
        })
    overall_rate = round(err / tot, 4) if tot else 0.0
    status, offenders = _status_for_llm_errors(
        per_called_for, credit_live, currently_failing, thresholds,
    )

    if credit_live:
        message = (
            "LLM calls are failing with credit-balance errors right now — the Anthropic "
            "account is out of credit. Every paid LLM path (dedup vision, estimations, "
            f"summaries, condition scoring) is down ({credit_errors} credit errors in 24h)."
        )
    elif offenders:
        message = (
            f"LLM error rate exceeded {thresholds['llm_error_rate_warn']:.0%} and is still "
            f"live for: {', '.join(offenders)} (24h window, >= 20 calls) — the provider is erroring."
        )
    else:
        message = f"LLM calls healthy ({overall_rate:.1%} error rate over 24h)."
    return {
        "check_key": "llm_errors",
        "status": status,
        "value": overall_rate,
        "details": {
            "overall_rate": overall_rate,
            "credit_balance_errors": credit_errors,
            "currently_failing": currently_failing,
            "credit_live": credit_live,
            "last_error_at": str(last_err_at) if last_err_at else None,
            "last_success_at": str(last_ok_at) if last_ok_at else None,
            "warn_rate": thresholds["llm_error_rate_warn"],
            "offending_called_for": offenders,
            "per_called_for": per_called_for,
        },
        "message": message,
    }


_LLM_SILENCE_SQL = """
select extract(epoch from (now() - max(called_at))) / 3600.0 as hours_since_last
from llm_calls
"""


def check_llm_liveness(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Total-silence guard: the platform runs paid LLM traffic continuously (dedup vision
    on the always-on worker), so a stretch with ZERO llm_calls means the pipeline is dead —
    worker down, key unset, or an outage so hard nothing is even attempted. This is the
    failure mode error-rate checks are structurally blind to (no calls → no errors → false
    green). p99 inter-call gap is ~1 min, so the 4h default never trips in normal operation.
    Folds in the unique liveness intent of the retired check_llm_health.py, but UNGATED — the
    old probe hid behind a condition-scoring `pending` gate that is dead while scoring is paused."""
    fail_hours = float(thresholds["llm_silence_fail_hours"])
    row = _fetchone(conn, _LLM_SILENCE_SQL)
    hours = float(row[0]) if row and row[0] is not None else None
    status = _status_for_llm_silence(hours, fail_hours)
    if hours is None:
        message = f"No LLM calls on record at all — the LLM pipeline looks dead (threshold {fail_hours:.0f}h)."
    elif status == "fail":
        message = (
            f"No LLM calls in {hours:.1f}h (> {fail_hours:.0f}h) — the LLM pipeline is silent "
            "(worker down / key unset / hard outage). No paid path is running."
        )
    else:
        message = f"LLM pipeline live (last call {hours:.2f}h ago)."
    return {
        "check_key": "llm_liveness",
        "status": status,
        "value": round(hours, 3) if hours is not None else None,
        "details": {"hours_since_last_call": hours, "fail_hours": fail_hours},
        "message": message,
    }


_LLM_BURN_SQL = """
select coalesce(sum(cost_usd), 0) as spend_24h
from llm_calls where called_at > now() - interval '24 hours'
"""

_LLM_BURN_TOP_SQL = """
select called_for, round(sum(cost_usd)::numeric, 2) as spend
from llm_calls
where called_at > now() - interval '24 hours' and cost_usd > 0
group by called_for order by spend desc limit 3
"""


def check_llm_burn_rate(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Spend-based credit-runway guard (see _status_for_burn). Names the top spenders so
    the alert says what to throttle, not just that money is burning."""
    warn_usd = float(thresholds["llm_spend_24h_warn_usd"])
    fail_usd = float(thresholds["llm_spend_24h_fail_usd"])
    row = _fetchone(conn, _LLM_BURN_SQL)
    spend = float(row[0]) if row and row[0] is not None else 0.0
    top = [(str(cf), float(s)) for (cf, s) in _fetchall(conn, _LLM_BURN_TOP_SQL)]
    status = _status_for_burn(spend, warn_usd, fail_usd)
    top_str = ", ".join(f"{cf} ${s:.2f}" for cf, s in top) or "none"
    if status == "fail":
        message = (
            f"LLM spend is ${spend:.2f} in 24h (> ${fail_usd:.0f}) — at this burn the credit "
            f"balance drains in days; check Plans & Billing / top up or throttle. Top spenders: {top_str}."
        )
    elif status == "warn":
        message = (
            f"LLM spend is ${spend:.2f} in 24h (> ${warn_usd:.0f}) — top-up cadence risk. "
            f"Top spenders: {top_str}."
        )
    else:
        message = f"LLM spend ${spend:.2f} in 24h (top: {top_str})."
    return {
        "check_key": "llm_burn_rate",
        "status": status,
        "value": round(spend, 2),
        "details": {"spend_24h_usd": round(spend, 2), "warn_usd": warn_usd,
                    "fail_usd": fail_usd, "top_spenders": dict(top)},
        "message": message,
    }


_DB_CRON_SQL = """
select j.jobname,
       count(*) filter (where d.status = 'succeeded') as ok,
       count(*) filter (where d.status = 'failed')    as failed
from cron.job_run_details d
join cron.job j using (jobid)
where d.start_time > now() - interval '6 hours'
group by j.jobname
"""


def check_db_saturation(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Watch pg_cron's own run ledger for the DB-saturation signature. Skips cleanly if
    the cron schema isn't visible (e.g. a branch DB without pg_cron) rather than false-fail."""
    fail_rate = float(thresholds["db_cron_fail_rate_fail"])
    try:
        rows = _fetchall(conn, _DB_CRON_SQL)
    except Exception as exc:  # noqa: BLE001 — cron schema not readable → warn (visible), never false-fail
        # verify connects via SUPABASE_DB_URL (postgres role, which has cron access); this
        # path only trips if that changes to a role lacking USAGE on schema cron. warn (not
        # ok) so the /health page shows the check is INERT rather than silently green.
        return {
            "check_key": "db_saturation", "status": "warn", "value": None,
            "details": {"skipped": f"cron.job_run_details unreadable: {exc}",
                        "fix": "GRANT USAGE ON SCHEMA cron TO service_role;"},
            "message": ("DB-saturation check is inert — can't read pg_cron's ledger. "
                        "Fix: GRANT USAGE ON SCHEMA cron TO service_role;"),
        }
    jobs = [{"jobname": jn, "ok": int(ok), "failed": int(fl)} for (jn, ok, fl) in rows]
    status, offenders = _status_for_cron(jobs, fail_rate)
    worst_rate = max(
        (j["failed"] / (j["ok"] + j["failed"]) for j in jobs if j["ok"] + j["failed"] > 0),
        default=0.0,
    )
    if len(offenders) >= 2:
        message = (
            f"{len(offenders)} pg_cron jobs failing over the last 6h (> {fail_rate:.0%}): "
            f"{', '.join(offenders)} — the database is likely saturated (statement timeouts hitting "
            "multiple jobs at once)."
        )
    elif offenders:
        message = (
            f"pg_cron job failing over the last 6h (> {fail_rate:.0%}): {offenders[0]} — that job "
            "(or a query it runs) is over the statement-timeout ceiling."
        )
    else:
        message = f"pg_cron healthy (worst job failure rate {worst_rate:.0%} over 6h)."
    return {
        "check_key": "db_saturation",
        "status": status,
        "value": round(worst_rate, 3),
        "details": {"offenders": offenders, "fail_rate": fail_rate,
                    "jobs": {j["jobname"]: {"ok": j["ok"], "failed": j["failed"]} for j in jobs}},
        "message": message,
    }


_WORKER_LIVENESS_SQL = """
select worker, extract(epoch from (now() - max(beat_at))) / 60.0 as age_min
from worker_heartbeats
group by worker
"""


def check_worker_liveness(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Watch the realtime worker's heartbeat — it owns the latency-critical loops but
    worker_heartbeats had no reader, so a dead worker was invisible."""
    stale_minutes = float(thresholds["worker_stale_fail_minutes"])
    rows = _fetchall(conn, _WORKER_LIVENESS_SQL)
    ages = [(str(w), float(age)) for (w, age) in rows if age is not None]
    status, stale = _status_for_worker(ages, stale_minutes)
    oldest = max((age for _, age in ages), default=0.0)
    if stale:
        message = (
            f"Realtime worker heartbeat is stale (> {stale_minutes:.0f}m): {', '.join(stale)} "
            "— the worker owns newest-first probes, the detail drain and real-time dedup; those loops are down."
        )
    elif not ages:
        message = "No worker heartbeats on record (worker not deployed) — nothing to watch."
    else:
        message = f"Realtime worker alive (last beat {oldest:.1f}m ago)."
    return {
        "check_key": "worker_liveness",
        "status": status,
        "value": round(oldest, 2),
        "details": {"stale_minutes": stale_minutes,
                    "workers": {w: round(age, 2) for (w, age) in ages}},
        "message": message,
    }


_PRECISION_SAMPLE_SQL = """
select id, run_at, left_sreality_id, right_sreality_id,
       left_property_id, right_property_id, category_main, stage, detail
from dedup_pair_audit
where source = 'engine' and outcome = 'merged'
  and run_at > now() - interval '7 days'
order by random()
limit %(n)s
"""


def check_merge_precision_sample(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    n = int(thresholds["precision_sample_n"])
    rows = _fetchall(conn, _PRECISION_SAMPLE_SQL, {"n": n})
    samples = [{
        "audit_id": int(r[0]),
        "run_at": r[1].isoformat() if r[1] else None,
        "sreality_a": r[2], "sreality_b": r[3],
        "property_a": r[4], "property_b": r[5],
        "category_main": r[6], "stage": r[7], "detail": r[8],
    } for r in rows]
    return {
        "check_key": "merge_precision_sample",
        "status": "ok",
        "value": len(samples),
        "details": {"sampled": len(samples), "requested": n, "samples": samples},
    }


# Keep every parity scan bounded so the 6-hourly run never degenerates into a seq
# scan of 8M images rows: look only at the newest slice above the watermark. A live
# writer gap shows up continuously, so the recent window catches it just as well as
# a full scan would — and stays index-driven as the tables grow.
_PARITY_ID_LOOKBACK = 200_000
_PARITY_TS_LOOKBACK_DAYS = 7


def _parity_carrier_sql(carrier: dict[str, Any]) -> str:
    table, cursor = carrier["table"], carrier["cursor"]
    if carrier.get("kind") == "ts":
        floor = f"greatest(w.cursor_ts, now() - interval '{_PARITY_TS_LOOKBACK_DAYS} days')"
    else:
        floor = (
            f"greatest(w.cursor_id, coalesce((select max({cursor}) from {table}), 0)"
            f" - {_PARITY_ID_LOOKBACK})"
        )
    skip = carrier.get("skip")
    skip_clause = f" and not ({skip})" if skip else ""
    parts: list[str] = []
    for legacy, new in carrier["cols"]:
        parts.append(f"count(*) filter (where t.{legacy} is not null and t.{new} is null{skip_clause})")
        parts.append(
            f"count(*) filter (where t.{legacy} is not null and t.{new} is not null"
            f" and t.{new} is distinct from"
            f" (select l.id from listings l where l.sreality_id = t.{legacy}){skip_clause})"
        )
        # Once Gate-2 flips, a brand-new non-sreality-portal row carries a NULL
        # legacy id by design — the two filters above (both anchored on
        # `t.{legacy} is not null`) silently stop seeing it. This counts rows
        # where the surrogate is ALSO missing despite the legacy id being absent:
        # the one shape of gap that is still detectable with no legacy value to
        # cross-check against (existence, not correctness — there's nothing to
        # compare a NULL legacy id to).
        parts.append(f"count(*) filter (where t.{legacy} is null and t.{new} is null{skip_clause})")
    return (
        f"select {', '.join(parts)}, count(*) "
        f"from {table} t, dual_write_watermark w "
        f"where w.child = '{table}' and t.{cursor} > {floor}"
    )


def check_dual_write_parity(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """R2 dual-write parity: every row written since the watermark that carries a
    legacy listing id must carry the matching surrogate, and it must be the RIGHT one.

    Three distinct failures, all otherwise silent: a writer nobody censused keeps
    stamping only the legacy id (gap), a writer stamps a surrogate belonging to a
    different listing (mismatch — what a positional zip of an unordered RETURNING
    produces), or — once Gate-2 flips and new non-sreality-portal rows carry a NULL
    legacy id by design — a writer stamps NEITHER id (orphan; the gap/mismatch
    filters are both anchored on "legacy is not null" and go blind to these rows).
    Gap detection is structural: it observes rows, not code paths, so it catches
    writers this refactor never enumerated.
    """
    unarmed: list[str] = []
    gaps: dict[str, int] = {}
    mismatches: dict[str, int] = {}
    orphans: dict[str, int] = {}
    scanned: dict[str, int] = {}
    # Which carriers are armed has to be established SEPARATELY, before counting.
    # The per-carrier query is aggregate-only, so with no watermark row it still
    # returns one row of zeros — indistinguishable from "clean". Reading armedness
    # off the counts would make every unarmed carrier silently green, which is the
    # exact failure this check exists to catch.
    armed = {str(r[0]) for r in _fetchall(conn, "select child from dual_write_watermark")}
    for carrier in _PARITY_CARRIERS:
        table = carrier["table"]
        if table not in armed:
            unarmed.append(table)
            continue
        rows = _fetchall(conn, _parity_carrier_sql(carrier))
        row = rows[0]
        for idx, (_legacy, new) in enumerate(carrier["cols"]):
            gap, bad, orphan = (
                int(row[idx * 3]), int(row[idx * 3 + 1]), int(row[idx * 3 + 2]),
            )
            if gap:
                gaps[f"{table}.{new}"] = gap
            if bad:
                mismatches[f"{table}.{new}"] = bad
            if orphan:
                orphans[f"{table}.{new}"] = orphan
        scanned[table] = int(row[-1])

    if gaps or mismatches or orphans:
        status = "fail"
        bits: list[str] = []
        if gaps:
            bits.append("missing surrogate on "
                        + ", ".join(f"{k} ({v} rows)" for k, v in sorted(gaps.items())))
        if mismatches:
            bits.append("WRONG surrogate on "
                        + ", ".join(f"{k} ({v} rows)" for k, v in sorted(mismatches.items())))
        if orphans:
            bits.append("NEITHER id on (NULL-legacy, i.e. post-flip) "
                        + ", ".join(f"{k} ({v} rows)" for k, v in sorted(orphans.items())))
        message = (
            "R2 dual-write parity broken: " + "; ".join(bits) + ". A writer is not "
            "stamping listings.id (or is stamping the wrong one) — the child FK backfill "
            "cannot converge until it is fixed."
        )
    elif len(unarmed) == len(_PARITY_CARRIERS):
        status = "warn"
        message = (
            "R2 dual-write parity is INERT — no carrier has a dual_write_watermark row. "
            "Arm it after the dual-write deploy: "
            "python -m scripts.verify_pipeline --arm-dual-write-parity"
        )
    elif unarmed:
        status = "warn"
        message = (
            f"R2 dual-write parity is partially armed — {len(unarmed)} carrier(s) have no "
            f"watermark and are unwatched: {', '.join(sorted(unarmed))}."
        )
    else:
        status = "ok"
        message = (
            f"R2 dual-write parity clean across {len(_PARITY_CARRIERS)} carriers "
            f"({sum(scanned.values())} recent rows checked)."
        )
    return {
        "check_key": "dual_write_parity",
        "status": status,
        "value": sum(gaps.values()) + sum(mismatches.values()) + sum(orphans.values()),
        "details": {"gaps": gaps, "mismatches": mismatches, "orphans": orphans,
                    "unarmed": unarmed, "scanned": scanned},
        "message": message,
    }


def arm_dual_write_parity(conn: Any) -> list[str]:
    """Seed/refresh each carrier's watermark from where its cursor stands NOW.

    Run once, AFTER the dual-write deploy is live. Arming late is safe (rows written
    in between merely look like backfill work); arming before the deploy would mark
    old-code rows as post-dual-write and alarm falsely.
    """
    armed: list[str] = []
    for carrier in _PARITY_CARRIERS:
        table, cursor = carrier["table"], carrier["cursor"]
        legacy, new = carrier["cols"][0]
        is_ts = carrier.get("kind") == "ts"
        col = "cursor_ts" if is_ts else "cursor_id"
        default = "now()" if is_ts else "0"
        with conn.cursor() as cur:
            cur.execute(
                f"insert into dual_write_watermark "
                f"(child, legacy_col, new_col, cursor_col, {col}) "
                f"select %s, %s, %s, %s, coalesce(max({cursor}), {default}) from {table} "
                f"on conflict (child) do update set "
                f"{col} = excluded.{col}, legacy_col = excluded.legacy_col, "
                f"new_col = excluded.new_col, cursor_col = excluded.cursor_col, "
                f"armed_at = now()",
                (table, legacy, new, cursor),
            )
        armed.append(table)
    return armed


_CHECKS: list[tuple[str, Callable[[Any, dict[str, Any]], dict[str, Any]]]] = [
    ("street_debt", check_street_debt),
    ("geo_debt", check_geo_debt),
    ("eligibility_funnel", check_eligibility_funnel),
    ("merge_latency", check_merge_latency),
    ("engine_health", check_engine_health),
    ("llm_errors", check_llm_errors),
    ("llm_liveness", check_llm_liveness),
    ("llm_burn_rate", check_llm_burn_rate),
    ("db_saturation", check_db_saturation),
    ("worker_liveness", check_worker_liveness),
    ("dual_write_parity", check_dual_write_parity),
]

_WEEKLY_CHECKS: list[tuple[str, Callable[[Any, dict[str, Any]], dict[str, Any]]]] = [
    ("merge_precision_sample", check_merge_precision_sample),
]


def run_checks(
    conn: Any, thresholds: dict[str, Any], *, weekly: bool = False,
    only: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Run every check in isolation — a raising check becomes a `fail` row carrying
    the error, so one broken check never aborts the run. `only` restricts to the named
    check keys (the hourly LLM-liveness lane runs just the two llm_* checks)."""
    results: list[dict[str, Any]] = []
    checks = list(_CHECKS) + (list(_WEEKLY_CHECKS) if weekly else [])
    if only:
        checks = [(k, fn) for (k, fn) in checks if k in only]
    for key, fn in checks:
        try:
            results.append(fn(conn, thresholds))
        except Exception as exc:  # noqa: BLE001
            LOG.exception("check %s errored", key)
            results.append({
                "check_key": key,
                "status": "fail",
                "value": None,
                "details": {"error": str(exc)},
                "message": f"Pipeline verification check '{key}' errored: {exc}",
            })
    return results


def write_results(
    conn: Any, results: list[dict[str, Any]], run_at: _dt.datetime,
) -> dict[str, int]:
    """Persist one row per check, then ring the bell on TRANSITIONS only (onset /
    recovery), not on every red run. Returns {onset, recovery} counts.

    The previous stored status is read BEFORE this run's rows are inserted, so the
    baseline is the prior run — see toolkit.system_alerts.emit_transition_alerts."""
    prev = latest_statuses(conn)
    with conn.cursor() as cur:
        for r in results:
            cur.execute(
                "INSERT INTO pipeline_check_results (run_at, check_key, status, value, details) "
                "VALUES (%s, %s, %s, %s, %s::jsonb)",
                (run_at, r["check_key"], r["status"],
                 r.get("value"), json.dumps(r.get("details") or {})),
            )
    return emit_transition_alerts(conn, results, prev, run_at)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute + log, write nothing (no result rows, no alerts).")
    parser.add_argument("--weekly", action="store_true",
                        help="Also emit the weekly merge-precision sample.")
    parser.add_argument("--only", default="",
                        help="Comma-separated check keys to run (e.g. 'llm_errors,llm_liveness' "
                             "for the hourly LLM lane). Empty = all checks.")
    parser.add_argument("--exit-nonzero-on-fail", action="store_true",
                        help="Exit 1 if any run check is 'fail' — so the hourly LLM lane's "
                             "GitHub run goes red and emails the operator (belt-and-braces "
                             "for when the in-app bell path itself is down).")
    parser.add_argument("--arm-dual-write-parity", action="store_true",
                        help="Seed each R2 carrier's dual_write_watermark from where its "
                             "cursor stands now, then exit. Run ONCE, after the dual-write "
                             "deploy is live — arming before it would mark old-code rows as "
                             "post-dual-write and alarm falsely.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    if args.arm_dual_write_parity:
        with connect() as conn:
            armed = arm_dual_write_parity(conn)
        LOG.info("armed dual-write parity watermarks for %d carriers", len(armed))
        return 0

    only = {k.strip() for k in args.only.split(",") if k.strip()} or None
    run_at = _dt.datetime.now(_dt.timezone.utc)
    with connect() as conn:
        thresholds = load_thresholds(conn)
        results = run_checks(conn, thresholds, weekly=args.weekly, only=only)
        for r in results:
            LOG.info("CHECK %s status=%s value=%s", r["check_key"], r["status"], r.get("value"))
        if args.dry_run:
            LOG.info("dry-run: %d checks computed, no rows written", len(results))
            return 0
        counts = write_results(conn, results, run_at)
    LOG.info(
        "verify_pipeline wrote %d rows, emitted %d onset + %d recovery alerts",
        len(results), counts["onset"], counts["recovery"],
    )
    if args.exit_nonzero_on_fail and any(r["status"] == "fail" for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
