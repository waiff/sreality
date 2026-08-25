"""verify_pipeline.py — scheduled pipeline-health harness.

Computes a fixed set of pipeline-health metrics (LLM error rate + liveness + burn
rate, DB saturation, worker liveness, dual-write parity, property maintenance,
broker-resolution freshness),
writes one `pipeline_check_results` row per check, and rings the in-app bell on
STATE TRANSITIONS only (toolkit.system_alerts.emit_transition_alerts): once when a check
goes red, once when it recovers — not on every red run.

Born from the 2026-07 incident: the pipeline stalled silently for two days
(Anthropic credit exhaustion; 38k+ failed LLM calls) with no in-app signal. This
job makes that loud and durable.

Each check is isolated (one failing check writes a `fail` row with the error in
`details`, never kills the run). Thresholds live in
`app_settings.pipeline_check_thresholds` with the code defaults below as fallbacks.

    python -m scripts.verify_pipeline            # compute + write + alert
    python -m scripts.verify_pipeline --dry-run  # compute + log only, no writes
    python -m scripts.verify_pipeline --weekly   # also run the weekly-only checks

Needs only SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
from decimal import Decimal
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
    "llm_error_rate_warn": 0.2,
    "llm_silence_fail_hours": 4,
    "llm_spend_24h_warn_usd": 90,
    "llm_spend_24h_fail_usd": 150,
    "db_cron_fail_rate_fail": 0.5,
    "worker_stale_fail_minutes": 5,
    "verification_stale_hours": 24,
    # Property maintenance (2026-08-06 incident: 4 days of silently dead daily
    # sweeps + a stranded lease freezing every maintenance lane). The sweep
    # stamps app_settings.property_sweep_last_complete ONLY on a complete
    # walk; healthy age is ~24h (daily 04:15 cadence), so fail at 30h fires
    # ~5-6h after a dead/killed/incomplete sweep — however the process died.
    # Dirty rows drain within ~2 min of the worker lane's tick — EXCEPT while the
    # daily full sweep holds the maintenance lease, which blocks every incremental
    # pass and only clears dirty_properties at the very end, so oldest-dirt ages
    # 1:1 with sweep elapsed. That hold is bounded by the sweep's own budget
    # (recompute_property_stats._MAX_BUDGET_SECONDS, raised to 6000s = 100 min in
    # #1026) plus lease wait + finalize, so warn must sit ABOVE it or a perfectly
    # healthy long sweep turns this axis amber and trains the operator to ignore
    # it. 1.5h -> 2.5h; fail stays 3h. Raise this together with the sweep budget.
    "property_sweep_warn_hours": 26,
    "property_sweep_fail_hours": 30,
    "property_dirty_warn_hours": 2.5,
    "property_dirty_fail_hours": 3,
    # Broker resolution. Same two axes as property maintenance, but the sweep axis
    # measures a ROTATION LAP, not one run: attribution truncates on its --max-seconds
    # budget on most days (480,000 of 535,007 ids on 2026-08-10), so the corpus is
    # covered across one or two daily runs and the stamp lands once per lap. Warn
    # above a two-run lap plus the 110-min backstop drift (48 + 1.9); fail above a
    # three-run lap plus a fully missed day, which is a rotation that has genuinely
    # stopped reconciling rather than one having a slow week.
    "broker_sweep_warn_hours": 52,
    "broker_sweep_fail_hours": 84,
    # A third, independent axis on the RUN rather than the rotation.
    # _record_sweep_progress stamps lap completion straight after attribution,
    # ~17-25 min ahead of the tail (cross-source merge, the three rollups, the
    # matview, the candidate generator, _finalize's dirty-clear) — so a sweep
    # whose tail dies still leaves a minutes-old lap stamp and the lap axis
    # certifies it. Deferring the stamp would only move the wrongness (a lap that
    # HAS closed would then age from the previous one), so completion gets its own
    # axis: the age of the last full run that reached _finalize's ended_at. That
    # tail runs on every sweep regardless of lap closure, so steady state is ~24h
    # and the lap's deliberately wide 52/84 would be far too slack here.
    #
    # Sized on the GAP between consecutive ended_at, which is a whole number of
    # daily runs PLUS the spread in when a run finishes — GH's scheduled-run delay,
    # the <=21-min lock wait and the run itself, ~2.5h live to 2026-08-12 (06:25 to
    # 08:52 UTC on a 04:35 cron). So one ordinary night reaches ~26.5h (warn 30
    # clears it), one MISSED night ~50.5h and two ~69.5h. Fail therefore has to sit
    # between those last two: 50 sat below one missed night's worst case, which reds
    # the hourly acute lane — an onset alert plus llm_health.yml's
    # --exit-nonzero-on-fail emailing a second time — for a single miss the sweep's
    # own red run already emailed about. 60 keeps one miss a warn and still fails
    # two, and costs ~10h of detection latency on the nightly-dead tail this axis
    # exists for, which warn already ambers within ~30h.
    "broker_finished_warn_hours": 30,
    "broker_finished_fail_hours": 60,
    # The full sweep holds broker_resolution_lock for its whole run — every */10
    # incremental skips cleanly meanwhile — and clears dirty_broker_listings only at
    # finalize, so oldest-dirt ages 1:1 with the sweep and can legitimately reach
    # that same 3h backstop (raised from 1.9h with resolve_brokers_full.yml's
    # timeout-minutes 110 -> 180). Warn above it or a healthy long sweep turns the
    # axis amber; the extra headroom covers the post-sweep catch-up drain (the
    # incremental claims --batch-size 5000 per */10 tick = 30k/h).
    "broker_dirty_warn_hours": 4,
    "broker_dirty_fail_hours": 5,
    # The suppression rail is a binary invariant, not a gradient: one active
    # suppression whose two identities sit under the same broker means a NO the
    # operator recorded was bypassed. No warn tier — there is no "slightly merged".
    "broker_suppression_violations_fail": 1,
    # --- per-m2 measure plausibility (W9) -----------------------------------
    # Every number below is sized against a live measurement of production on
    # 2026-08-25, BEFORE the W2 backfill healed the two defects — so each one is
    # verified to fire on the bug it was written for and verified NOT to fire on
    # the healthy portals in the same cell. migrations/427 records the full table.
    #
    # Median shift is week-over-week within ONE (source, category_main,
    # category_type) cell, never across portals: cross-portal medians legitimately
    # differ by up to 19x (portal mix), so a peer comparison cannot separate a bug
    # from a rural land catalogue. The noisiest weekly move measurable today —
    # medians of the NEW-ARRIVAL cohort, which swings far harder than the stock
    # medians this check compares — is 1.47x on area and 1.90x on Kc/m2 across 21
    # cells. Warn sits just above that, fail at 3x. The mmreality heal will move
    # that cell 6.96x (area) and 7.45x (Kc/m2), so the gap is wide in both
    # directions: no false alarm from an ordinary week, and a basis flip cannot
    # sneak under it. 200 rows in BOTH weeks or the median is not compared — and
    # 200 rows means 200 rows CARRYING THAT MEDIAN, not 200 rows in the cell. The
    # two are wildly different live: 64 cells clear n_active >= 200, but only 58
    # clear it on area support and 56 on Kc/m2 support, so gating on cell size
    # would compare 6 and 8 cells' medians resting on as few as 9 values —
    # bezrealitky pozemek/prodej is 1 643 active rows, 9 areas, a 17.6x spread
    # across those 9, and two ordinary delistings move it past the 3x fail.
    "ppm2_median_shift_warn_ratio": 2.0,
    "ppm2_median_shift_fail_ratio": 3.0,
    "ppm2_median_shift_min_rows": 200,
    # Floor share is an absolute LEVEL, not a jump — the unit-price masquerade has
    # been stationary for the whole life of the portals carrying it, so there is no
    # jump to detect. Live: ceskereality komercni/pronajem 20.0%, realitymix 19.0%,
    # realitymix pozemek/pronajem 15.7%, remax 11.3%, bazos 6.7% — against idnes and
    # sreality at 0.5% on the same cell. fail 10% indicts the first four, warn 5%
    # ambers bazos, and the two portals with a working per-area guard stay green.
    "ppm2_basis_floor_share_warn": 0.05,
    "ppm2_basis_floor_share_fail": 0.10,
    "ppm2_basis_floor_min_rows": 100,
    # Area divergence: share of rows carrying BOTH areas whose values differ by more
    # than the view's 10% material band. Live, mmreality dum/prodej is 99.7% (100.0%
    # over the trailing week) while every other portal's dum/prodej is 0.0%. The one
    # cell in between is realitymix byt at 10.5% — a genuine but far milder field
    # convention, decaying legacy stock post-W1 — so warn sits at 20% to leave it
    # green rather than amber it forever, and fail at 40% still catches a whole-cell
    # basis flip an order of magnitude before it gets there.
    "area_divergence_share_warn": 0.20,
    "area_divergence_share_fail": 0.40,
    "area_divergence_min_rows": 100,
    # Coverage: the share of a cell's active rows the measure has NO INPUT for (no
    # price, or no positive area). Severity is a property of the ARM, not of the
    # number. The stock arm can only WARN: the live offenders are the four sreality
    # `pozemek` cells at 100.0% and bezrealitky pozemek/prodej at 99.5% — land plot
    # size lives in `estate_area`, which the measure does not read — and that is a
    # standing, sanctioned gap (charter: a NULL measure is a visible gap, never a
    # guess), so it is amber, named, and not a red tile nobody can clear. The next
    # cell down is realitymix ostatni/pronajem at 89.4%, so 0.95 separates "this
    # cell has no measure" from ordinary portal incompleteness with 5.6pp of
    # headroom. The 7d arm FAILS at 0.90: a gap that large among the rows that
    # arrived this week is a parser regression in flight, and the worst live 7d gap
    # over cells with 200+ new rows is 0.358 — so the fail tier has 54pp of headroom
    # and stays reserved for the case the other three axes go QUIET on.
    "ppm2_coverage_gap_warn": 0.95,
    "ppm2_coverage_gap_fail_7d": 0.90,
    "ppm2_coverage_min_rows": 200,
}

# --- pure status derivation (unit-tested without a DB) ---------------------


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


def _status_for_property_maintenance(
    sweep_age_hours: float | None,
    oldest_dirty_hours: float | None,
    thresholds: dict[str, Any],
) -> tuple[str, list[str]]:
    """Worst-of over the two maintenance liveness axes.

    `sweep_age_hours` is the age of the last COMPLETE full sweep's stamp
    (app_settings.property_sweep_last_complete, written by the sweep itself) —
    None means no stamp on record, which is a warn, not a fail: it is the
    expected state between deploying this check and the first complete sweep,
    and permanently red would train the operator to ignore the check. A dirty
    row aging past its axis means the incremental drain (worker lane + cron)
    is frozen. Both axes are O(1) reads: a per-row staleness scan over 620k
    properties measured ~3.5 min live and would blow the hourly acute lane's
    own 5-min job timeout — recreating the silent-`cancelled` mode this check
    exists to catch."""
    return _status_over_age_axes(
        [("last complete sweep", sweep_age_hours,
          thresholds["property_sweep_warn_hours"],
          thresholds["property_sweep_fail_hours"]),
         ("oldest dirty-queue row", oldest_dirty_hours,
          thresholds["property_dirty_warn_hours"],
          thresholds["property_dirty_fail_hours"])],
        stamp_missing=sweep_age_hours is None,
    )


def _status_for_broker_resolution(
    sweep_age_hours: float | None,
    oldest_dirty_hours: float | None,
    thresholds: dict[str, Any],
    *,
    finished_age_hours: float | None = None,
    sweep_label: str = "last complete broker sweep",
) -> tuple[str, list[str]]:
    """Worst-of over the three broker-resolution liveness axes (rule-20 shape).

    `sweep_age_hours` is how long ago the rotation last covered the whole corpus —
    app_settings.broker_resolution_last_complete, which the sweep stamps when a LAP
    closes, falling back to the open lap's start so a rotation that has never closed
    one still ages into `fail` instead of parking on the missing-stamp warn. That is
    the axis that matters: attribution breaks out on --max-seconds and, before the
    rotation cursor, silently re-walked the same head every day — a green exit code
    over a permanently unattributed tail. `finished_age_hours` is the independent
    completion axis: the lap stamp lands before the sweep's whole tail, so it says
    nothing about whether the merges, rollups, matview and dirty-clear ever ran —
    only broker_resolution_runs.ended_at does. `oldest_dirty_hours` watches the */10
    incremental drain of dirty_broker_listings, which is what attributes a new or
    re-brokered listing inside the day. A missing completion axis is skipped, never
    a fail: only the LAP stamp's absence is the deploy-day warn."""
    return _status_over_age_axes(
        [(sweep_label, sweep_age_hours,
          thresholds["broker_sweep_warn_hours"],
          thresholds["broker_sweep_fail_hours"]),
         ("last finished full sweep", finished_age_hours,
          thresholds["broker_finished_warn_hours"],
          thresholds["broker_finished_fail_hours"]),
         ("oldest broker dirty-queue row", oldest_dirty_hours,
          thresholds["broker_dirty_warn_hours"],
          thresholds["broker_dirty_fail_hours"])],
        stamp_missing=sweep_age_hours is None,
    )


def _status_over_age_axes(
    axes: list[tuple[str, float | None, float, float]], *, stamp_missing: bool,
) -> tuple[str, list[str]]:
    """Worst-of over (label, age_hours, warn_h, fail_h) axes; a None age is skipped.

    A missing completion stamp is a warn, not a fail: it is the expected state
    between deploying a check and the first complete sweep, and permanently red
    would train the operator to ignore the check."""
    status = "ok"
    offenders: list[str] = []
    if stamp_missing:
        status = "warn"
        offenders.append(
            "no complete-sweep stamp on record (first sweep since deploy "
            "still pending, or the sweep has never completed)")
    for name, hours, warn_h, fail_h in axes:
        if hours is None:
            continue
        # :g, not :.0f — the dirty thresholds are fractional, and rounding 2.5 down
        # to "2" renders self-contradictory offenders like "2.6h (warn > 2h)".
        if hours > fail_h:
            status = "fail"
            offenders.append(f"{name} {hours:.1f}h (fail > {fail_h:g}h)")
        elif hours > warn_h:
            if status == "ok":
                status = "warn"
            offenders.append(f"{name} {hours:.1f}h (warn > {warn_h:g}h)")
    return status, offenders


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


# --- per-m2 measure plausibility (W9) --------------------------------------
#
# The four checks below exist because the OTHER health surfaces cannot see the
# defects the per-m2 program fixed. `data_quality_by_source` tests 29 fields for
# IS NOT NULL; both defects produce 100% non-NULL values. A null-check cannot see
# a plot area sitting in a floor-area column, and it cannot see 136 Kc sitting in
# a price column. These read `measure_plausibility_by_source` (migration 427)
# instead, which measures what the value IS rather than whether it exists.


def _cell_key(cell: dict[str, Any]) -> str:
    """Stable (source, category_main, category_type) label — also the key the
    week-over-week baseline is stored under, so it must not change spelling."""
    return (
        f"{cell['source']}/{cell['category_main'] or '?'}/{cell['category_type'] or '?'}"
    )


def _status_for_share(
    cells: list[dict[str, Any]],
    arms: list[tuple[str, str, str, float, float | None]],
    *,
    min_rows: int,
    skip_category_main: frozenset[str] = frozenset(),
) -> tuple[str, list[str], float, int]:
    """Worst-of a share metric over cells × arms; (status, offenders, worst, scored).

    `arms` is [(label, share_key, count_key, warn, fail)] — every one of these checks
    has two: the whole active stock, and the rows first seen in the trailing 7 days.
    The fresh arm is the one with low detection latency (a regression is ~100% of what
    arrived since it shipped, but only churn-fraction of the stock), the stock arm
    is the one that indicts a defect that has been standing for months. Alarming on
    the worse of the two is what makes the pair catch both a new regression and an
    old one. A cell under `min_rows` on an arm is skipped on that arm, not scored
    zero — a 12-row cell's share is noise, and treating it as clean would let a
    small portal hide.

    Each arm carries its OWN warn/fail because severity is not a property of the
    metric but of the arm: a standing coverage gap is amber (visible, named, nobody
    can fix it today), the SAME share among this week's arrivals is a regression that
    shipped and is red. `fail=None` means the arm can only amber.

    `scored` is the count of (cell, arm) pairs that actually produced a number, and it
    is the whole point of the return tuple: `worst` starts at 0.0, so a corpus where
    every arm was skipped is indistinguishable by value from a corpus that was
    measured and clean. Callers MUST refuse to report `ok` on scored == 0."""
    status = "ok"
    offenders: list[str] = []
    worst = 0.0
    scored = 0
    for cell in cells:
        if cell["category_main"] in skip_category_main:
            continue
        for label, share_key, count_key, warn, fail in arms:
            share, n = cell.get(share_key), cell.get(count_key)
            if share is None or n is None or n < min_rows:
                continue
            scored += 1
            worst = max(worst, float(share))
            if fail is not None and share >= fail:
                status = "fail"
            elif share >= warn:
                if status == "ok":
                    status = "warn"
            else:
                continue
            hard = fail is not None and share >= fail
            gate = "fail" if hard else "warn"
            offenders.append(
                f"{_cell_key(cell)} {label} {share:.1%} of {int(n)} "
                f"({gate} >= {fail if hard else warn:.0%})"
            )
    return status, offenders, worst, scored


def _status_for_median_shift(
    cells: list[dict[str, Any]],
    baseline: dict[str, Any] | None,
    history_days: float | None,
    thresholds: dict[str, Any],
) -> tuple[str, list[str], float]:
    """Week-over-week move of a cell's own median area and median Kc/m2.

    Compared against ITSELF a week ago, never against other portals: cross-portal
    medians differ by up to 19x for legitimate mix reasons (idnes' rural land is
    8.5x the peer Kc/m2 and is not a bug), so a peer arm would be permanently red
    on land and `ostatni` while adding nothing the two direct detectors catch. What
    this arm catches that they cannot is the NEXT basis regression, on the run after
    it ships, in a cell nobody thought to write a detector for.

    Each of the two medians is gated on its own support count in BOTH weeks — the rows
    that actually carry that value — and never on the cell's row count, which is a
    different and much larger number. Returns the number of comparisons actually made:
    `worst` starts at the identity ratio 1.0, so "compared 40 cells, all stable" and
    "compared nothing at all" are the same number and only the count separates them.

    A missing baseline is `ok` for the first week after deploy — there is nothing
    the operator could do about it and a permanent amber trains them to ignore the
    axis — but `warn` after that, because a check that has been erroring for two
    weeks also leaves no baseline and must not read as green."""
    warn_ratio = float(thresholds["ppm2_median_shift_warn_ratio"])
    fail_ratio = float(thresholds["ppm2_median_shift_fail_ratio"])
    min_rows = int(thresholds["ppm2_median_shift_min_rows"])
    if not baseline:
        if history_days is not None and history_days >= 8.0:
            return "warn", [
                "no usable baseline in the 6-14 day window although this check has "
                f"{history_days:.0f} days of history — it has been failing or not running"
            ], 0.0, 0
        return "ok", [], 0.0, 0

    status = "ok"
    offenders: list[str] = []
    worst = 1.0
    compared = 0
    for cell in cells:
        prev = baseline.get(_cell_key(cell))
        if not isinstance(prev, dict):
            continue
        for label, now_key, then_key, now_n_key, then_n_key in (
            ("median area_m2", "median_area_m2", "area", "n_area_valued", "n_area"),
            ("median Kc/m2", "median_price_per_m2", "ppm2", "n_ppm2_valued", "n_ppm2"),
        ):
            now_v, then_v = cell.get(now_key), prev.get(then_key)
            if not now_v or not then_v:
                continue
            # Gate each median on ITS OWN support in both weeks, never on n_active.
            # percentile_cont ignores NULLs, so a cell's median rests only on the rows
            # carrying that value: bezrealitky pozemek/prodej has 1 643 active rows and
            # 9 areas, and gating it on 1 643 compares two medians-of-9 whose live
            # spread is 17.6x — two ordinary delistings move it past the 3.0x fail and
            # ring the bell, with no parser change anywhere. A baseline row written
            # before the counts existed carries neither key; it is skipped rather than
            # falling back to `n`, which would silently restore the same false gate.
            now_n, then_n = cell.get(now_n_key), prev.get(then_n_key)
            if now_n is None or then_n is None or now_n < min_rows or then_n < min_rows:
                continue
            compared += 1
            ratio = max(float(now_v) / float(then_v), float(then_v) / float(now_v))
            worst = max(worst, ratio)
            if ratio >= fail_ratio:
                status = "fail"
            elif ratio >= warn_ratio:
                if status == "ok":
                    status = "warn"
            else:
                continue
            gate = "fail" if ratio >= fail_ratio else "warn"
            offenders.append(
                f"{_cell_key(cell)} {label} {float(now_v):,.0f} vs {float(then_v):,.0f} "
                f"a week ago ({ratio:.2f}x, {gate} >= "
                f"{fail_ratio if gate == 'fail' else warn_ratio:g}x)"
            )
    return status, offenders, worst, compared


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


# One O(1) round trip: the sweep-completion stamp + the tiny dirty queue.
# Deliberately NOT a per-row staleness scan over properties — that measured
# ~3.5 min live (620k-row heap × listings semi-join) and would blow the hourly
# acute lane's 5-min job timeout, taking every other acute check's rows and
# alerts down with it.
_PROPERTY_MAINTENANCE_SQL = """
select
  (select extract(epoch from (now() - (value->>'completed_at')::timestamptz)) / 3600.0
     from app_settings where key = 'property_sweep_last_complete')
    as sweep_age_hours,
  (select extract(epoch from (now() - min(d.marked_at))) / 3600.0
     from dirty_properties d) as oldest_dirty_hours,
  (select count(*) from dirty_properties) as dirty_depth
"""


def check_property_maintenance(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Watch the property-stats maintenance loop (rule 20): the daily full sweep,
    the incremental dirty drain, and the lease that serializes them. Born from the
    2026-08-06 incident — the sweep outgrew its job timeout and died `cancelled`
    (not `failed`) for 4 days straight while each kill's stranded lease froze every
    maintenance lane; no check watched any of it. The sweep axis reads the
    completion stamp the (fixed) sweep writes on complete walks only, so ANY way
    the sweep dies — SIGKILL, runner death, chronic budget exhaustion — surfaces
    as a stale stamp within hours."""
    row = _fetchone(conn, _PROPERTY_MAINTENANCE_SQL)
    sweep_age, oldest_dirty, dirty_depth = (
        (None, None, 0) if row is None else (
            float(row[0]) if row[0] is not None else None,
            float(row[1]) if row[1] is not None else None,
            int(row[2] or 0),
        )
    )
    status, offenders = _status_for_property_maintenance(
        sweep_age, oldest_dirty, thresholds)
    if offenders:
        message = (
            "Property maintenance is falling behind: " + "; ".join(offenders)
            + " — check the daily sweep's runs (timeout kills report as "
            "cancelled) and the maintenance lease."
        )
    else:
        message = (
            f"Property maintenance healthy (last complete sweep "
            f"{sweep_age:.1f}h ago, dirty queue {dirty_depth})."
        )
    return {
        "check_key": "property_maintenance",
        "status": status,
        "value": round(sweep_age, 2) if sweep_age is not None else None,
        "details": {
            "sweep_age_hours": sweep_age,
            "oldest_dirty_hours": oldest_dirty,
            "dirty_depth": dirty_depth,
            "offenders": offenders,
        },
        "message": message,
    }


# The broker analogue of _PROPERTY_MAINTENANCE_SQL, and O(1) for the same reason:
# a per-row scan for unattributed listings is exactly the query resolve_brokers
# deleted from its incremental (broker_identity_id IS NULL is a permanent state for
# ~110k listings, so it detoasted the whole raw_json corpus for ~7 stragglers).
_BROKER_RESOLUTION_SQL = """
select
  (select extract(epoch from (now() - (value->>'completed_at')::timestamptz)) / 3600.0
     from app_settings where key = 'broker_resolution_last_complete')
    as sweep_age_hours,
  (select extract(epoch from (now() - (value->>'lap_started_at')::timestamptz)) / 3600.0
     from app_settings where key = 'broker_sweep_cursor') as lap_age_hours,
  (select extract(epoch from (now() - max(r.ended_at))) / 3600.0
     from broker_resolution_runs r where r.mode = 'full' and r.ended_at is not null)
    as finished_age_hours,
  (select extract(epoch from (now() - min(d.marked_at))) / 3600.0
     from dirty_broker_listings d) as oldest_dirty_hours,
  (select count(*) from dirty_broker_listings) as dirty_depth
"""


def check_broker_resolution_freshness(
    conn: Any, thresholds: dict[str, Any],
) -> dict[str, Any]:
    """Watch the broker-resolution loop — the daily full sweep and the */10
    incremental drain. Born from the 2026-08-12 E2E review: the sweep broke out of
    attribution on its budget, wiped the whole dirty queue anyway and restarted at
    the same low id next time, so the newest ~10% of broker-bearing listings were
    skipped every single day — and nothing watched it, because the job still exited
    0. The sweep axis ages the rotation's last closed LAP; before any lap has
    closed it ages the open one, so a rotation that never gets around the corpus
    reds instead of hiding behind a missing stamp. The lap stamp lands before the
    sweep's 17-25 min tail, so a separate axis ages the last run that actually
    reached ended_at — a tail that dies is invisible to the other two."""
    row = _fetchone(conn, _BROKER_RESOLUTION_SQL)
    stamp_age, lap_age, finished_age, oldest_dirty, dirty_depth = (
        (None, None, None, None, 0) if row is None else (
            float(row[0]) if row[0] is not None else None,
            float(row[1]) if row[1] is not None else None,
            float(row[2]) if row[2] is not None else None,
            float(row[3]) if row[3] is not None else None,
            int(row[4] or 0),
        )
    )
    sweep_age = stamp_age if stamp_age is not None else lap_age
    status, offenders = _status_for_broker_resolution(
        sweep_age, oldest_dirty, thresholds, finished_age_hours=finished_age,
        sweep_label=("last complete broker sweep" if stamp_age is not None
                     else "open rotation lap (no lap closed yet)"))
    if offenders:
        message = (
            "Broker resolution is falling behind: " + "; ".join(offenders)
            + " — check the daily sweep's runs (a budget-truncated sweep logs "
            "'time budget reached during attribution' and still exits 0, and a "
            "sweep whose tail dies still stamps its lap) and "
            "broker_resolution_lock."
        )
    else:
        finished_txt = (f"{finished_age:.1f}h ago" if finished_age is not None
                        else "none on record")
        message = (
            f"Broker resolution healthy (last complete sweep "
            f"{sweep_age:.1f}h ago, last finished run {finished_txt}, "
            f"dirty queue {dirty_depth})."
        )
    return {
        "check_key": "broker_resolution_freshness",
        "status": status,
        "value": round(sweep_age, 2) if sweep_age is not None else None,
        "details": {
            "sweep_age_hours": sweep_age,
            "stamp_age_hours": stamp_age,
            "lap_age_hours": lap_age,
            "finished_age_hours": finished_age,
            "oldest_dirty_hours": oldest_dirty,
            "dirty_depth": dirty_depth,
            "offenders": offenders,
        },
        "message": message,
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


_BROKER_SUPPRESSION_SQL = """
select
  (select count(*) from broker_merge_suppressions where lifted_at is null) as active,
  (select count(*) from broker_merge_suppressions where lifted_at is not null) as lifted,
  (select count(*) from broker_merge_suppressions s
     join broker_identities lo on lo.id = s.identity_lo
     join broker_identities hi on hi.id = s.identity_hi
    where s.lifted_at is null and lo.broker_id is not null
      and lo.broker_id = hi.broker_id) as violations
"""


def check_broker_merge_suppression(
    conn: Any, thresholds: dict[str, Any],
) -> dict[str, Any]:
    """Assert the one invariant the suppression rail exists to hold: two identities
    the operator separated (unmerge) or refused (dismiss) never end up under one
    broker again while the suppression is active. The nightly sweep re-derives its
    whole candidate set from broker_identity_contacts, so before the rail an undone
    merge simply came back the next night; a violation here means it was bypassed —
    a lift that should have been recorded, a merge path that skips the rail, or the
    apply-time backstop failing. An explicit operator merge LIFTS the suppression,
    so a legitimate override never shows up as one."""
    row = _fetchone(conn, _BROKER_SUPPRESSION_SQL)
    active, lifted, violations = (
        (0, 0, 0) if row is None
        else (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)))
    fail_at = int(thresholds["broker_suppression_violations_fail"])
    status = "fail" if violations >= fail_at else "ok"
    message = (
        f"{violations} active broker merge suppression(s) are co-located under one "
        "broker — an operator NO was bypassed; check broker_merge_suppressions "
        "against broker_identities.broker_id and the sweep's suppressed_pairs count."
        if status == "fail"
        else f"Broker merge suppressions holding ({active} active, {lifted} lifted)."
    )
    return {
        "check_key": "broker_merge_suppression",
        "status": status,
        "value": violations,
        "details": {"active_suppressions": active, "lifted": lifted,
                    "violations": violations},
        "message": message,
    }


_PLAUSIBILITY_COLS = (
    "source", "category_main", "category_type", "price_per_m2_basis", "n_active",
    "median_area_m2", "median_usable_area", "median_price_per_m2",
    "n_floor_eligible", "floor_null_share", "n_floor_eligible_7d", "floor_null_share_7d",
    "n_area_pairs", "area_divergence_share", "n_area_pairs_7d", "area_divergence_share_7d",
    "n_area_valued", "n_ppm2_valued", "n_active_7d",
    "measure_input_gap_share", "measure_input_gap_share_7d",
)

_MEASURE_PLAUSIBILITY_SQL = f"""
select {', '.join(_PLAUSIBILITY_COLS)}
from measure_plausibility_by_source
order by source, category_main, category_type
"""

# One read per run, shared by all four measure checks. The view is a ~12 s
# sequential scan of 386k active listings with an external merge sort; reading it
# four times would quadruple that for four identical answers. Cleared at the top of
# run_checks so a long-lived process (or a test) never serves a stale corpus.
_PLAUSIBILITY_CACHE: dict[str, Any] = {}


def _plausibility_cells(conn: Any) -> tuple[list[dict[str, Any]], str | None]:
    """(cells, unavailable_reason). An unreadable view is reported, never raised:
    between merging this PR and applying migration 427 the relation does not exist
    yet, and three red tiles reading `relation does not exist` would be indistinguishable
    from three real defects — the db_saturation precedent."""
    if "cells" not in _PLAUSIBILITY_CACHE:
        try:
            rows = _fetchall(conn, _MEASURE_PLAUSIBILITY_SQL)
        except Exception as exc:  # noqa: BLE001 — view missing / not granted → warn, never false-fail
            _PLAUSIBILITY_CACHE["cells"], _PLAUSIBILITY_CACHE["error"] = [], str(exc)
        else:
            _PLAUSIBILITY_CACHE["cells"] = [
                {k: (float(v) if isinstance(v, (int, Decimal)) and not isinstance(v, bool) else v)
                 for k, v in zip(_PLAUSIBILITY_COLS, row)}
                for row in rows
            ]
            _PLAUSIBILITY_CACHE["error"] = None
    return _PLAUSIBILITY_CACHE["cells"], _PLAUSIBILITY_CACHE["error"]


def _inert_measure_check(check_key: str, reason: str | None) -> dict[str, Any]:
    """warn, not ok: a check that cannot read its input has not certified anything."""
    detail = (
        f"measure_plausibility_by_source is unreadable: {reason}"
        if reason else
        "measure_plausibility_by_source returned no rows (no active listings, or the "
        "view's is_platform_admin() gate is failing for this job's role — it needs a "
        "rolbypassrls role with no JWT claims)"
    )
    message = (
        f"Per-m2 plausibility check '{check_key}' is INERT — {detail}. If migration 427 "
        "has not been applied yet, apply it."
    )
    return {"check_key": check_key, "status": "warn", "value": None,
            "details": {"skipped": detail}, "message": message}


def _unmeasured_check(check_key: str, cells: int, needed: str) -> dict[str, Any]:
    """The other half of `_inert_measure_check`: the view answered, with rows, and not
    one of them could be scored. `_inert_measure_check` only fires on ZERO ROWS, which
    is the rarer accident — the live shape is rows whose measurable content is empty
    (sreality publishes 27k active `pozemek` rows with `area_m2` NULL on every one, so
    those cells carry no floor-eligible rows, no area pairs and no medians). Every arm
    skips them and `worst` never leaves its initial value, so the check would report
    `ok` with a message asserting a fact it never checked. A check that scored nothing
    has certified nothing; `value` is None so the tile renders an em-dash rather than a
    0 that reads as a measurement."""
    detail = (
        f"{cells} cell(s) read but not one has {needed} — nothing was verified"
    )
    return {
        "check_key": check_key, "status": "warn", "value": None,
        "details": {"skipped": detail, "cells_read": cells, "arms_scored": 0},
        "message": (
            f"Per-m2 plausibility check '{check_key}' verified NOTHING — {detail}. "
            "This is a coverage failure, not a clean bill of health: check that the "
            "measure's inputs are still being written."
        ),
    }


def check_ppm2_basis_floor_share(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Watch the share of measurable rows the per-basis floor NULLs, per portal and
    basis. This is the detector for the unit-price masquerade: a portal that writes a
    per-m2 UNIT price into `price_czk` (136 Kc for a commercial rental) produces a
    perfectly non-NULL, perfectly typed number that every presence-based health surface
    calls healthy, and that only the floor inside `measure_price_per_m2` rejects. The
    denominator is rows that have a price, a positive area AND a decidable basis, so a
    portal that stops publishing prices is billed to `ppm2_measure_coverage` — the
    coverage arm — rather than accused of bad prices here."""
    cells, unavailable = _plausibility_cells(conn)
    if not cells:
        return _inert_measure_check("ppm2_basis_floor_share", unavailable)
    warn = float(thresholds["ppm2_basis_floor_share_warn"])
    fail = float(thresholds["ppm2_basis_floor_share_fail"])
    min_rows = int(thresholds["ppm2_basis_floor_min_rows"])
    status, offenders, worst, scored = _status_for_share(
        cells,
        [("of priced rows", "floor_null_share", "n_floor_eligible", warn, fail),
         ("of rows first seen in 7d", "floor_null_share_7d", "n_floor_eligible_7d",
          warn, fail)],
        min_rows=min_rows,
    )
    if not scored:
        return _unmeasured_check(
            "ppm2_basis_floor_share", len(cells),
            f"{min_rows}+ rows carrying a price, a positive area and a decidable basis")
    message = (
        f"{len(offenders)} portal/basis cell(s) lose too much of their price to the "
        f"per-basis floor (worst {worst:.1%}): " + "; ".join(offenders[:6])
        + " — a per-m2 unit price is being written into price_czk; check that portal's "
        "_parse_price against scraper/price_text.is_per_area_price."
        if offenders
        else f"Per-basis price floor healthy (worst {worst:.1%} of priced rows NULLed "
             f"across {scored} scored portal/basis arm(s))."
    )
    return {
        "check_key": "ppm2_basis_floor_share",
        "status": status,
        "value": round(worst * 100, 2),
        "details": {"worst_share": round(worst, 4), "warn": warn, "fail": fail,
                    "min_rows": min_rows, "offenders": offenders,
                    "cells_read": len(cells), "arms_scored": scored},
        "message": message,
    }


def check_area_vs_usable_divergence(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Watch `area_m2` against `usable_area` on rows that carry both. This is the direct
    detector for the mmreality defect: a portal writing the PLOT area into the headline
    floor-area column, which no presence check can see because the column is populated on
    every row. Measured only over rows carrying both areas (a portal that publishes one
    field is not divergent, it is silent) and only above the view's 10% material band
    (rounding and balcony conventions are not a basis error). `pozemek` is skipped by
    name: under Option A `area_m2` IS the plot for land, so divergence there is the
    correct answer, not a defect."""
    cells, unavailable = _plausibility_cells(conn)
    if not cells:
        return _inert_measure_check("area_vs_usable_divergence", unavailable)
    warn = float(thresholds["area_divergence_share_warn"])
    fail = float(thresholds["area_divergence_share_fail"])
    min_rows = int(thresholds["area_divergence_min_rows"])
    status, offenders, worst, scored = _status_for_share(
        cells,
        [("of both-area rows", "area_divergence_share", "n_area_pairs", warn, fail),
         ("of both-area rows first seen in 7d", "area_divergence_share_7d",
          "n_area_pairs_7d", warn, fail)],
        min_rows=min_rows,
        skip_category_main=frozenset({"pozemek"}),
    )
    if not scored:
        return _unmeasured_check(
            "area_vs_usable_divergence", len(cells),
            f"{min_rows}+ non-land rows carrying BOTH area_m2 and usable_area")
    message = (
        f"{len(offenders)} portal/category cell(s) disagree between area_m2 and usable_area "
        f"(worst {worst:.1%}): " + "; ".join(offenders[:6])
        + " — area_m2 is carrying a different physical area than the headline one; check "
        "that parser against scraper/area.derive_headline_area."
        if offenders
        else f"area_m2 agrees with usable_area (worst {worst:.1%} of both-area rows "
             f"across {scored} scored portal/category arm(s))."
    )
    return {
        "check_key": "area_vs_usable_divergence",
        "status": status,
        "value": round(worst * 100, 2),
        "details": {"worst_share": round(worst, 4), "warn": warn, "fail": fail,
                    "min_rows": min_rows, "offenders": offenders,
                    "cells_read": len(cells), "arms_scored": scored,
                    "skipped_category_main": ["pozemek"]},
        "message": message,
    }


def check_ppm2_measure_coverage(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Watch the share of active rows the measure has NO INPUT for, per portal and basis.

    The other three axes are ratios over rows that HAVE the inputs, which makes every one
    of them blind in the same direction: a cell with nothing to measure scores no arm and
    is skipped, and a skipped arm is indistinguishable from a clean one. This is the arm
    that looks at the denominator. Live, `sreality` carries 27 174 active `pozemek` rows
    with `area_m2` NULL on all of them (the plot size is in `estate_area`), so four cells
    covering ~7% of the active corpus produce no per-m2 measure at all while the other
    three axes call them healthy — and `data_quality_by_source` cannot see it either,
    grouping by (source, field) with no category grain (sreality `area_m2` reads 71.7%
    populated overall).

    Rows the basis FLOOR rejected are not counted here — they have their inputs and
    `ppm2_basis_floor_share` already indicts them; billing one portal twice for one
    defect is how an operator learns to dismiss both tiles. Cells whose basis is
    undecidable are skipped entirely: there the absent measure is the specified answer.

    Severity splits by arm, not by size. A standing gap ambers — it is real, it is named,
    and nothing shipped today caused it. The same gap among the rows that arrived THIS
    WEEK is a parser regression in flight and fails: that is the case where the old
    behaviour was actively perverse, since a portal that stopped writing `area_m2` would
    have made the divergence and floor axes go QUIET."""
    cells, unavailable = _plausibility_cells(conn)
    if not cells:
        return _inert_measure_check("ppm2_measure_coverage", unavailable)
    warn = float(thresholds["ppm2_coverage_gap_warn"])
    fail_7d = float(thresholds["ppm2_coverage_gap_fail_7d"])
    min_rows = int(thresholds["ppm2_coverage_min_rows"])
    status, offenders, worst, scored = _status_for_share(
        cells,
        [("of active rows", "measure_input_gap_share", "n_active", warn, None),
         ("of rows first seen in 7d", "measure_input_gap_share_7d", "n_active_7d",
          fail_7d, fail_7d)],
        min_rows=min_rows,
    )
    if not scored:
        return _unmeasured_check(
            "ppm2_measure_coverage", len(cells),
            f"{min_rows}+ active rows under a decidable basis")
    message = (
        f"{len(offenders)} portal/basis cell(s) have no per-m2 measure to speak of "
        f"(worst {worst:.1%} of rows with no price or no area): " + "; ".join(offenders[:6])
        + " — the measure is undefined for this cohort; check that the portal writes "
        "area_m2 and price_czk for it (land plot size lives in estate_area, which the "
        "measure does not read)."
        if offenders
        else f"The per-m2 measure resolves across the corpus (worst cell {worst:.1%} of "
             f"rows without inputs, across {scored} scored arm(s))."
    )
    return {
        "check_key": "ppm2_measure_coverage",
        "status": status,
        "value": round(worst * 100, 2),
        "details": {"worst_share": round(worst, 4), "warn": warn, "fail_7d": fail_7d,
                    "min_rows": min_rows, "offenders": offenders,
                    "cells_read": len(cells), "arms_scored": scored},
        "message": message,
    }


# The baseline is this check's OWN result row from 6-14 days ago — no new table, no new
# DDL. verify_pipeline.yml runs every 6h, so the window holds ~32 candidates and a few
# missed runs cost nothing. Rows whose `details` carries no `cells` object (an errored run
# writes {"error": ...}) are skipped rather than read as an empty baseline. `history_days`
# separates "deployed three days ago" from "has recorded no usable baseline in two weeks",
# which must not both read as green.
_PPM2_BASELINE_SQL = """
select
  (select r.details -> 'cells'
     from pipeline_check_results r
    where r.check_key = 'ppm2_median_shift'
      and r.run_at < now() - interval '6 days'
      and r.run_at > now() - interval '14 days'
      and jsonb_typeof(r.details -> 'cells') = 'object'
    order by r.run_at desc
    limit 1) as baseline_cells,
  (select extract(epoch from (now() - min(r.run_at))) / 86400.0
     from pipeline_check_results r
    where r.check_key = 'ppm2_median_shift') as history_days
"""


def check_ppm2_median_shift(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Watch each (source, category_main, category_type) cell's own median area and median
    Kc/m2 week over week. The two direct detectors above catch the two defect shapes we
    have already seen; this is the general net for the shape we have not — any change that
    moves a portal's headline number without the market moving. It cannot see a defect that
    predates its own baseline, which is exactly why it is not the mmreality detector, and it
    WILL fire when the W2 backfill heals mmreality (6.96x on area) — an expected move, and
    the proof the axis is live."""
    cells, unavailable = _plausibility_cells(conn)
    if not cells:
        return _inert_measure_check("ppm2_median_shift", unavailable)
    min_rows = int(thresholds["ppm2_median_shift_min_rows"])
    row = _fetchone(conn, _PPM2_BASELINE_SQL)
    baseline = row[0] if row and isinstance(row[0], dict) else None
    history_days = float(row[1]) if row and row[1] is not None else None
    status, offenders, worst, compared = _status_for_median_shift(
        cells, baseline, history_days, thresholds)
    # Written for the NEXT run to compare against. `n_active >= min_rows` is only a size
    # prefilter (support can never exceed it) — the two SUPPORT counts travel with the
    # medians so next week's run can gate each median on the rows that actually carry it
    # in BOTH weeks. Keeps `details` a few KB.
    snapshot = {
        _cell_key(c): {
            "n": int(c["n_active"]),
            "area": round(c["median_area_m2"], 2) if c["median_area_m2"] else None,
            "ppm2": round(c["median_price_per_m2"], 2) if c["median_price_per_m2"] else None,
            "n_area": int(c["n_area_valued"] or 0),
            "n_ppm2": int(c["n_ppm2_valued"] or 0),
        }
        for c in cells if (c["n_active"] or 0) >= min_rows
    }
    if offenders:
        message = (
            f"{len(offenders)} per-m2 median(s) moved week-over-week beyond the plausible "
            f"band (worst {worst:.2f}x): " + "; ".join(offenders[:6])
            + " — either a parser changed what it writes, or a backfill just ran; confirm "
            "which before dismissing."
        )
    elif baseline is None:
        message = (
            "Per-m2 medians recorded; no week-old baseline to compare against yet (the "
            "first comparison lands one week after this check's first run)."
        )
    elif not compared:
        # A baseline exists and not one median could be matched against it. Reporting
        # "stable" here would be the loudest lie the axis can tell: it is exactly what a
        # regression that NULLs area_m2 or price_czk platform-wide looks like.
        status = "warn"
        message = (
            f"Per-m2 medians compared NOTHING against the {len(baseline)}-cell baseline "
            f"from a week ago: no cell has {min_rows}+ rows carrying a median in both "
            "weeks. Nothing was verified — check the measure's inputs."
        )
    else:
        message = (
            f"Per-m2 medians stable week-over-week (worst move {worst:.2f}x across "
            f"{compared} median(s) compared)."
        )
    return {
        "check_key": "ppm2_median_shift",
        "status": status,
        "value": round(worst, 3) if compared else None,
        "details": {"worst_ratio": round(worst, 3) if compared else None,
                    "offenders": offenders, "min_rows": min_rows,
                    "medians_compared": compared,
                    "baseline_cells": len(baseline or {}),
                    "history_days": round(history_days, 1) if history_days is not None else None,
                    "cells": snapshot},
        "message": message,
    }


_CHECKS: list[tuple[str, Callable[[Any, dict[str, Any]], dict[str, Any]]]] = [
    ("llm_errors", check_llm_errors),
    ("llm_liveness", check_llm_liveness),
    ("llm_burn_rate", check_llm_burn_rate),
    ("db_saturation", check_db_saturation),
    ("worker_liveness", check_worker_liveness),
    ("dual_write_parity", check_dual_write_parity),
    ("property_maintenance", check_property_maintenance),
    ("broker_resolution_freshness", check_broker_resolution_freshness),
    ("broker_merge_suppression", check_broker_merge_suppression),
    ("ppm2_median_shift", check_ppm2_median_shift),
    ("ppm2_basis_floor_share", check_ppm2_basis_floor_share),
    ("area_vs_usable_divergence", check_area_vs_usable_divergence),
    ("ppm2_measure_coverage", check_ppm2_measure_coverage),
]

# --weekly stays a valid (currently empty) lane so the scheduled invocation keeps
# working; the merge-precision sample went with the legacy decision engine.
_WEEKLY_CHECKS: list[tuple[str, Callable[[Any, dict[str, Any]], dict[str, Any]]]] = []


def run_checks(
    conn: Any, thresholds: dict[str, Any], *, weekly: bool = False,
    only: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Run every check in isolation — a raising check becomes a `fail` row carrying
    the error, so one broken check never aborts the run. `only` restricts to the named
    check keys (the hourly LLM-liveness lane runs just the two llm_* checks)."""
    results: list[dict[str, Any]] = []
    # The measure checks share one 12 s read of measure_plausibility_by_source; the
    # cache is per RUN, never across runs.
    _PLAUSIBILITY_CACHE.clear()
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
                        help="Also run the weekly-only checks.")
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
