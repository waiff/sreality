"""verify_pipeline.py — scheduled pipeline-health harness.

Computes a fixed set of pipeline-health metrics (LLM error rate + liveness + burn
rate, DB saturation, worker liveness, dual-write parity, property maintenance,
broker-resolution freshness),
writes one `pipeline_check_results` row per check, and rings the in-app bell once per
INCIDENT (toolkit.system_alerts.emit_transition_alerts): at onset, again at 6h / 24h /
72h / weekly while it stays red, and once when it recovers — not on every red run, and
not on every edge (a check that flaps back to red inside the cooldown re-enters the same
incident rather than opening a new one).

Born from the 2026-07 incident: the pipeline stalled silently for two days
(Anthropic credit exhaustion; 38k+ failed LLM calls) with no in-app signal. This
job makes that loud and durable.

Each check is isolated (one failing check writes a `fail` row with the error in
`details`, never kills the run). Thresholds live in
`app_settings.pipeline_check_thresholds` with the code defaults below as fallbacks.

Each result is persisted AND alerted the moment its check completes, under a per-check
and a whole-lane wall-clock budget (`_LANE_BUDGET_S`). The lane runs inside a job with
`timeout-minutes: 5`, and it used to compute every result before writing any — so a
timeout wrote zero rows and fired zero alerts, blinding `db_saturation` and
`worker_liveness` at exactly the moment DB saturation would make the checks slow. A
check that overruns its budget reports `warn` ("I could not measure this"), never `fail`
and never `ok`.

    python -m scripts.verify_pipeline            # compute + write + alert
    python -m scripts.verify_pipeline --dry-run  # compute + log only, no writes
    python -m scripts.verify_pipeline --weekly   # weekly-only checks + the heartbeat

Needs only SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import re
import sys
import time as _time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import requests

from location_data.refetch_cohort import SREALITY_SHAPE_CASE_SQL
from scraper import media as _media
from scraper.db import QUEUE_PRIORITY_NEW, connect
from scraper.image_storage import IMAGE_TRANSFORM_OPS, image_dimensions, with_transform
from scraper.parser import parse_images
from scraper.portal_base import _BASE_HEADERS as _PORTAL_HEADERS
from scraper.sreality_client import (
    CZ_COUNTRY_ID,
    DETAIL_URL as _SREALITY_DETAIL_URL,
    INDEX_URL as _SREALITY_INDEX_URL,
    _unwrap_estate as _unwrap_sreality_estate,
)
from toolkit.listing_identity import R2_CARRIERS as _PARITY_CARRIERS
from toolkit.system_alerts import (
    AlertPolicy,
    check_states,
    emit_transition_alerts,
    emit_weekly_heartbeat,
)

LOG = logging.getLogger("verify_pipeline")

# Code fallbacks. The pipeline_check_thresholds seed (migration 274) is merged OVER
# these in load_thresholds, so a key present here but not in the DB seed (e.g.
# llm_silence_fail_hours, added with the WS4 alerting rebuild) is served from this
# default until a future seed migration includes it.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "llm_error_rate_warn": 0.2,
    # Sized to the workload that ACTUALLY runs (W0.5). The old 4h was sized for
    # dedup vision on the always-on worker — a p99 inter-call gap of ~1 minute —
    # and that workload was deleted on 2026-08-06 with the decision engine (rule
    # 15). The only recurring producer left is bazos description enrichment
    # (`enrich_bazos.yml`); every other LLM workflow is dispatch-only or paused.
    # Its schedule reads `20 */3` but its own name still says "every 6h", and
    # under the Actions cron throttle the observed run-to-run gaps over Aug 27-30
    # were 2.4-15.0h. 13h = 2x the 6h nominal cadence plus throttle slack: above
    # every gap this check has actually fired on (the false reds were 4.2-7h+)
    # while still catching a genuinely dead pipeline within one 6h lane tick.
    # This check has no warn tier, so a too-tight number is pure false red.
    "llm_silence_fail_hours": 13.0,
    "llm_spend_24h_warn_usd": 90,
    "llm_spend_24h_fail_usd": 150,
    # The llm-cost rollup (migration 437) absorbs late arrivals by re-scanning the
    # trailing 3 hours on every tick: called_at defaults to now() = TRANSACTION START,
    # so a call whose transaction opened at 10:59:59 and committed at 11:00:05 lands in
    # the already-closed hour 10 and is repaired by the next tick. That holds ONLY while
    # no transaction stays open longer than the window. Measured 2026-08-25 the oldest
    # open transaction on this instance was 28.7 minutes — 6x inside the margin, but not
    # a comfortable order of magnitude on an instance that runs 600-second cron
    # statements. Warn at 1 hour: a third of the window, so there is time to act before
    # correctness is at stake. No fail tier — a long transaction is not itself a fault.
    "long_open_txn_warn_minutes": 60,
    "db_cron_fail_rate_fail": 0.5,
    "worker_stale_fail_minutes": 5,
    "verification_stale_hours": 24,
    # Ingestion. Measured on the QUEUE, so these are portal-size-independent: a
    # healthy drain keeps the oldest never-fetched listing at minutes whatever the
    # portal. Warn at 6h is well above the 6-hourly walk cadence (a listing found
    # right after a drain legitimately waits until the next one) and far below the
    # scale of a real fault; fail at 24h means a full day of a portal's new
    # inventory is missing from Browse, the watchdog and every estimate. Under the
    # 2026-08-17 starvation sreality reached 216h.
    "acquisition_lag_warn_hours": 6,
    "acquisition_lag_fail_hours": 24,
    # Walk coverage against the portal's own advertised total. Healthy portals sit
    # at 0.0-0.2% (measured 2026-08-27: sreality 0.00%, realitymix 0.00%, bazos
    # 0.16%). Warn at 5% is ~25x the observed noise floor. Fail at 15% is a portal
    # whose delisting rail is ALSO suppressed by the same completeness gate, so a
    # coverage hole that deep silently stops history as well as intake --
    # ceskereality sits at 9.96% today, which is the known facet-partition gap and
    # should read amber, not red, until that is fixed on its own merits.
    # How many of the newest migrations to probe. 25 spans several weeks of this
    # repo's cadence, so a drift is caught long before the window rolls past it,
    # while keeping the probe to a few hundred catalog lookups.
    # The drain lane legitimately loops eight portals at up to DRAIN_MAX_SECONDS
    # (120 s) each, so ~16 min is a normal long pass. Warn above that, fail at an
    # hour — no lane has a legitimate reason to spend an hour in one pass, and
    # the observed wedge sat for NINE HOURS.
    "worker_lane_stall_warn_seconds": 1200,
    "worker_lane_stall_fail_seconds": 3600,
    "migration_drift_window": 25,
    "walk_coverage_warn_gap": 0.05,
    "walk_coverage_fail_gap": 0.15,
    # Portal-URL contract (docs/design/portal-listing-url.md). ABSOLUTE counts of active
    # rows with no source_url, per source: the failure being watched — sreality adds a
    # sub-category code the closed codebook does not know and every new row of it
    # silently gets NULL — would never move a share against ~800k rows, but it moves a
    # count within a day. Crawler portals always emit a URL, so any NULL there is a
    # parser regression.
    "outbound_url_null_warn": 50,
    "outbound_url_null_fail": 500,
    # Weekly live conformance (scripts/verify_outbound_urls.py): a slug defect fails a
    # whole (category_main, category_sub_cb) cell together, a scattered 404 is a
    # delisting rule #3 has not caught up with — so the fail arm is CONCENTRATION.
    "outbound_url_conformance_warn_share": 0.05,
    "outbound_url_conformance_cell_min": 3,
    "outbound_url_conformance_cell_fail_share": 0.67,
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
    # Location payload-shape drift (W4's standing P6 check): share of rows FIRST SEEN in
    # the trailing window whose payload has the shape the location contracts cannot
    # read — sreality's `locality` object not post-cutover, bezrealitky's `ruianId` key
    # absent. A fresh row always gets a detail fetch within the hour, so after a
    # source-side cutover the share climbs as elapsed/window: with 48 h, one 6-hourly
    # lane tick later it reads 12.5%, above fail — the bell rings on the first tick. A
    # 7-day window would need ~1.4 days to cross the same line. The standing noise is
    # the odd truncated payload (0 of 8,280 sampled on 2026-09-08), so warn sits at 3%.
    # min_rows 30 lets bezrealitky's ~5.9k-row inventory score most windows.
    "location_payload_shape_drift_warn": 0.03,
    "location_payload_shape_drift_fail": 0.10,
    "location_payload_shape_drift_min_rows": 30,
    "location_payload_shape_drift_window_hours": 48,
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
    # Workflow-failure poller liveness, keyed on the AGE of its own high-water cursor.
    # Its cron is `*/30`, but the Actions throttle really runs it 80-256 min apart, so
    # the fail tier has to clear the worst observed gap (~4.3h) with room to spare: warn
    # at 6h is "two throttled polls missed", fail at 12h is unambiguous. The poller
    # deliberately excludes its own runs from workflow_failures, so a dead poller cannot
    # show up in the table it feeds — it just stops accumulating rows, which is exactly
    # what a quiet week looks like. The cursor is the only thing that distinguishes them.
    "workflow_poller_stale_warn_hours": 6.0,
    "workflow_poller_stale_fail_hours": 12.0,
    # Sreality image template canary. The CDN is an exact-template ALLOWLIST, not a
    # transform language, and sreality has re-cut its catalogue once before with no
    # notice: the day the deployed chain leaves the allowlist every request 400s,
    # `_classify_image_failure` parks each one terminally, and the only symptom is a
    # download lane that quietly stops storing bytes. The canary asks the live CDN,
    # daily, whether the template we ACTUALLY ship still returns a full-size frame.
    # The verdict is RELATIVE, never an absolute px floor: `res,1800,1800,1` fits inside
    # the source and never upscales, so a listing whose first photo is a 640x480 original
    # comes back at 640x480 through a perfectly healthy template (measured: ~4% of live
    # byt/prodej first photos). An absolute floor would page on ordinary data. The
    # estate's OWN declared width/height (the DETAIL payload carries them per image) says
    # what the deployed chain should return; 0.9 tolerates the CDN's rounding (a
    # 1867x1400 source measured 1800x1349 against an expected 1350) while the superseded
    # `res,749,562,3` chain — the regression the master-template work undid — lands far
    # under it on both axes, which is also how a CROP is caught. Timeout is the read half
    # of a (5, 8) connect/read pair, bounded per call in the seam; worst case across the
    # three calls is 39 s, inside the 45 s per-check budget.
    "sreality_image_template_min_ratio": 0.9,
    "sreality_image_template_timeout_s": 8.0,
    # --- alert escalation policy (W3.4) -------------------------------------
    # Not per-check thresholds: these govern toolkit.system_alerts, so EVERY check
    # inherits one escalation policy instead of each one growing its own. Scalars, one
    # key per rung, because load_thresholds drops any non-scalar from the DB merge — a
    # JSON array here would be silently ignored and the code default would win forever.
    # The rungs answer "red for six days, silent for six" (property_maintenance,
    # 2026-08-20..26); the cooldown answers its mirror image, the 114 alternating
    # onset/recovery alerts llm_errors produced for an outage that never recovered. It
    # must stay above the acute lane's hourly cadence or hourly flapping still rings.
    "alert_reescalate_1_hours": 6.0,
    "alert_reescalate_2_hours": 24.0,
    "alert_reescalate_3_hours": 72.0,
    "alert_reescalate_weekly_hours": 168.0,
    "alert_flap_cooldown_hours": 6.0,
}

# --- lane + per-check wall-clock budgets (W0.4) -----------------------------
#
# The acute lane runs under `timeout-minutes: 5`. Before W0.4 `run_checks` computed EVERY
# result before `write_results` persisted ANY of them, so a job timeout wrote zero rows and
# fired zero alerts — blinding db_saturation and worker_liveness at exactly the moment (DB
# saturation) that made the checks slow in the first place. Results are now persisted as
# each check completes, and these budgets keep one slow check from eating the lane.
#
# 120s of the job's 300s. The remaining 180s is deliberate headroom: process start, the
# threshold read, alert emission, and the checks W2/W3 will add. This wave owns the number;
# later waves spend against it.
_LANE_BUDGET_S = 120.0
# No single check may hold the lane for more than this. The slowest today is the shared
# measure_plausibility read at ~12s, so 45s is ~4x headroom over the known worst case.
_CHECK_BUDGET_S = 45.0
# Outside run_checks (ad-hoc use, tests) keep the historical 10-minute ceiling.
_DEFAULT_STATEMENT_TIMEOUT_MS = 600_000


class _LaneBudget:
    """Mutable per-run budget state, reset at the top of every run_checks."""

    def __init__(self) -> None:
        self.deadline: float | None = None
        self.statement_timeout_ms: float = _DEFAULT_STATEMENT_TIMEOUT_MS

    def start(self, budget_s: float) -> None:
        self.deadline = _time.monotonic() + budget_s

    def reset(self) -> None:
        self.deadline = None
        self.statement_timeout_ms = _DEFAULT_STATEMENT_TIMEOUT_MS

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        return self.deadline - _time.monotonic()

    def arm_for_check(self, check_budget_s: float) -> float | None:
        """Give the next check the smaller of its own budget and what the lane has
        left; returns the remaining lane seconds (None = unbudgeted)."""
        left = self.remaining()
        if left is None:
            self.statement_timeout_ms = _DEFAULT_STATEMENT_TIMEOUT_MS
            return None
        self.statement_timeout_ms = max(1000.0, min(check_budget_s, left) * 1000.0)
        return left


_LANE = _LaneBudget()


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


def _llm_live_state(
    last_err_at: Any, last_ok_at: Any, last_credit_err_at: Any,
) -> tuple[bool, bool]:
    """Derive (currently_failing, credit_live) from STATE, not recency.

    `currently_failing` is simply "the newest call is a failure": `last_ok_at <
    last_err_at`. It used to additionally require the failure to be newer than a
    90-minute window (`min_live_at`), which is wrong for a reason worth stating —
    **silence is not recovery.** A failure is superseded only by a newer SUCCESS,
    never by elapsed time. The producers here have circuit breakers (the enrichment
    loop aborts at 5 consecutive errors), so once an outage is total the traffic
    stops, the last error ages past the window, and the check reads `ok`.

    Measured: OpenAI was credit-exhausted for 11 days (63,547 error rows, zero
    successes) and `llm_errors` read `ok` for most of it, flapping `fail` -> `ok`
    within an hour on unchanged inputs and emitting 114 alerts alternating onset with
    a literal "Recovered: llm_errors is healthy again" for an outage that never
    recovered. Edge-triggered alerting turns a duty-cycle sample into a siren.
    """
    currently_failing = bool(
        last_err_at is not None
        and (last_ok_at is None or last_err_at > last_ok_at)
    )
    credit_live = bool(
        currently_failing
        and last_credit_err_at is not None
        and (last_ok_at is None or last_credit_err_at > last_ok_at)
    )
    return currently_failing, credit_live


def _status_for_llm_silence(hours: float | None, fail_hours: float) -> str:
    """Fail when the newest llm_call is older than `fail_hours` (or there are none at all)."""
    if hours is None or hours > fail_hours:
        return "fail"
    return "ok"


def _status_for_poller_staleness(
    hours: float | None, warn_hours: float, fail_hours: float,
) -> str:
    """Age of the workflow-failure poller's cursor -> status.

    `None` (no cursor row at all) is `warn`, not `fail`: it is also the legitimate
    first-run state, and a check that is red from the moment it ships teaches the
    operator to ignore every check.
    """
    if hours is None:
        return "warn"
    if hours > fail_hours:
        return "fail"
    if hours > warn_hours:
        return "warn"
    return "ok"


def _status_for_burn(spend_24h: float, warn_usd: float, fail_usd: float) -> str:
    """Credit-depletion early warning: the account has run dry repeatedly (Jul 3-10)
    because paid burn silently outpaces manual top-ups. Balance isn't queryable via
    API, so trailing-24h SPEND is the runway proxy: warn = top-up cadence risk, fail =
    runaway burn worth an email before the hard gate hits.

    Upper arms only — see _status_for_burn_lanes for the arm that catches the OPPOSITE
    failure, where spend collapses to zero because nothing is succeeding."""
    if spend_24h > fail_usd:
        return "fail"
    if spend_24h > warn_usd:
        return "warn"
    return "ok"


def _starved_lanes(per_called_for: list[dict[str, Any]]) -> list[str]:
    """`called_for` lanes that are trying and getting nothing: attempts but no
    success and no spend. Evaluated PER LANE on purpose — a 24h aggregate arm is
    defeated by a single unrelated cheap success, which is exactly what happened: one
    `summarize_region_dispositions` call held `llm_burn_rate` at $0.01 for ~24 of 30
    sampled hours while the only recurring lane was totally dead."""
    return [
        c["called_for"] for c in per_called_for
        if c.get("attempts", 0) > 0
        and c.get("successes", 0) == 0
        and (c.get("spend", 0) or 0) == 0
    ]


def _status_for_burn_lanes(
    per_called_for: list[dict[str, Any]],
    spend_24h: float,
    warn_usd: float,
    fail_usd: float,
) -> tuple[str, str, list[str]]:
    """Return (status, arm, starved lanes) for llm_burn_rate.

    Three arms on two different axes, because `value=0.0` is otherwise ambiguous —
    it is the maximally healthy number AND the signature of a total outage:
      - `starved`: some lane has attempts but zero successes and zero spend -> fail.
        `_record_failure` writes `cost_usd=0.0`, so a total outage drives spend DOWN.
      - `idle`: nothing was attempted at all -> ok. Silence is llm_liveness's axis.
      - `runaway`/`ok`: the existing upper spend arms.
    The arm is carried in `details.arm` so a red or green zero is legible in logs.
    """
    starved = _starved_lanes(per_called_for)
    if starved:
        return "fail", "starved", starved
    if not any(c.get("attempts", 0) for c in per_called_for):
        return "ok", "idle", []
    status = _status_for_burn(spend_24h, warn_usd, fail_usd)
    return status, ("runaway" if status != "ok" else "ok"), []


def _status_for_long_open_txn(oldest_minutes: float, warn_minutes: float) -> str:
    """Warn (never fail) once the oldest open transaction passes `warn_minutes`.

    A long transaction is not a fault in itself — it is the one condition under which the
    llm-cost rollup's trailing re-scan stops being self-healing, so this is an advisory
    axis, not a gate."""
    return "warn" if oldest_minutes > warn_minutes else "ok"


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


def _statement_timeout_clause() -> str:
    """The `SET LOCAL statement_timeout` value for the check currently running.

    Server-side enforcement is what gives a check a real wall-clock bound: the
    connection is autocommit and shared by every check, so a Python-side timeout
    (a thread we cannot cancel, or a signal raised mid-query) would leave the
    connection wedged for everyone after it. Postgres cancelling its own query is
    the only mechanism that hands the connection back clean.
    """
    return f"{int(_LANE.statement_timeout_ms)}ms"


def _fetchone(conn: Any, sql: str, params: Any = None) -> tuple[Any, ...] | None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"SET LOCAL statement_timeout = '{_statement_timeout_clause()}'")
        cur.execute(sql, params or ())
        return cur.fetchone()


def _fetchall(conn: Any, sql: str, params: Any = None) -> list[tuple[Any, ...]]:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"SET LOCAL statement_timeout = '{_statement_timeout_clause()}'")
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
where called_at > now() - interval '24 hours' and (error ilike %s or error ilike %s)
"""

# Liveness: is the provider failing RIGHT NOW? Compares the newest failure vs the newest
# success — a success after the last error means recovered, and nothing else does. The
# `min_live_at` staleness column this used to select was removed in W0.3: see
# _llm_live_state for why bounding "live" by elapsed time made a total outage read `ok`.
_LLM_LIVENESS_SQL = """
select
  max(called_at) filter (where error is not null) as last_err_at,
  max(called_at) filter (where error is null) as last_ok_at,
  max(called_at) filter (where error ilike %s or error ilike %s) as last_credit_err_at
from llm_calls
where called_at > now() - interval '24 hours'
"""

# Two providers, two wordings for the same outage: OpenAI's 429 says "You have no credits
# remaining"; Anthropic's says "credit balance". 2026-08-25: the OpenAI wording went
# unmatched since 08-15, so health stayed green through a real 10-day outage.
_CREDIT_ERROR_PATTERNS = ("%credit balance%", "%no credits remaining%")


def check_llm_errors(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    rows = _fetchall(conn, _LLM_ERRORS_SQL)
    credit_row = _fetchone(conn, _LLM_CREDIT_SQL, _CREDIT_ERROR_PATTERNS)
    credit_errors = int(credit_row[0]) if credit_row and credit_row[0] is not None else 0

    live = _fetchone(conn, _LLM_LIVENESS_SQL, _CREDIT_ERROR_PATTERNS)
    last_err_at, last_ok_at, last_credit_err_at = (
        (live[0], live[1], live[2]) if live else (None, None, None)
    )
    currently_failing, credit_live = _llm_live_state(
        last_err_at, last_ok_at, last_credit_err_at)

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
            "LLM calls are failing with credit-balance errors right now — the provider "
            "account is out of credit. Every paid LLM path (estimations, summaries, "
            "listing enrichment, URL parsing) is down "
            f"({credit_errors} credit errors in 24h, no successful call since)."
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
    """Total-silence guard: a stretch with ZERO llm_calls means the paid pipeline is dead —
    key unset, provider unreachable, or an outage so hard nothing is even attempted. This is
    the failure mode error-rate checks are structurally blind to (no calls → no errors → false
    green).

    **The threshold is sized to the cadence of the one recurring producer, not to a
    continuous stream** (W0.5). This check used to document itself as "p99 inter-call gap is
    ~1 min, so the 4h default never trips in normal operation" — true only while dedup vision
    ran on the always-on worker, which was deleted on 2026-08-06 with the decision engine
    (rule 15). Nothing has run continuously since. The recurring producer today is bazos
    description enrichment (`enrich_bazos.yml`, the only LLM workflow still on a schedule —
    condition scoring is paused and the rest are dispatch-only), so the healthy inter-call gap
    is a CRON PERIOD stretched by the Actions throttle, not a minute. At 4h that made the
    check a false-red generator: it fired `fail value=4.195` and reds against a perfectly
    healthy pipeline. See `llm_silence_fail_hours` in DEFAULT_THRESHOLDS for the 13h sizing.

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

# Per-lane attempts / successes / spend. NOT filtered on `cost_usd > 0` — that filter is
# precisely what made a starved lane invisible: a lane that attempts and never succeeds
# books cost_usd=0.0 on every row and simply vanishes from a spend-ranked query.
_LLM_BURN_BY_LANE_SQL = """
select called_for,
       count(*) as attempts,
       count(*) filter (where error is null) as successes,
       round(coalesce(sum(cost_usd), 0)::numeric, 2) as spend
from llm_calls
where called_at > now() - interval '24 hours'
group by called_for order by spend desc, attempts desc
"""


def check_llm_burn_rate(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Credit-runway guard on TWO axes (see _status_for_burn_lanes).

    Upper arms catch runaway burn. The starvation arm catches the opposite and far more
    common failure: `_record_failure` writes `cost_usd=0.0`, so a total provider outage
    drives 24h spend toward zero — the maximally healthy number. This check reported
    `ok value=0.0` for the entire 11-day OpenAI outage. Evaluated per `called_for` so one
    unrelated cheap success cannot mask a dead lane.
    """
    warn_usd = float(thresholds["llm_spend_24h_warn_usd"])
    fail_usd = float(thresholds["llm_spend_24h_fail_usd"])
    row = _fetchone(conn, _LLM_BURN_SQL)
    spend = float(row[0]) if row and row[0] is not None else 0.0
    per_called_for = [
        {"called_for": str(cf), "attempts": int(a), "successes": int(s), "spend": float(c)}
        for (cf, a, s, c) in _fetchall(conn, _LLM_BURN_BY_LANE_SQL)
    ]
    top = [(c["called_for"], c["spend"]) for c in per_called_for if c["spend"] > 0][:3]
    status, arm, starved = _status_for_burn_lanes(
        per_called_for, spend, warn_usd, fail_usd)
    top_str = ", ".join(f"{cf} ${s:.2f}" for cf, s in top) or "none"

    if arm == "starved":
        attempts = sum(
            c["attempts"] for c in per_called_for if c["called_for"] in starved)
        message = (
            f"LLM lanes are burning attempts and producing nothing: {', '.join(starved)} "
            f"({attempts} calls in 24h, zero successes, $0.00 spent). Spend near zero here "
            "is the SYMPTOM, not health — the provider is refusing every call (credit "
            "exhausted, key revoked, or model access lost)."
        )
    elif arm == "idle":
        message = (
            "No LLM calls attempted in 24h — nothing to bill and nothing to judge. "
            "Whether that silence is itself wrong is llm_liveness's call."
        )
    elif status == "fail":
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
        "details": {
            "spend_24h_usd": round(spend, 2), "warn_usd": warn_usd,
            "fail_usd": fail_usd, "top_spenders": dict(top),
            # Which arm decided this, so a red or green `value=0.0` is legible.
            "arm": arm, "starved_called_for": starved,
            "per_called_for": per_called_for,
        },
        "message": message,
    }


# The cursor is stored as a jsonb scalar by record_workflow_failures._write_cursor, so it
# comes back out with #>> '{}' exactly as that module's _read_cursor does.
_WORKFLOW_POLLER_CURSOR_SQL = """
select extract(epoch from (now() - (value #>> '{}')::timestamptz)) / 3600.0 as age_hours
from app_settings where key = 'workflow_failures_cursor'
"""


def check_workflow_poller_liveness(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Is the workflow-failure poller still polling?

    `workflow_failures` cannot answer this about itself. The poller excludes its own runs
    from the table it feeds (or one red poll would re-alarm the surface forever), so when
    it dies the table simply stops growing — indistinguishable from a healthy week. On
    2026-08-26 the ingest fleet was red for hours and the ops surface stayed quiet.

    The liveness signal is the age of its high-water cursor in `app_settings`, which every
    successful poll rewrites. O(1): one indexed row by primary key.
    """
    warn_hours = float(thresholds["workflow_poller_stale_warn_hours"])
    fail_hours = float(thresholds["workflow_poller_stale_fail_hours"])
    row = _fetchone(conn, _WORKFLOW_POLLER_CURSOR_SQL)
    hours = float(row[0]) if row and row[0] is not None else None
    status = _status_for_poller_staleness(hours, warn_hours, fail_hours)
    if hours is None:
        message = (
            "The workflow-failure poller has no cursor on record — it has never completed "
            "a poll, or the app_settings row was cleared. Until it does, nothing is "
            "watching for red workflow runs."
        )
    elif status == "ok":
        message = f"Workflow-failure poller last advanced its cursor {hours:.1f}h ago."
    else:
        message = (
            f"The workflow-failure poller has not advanced its cursor in {hours:.1f}h "
            f"(warn > {warn_hours:.0f}h, fail > {fail_hours:.0f}h) — monitor_workflow_failures.yml "
            "is failing, disabled, or wedged. Red workflow runs are going unrecorded right "
            "now, and the Health page's failure list is silently frozen, not empty."
        )
    return {
        "check_key": "workflow_poller_liveness",
        "status": status,
        "value": round(hours, 2) if hours is not None else None,
        "details": {
            "cursor_age_hours": round(hours, 2) if hours is not None else None,
            "warn_hours": warn_hours, "fail_hours": fail_hours,
            "cursor_present": hours is not None,
        },
        "message": message,
    }


_LONG_OPEN_TXN_SQL = """
select coalesce(max(extract(epoch from (now() - xact_start)) / 60.0), 0) as oldest_min
from pg_stat_activity
where xact_start is not null and pid <> pg_backend_pid()
"""

_LONG_OPEN_TXN_TOP_SQL = """
select coalesce(nullif(application_name, ''), backend_type) as who,
       coalesce(state, 'unknown') as state,
       round((extract(epoch from (now() - xact_start)) / 60.0)::numeric, 1) as age_min
from pg_stat_activity
where xact_start is not null and pid <> pg_backend_pid()
order by xact_start
limit 3
"""


def check_long_open_transaction(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """The one condition under which the llm-cost rollup stops self-healing.

    `llm_calls.called_at` defaults to now() = transaction START, so a call whose transaction
    opened just before an hour boundary and committed just after it lands in an
    already-closed hour. `refresh_llm_cost_rollups` absorbs that by fully recomputing the
    trailing 3 hours on every tick — never a double-count, never a drop that outlives one
    tick — but only while no transaction outlives the window. Names the oldest backends so
    the alert says WHAT to look at; the repair, if it ever fires, is one statement:
    `select refresh_llm_cost_rollups('-infinity');`"""
    warn_minutes = float(thresholds["long_open_txn_warn_minutes"])
    row = _fetchone(conn, _LONG_OPEN_TXN_SQL)
    oldest = float(row[0]) if row and row[0] is not None else 0.0
    top = [
        (str(who), str(state), float(age))
        for (who, state, age) in _fetchall(conn, _LONG_OPEN_TXN_TOP_SQL)
    ]
    status = _status_for_long_open_txn(oldest, warn_minutes)
    top_str = ", ".join(f"{who} [{state}] {age:.1f}m" for who, state, age in top) or "none"
    if status == "warn":
        message = (
            f"Oldest open transaction is {oldest:.1f}m (> {warn_minutes:.0f}m) — the "
            "llm-cost rollup's 3h trailing re-scan only absorbs late arrivals shorter than "
            "the transaction that produced them. Oldest: "
            f"{top_str}. Repair after it clears: select refresh_llm_cost_rollups('-infinity');"
        )
    else:
        message = f"Oldest open transaction {oldest:.1f}m (oldest backends: {top_str})."
    return {
        "check_key": "long_open_transaction",
        "status": status,
        "value": round(oldest, 1),
        "details": {"oldest_minutes": round(oldest, 1), "warn_minutes": warn_minutes,
                    "oldest_backends": [
                        {"who": who, "state": state, "age_minutes": age}
                        for who, state, age in top
                    ]},
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


# --- ingestion: the axis nothing watched until 2026-08-27 --------------------
#
# Sixteen scraper health checks existed and fifteen compared our data to our own
# data; all of them rendered as a dot on a page nobody is required to open. So a
# portal could ingest ZERO new listings for nine days -- sreality did, 2026-08-17
# to 08-27, while 15,064 discovered listings sat unfetched -- without a single
# signal leaving the database. These two checks are the push half.

# Deliberately keyed on the QUEUE, not on listings.first_seen_at. A "no new rows
# in N hours" check needs a historical baseline, and the outage itself erodes
# that baseline: nine days of zeros makes "zero is normal" the expected value,
# so the guard goes quiet exactly when it should scream. The oldest unclaimed
# never-fetched row has no such feedback loop -- a healthy drain keeps it at
# minutes whatever the portal's size, and a starved one grows without bound.
_ACQUISITION_LAG_SQL = """
select source,
       count(*) as waiting,
       max(extract(epoch from (now() - enqueued_at)) / 3600.0) as oldest_hours,
       percentile_cont(0.5) within group (
         order by extract(epoch from (now() - enqueued_at)) / 3600.0
       ) as median_hours
  from listing_detail_queue
 where priority = %s and claimed_at is null and given_up = false
 group by source
"""


def check_acquisition_lag(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """How long a DISCOVERED but never-fetched listing has been waiting, per portal."""
    with conn.cursor() as cur:
        cur.execute(_ACQUISITION_LAG_SQL, (QUEUE_PRIORITY_NEW,))
        rows = cur.fetchall()

    warn_h = thresholds["acquisition_lag_warn_hours"]
    fail_h = thresholds["acquisition_lag_fail_hours"]
    per_source: dict[str, Any] = {}
    offenders: list[str] = []
    status = "ok"
    worst = 0.0
    for source, waiting, oldest_hours, median_hours in rows:
        oldest = float(oldest_hours or 0.0)
        per_source[source] = {
            "waiting": int(waiting),
            "oldest_hours": round(oldest, 2),
            "median_hours": round(float(median_hours or 0.0), 2),
        }
        worst = max(worst, oldest)
        if oldest >= fail_h:
            status = "fail"
            offenders.append(f"{source} {oldest:.1f}h ({waiting} waiting)")
        elif oldest >= warn_h:
            if status != "fail":
                status = "warn"
            offenders.append(f"{source} {oldest:.1f}h ({waiting} waiting)")

    if offenders:
        message = (
            "New listings are queued but not being fetched: " + "; ".join(offenders)
            + " -- the drain is not reaching its acquisition class. Check the detail-drain "
            "runs and the realtime worker's drain lane."
        )
    else:
        message = (
            f"Acquisition healthy (oldest never-fetched listing {worst:.1f}h across "
            f"{len(per_source)} portals)."
        )
    return {
        "check_key": "acquisition_lag",
        "status": status,
        "value": round(worst, 2),
        "details": {"per_source": per_source, "offenders": offenders},
        "message": message,
    }


# The one comparison the platform makes against EXTERNAL truth: what the portal
# says it has vs what the walk collected. It was already being recorded per
# category in scrape_runs.by_category and read by nothing that can raise an alarm.
#
# `max_categories` is the portal's own recent best rather than portals.categories:
# that registry row is stale for sreality (6 pairs on record, 20 walked in code),
# so config would false-fire. Self-baselining also makes a TRUNCATED walk visible
# -- categories a budget-stopped run never reached leave no by_category entry at
# all, which otherwise makes the coverage number look BETTER by shrinking the
# population it averages over.
_WALK_COVERAGE_SQL = """
with latest as (
  select distinct on (source) source, started_at, by_category
    from scrape_runs
   where run_type = 'index' and ended_at is not null and by_category is not null
     and started_at > now() - interval '48 hours'
   order by source, started_at desc
),
best as (
  select source, max(jsonb_array_length(by_category)) as max_categories
    from scrape_runs
   where run_type = 'index' and ended_at is not null and by_category is not null
     and started_at > now() - interval '7 days'
   group by source
)
select l.source,
       jsonb_array_length(l.by_category) as categories_walked,
       b.max_categories,
       extract(epoch from (now() - l.started_at)) / 3600.0 as age_hours,
       sum((c->>'collected')::bigint) filter (
         where (c->>'sreality_result_size') is not null) as collected,
       sum((c->>'sreality_result_size')::bigint) filter (
         where (c->>'sreality_result_size') is not null) as portal_total,
       count(*) filter (where (c->>'walk_reached_end')::boolean) as nominating
  from latest l
  join best b on b.source = l.source
  left join lateral jsonb_array_elements(l.by_category) c on true
 group by l.source, l.by_category, l.started_at, b.max_categories
"""

# These portals derive their "advertised total" as len(seen) -- the number they
# just collected -- so their gap is 0% BY CONSTRUCTION and proves nothing.
# Reporting them as 100% covered would be the worst kind of green: a number that
# cannot be wrong is not a measurement. (mmreality used to sit here too; since the
# 2026-09 per-type split every mmreality category declares its own count, so it is
# measured like the rest and only falls to `verifiable: false` if a walk records
# no total at all.)
_SELF_CERTIFYING_TOTALS = frozenset({"remax", "maxima"})


def check_walk_coverage(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Collected vs portal-advertised inventory on the most recent index walk.

    Since 2026-09-08 the count no longer gates nomination (rule #3), so this is the
    standing rail on it. `categories_nominating` is reported beside the gap — the
    count of categories whose walk reached the portal's end — but never changes the
    status: a walk can legitimately reach the end and still be short."""
    with conn.cursor() as cur:
        cur.execute(_WALK_COVERAGE_SQL)
        rows = cur.fetchall()

    warn_gap = thresholds["walk_coverage_warn_gap"]
    fail_gap = thresholds["walk_coverage_fail_gap"]
    per_source: dict[str, Any] = {}
    offenders: list[str] = []
    unverified: list[str] = []
    status = "ok"
    worst = 0.0
    for source, walked, max_cats, age_hours, collected, portal_total, nominating in rows:
        entry: dict[str, Any] = {
            "categories_walked": int(walked),
            "categories_nominating": int(nominating or 0),
            "categories_best_7d": int(max_cats or 0),
            "age_hours": round(float(age_hours or 0.0), 2),
            "collected": int(collected or 0),
            "portal_total": int(portal_total) if portal_total is not None else None,
        }
        if int(walked) < int(max_cats or 0):
            entry["truncated"] = True
            offenders.append(
                f"{source} walked {walked}/{max_cats} categories (truncated run)"
            )
            status = "fail" if status == "fail" else "warn"
        if source in _SELF_CERTIFYING_TOTALS or not portal_total:
            entry["gap_pct"] = None
            entry["verifiable"] = False
            unverified.append(source)
        else:
            gap = max(0.0, (float(portal_total) - float(collected or 0)) / float(portal_total))
            entry["gap_pct"] = round(gap * 100, 2)
            entry["verifiable"] = True
            worst = max(worst, gap)
            if gap >= fail_gap:
                status = "fail"
                offenders.append(f"{source} missing {gap * 100:.1f}% of its inventory")
            elif gap >= warn_gap:
                if status != "fail":
                    status = "warn"
                offenders.append(f"{source} missing {gap * 100:.1f}% of its inventory")
        per_source[source] = entry

    if offenders:
        message = "Index walks are under-collecting: " + "; ".join(offenders) + "."
    else:
        message = (
            f"Walk coverage healthy (worst verifiable gap {worst * 100:.1f}%; "
            f"{len(unverified)} portal(s) cannot be verified: {', '.join(sorted(unverified)) or 'none'})."
        )
    return {
        "check_key": "walk_coverage",
        "status": status,
        "value": round(worst * 100, 2),
        "details": {"per_source": per_source, "offenders": offenders,
                    "unverified": sorted(unverified)},
        "message": message,
    }


# --- migration drift: merged is not applied (2026-08-25 outage) -------------
#
# Applying a migration is a SEPARATE act from merging it, and nothing connected
# the two. Migration 438 merged at 17:12 and was applied 29 hours later; in
# between, every write on six portals violated a CHECK constraint the code
# assumed existed, and scrape_runs.errors read 0 the whole time. CI proves a
# migration REPLAYS against an empty database; it says nothing about production.
#
# This asks the live catalog directly: for each of the newest migrations, do the
# objects it declares actually exist? The ledger (supabase_migrations.
# schema_migrations) is deliberately NOT the oracle — see scripts/
# migration_objects.py for why its names cannot be matched reliably.

_MIGRATION_OBJECT_PROBE_SQL = """
select o.kind, o.ident,
  case o.kind
    when 'relation' then to_regclass(o.ident) is not null
    when 'function' then exists (
      select 1 from pg_proc p
        join pg_namespace n on n.oid = p.pronamespace
       where n.nspname = 'public'
         and p.proname = split_part(o.ident, '.', greatest(
               array_length(string_to_array(o.ident, '.'), 1), 1)))
    when 'column' then exists (
      select 1 from information_schema.columns c
       where c.table_schema = 'public'
         and c.table_name = split_part(o.ident, '.', 1)
         and c.column_name = split_part(o.ident, '.', 2))
    when 'constraint' then exists (
      select 1 from pg_constraint k
        join pg_class rel on rel.oid = k.conrelid
        join pg_namespace n on n.oid = rel.relnamespace
       where n.nspname = 'public'
         and rel.relname = split_part(o.ident, '.', 1)
         and k.conname = split_part(o.ident, '.', 2))
    when 'policy' then exists (
      select 1 from pg_policies pl
       where pl.schemaname = 'public'
         and pl.tablename = split_part(o.ident, '.', 1)
         and pl.policyname = split_part(o.ident, '.', 2))
  end as present
from unnest(%(kinds)s::text[], %(idents)s::text[]) as o(kind, ident)
"""

# to_regclass raises on a malformed identifier rather than returning NULL, so an
# ident that survived parsing but is not a plain dotted name never reaches SQL.
_SAFE_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_$]*(?:\.[a-zA-Z_][a-zA-Z0-9_$]*)?$")


def check_migration_drift(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Are the newest merged migrations actually present in this database?"""
    from scripts.migration_objects import load_migrations

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    newest = int(thresholds["migration_drift_window"])
    migrations = load_migrations(Path(root) / "migrations", newest=newest)

    probes: list[tuple[str, str]] = []
    for mig in migrations:
        for obj in mig.objects:
            if _SAFE_IDENT.match(obj.ident):
                probes.append((obj.kind, obj.ident))
    present: dict[tuple[str, str], bool] = {}
    if probes:
        with conn.cursor() as cur:
            cur.execute(_MIGRATION_OBJECT_PROBE_SQL, {
                "kinds": [k for k, _ in probes], "idents": [i for _, i in probes],
            })
            for kind, ident, is_present in cur.fetchall():
                present[(kind, ident)] = bool(is_present)

    missing_all: list[str] = []   # merged and demonstrably not applied
    partial: list[str] = []       # some objects landed, some did not
    unverifiable: list[str] = []  # declares nothing this parser can probe
    for mig in migrations:
        checkable = [o for o in mig.objects if _SAFE_IDENT.match(o.ident)]
        if not checkable:
            unverifiable.append(mig.filename)
            continue
        absent = [o for o in checkable if not present.get((o.kind, o.ident), False)]
        if len(absent) == len(checkable):
            missing_all.append(f"{mig.filename} ({checkable[0]} absent)")
        elif absent:
            partial.append(f"{mig.filename} ({len(absent)}/{len(checkable)} absent: {absent[0]})")

    if missing_all:
        status = "fail"
        message = (
            "Merged migrations are NOT applied to this database: "
            + "; ".join(missing_all)
            + " -- code that assumes this schema will fail on every write. Apply them."
        )
    elif partial:
        status = "warn"
        message = (
            "Migrations only partly present: " + "; ".join(partial)
            + " -- either a half-applied migration, or the object parser mis-read the file."
        )
    else:
        status = "ok"
        message = (
            f"All {len(migrations) - len(unverifiable)} checkable of the newest "
            f"{len(migrations)} migrations are present."
        )
    return {
        "check_key": "migration_drift",
        "status": status,
        "value": len(missing_all),
        "details": {
            "window": newest,
            "missing_entirely": missing_all,
            "partially_missing": partial,
            # Surfaced, not swallowed: these declare only grants, drops, data
            # updates or cron changes, which this probe cannot see. A guard whose
            # blind spot is invisible is worse than no guard.
            "unverifiable": unverifiable,
            "objects_probed": len(probes),
        },
        "message": message,
    }


# --- worker lanes: alive is not the same as working -------------------------
#
# `worker_liveness` already catches a DEAD worker (stale heartbeat). It cannot
# catch a live worker with a wedged lane, which is what actually happened: the
# realtime worker beat every 30 s for nine hours while its detail-drain lane
# completed ONE pass and its images lane completed 486. `_lane_loop` awaits
# run_pass() with no timeout and `_supervised` catches exceptions rather than
# hangs, so an unbounded await inside a pass is both invisible and unrecoverable
# short of a redeploy. The worker now stamps when a pass BEGINS and publishes
# `in_flight_s`; this is the check that reads it.
_WORKER_LANE_SQL = """
select h.worker, l.key as lane,
       (l.value->>'in_flight_s')::numeric as in_flight_s,
       (l.value->>'passes')::int as passes,
       (l.value->>'failed_passes')::int as failed_passes,
       (l.value->>'last_duration_s')::numeric as last_duration_s,
       extract(epoch from (now() - h.started_at)) / 60.0 as worker_uptime_min
  from worker_heartbeats h, lateral jsonb_each(h.details) l
 where h.beat_at > now() - interval '10 minutes'
   and jsonb_typeof(l.value) = 'object'
"""


def check_worker_lane_stall(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Is any always-on worker lane stuck inside a single pass?"""
    with conn.cursor() as cur:
        cur.execute(_WORKER_LANE_SQL)
        rows = cur.fetchall()

    warn_s = float(thresholds["worker_lane_stall_warn_seconds"])
    fail_s = float(thresholds["worker_lane_stall_fail_seconds"])
    lanes: dict[str, Any] = {}
    offenders: list[str] = []
    status = "ok"
    worst = 0.0
    for worker, lane, in_flight, passes, failed, last_duration, uptime in rows:
        elapsed = float(in_flight) if in_flight is not None else None
        lanes[f"{worker}.{lane}"] = {
            "in_flight_s": elapsed,
            "passes": int(passes or 0),
            "failed_passes": int(failed or 0),
            "last_duration_s": float(last_duration) if last_duration is not None else None,
            "worker_uptime_min": round(float(uptime or 0), 1),
        }
        if elapsed is None:
            continue
        worst = max(worst, elapsed)
        if elapsed >= fail_s:
            status = "fail"
            offenders.append(f"{worker}.{lane} stuck {elapsed / 60:.0f}min")
        elif elapsed >= warn_s:
            if status != "fail":
                status = "warn"
            offenders.append(f"{worker}.{lane} running {elapsed / 60:.0f}min")

    if not rows:
        # No beating worker at all is worker_liveness's job, not this check's.
        # Saying "ok" here would be a lie; saying "fail" would double-alarm.
        return {
            "check_key": "worker_lane_stall", "status": "warn", "value": None,
            "details": {"lanes": {}},
            "message": "No worker heartbeat in the last 10 minutes — see worker_liveness.",
        }
    if offenders:
        message = (
            "Worker lane(s) stuck inside a single pass: " + "; ".join(offenders)
            + " -- run_pass() has no timeout, so this will not clear without a redeploy."
        )
    else:
        message = f"All {len(lanes)} worker lane(s) idle or mid-pass within budget."
    return {
        "check_key": "worker_lane_stall",
        "status": status,
        "value": round(worst, 1),
        "details": {"lanes": lanes, "offenders": offenders},
        "message": message,
    }


# --- a refused delisting sweep is an operator decision, not a log line -------
_DELIST_REFUSAL_SQL = """
select source, category_main, category_type, candidates, active_rows, cap, refused_at
  from delist_flip_refusals
 where refused_at > now() - interval '7 days'
 order by refused_at desc
 limit 20
"""


def check_delist_flip_refused(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Did a walk nominate far more of a category for a page check than the
    per-walk throttle lets through?

    Since 2026-09-07 (rule #3) a row in `delist_flip_refusals` means DEFERRED,
    not refused: the walk nominated more unseen rows than `delist_flip_cap`
    allows per walk, the oldest share was queued for a page check and the rest
    waits for the next walk. That is routine while a backlog drains. What is
    NOT routine is a walk that saw less than half of a category it called
    complete -- that is the signature of a broken walk (a retired slug, a
    throttled portal), so it warns. The operator's move is to check the walk,
    not to raise anything: the drain is already fetching the pages.
    """
    with conn.cursor() as cur:
        cur.execute(_DELIST_REFUSAL_SQL)
        rows = cur.fetchall()
    if not rows:
        return {
            "check_key": "delist_flip_refused", "status": "ok", "value": 0,
            "details": {"deferrals": []},
            "message": "No walk nominated more than the per-walk throttle allows in 7 days.",
        }
    deferrals = [
        {"source": s, "category_main": cm, "category_type": ct,
         "candidates": int(cand), "active_rows": int(act), "cap": int(cap),
         "deferred_at": ra.isoformat() if hasattr(ra, "isoformat") else str(ra)}
        for s, cm, ct, cand, act, cap, ra in rows
    ]
    suspicious = [d for d in deferrals if d["active_rows"] and d["candidates"] > d["active_rows"] / 2]
    worst = max(d["candidates"] for d in deferrals)
    named = "; ".join(
        f"{d['source']} {d['category_main'] or '*'}/{d['category_type']} "
        f"{d['candidates']} of {d['active_rows']} (per-walk {d['cap']})"
        for d in (suspicious or deferrals)[:3]
    )
    if suspicious:
        return {
            "check_key": "delist_flip_refused", "status": "warn", "value": worst,
            "details": {"deferrals": deferrals},
            "message": (
                f"{len(suspicious)} walk(s) called complete while more than half of "
                f"the category was unseen: {named}. Check the walk (a retired slug, a "
                "throttled portal) -- nothing is deleted without a page check, but a "
                "walk this short is not one to trust."
            ),
        }
    return {
        "check_key": "delist_flip_refused", "status": "ok", "value": worst,
        "details": {"deferrals": deferrals},
        "message": (
            f"{len(deferrals)} nomination(s) throttled to the per-walk share, the rest "
            f"deferred to the next walk: {named}. Routine while a backlog drains."
        ),
    }


_LOCATION_PAYLOAD_SHAPE_DRIFT_SQL = f"""
    SELECT source, count(*) AS n, count(*) FILTER (WHERE unexpected) AS unexpected
    FROM (
      SELECT source,
             CASE WHEN source = 'sreality'
                  THEN ({SREALITY_SHAPE_CASE_SQL}) <> 'post_cutover'
                  ELSE NOT (raw_json ? 'ruianId')
             END AS unexpected
      FROM listings
      WHERE source IN ('sreality', 'bezrealitky') AND is_active
        AND first_seen_at > now() - make_interval(hours => %(window_hours)s)
    ) fresh
    GROUP BY source
"""

_LOCATION_SHAPE_REMEDY = {
    "sreality": ("the v1 API changed the shape of `locality`; W1 intake is enrolling every "
                 "new row into the refetch cohort, where a refetch cannot fix a NEW shape — "
                 "update claims_intake.sreality_payload_shape + contracts/portals/sreality.yaml's "
                 "payload_schema_detector and re-extract"),
    "bezrealitky": ("new rows arrive without the `ruianId` key; the GraphQL detail query lost "
                    "the field W0 item 0m added — restore it in "
                    "scraper/bezrealitky_client._DETAIL_QUERY (adding it back has no "
                    "retroactive effect: the rows fetched meanwhile need a refetch)"),
}


def check_location_payload_shape_drift(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """W4's standing payload-schema-version check (06 §6.4 W4 gate, P6): the next
    source-side shape change must force re-extraction instead of silently degrading.
    Today a sreality payload of unknown shape classifies `absent`, is routed to the
    refetch cohort and burns five fetches before retiring as `error` — nobody is told.
    This check tells someone. Measured on rows first seen in the trailing window (48 h
    by default), per source: after a cutover the share climbs as elapsed/window, so the
    first 6-hourly tick already reads 12.5% and crosses fail — a 7-day window would sit
    under it for ~1.4 days."""
    warn = float(thresholds["location_payload_shape_drift_warn"])
    fail = float(thresholds["location_payload_shape_drift_fail"])
    min_rows = int(thresholds["location_payload_shape_drift_min_rows"])
    window_hours = int(thresholds["location_payload_shape_drift_window_hours"])
    rows = _fetchall(conn, _LOCATION_PAYLOAD_SHAPE_DRIFT_SQL, {"window_hours": window_hours})
    cells = [{"source": s, "n": int(n), "unexpected": int(u),
              "share": (int(u) / int(n)) if int(n) else None}
             for s, n, u in rows]
    scored = [c for c in cells if c["n"] >= min_rows]
    if not scored:
        detail = (f"{len(cells)} source(s) read, none with {min_rows}+ rows first seen in "
                  f"{window_hours} h — nothing was verified")
        return {"check_key": "location_payload_shape_drift", "status": "warn", "value": None,
                "details": {"skipped": detail, "cells": cells, "arms_scored": 0},
                "message": f"Location payload-shape drift verified NOTHING — {detail}."}
    worst = max(c["share"] for c in scored)
    status = "fail" if worst >= fail else "warn" if worst >= warn else "ok"
    offenders = [f"{c['source']}: {c['share']:.1%} of {c['n']} rows first seen in "
                 f"{window_hours}h — {_LOCATION_SHAPE_REMEDY[c['source']]}"
                 for c in scored if c["share"] >= warn]
    message = (
        f"{len(offenders)} source(s) are landing payloads the location contracts cannot "
        f"read (worst {worst:.1%}): " + "; ".join(offenders)
        if offenders
        else f"Location payload shapes stable (worst {worst:.1%} unexpected across "
             f"{len(scored)} scored source(s))."
    )
    return {
        "check_key": "location_payload_shape_drift",
        "status": status,
        "value": round(worst * 100, 2),
        "details": {"worst_share": round(worst, 4), "warn": warn, "fail": fail,
                    "min_rows": min_rows, "window_hours": window_hours, "cells": cells,
                    "arms_scored": len(scored), "offenders": offenders},
        "message": message,
    }


# The location programme's one invariant (CLAUDE.md rule 25, 2026-09-11): every active
# listing has a projection row, and every active listing that is not foreign has a town
# (`obec_kod`). Per portal, absolute counts, red when either is not zero — this is the line
# the S2 contract rewrites drive to zero portal by portal and the guard that keeps it there.
# `country_status <> 'foreign'` deliberately counts `undetermined` and `disputed` as Czech:
# foreign is a determination the resolver makes, never a default for "no town found".
_LOCATION_TOWN_COVERAGE_SQL = """
    SELECT l.source,
           count(*)                                                   AS active_n,
           count(*) FILTER (WHERE p.listing_id IS NULL)                AS no_row_n,
           count(*) FILTER (WHERE p.listing_id IS NOT NULL
                              AND p.country_status <> 'foreign'
                              AND p.obec_kod IS NULL)                  AS cz_no_town_n,
           count(*) FILTER (WHERE p.obec_kod IS NOT NULL)              AS town_n
      FROM listings l
      LEFT JOIN listing_location_current p ON p.listing_id = l.id
     WHERE l.is_active
     GROUP BY l.source
     ORDER BY l.source
"""


def check_location_town_coverage(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Red when any active listing has no projection row, or any active non-foreign listing
    has no town. Absolute counts, no threshold: the invariant is zero, and a number that is
    not zero names the portal whose contract has to change."""
    rows = _fetchall(conn, _LOCATION_TOWN_COVERAGE_SQL)
    cells = [{"source": s, "active": int(a), "no_row": int(nr), "cz_no_town": int(nt),
              "town": int(t), "town_share": (int(t) / int(a)) if int(a) else None}
             for s, a, nr, nt, t in rows]
    no_row = sum(c["no_row"] for c in cells)
    cz_no_town = sum(c["cz_no_town"] for c in cells)
    active = sum(c["active"] for c in cells)
    if not cells:
        return {"check_key": "location_town_coverage", "status": "warn", "value": None,
                "details": {"skipped": "no active listings read", "cells": []},
                "message": "Location town coverage verified NOTHING — no active listings read."}
    offenders = [f"{c['source']}: {c['no_row']:,} without a row, {c['cz_no_town']:,} Czech "
                 f"without a town (of {c['active']:,})"
                 for c in cells if c["no_row"] or c["cz_no_town"]]
    missing = no_row + cz_no_town
    status = "fail" if missing else "ok"
    message = (
        f"{missing:,} of {active:,} active listings have no town "
        f"({no_row:,} without a projection row, {cz_no_town:,} Czech without obec_kod): "
        + "; ".join(offenders)
        if missing
        else f"Every one of {active:,} active listings has a projection row and every Czech one a town."
    )
    return {
        "check_key": "location_town_coverage",
        "status": status,
        "value": missing,
        "details": {"active": active, "no_row": no_row, "cz_no_town": cz_no_town,
                    "cells": cells, "offenders": offenders},
        "message": message,
    }


_OUTBOUND_URL_COVERAGE_SQL = """
    SELECT source,
           count(*)                                                       AS active_n,
           count(*) FILTER (WHERE source_url IS NULL)                     AS null_n,
           count(*) FILTER (WHERE source_url IS NULL
                              AND first_seen_at > now() - interval '7 days') AS null_7d
    FROM listings
    WHERE is_active
    GROUP BY source
    ORDER BY source
"""


def check_outbound_url_coverage(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Active rows with no page URL, per portal — an ABSOLUTE count, not a share.

    A listing's `source_url` is a stored fact for all nine portals (W0 of
    docs/design/portal-listing-url.md): the eight crawlers emit the page they fetched,
    sreality assembles its canonical from a closed codebook. The one silent failure is
    sreality adding a sub-category code the codebook does not know — every new row of it
    gets NULL, the SPA shows no chip, and a percentage over ~800k rows never notices.
    A count of active NULLs does, within a day; `null_7d` names the fresh cohort so a
    standing archive gap (rows the reconciler declined) is not mistaken for a regression.
    The remedy is one codebook entry in scraper/sreality_url.py + a reconciler run.
    """
    with conn.cursor() as cur:
        cur.execute(_OUTBOUND_URL_COVERAGE_SQL)
        rows = cur.fetchall()
    warn = int(thresholds["outbound_url_null_warn"])
    fail = int(thresholds["outbound_url_null_fail"])
    per_source = [
        {"source": src, "active_n": int(active), "null_n": int(nulls), "null_7d": int(fresh)}
        for src, active, nulls, fresh in rows
    ]
    worst = max(per_source, key=lambda r: r["null_n"], default=None)
    worst_n = worst["null_n"] if worst else 0
    status = "fail" if worst_n >= fail else "warn" if worst_n >= warn else "ok"
    offenders = [r for r in per_source if r["null_n"] >= warn]
    named = "; ".join(
        f"{r['source']} {r['null_n']} active rows without a URL ({r['null_7d']} first seen "
        f"in 7d)" for r in sorted(offenders, key=lambda r: -r["null_n"])[:4]
    )
    if offenders:
        message = (
            f"{named}. A sreality NULL means a sub-category code outside the closed "
            "codebook (scraper/sreality_url.py) — add the entry, then run "
            "reconcile_source_url; a crawler NULL is a parser regression."
        )
    else:
        message = "Every active listing on every portal carries its page URL."
    return {
        "check_key": "outbound_url_coverage",
        "status": status,
        "value": worst_n,
        "details": {"per_source": per_source, "warn": warn, "fail": fail},
        "message": message,
    }


# --- the sreality image template canary (daily probe of the DEPLOYED chain) --
#
# The one check in this harness that leaves the database. Everything else reads
# our own tables, which can only tell us what we already stored; a CDN that
# started refusing our transform stores nothing, so there is no row to read.

_SREALITY_PROBE_MAX_BYTES = 6 * 1024 * 1024

# byt / prodej / CZ, one row, newest page — the same v1 search the walk uses, at
# limit 1. Any live category would do; byt-prodej is simply the slice that is
# never empty.
_SREALITY_PROBE_PARAMS: dict[str, Any] = {
    "category_main_cb": 1,
    "category_type_cb": 1,
    "locality_country_id": CZ_COUNTRY_ID,
    "limit": 1,
    "offset": 0,
}


def _fetch_sreality_probe(
    url: str, timeout: tuple[float, float], max_bytes: int, *, accept: str = "*/*"
) -> tuple[int, bytes]:
    """The canary's ONE network seam — the whole check is hermetic behind it.

    Two independent bounds, because neither covers the other and nothing outside can
    preempt this call (the lane budget is armed only as a Postgres statement_timeout,
    and run_checks tests the lane deadline BEFORE a check starts): `max_bytes` bounds
    MEMORY if the CDN ever answers an image URL with a video, and the monotonic deadline
    bounds WALL CLOCK — requests' read timeout is per socket read, so a peer trickling
    one chunk per 7.9 s would otherwise hold the lane for minutes without ever tripping it.
    """
    connect_timeout, read_timeout = timeout
    budget_s = connect_timeout + read_timeout
    deadline = _time.monotonic() + budget_s
    headers = {**_PORTAL_HEADERS, "Accept": accept}
    with requests.get(url, timeout=timeout, headers=headers, stream=True) as response:
        buf = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            buf += chunk
            if len(buf) >= max_bytes:
                break
            if _time.monotonic() >= deadline:
                raise requests.Timeout(
                    f"probe exceeded its {budget_s:.0f}s wall-clock budget reading {url}"
                )
        return response.status_code, bytes(buf)


def _probe_skipped(detail: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """warn, never fail: the canary could not REACH sreality, so it verified nothing
    about our transform — and a flaky network must not manufacture a red that reads as
    'the CDN rejected our template'."""
    return {
        "check_key": "sreality_image_template",
        "status": "warn",
        "value": None,
        "details": {"skipped": True, "reason": detail, **(extra or {})},
        "message": (
            f"Sreality image template verified NOTHING this run — {detail}. The "
            "deployed transform is UNKNOWN, not healthy; if this persists the probe "
            "itself needs attention."
        ),
    }


def _fit_box(ops: str) -> tuple[int, int] | None:
    """The (w, h) box the deployed chain's `res` op fits a frame inside.

    Read OUT of IMAGE_TRANSFORM_OPS rather than restated beside it, so the expectation
    moves with the template the day the template moves.
    """
    for op in ops.split("|"):
        parts = op.split(",")
        if parts[0] == "res" and len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
            return int(parts[1]), int(parts[2])
    return None


def _expected_frame(source: tuple[int, int] | None, ops: str) -> tuple[int, int] | None:
    """What the CDN should serve for a source frame of this size: fit inside the `res`
    box, NEVER upscaled (verified live — a 1450x967 source comes back untouched)."""
    box = _fit_box(ops)
    if box is None or source is None:
        return None
    width, height = source
    scale = min(1.0, box[0] / width, box[1] / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _declared_source_size(estate: dict[str, Any], image_url: str) -> tuple[int, int] | None:
    """The estate's own width/height for this image — the only honest yardstick for what
    the transform should return (the CDN does not upscale a small original)."""
    for img in estate.get("advert_images") or []:
        if not isinstance(img, dict):
            continue
        url = img.get("url")
        if not isinstance(url, str) or not url:
            continue
        if ("https:" + url if url.startswith("//") else url) != image_url:
            continue
        width, height = img.get("width"), img.get("height")
        if _positive_int(width) and _positive_int(height):
            return int(width), int(height)
        return None
    return None


def check_sreality_image_template(conn: Any, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Does sreality's CDN still serve the transform template the download path ships?

    Walks the live v1 index (one row) to a FRESH listing id, fetches that listing's
    DETAIL payload — the endpoint whose `advert_images` carries the `{url, width,
    height}` dicts `parse_images` and the download path both read; the search endpoint
    sends bare URL strings the parser skips — and requests the first image through
    `image_storage.with_transform`, so the probe is the deployed chain by construction
    and cannot drift from it. Nothing stored is consulted: a stored URL proves only that
    the template worked the day we stored it.

    The failure mode that matters answers HTTP 200 with a smaller or cropped rendition,
    which no status check would ever see. The verdict is RELATIVE — the served frame
    against what the `res` op should yield for THIS estate's declared source size —
    because the CDN never upscales, so a small original is not a template fault.
    """
    min_ratio = float(thresholds["sreality_image_template_min_ratio"])
    read_timeout = float(thresholds["sreality_image_template_timeout_s"])
    timeout = (5.0, read_timeout)
    index_url = f"{_SREALITY_INDEX_URL}?{urlencode(_SREALITY_PROBE_PARAMS)}"

    try:
        status_code, body = _fetch_sreality_probe(
            index_url, timeout, _SREALITY_PROBE_MAX_BYTES, accept="application/json")
    except requests.RequestException as exc:
        return _probe_skipped(f"the sreality index is unreachable ({exc})")
    if status_code != 200:
        return _probe_skipped(f"the sreality index answered HTTP {status_code}",
                              {"http_status": status_code})
    try:
        payload = json.loads(body)
    except ValueError as exc:
        return _probe_skipped(f"the sreality index body is not JSON ({exc})")
    results = (payload or {}).get("results") or []
    if not results or not isinstance(results[0], dict):
        return _probe_skipped("the sreality index returned no estates")
    hash_id = results[0].get("hash_id")
    if not isinstance(hash_id, (int, str)) or not str(hash_id).strip():
        return _probe_skipped("the newest sreality estate carries no hash_id")

    detail_url = _SREALITY_DETAIL_URL.format(id=hash_id)
    try:
        detail_status, detail_body = _fetch_sreality_probe(
            detail_url, timeout, _SREALITY_PROBE_MAX_BYTES, accept="application/json")
    except requests.RequestException as exc:
        return _probe_skipped(f"the sreality detail endpoint is unreachable ({exc})",
                              {"detail_url": detail_url})
    if detail_status != 200:
        return _probe_skipped(f"the sreality detail endpoint answered HTTP {detail_status}",
                              {"detail_url": detail_url, "http_status": detail_status})
    try:
        detail_payload = json.loads(detail_body)
    except ValueError as exc:
        return _probe_skipped(f"the sreality detail body is not JSON ({exc})")
    if not isinstance(detail_payload, dict):
        return _probe_skipped("the sreality detail body is not an estate object")
    estate = _unwrap_sreality_estate(detail_payload)
    images = parse_images(estate)
    if not images:
        return _probe_skipped(f"sreality estate {hash_id} carries no images",
                              {"detail_url": detail_url})

    image_url = images[0]["url"]
    transform = with_transform(image_url)
    if transform == image_url:
        # with_transform is a no-op off the sreality CDN host, so the bare URL would
        # serve a normal photo and the check would report health for a template it
        # never exercised. A host migration must surface, not green.
        return _probe_skipped(
            "the probed image is not on the sreality CDN host, so our transform is a "
            "no-op on it — the template was not exercised",
            {"image_url": image_url, "detail_url": detail_url},
        )
    source = _declared_source_size(estate, image_url)
    expected = _expected_frame(source, IMAGE_TRANSFORM_OPS)
    source_width, source_height = source if source else (None, None)
    expected_width, expected_height = expected if expected else (None, None)
    started = _time.monotonic()
    try:
        cdn_status, data = _fetch_sreality_probe(
            transform, timeout, _SREALITY_PROBE_MAX_BYTES, accept="image/*")
    except requests.RequestException as exc:
        return _probe_skipped(f"the sreality CDN is unreachable ({exc})",
                              {"image_url": image_url, "transform": transform})
    # 403/429 is how sreality throttles (never a dead template) and a 5xx is
    # theirs to fix — a transient must read as "verified nothing", not as a
    # catalogue change, or one busy minute pages the operator.
    if cdn_status in (403, 429) or cdn_status >= 500:
        return _probe_skipped(f"the sreality CDN throttled or errored: HTTP {cdn_status}",
                              {"image_url": image_url, "transform": transform,
                               "http_status": cdn_status})
    elapsed_ms = int((_time.monotonic() - started) * 1000)
    size = image_dimensions(data) if _media.is_image_bytes(data) is not None else None
    width, height = size if size else (None, None)
    details: dict[str, Any] = {
        "image_url": image_url, "transform": transform, "http_status": cdn_status,
        "width": width, "height": height, "bytes": len(data), "elapsed_ms": elapsed_ms,
        "source_width": source_width, "source_height": source_height,
        "expected_width": expected_width, "expected_height": expected_height,
        "min_ratio": min_ratio, "detail_url": detail_url,
    }
    # Both axes: a CROP keeps one of them and collapses the other (the superseded
    # 749x562 chain cut a 3:2 frame to 4:3), so one-axis-only would miss it.
    downgraded = bool(
        expected_width and expected_height and width is not None and height is not None
        and (width < expected_width * min_ratio or height < expected_height * min_ratio)
    )

    if cdn_status != 200:
        message = (
            f"Sreality REFUSED our image transform: HTTP {cdn_status} for "
            f"`{IMAGE_TRANSFORM_OPS}`. Their CDN is an exact-template allowlist, so this "
            "is a catalogue change — every image download is now parking terminally. "
            "Re-derive the template from sreality's own frontend and update "
            "scraper/image_storage.IMAGE_TRANSFORM_OPS."
        )
    elif width is None:
        kind = "a non-image body" if _media.is_image_bytes(data) is None else "undecodable bytes"
        message = (
            f"Sreality answered HTTP 200 for our image transform but returned {kind} "
            f"({len(data)} bytes) — the template is off their allowlist or the URL no "
            "longer serves a photo. Check scraper/image_storage.IMAGE_TRANSFORM_OPS."
        )
    elif downgraded:
        message = (
            f"Sreality's CDN served {width}x{height} px for a "
            f"{source_width}x{source_height} px source, where `{IMAGE_TRANSFORM_OPS}` "
            f"should return {expected_width}x{expected_height} px — the template "
            "silently downgraded (the superseded chain served 749 px and cropped to "
            "4:3). Downloads are storing small, possibly cropped frames. Re-derive "
            "IMAGE_TRANSFORM_OPS."
        )
    elif expected_width:
        message = (
            f"Sreality's image template is live: {width}x{height} px for a "
            f"{source_width}x{source_height} px source, {len(data)} bytes in "
            f"{elapsed_ms} ms."
        )
    else:
        message = (
            f"Sreality's image template is live: {width}x{height} px, {len(data)} bytes "
            f"in {elapsed_ms} ms. The estate declares no source size for this image, so "
            "the frame size itself was not judged this run."
        )
    status = "ok" if cdn_status == 200 and width is not None and not downgraded else "fail"
    return {
        "check_key": "sreality_image_template",
        "status": status,
        "value": width,
        "details": details,
        "message": message,
    }


_CHECKS: list[tuple[str, Callable[[Any, dict[str, Any]], dict[str, Any]]]] = [
    ("llm_errors", check_llm_errors),
    ("llm_liveness", check_llm_liveness),
    ("llm_burn_rate", check_llm_burn_rate),
    ("long_open_transaction", check_long_open_transaction),
    ("db_saturation", check_db_saturation),
    ("worker_liveness", check_worker_liveness),
    ("dual_write_parity", check_dual_write_parity),
    ("property_maintenance", check_property_maintenance),
    ("broker_resolution_freshness", check_broker_resolution_freshness),
    ("broker_merge_suppression", check_broker_merge_suppression),
    # Registered here (6h lane) but deliberately NOT in llm_health.yml's hourly --only
    # list yet: ship, soak, then promote. Registration alone rings the in-app bell;
    # the hourly lane is what emails.
    ("workflow_poller_liveness", check_workflow_poller_liveness),
    ("ppm2_median_shift", check_ppm2_median_shift),
    ("ppm2_basis_floor_share", check_ppm2_basis_floor_share),
    ("area_vs_usable_divergence", check_area_vs_usable_divergence),
    ("ppm2_measure_coverage", check_ppm2_measure_coverage),
    ("acquisition_lag", check_acquisition_lag),
    ("walk_coverage", check_walk_coverage),
    ("migration_drift", check_migration_drift),
    ("worker_lane_stall", check_worker_lane_stall),
    ("delist_flip_refused", check_delist_flip_refused),
    # W4's standing P6 check. 6h lane + in-app bell; NOT in llm_health.yml's hourly
    # --only list yet — ship, soak, then promote (the same ladder as the ppm2 checks).
    ("location_payload_shape_drift", check_location_payload_shape_drift),
    # The location programme's coverage invariant (rule 25): absolute counts per portal,
    # red until they are zero. 6h lane + in-app bell; not in the hourly e-mail list.
    ("location_town_coverage", check_location_town_coverage),
    # Portal-URL contract: absolute count of active rows with no page URL. 6h lane +
    # in-app bell; not in the hourly --only list (ship, soak, then promote).
    ("outbound_url_coverage", check_outbound_url_coverage),
    # LAST deliberately: the only check that makes an outbound request, so if the
    # lane budget runs out it is the one that goes unrun, never a DB check. 6h lane
    # + in-app bell; not in llm_health.yml's hourly --only list (ship, soak, promote).
    ("sreality_image_template", check_sreality_image_template),
]
# (the check body sits above this registry; the SQL it runs is
# `_LOCATION_PAYLOAD_SHAPE_DRIFT_SQL`, built on the shape CASE the W4 gate shares)

# No weekly-only CHECKS today (the merge-precision sample went with the legacy decision
# engine), but the lane is no longer inert: main() emits the once-per-ISO-week health
# heartbeat under --weekly (toolkit.system_alerts.emit_weekly_heartbeat).
_WEEKLY_CHECKS: list[tuple[str, Callable[[Any, dict[str, Any]], dict[str, Any]]]] = []


def _is_timeout_error(exc: BaseException) -> bool:
    """A statement cancelled by our own per-check budget, vs a genuinely broken check."""
    name = type(exc).__name__
    if name in ("QueryCanceled", "QueryCanceledError"):
        return True
    return "canceling statement due to statement timeout" in str(exc)


def _timed_out_result(key: str, budget_s: float, exc: BaseException | None) -> dict[str, Any]:
    """A check that ran past its budget is `warn`, never `fail`.

    It reports "I could not measure this", which is a different claim from "I measured
    this and it is broken" — and a lane under DB pressure would otherwise manufacture a
    wall of false reds at the exact moment the operator needs to read the real one."""
    return {
        "check_key": key,
        "status": "warn",
        "value": None,
        "details": {"timed_out": True, "budget_seconds": round(budget_s, 1),
                    "error": str(exc) if exc is not None else None},
        "message": (
            f"Check '{key}' exceeded its {budget_s:.0f}s budget and was cancelled — its "
            "result is UNKNOWN this run, not healthy. Usually DB pressure; if it persists, "
            "the check's query needs work."
        ),
    }


def _not_run_result(key: str, budget_s: float) -> dict[str, Any]:
    return {
        "check_key": key,
        "status": "warn",
        "value": None,
        "details": {"not_run": True, "lane_budget_seconds": round(budget_s, 1)},
        "message": (
            f"Check '{key}' did not run: the verification lane exhausted its "
            f"{budget_s:.0f}s budget first. Result UNKNOWN this run."
        ),
    }


def run_checks(
    conn: Any, thresholds: dict[str, Any], *, weekly: bool = False,
    only: set[str] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    lane_budget_s: float | None = _LANE_BUDGET_S,
    check_budget_s: float = _CHECK_BUDGET_S,
) -> list[dict[str, Any]]:
    """Run every check in isolation, handing each result to `on_result` AS IT COMPLETES.

    A raising check becomes a `fail` row carrying the error, so one broken check never
    aborts the run. `only` restricts to the named check keys (the acute lane's `--only`).

    Two budgets bound the lane (W0.4). Each check gets `check_budget_s` (or whatever the
    lane has left, whichever is smaller) enforced server-side as `statement_timeout`; if
    it is cancelled it reports `warn` "timed out", not `fail`. When the lane budget is
    gone the remaining checks are not started and report `warn` "not run". Both say
    UNKNOWN rather than healthy — silence is never recovery.

    `on_result` is what makes a timeout survivable: persisting per check means a killed
    job keeps every result it had already computed instead of losing all of them.
    """
    results: list[dict[str, Any]] = []
    # The measure checks share one 12 s read of measure_plausibility_by_source; the
    # cache is per RUN, never across runs.
    _PLAUSIBILITY_CACHE.clear()
    checks = list(_CHECKS) + (list(_WEEKLY_CHECKS) if weekly else [])
    if only:
        checks = [(k, fn) for (k, fn) in checks if k in only]

    _LANE.reset()
    if lane_budget_s is not None:
        _LANE.start(lane_budget_s)
    try:
        for key, fn in checks:
            left = _LANE.arm_for_check(check_budget_s)
            if left is not None and left <= 0:
                LOG.error(
                    "lane budget %.0fs exhausted; check %s not run", lane_budget_s, key)
                result = _not_run_result(key, lane_budget_s or 0.0)
            else:
                started = _time.monotonic()
                try:
                    result = fn(conn, thresholds)
                except Exception as exc:  # noqa: BLE001
                    elapsed = _time.monotonic() - started
                    if _is_timeout_error(exc):
                        LOG.error("check %s timed out after %.1fs", key, elapsed)
                        result = _timed_out_result(key, elapsed, exc)
                    else:
                        LOG.exception("check %s errored", key)
                        result = {
                            "check_key": key,
                            "status": "fail",
                            "value": None,
                            "details": {"error": str(exc)},
                            "message": f"Pipeline verification check '{key}' errored: {exc}",
                        }
            results.append(result)
            if on_result is not None:
                try:
                    on_result(result)
                except Exception:  # noqa: BLE001 - persisting one result must not end the lane
                    LOG.exception("could not persist result for check %s", key)
    finally:
        _LANE.reset()
    return results


def insert_result(conn: Any, result: dict[str, Any], run_at: _dt.datetime) -> None:
    """Persist ONE check result. Called as each check completes (W0.4)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pipeline_check_results (run_at, check_key, status, value, details) "
            "VALUES (%s, %s, %s, %s, %s::jsonb)",
            (run_at, result["check_key"], result["status"],
             result.get("value"), json.dumps(result.get("details") or {})),
        )


def write_results(
    conn: Any, results: list[dict[str, Any]], run_at: _dt.datetime,
    *, policy: AlertPolicy | None = None,
) -> dict[str, int]:
    """Persist one row per check, then ring the bell per INCIDENT (onset / re-escalation
    / recovery), not on every red run. Returns {onset, recovery, reescalation} counts.

    The stored history is read BEFORE this run's rows are inserted, so the baseline is
    the prior run — see toolkit.system_alerts.emit_transition_alerts.

    main() no longer takes this path (it persists and alerts per check so a job timeout
    keeps what it computed); kept for callers that already hold a full result list."""
    pol = policy or AlertPolicy()
    states = check_states(conn, policy=pol)
    prev = {k: s.status for k, s in states.items() if s.status is not None}
    for r in results:
        insert_result(conn, r, run_at)
    return emit_transition_alerts(
        conn, results, prev, run_at, states=states, policy=pol)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute + log, write nothing (no result rows, no alerts).")
    parser.add_argument("--weekly", action="store_true",
                        help="Also run the weekly-only checks and emit the once-per-ISO"
                             "-week health heartbeat (idempotent: the workflow appends "
                             "this to all four of Monday's runs).")
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
    counts = {"onset": 0, "recovery": 0, "reescalation": 0}
    written = 0
    with connect() as conn:
        thresholds = load_thresholds(conn)
        policy = AlertPolicy.from_thresholds(thresholds)
        # The alerting baseline must be read BEFORE this run writes anything, and this
        # run now writes incrementally — so it is captured here rather than inside a
        # batch writer (see toolkit.system_alerts.emit_transition_alerts). It carries
        # the stored history, not just the last status: the ladder needs each open
        # incident's onset time to know which rung is due.
        states = check_states(conn, policy=policy) if not args.dry_run else {}
        prev = {k: s.status for k, s in states.items() if s.status is not None}

        def _persist(result: dict[str, Any]) -> None:
            """Persist + alert on THIS check, immediately. Both are per-check so a job
            timeout mid-lane keeps the results it already has AND their alerts, instead
            of losing every row (the pre-W0.4 behavior)."""
            nonlocal written
            LOG.info(
                "CHECK %s status=%s value=%s",
                result["check_key"], result["status"], result.get("value"),
            )
            if args.dry_run:
                return
            insert_result(conn, result, run_at)
            written += 1
            emitted = emit_transition_alerts(
                conn, [result], prev, run_at, states=states, policy=policy)
            for kind in counts:
                counts[kind] += emitted[kind]

        results = run_checks(
            conn, thresholds, weekly=args.weekly, only=only, on_result=_persist)
        if args.dry_run:
            LOG.info("dry-run: %d checks computed, no rows written", len(results))
            return 0
        if args.weekly:
            # The weekly lane's heartbeat: one digest per ISO week, so the operator can
            # tell "nothing is wrong" from "nothing is running". --weekly rides all four
            # of Monday's runs, hence the week-grain dedupe key. Best-effort, like every
            # other write here: a failed heartbeat must not discard the run's results.
            try:
                if emit_weekly_heartbeat(conn, results, states, run_at):
                    LOG.info("emitted the weekly health heartbeat")
            except Exception:  # noqa: BLE001
                LOG.exception("could not emit the weekly health heartbeat")
    LOG.info(
        "verify_pipeline wrote %d rows, emitted %d onset + %d re-escalation + "
        "%d recovery alerts",
        written, counts["onset"], counts["reescalation"], counts["recovery"],
    )
    if args.exit_nonzero_on_fail and any(r["status"] == "fail" for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
