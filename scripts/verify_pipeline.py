"""verify_pipeline.py — the health-check registry and the harness that measures it.

`CHECKS` declares every check once: key, page section (`domain`), class (an alarm or a
gauge), host, cadence, time budget, label, unit and owner page. A run from main projects its
host's rows into `health_checks` (`project_registry`), then `run_due` measures the checks that
are due by cadence (alarms first, each on its own wall clock) and stores one
`pipeline_check_results` row per result the moment it completes, its message and cells inside
`details`. A status is ok, warn, fail or unknown, and unknown means "not measured" (timed out,
errored, or nothing to score): never OK. A measured status always rides on cells; a result
without them is stored Not measured. A check the run's budget does not reach, or the worker's
capped pass cannot fit, writes nothing and stays due. Nothing here tells anyone — the API's
ladder (`toolkit.system_alerts.reconcile`) reads the stored results, opens one incident per
failing cell and sends the notices — and no result changes the exit code: the canary's
assertions (`scripts/health_canary.py`) are the one way a monitoring run goes red.

Thresholds live in `THRESHOLDS` below, each beside its basis; changing one is a PR. The canary
runs `main()` for `github-actions`; the worker's health lane calls `project_registry` and
`run_due` for `realtime-worker` itself.

There is deliberately NO total-silence LLM check: every LLM producer left is dispatch-driven or
on demand, so silence is the normal resting state (the longest gap in the 30 days to
2026-09-21 was 143.8 h, and the old `llm_liveness` failed 52 of 347 runs on a healthy
pipeline). The `text_extraction` gauge watches the one recurring lane: its stock through the
lane's own predicate, its flow over the new ads that arrived.

    python -m scripts.verify_pipeline            # the due checks of github-actions
    python -m scripts.verify_pipeline --dry-run  # the same, writing nothing

Needs only SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
import time as _time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from scraper.db import connect
from scripts.checks.clock import (
    CHECK_BUDGET_S as _CHECK_BUDGET_S,
    LANE as _LANE,
    PASS as _PASS,
)
from scripts.checks.dedup import check_dedup
from scripts.checks.delisting import check_delisting
from scripts.checks.notifications import check_notifications
from scripts.checks.platform import (
    check_capacity,
    check_jobs,
    check_llm,
    check_migration_drift,
)
from scripts.checks.publishing import check_publishing
from scripts.checks.quality import (
    check_data_quality,
    check_location_town_coverage,
    check_parsing,
    check_text_extraction,
)
from scripts.checks.scraping import check_coverage, check_scraping
from scripts.verify_outbound_urls import PROBE_BUDGET_S, check_outbound_url_conformance
from toolkit.system_alerts import due_after_s as _due_after_s


LOG = logging.getLogger("verify_pipeline")

# Every threshold a check reads, each beside its basis: the one home, so changing one is a
# reviewed PR. Nothing overrides it at run time (app_settings.pipeline_check_thresholds is
# read by nothing since wave 3; migration 643 deletes the row).
THRESHOLDS: dict[str, Any] = {
    # --- jobs (platform alarm): every scheduled machine, one cell per job ------------------
    # One red loses a whole cycle at a 24 h gap: location_registry_load's lone 09-01 timeout.
    "jobs_gh_long_cycle_h": 24,
    # 35 of migration 462's 63 red streaks were lone; recompute_mf_yields' 3rd red came 09-13.
    "jobs_gh_streak_fail": 3,
    # Late past 3 x the job's own p90 start gap: GitHub delivers about a fifth of cron's runs.
    "jobs_gh_gap_p90_multiple": 3,
    # recent_starts accumulates from wave 3, so the p90 arm is Not measured until 10 starts.
    "jobs_gh_gap_min_starts": 10,
    # The canary's measured 13.3 h maximum gap plus margin; the old check's 46 fails were real.
    "jobs_poller_stale_h": 18,
    # pg_cron keeps time: 2 of a job's last 3 finished runs failing is the job, not a blip.
    "jobs_cron_fail_of_3": 2,
    # 3 median start gaps with no start is two missed cycles, never a scheduler's jitter.
    "jobs_cron_gap_multiple": 3,
    # Under 5 starts in 7 days a job's gap is unknown, so its lateness arm is Not measured.
    "jobs_cron_gap_min_starts": 5,
    # Never late or failing under 15 min: each of ~11 deploys a day pauses lanes under 2 min.
    "jobs_lane_floor_s": 900,
    # 15 min of worker uptime before a lane that has not woken or passed can be judged.
    "jobs_lane_boot_grace_s": 900,
    # Eight portals at up to 120 s each make ~16 min an honest drain pass (the old stall warn).
    "jobs_lane_inflight_warn_s": 1200,
    # LANE_PASS_TIMEOUT_SECONDS: past it the worker abandons the pass (a wedge sat nine hours).
    "jobs_lane_inflight_fail_s": 1800,
    # Hourly-or-slower lanes pass about once per boot, so any failing_since there is a Problem.
    "jobs_lane_daily_interval_s": 3600,
    # Half the passes since the previous result failed: autodedup's failed_passes 6 on 10-02.
    "jobs_lane_flap_share": 0.5,
    # ...over at least 3 passes, so one failure in two passes is not a flap.
    "jobs_lane_flap_min_passes": 3,
    # --- capacity (platform alarm): is the database, and the worker's memory, keeping up ----
    # browse-list-rebuild ran 15-28 x its ~9.9 s reference from 08-17: Watch at 3 x and 30 s.
    "capacity_p50_warn_multiple": 3,
    "capacity_p50_warn_floor_s": 30,
    # Problem at 4 x and 60 s; a trailing 28-day baseline read recovered by early September.
    "capacity_p50_fail_multiple": 4,
    "capacity_p50_fail_floor_s": 60,
    # Watch at a p95 of half the schedule interval: at twice that, runs overlap.
    "capacity_p95_interval_warn": 0.5,
    # 80 % of interval or statement_timeout: browse-list-rebuild hit 1,800 s 09-26..10-03.
    "capacity_p95_interval_fail": 0.8,
    # A whole post-reference week (one weekly p50) before the relative arm may judge.
    "capacity_reference_min_days": 7,
    # The first full day on the 2XL instance (p50 41-100 s); re-blessing is a dated PR. A rail fails once it is over 90 days old.
    "CAPACITY_REFERENCE_FROM": _dt.date(2026, 10, 17),
    # Timed-out health checks per 24 h: up to ~12 %/day in the saturation, 0 since 10-10.
    "capacity_timeout_share_warn": 0.05,
    "capacity_timeout_share_fail": 0.20,
    # The saturation month's oldest open transaction was 18.3 min.
    "capacity_open_xact_warn_min": 60,
    "capacity_open_xact_fail_min": 360,
    # Connections against max_connections: live 29 of 380.
    "capacity_connections_warn": 0.7,
    "capacity_connections_fail": 0.9,
    # The worker's peak RSS against its container limit: live 7.4 of 22.9 GB.
    "capacity_memory_warn": 0.8,
    "capacity_memory_fail": 0.9,
    # --- llm (platform alarm): are the providers serving, and is spend sane ----------------
    # 20 % of 20+ calls in 24 h while the provider's newest call errors; 5 of 5 onsets real.
    "llm_error_share_warn": 0.2,
    "llm_error_share_min_calls": 20,
    # 5+ attempts, no success in an hour: a refusing provider books $0 (the 11-day outage).
    "llm_starved_min_attempts": 5,
    # Normal spend is $22-32 a day; 130, not 90, is what production carried since mig 297.
    "llm_spend_24h_warn_usd": 130,
    "llm_spend_24h_fail_usd": 150,
    # A credit outage stays red after its callers back off: 72 h, never a 24 h window.
    "llm_lookback_h": 72,
    # --- scraping (portal alarm): is every portal still finding and fetching its ads ------
    # Watch past 3 G (the median walk-start gap, 14 d: crawlers 7.15-7.33 h, sreality 4.42 h).
    "scraping_walk_watch_gaps": 3,
    # Problem past 6 G (crawlers ~43 h, sreality 26.5 h); no unit crossed it in the 14-day replay.
    "scraping_walk_fail_gaps": 6,
    # Under 10 finished gaps in 14 d a portal is scored on the slowest portal's G, never dropped.
    "scraping_walk_fallback_min_gaps": 10,
    # A unit walked lately with no end-reached walk in 14 d is a Problem whatever its G.
    "scraping_walk_stale_days": 14,
    # "Lately" is 30 d: a unit absent from every walk for 30 d has left its portal's walk.
    "scraping_walk_recent_days": 30,
    # Inflow: Problem under 20 % of the same 24 h 7/14/21/28 d back (0 in a 14-day backtest).
    "scraping_inflow_fail_share": 0.2,
    # Watch under 50 %: same-window baselines absorb the 30-45 % weekend dip.
    "scraping_inflow_warn_share": 0.5,
    # A spike past 300 % of the baseline is Watch, never Problem.
    "scraping_inflow_spike_share": 3.0,
    # Under 20 a day 7-day windows are compared; under 20 a week the cell is Not measured.
    "scraping_inflow_min_baseline": 20,
    # Fetch errors, Wilson LB of the ids attempted in 24 h: Watch past 2 % (worst day 0.18 %).
    "scraping_fetch_err_warn": 0.02,
    # Problem past 10 %: the old 25 % false FAIL's poison id is 1 of ~6,060 ids (0.02 %) here.
    "scraping_fetch_err_fail": 0.10,
    # Under 200 ids the window widens to 7 d, else the cell is Not measured.
    "scraping_fetch_err_min_ids": 200,
    # Missing, declared − collected at an end-reached walk: Watch past 2 % (walks: 99.96-100 %).
    "scraping_missing_warn": 0.02,
    # Problem past 5 %: a parser dropping cards on a walk that still reaches the portal's end.
    "scraping_missing_fail": 0.05,
    # A unit declaring under 500 ads is not scored: 10 missing ads would read 2 % there.
    "scraping_missing_min_declared": 500,
    # --- publishing (portal alarm): do discovered ads reach Browse, with their photos -----
    # Ingest (oldest NEW row) and changes (CHANGED/FAILURE): Watch past 3 h (7-day max 122 min).
    "publishing_queue_warn_h": 3,
    # Problem past 6 h: a starved NEW queue (08-17) fails here; a politeness burst never does.
    "publishing_queue_fail_h": 6,
    # Visible: Wilson LB of new active ads not in browse_list past 2 % (2 of 2,374 on 10-17).
    "publishing_visible_warn": 0.02,
    # Problem past 10 %; queued ads are ingest's, so the 10-13 burst (1,110 ads waiting, oldest 119 min) stays green.
    "publishing_visible_fail": 0.10,
    # Under 50 ads the window widens to 72 h, else the cell is Not measured.
    "publishing_visible_min_n": 50,
    # Stock: active properties missing from browse_list past 0.1 % (live 20 of 362,883).
    "publishing_stock_warn": 0.001,
    # Problem when the Wilson LB is past 1 %: the 09-14 Browse drop read ~87 %.
    "publishing_stock_fail": 0.01,
    # The Problem arm needs at least 200 properties behind the share.
    "publishing_stock_min_n": 200,
    # Photos of ads first written 1-24 h ago: Wilson LB not stored past 2 % (1 of 30,137 today).
    "publishing_photos_unstored_warn": 0.02,
    # Problem past 10 %: the iDNES throttle took new ads' given-up photos from 5 % to 52 %.
    "publishing_photos_unstored_fail": 0.10,
    # Under 200 photos the photo arms are Not measured.
    "publishing_photos_min_n": 200,
    # Stored narrower than min_px: Watch past 10 % (today 0-2.8 %).
    "publishing_photos_narrow_warn": 0.10,
    # Problem past 50 %: a thumbnail-template regression drives it towards 100 %.
    "publishing_photos_narrow_fail": 0.50,
    # Narrow is under 600 px wide; stored medians sit at 1,199-1,600 px.
    "publishing_photos_min_px": 600,
    # The oldest dirty property past 4 h, above the sweep's 100-min lease hold (7-day max 3.4 h).
    "publishing_dirty_warn_h": 4,
    # Problem past 8 h: the 08-20 outage held dirty rows for 6 d; 09-15's 165 h was the sweep's age (the
    # dirty queue stayed under 1 h that day), so the dirty arm alone would have missed it: jobs' sweep cell has it.
    "publishing_dirty_fail_h": 8,
    # --- delisting (portal alarm): are gone ads made inactive, on time and correctly ------
    # Outflow: Problem under 20 % of the quietest 24 h within ±3 h of this hour 7/14/21/28 d back
    # (10-10: 0 against 400-1,900).
    "delisting_outflow_fail_share": 0.2,
    # Watch under 50 %; revivals clear inactive_at 48-72 h on, so the ratio reads up to ~7 % high.
    "delisting_outflow_warn_share": 0.5,
    # A spike past 300 % of the busiest windows' median (baseline_busy)...
    "delisting_outflow_spike_share": 3.0,
    # ...and of at least 500 ads is Watch, never Problem.
    "delisting_outflow_spike_min": 500,
    # Under a baseline of 50 a day the cell is Not measured: the smallest portal, by its volume.
    "delisting_outflow_min_baseline": 50,
    # Each week back is read ±3 h around this hour, a drop on its quietest 24 h, a spike on its
    # busiest: fixed windows read Problem when a walk ran 1 h 53 min late (10-05); 2-4 h open the
    # replay's same 13 incidents, 3 h with an hour to spare.
    "delisting_outflow_align_h": 3,
    # Stuck: active ads past effective dwell + 2 × the p90 walk gap + slack; Watch at 25 (live 0).
    "delisting_stuck_warn": 25,
    # Problem at 250: verdict to inactive runs at p90 27.8-35.2 h, under limits of 38.9-47.5 h.
    "delisting_stuck_fail": 250,
    # The hour of slack in that limit, and in the age the precision cohort starts at.
    "delisting_stuck_slack_h": 1,
    # The oldest VERIFY-priority row: Watch past 6 h (7-day maximum 203 min).
    "delisting_verify_warn_h": 6,
    # Problem past 24 h: a whole day of nominations unchecked.
    "delisting_verify_fail_h": 24,
    # Watch after 2 end-reached walks in a row with deferrals; never Problem (a throttle by design).
    "delisting_deferral_walks": 2,
    # Precision: Wilson LB of a day's flips from 2 x the p90 walk gap + slack old, active again,
    # past 10 % (10-22: sreality 8.6 %, idnes 0.9 %).
    "delisting_precision_warn": 0.10,
    # Problem past 15 %: the blink cohorts read 26-49 %.
    "delisting_precision_fail": 0.15,
    # Under 50 flips in the portal's window the cell is Not measured; nothing is pooled.
    "delisting_precision_min_n": 50,
    # --- coverage (portal gauge): do we hold what each portal shows -----------------------
    # Residual (H − P − D) / D: Watch past 2 % (0-0.15 % on idnes, sreality and bazos).
    "coverage_residual_warn": 0.02,
    # Problem past 5 %: ceskereality byt/prodej's 27-day hole read +37 %.
    "coverage_residual_fail": 0.05,
    # A unit declaring under 500 ads is not scored.
    "coverage_residual_min_declared": 500,
    # Cannot be nominated: Watch at 25 per portal (in-flight ads, ~9 on bazos, stay ok).
    "coverage_unnominated_warn": 25,
    # Problem at 100: 422 today, 124 on ceskereality and 298 on realitymix (a filed parser issue).
    "coverage_unnominated_fail": 100,
    # --- dedup (engine alarm): are rulings honoured, is the engine deciding and merging ---
    # Rulings are binary: Problem at 1 pair ruled apart that the engine merged after the ruling
    # (live 0 of 765), or at 1 active broker suppression whose two identities share a broker.
    "dedup_rulings_fail": 1,
    # Decisions an hour over 3 h, clipped at the build start and the last brake lift: Problem at 0
    # with ads waiting and the lane on. The 10-02 stall decided nothing; builds ran 100-16,300/h
    # (the Prague build's first wave 16,275; 1,771-7,654 once the blocks were walked).
    "dedup_build_window_h": 3,
    # Watch past an ETA of 7 d (a lower bound: ads waiting / that rate); the largest ETA 50.7 h.
    "dedup_build_eta_warn_d": 7,
    # The engine's newest merge while building: Watch past 48 h, never Problem (Prague's: 52.3 h).
    "dedup_pause_warn_h": 48,
    # In-scope ads first seen 1-24 h ago with no decision, Wilson LB: Watch past 1 % (healthy: 0).
    "dedup_lag_warn": 0.01,
    # Problem past 10 %: arrival to decision p50 10.8 / p90 55.9 min; the 10-19 stall read 13.1 %
    # undecided (lower bound 10.2 %) at 09:00Z, Problem.
    "dedup_lag_fail": 0.10,
    # Under 20 ads in that cohort the arm is Not measured (about 400 arrive in scope a day).
    "dedup_lag_min_n": 20,
    # Problem past expiry by more than the lane's interval + 10 min, counted from the later of the
    # expiry, the last brake lift and the build end: 8-10 strands, each re-taken in ~1 min.
    "dedup_lease_fail_past_expiry_min": 10,
    # Splits: Wilson LB of the engine's 7-14 d old merge groups an operator split; Watch past 2 %...
    "dedup_splits_warn": 0.02,
    # ...over at least 100 groups, else Not measured (10-19: 0 of 149; 7 of 8,319 ever split).
    "dedup_splits_min_groups": 100,
    # --- parsing (portal alarm): are newly scraped ads still read the way they were ---------
    # Fill: Watch past a 10 pp drop from the blessed fill (the worst weekly drift was 4.1 pp over
    # 90 cells on 09-21; idnes has_balcony 26.8 -> 13.7 % on 10-19 is wave 4's terraces-out
    # definition converging, re-blessed in wave 6).
    "parsing_fill_drop_warn_pp": 0.10,
    # Problem past a 20 pp drop.
    "parsing_fill_drop_fail_pp": 0.20,
    # A drop is judged only on a blessed fill of at least 10 %.
    "parsing_fill_drop_min_baseline": 0.10,
    # Problem when a field keeps under a quarter of its blessed filled ads (a 5 -> 1 % break)...
    "parsing_fill_collapse_ratio": 0.25,
    # ...of at least 50 blessed filled ads.
    "parsing_fill_collapse_min_rows": 50,
    # Vocabulary: an off-canon share up 5 pp is Watch, 3.2 sigma of the 09-21 drift (0 off-canon
    # values in 7 d and 28 d on every portal on 10-19)...
    "parsing_vocabulary_rise_warn_pp": 0.05,
    # ...up 15 pp Problem, 9.5 sigma.
    "parsing_vocabulary_rise_fail_pp": 0.15,
    # A field census older than 30 d, the reviewed-diff cadence, is a Watch census cell.
    "parsing_census_stale_days": 30,
    # Measures of new ads: the Wilson LB of ads first seen in 7 d with no per-m² input, above the
    # cell's own stock share by 10 pp, is Watch (a coverage floor would ring 13 Problem and 6 Watch
    # cells on standing gaps on 10-19)...
    "parsing_measures_warn": 0.10,
    # ...by 25 pp Problem: the old 90 % gap line never reads a partial break such as remax's
    # price defect (~44 % of its pages with no price until 08-05).
    "parsing_measures_fail": 0.25,
    # ...over at least 50 ads first seen in 7 d (maxima's 21 a week stays Not measured).
    "parsing_measures_min_n": 50,
    # Median shift: the active stock's per-m² median against this check's own cell 6-14 d old,
    # Watch past 2x (the largest move since 09-26 is 1.49x, bazos pozemek/prodej)...
    "parsing_median_warn": 2.0,
    # ...Problem past 3x (the 09-17 W19/W21 heals moved five per-m² medians 3.2-6.9x).
    "parsing_median_fail": 3.0,
    # ...when at least 200 ads carry that median in both weeks.
    "parsing_median_min_n": 200,
    # Payload shape: active ads first seen in 48 h whose location payload the reader cannot read,
    # Watch past 3 % (0 of 35,127 arrivals in 28 d)...
    "parsing_payload_warn": 0.03,
    # ...Problem past 10 %: a source-side cutover reads 12.5 % one 6-hourly pass later.
    "parsing_payload_fail": 0.10,
    # ...over at least 30 ads (bezrealitky had 168 in 48 h on 10-19, maxima 2).
    "parsing_payload_min_n": 30,
    # Churn: re-snapshots per active ad in 24 h past 3x the portal's own 28-day median daily ratio
    # is Watch (the ratio's daily medians 0.0055-0.0201 over 09-21..10-18; its 28-day maximum
    # 0.0805, idnes on 10-01)...
    "parsing_churn_warn_x": 3,
    # ...past 6x Problem: idnes's FX-conversion re-snapshot waves read 6.6-11.9x its median day
    # (09-24, 10-01: #683's class, a real churn storm the operator can fix at the source)...
    "parsing_churn_fail_x": 6,
    # ...with at least 500 re-snapshots in the 24 h.
    "parsing_churn_min_snapshots": 500,
    # --- data_quality (quality gauge): standing value-quality conditions; shares on Wilson LBs -
    # Per-m² price floor: priced ads whose per-m² price the basis floor NULLs, Watch past 5 %...
    "data_quality_floor_warn": 0.05,
    # ...Problem past 10 %: realitymix komercni/pronajem 11.4 % of 4,140 (LB 10.5 %) on 10-19 is the
    # expected Problem at ship (idnes and sreality ~0.5 %).
    "data_quality_floor_fail": 0.10,
    # ...over at least 100 priced ads in the arm (the stock, or ads first seen in 7 d).
    "data_quality_floor_min_n": 100,
    # Area and usable area over 10 % apart, land skipped: Watch past 20 % (worst 0.1 % on 10-19)...
    "data_quality_divergence_warn": 0.20,
    # ...Problem past 40 % (mmreality dum/prodej's plot-as-headline read 57 % before its heal)...
    "data_quality_divergence_fail": 0.40,
    # ...over at least 100 ads carrying both areas.
    "data_quality_divergence_min_n": 100,
    # Estate area equal to area + usable within 1 %, a sum the page computed: Watch past 5 % (worst
    # sreality dum/pronajem 1.5 % of 875, LB 0.9 %, on 10-19)...
    "data_quality_estate_sum_warn": 0.05,
    # ...Problem past 10 %, which no coincidence reaches...
    "data_quality_estate_sum_fail": 0.10,
    # ...over at least 100 ads carrying all three areas.
    "data_quality_estate_sum_min_n": 100,
    # Active ads with no portal link, a count per portal (a codebook miss moves a count within a
    # day, never a share of ~400k): Watch past 50 (11 on 10-19, all sreality)...
    "data_quality_url_warn": 50,
    # ...Problem past 500.
    "data_quality_url_fail": 500,
    # The stock's own no-measure share, Watch only past 95 % (remax rows with no category_main at 93-97 % on 10-19; sreality pozemek healed to 6 %)...
    "data_quality_measure_gap_warn": 0.95,
    # ...over at least 200 active ads.
    "data_quality_measure_gap_min_n": 200,
    # --- location_town_coverage (quality gauge): every active Czech ad has a town (rule 25) ---
    # A current RÚIAN version older than 40 d is a Problem (monthly loads; 19 d old on 10-19)...
    "location_registry_max_age_days": 40,
    # ...as is a newer one staged 24 h without becoming current: a superseded load is cancelled,
    # not failed, so the jobs check never sees it stall.
    "location_registry_max_staged_hours": 24,
    # --- text_extraction (quality gauge): are new ads' descriptions read promptly -------------
    # Flow: Wilson LB of in-scope ads arrived in 24 h and not read yet, Watch past 5 % (0 of 707
    # on 10-19)...
    "text_extraction_flow_warn": 0.05,
    # ...Problem past 25 %...
    "text_extraction_flow_fail": 0.25,
    # ...over at least 30 arrivals: a small portal's two new ads both refused have a lower bound of
    # 34 % (wilson_lower(2, 2)), a Problem; bazos, the lane's one portal, had 707 on 10-19.
    "text_extraction_flow_min_n": 30,
    # Stock: Watch past an ETA of 14 d, waiting / readings in 24 h (4.3 d on 10-19: 55,285 waiting
    # at 12,790 a day; the 10-03 credit outage read 324 d).
    "text_extraction_eta_warn_d": 14,
    # --- notifications (platform alarm, on the worker): does every push leave ----------------
    # A delivery due longer than this is a lane that stopped claiming (it ticks every 15 s and a lease
    # is 2 min, so a healthy row is never due past ~2.5 min).
    "notifications_stall_min": 10,
    # Fan-out runs every 15 s: an event 5 min old with an older live device and no ledger row was lost.
    "notifications_recall_grace_min": 5,
    # Problem at 3 terminal failures in 24 h (a single BadDeviceToken is one device's churn).
    "notifications_failures_fail": 3,
    # --- migration_drift (platform alarm, on GitHub): is every merged migration applied -------
    # The newest 25 files span several weeks of this repo's cadence, so a drift is caught long
    # before the window rolls past it, with a few hundred catalog lookups.
    "migration_drift_window": 25,
    # --- outbound_url_conformance (quality alarm, on GitHub, weekly): do portal links open ----
    # A scattered 404 is a delisting rule #3 has not caught up with: Watch when the whole
    # sample's Wilson LB of non-conforming links is past 5 % (10-18: 6 of 44, LB 6.4 %, four of
    # them relative redirects and bot walls the probe now resolves or calls refused)...
    "outbound_url_conformance_warn_share": 0.05,
    # ...while a slug defect fails a whole (category_main, category_sub_cb) stratum together:
    # Problem on a stratum of at least 3 samples...
    "outbound_url_conformance_cell_min": 3,
    # ...more than 67 % of them non-conforming.
    "outbound_url_conformance_cell_fail_share": 0.67,
    # --- the registry ---------------------------------------------------------------------
    # GitHub delivers a scheduled run up to ~13.3 h late, so its keys stale at cadence + 18 h.
    "GITHUB_DELIVERY_SLACK_S": 18 * 3_600,
}

# Process start and the registry projection, reserved out of the harness step's limit on top
# of the lane budget (test_the_canary_job_fits_every_github_check_due_at_once).
_JOB_HEADROOM_S = 180.0


# --- the registry ------------------------------------------------------------

HOSTS = ("realtime-worker", "github-actions", "pg_cron")
CLASSES = ("alarm", "gauge")
# The Health page's sections, in its order (frontend/src/lib/healthChecks.ts SECTIONS): a new
# domain needs a section there, or the page has nowhere to show its checks.
DOMAINS = ("portals", "dedup", "data_quality", "platform")

_GITHUB, _WORKER = "github-actions", "realtime-worker"
_TEN_MINUTES, _HALF_HOURLY = 600, 1_800
_HOURLY, _SIX_HOURLY, _WEEKLY = 3_600, 21_600, 604_800
# The worker and pg_cron keep time, so a key they host reads stale after 3 cadences; GitHub's
# keys get their cadence plus THRESHOLDS["GITHUB_DELIVERY_SLACK_S"].
_STALE_CADENCES = 3
# The one check that leaves the database waits on third-party round trips, not on Postgres:
# its budget is declared beside its HEAD loop, which stops itself at it (nothing outside can
# preempt a request).
_OUTBOUND_BUDGET_S = PROBE_BUDGET_S
# Parsing reads the active stock several ways: the measure view it shares with data_quality
# through the pass memo (~12 s cold, 3.45 s warm on 10-19), the field matrix (~12 s cold, 3.7 s
# warm) and the payload, churn and baseline reads (about 5 s together cold on 10-19: 2.1 s,
# 2.8 s and 29 ms). Four default budgets.
_PARSING_BUDGET_S = 4 * _CHECK_BUDGET_S
# The push ledger's four reads touch a few hundred rows (the 24 h event window, the live devices), so 5 s; it is
# also what keeps every worker alarm due at once inside the 500 s pass (7 x 45 + 180 + 5).
_NOTIFICATIONS_BUDGET_S = 5.0


@dataclass(frozen=True)
class Check:
    """One registered check: its body, how it is judged, where it runs and how often."""

    key: str
    fn: Callable[[Any, dict[str, Any]], dict[str, Any]]
    domain: str
    cls: str
    host: str
    cadence_s: int
    label: str
    unit: str = ""
    owner_path: str = ""
    budget_s: float = _CHECK_BUDGET_S

    @property
    def staleness_budget(self) -> _dt.timedelta:
        """How old the newest result may grow before the page reads the key Not measured."""
        if self.host == _GITHUB:
            return _dt.timedelta(seconds=self.cadence_s + THRESHOLDS["GITHUB_DELIVERY_SLACK_S"])
        return _dt.timedelta(seconds=self.cadence_s * _STALE_CADENCES)

    @property
    def due_after_s(self) -> int:
        """How old the newest result must be before a lane measures this check again."""
        # Spelled in the ladder, whose recovery hold is two of these intervals.
        return _due_after_s(self.cadence_s)


# `run_due` runs a host's due alarms first, then its due gauges, each class in this order, and
# a check its budget does not reach (or the worker's capped pass cannot fit) stays due for the
# next run, so the order is a priority inside each class: cheap invariants first, the expensive
# reads late and the outbound probe last. The page's `sort` is the position here.
CHECKS: tuple[Check, ...] = (
    # FIRST of the gauges: 2026-09-11 21:02 lost the last seven checks to the budget, this
    # one among them, and the invariant the location programme is measured by reads in 3.1 s
    # with the served predicate's hidden count on 10-19 (0.62 s without it; 6.5 s whole-corpus
    # before).
    Check("location_town_coverage", check_location_town_coverage, "data_quality", "gauge",
          _WORKER, _SIX_HOURLY, "Location coverage", unit="cells",
          owner_path="/location-quality"),
    # The worker's health lane measures these thirteen (scripts/checks/: platform, the portal checks,
    # dedup, push notifications, then parsing and the quality gauges). GitHub keeps what needs it: migration_drift
    # reads a checkout, the outbound probe an address that is not the scrapers'.
    Check("jobs", check_jobs, "platform", "alarm", _WORKER, _TEN_MINUTES, "Jobs",
          unit="cells", owner_path="/settings"),  # Settings › GitHub Actions
    Check("capacity", check_capacity, "platform", "alarm", _WORKER, _HALF_HOURLY, "Capacity",
          unit="cells"),
    Check("llm", check_llm, "platform", "alarm", _WORKER, _TEN_MINUTES, "LLM providers",
          unit="cells", owner_path="/costs"),
    # Domain "portals" is the page's portal matrix: one column per check, one row per portal.
    Check("scraping", check_scraping, "portals", "alarm", _WORKER, _TEN_MINUTES, "Scraping",
          unit="cells", owner_path="/scrapers"),
    Check("publishing", check_publishing, "portals", "alarm", _WORKER, _TEN_MINUTES,
          "Publishing", unit="cells", owner_path="/scrapers"),
    Check("delisting", check_delisting, "portals", "alarm", _WORKER, _TEN_MINUTES,
          "Delisting", unit="cells", owner_path="/scrapers"),
    Check("coverage", check_coverage, "portals", "gauge", _WORKER, _HOURLY, "Coverage",
          unit="cells", owner_path="/scrapers"),
    Check("dedup", check_dedup, "dedup", "alarm", _WORKER, _TEN_MINUTES, "Engine",
          unit="cells", owner_path="/autodedup/progress"),
    # The push ledger (notifications push W4): stall, recall, failures, and the brake shown.
    Check("notifications", check_notifications, "platform", "alarm", _WORKER, _TEN_MINUTES,
          "Push notifications", unit="cells", owner_path="/notifications",
          budget_s=_NOTIFICATIONS_BUDGET_S),
    # LAST of the worker's alarms: the expensive reads. data_quality shares its measure read
    # through the pass memo whenever both are due in one pass.
    Check("parsing", check_parsing, "portals", "alarm", _WORKER, _SIX_HOURLY, "Parsing",
          unit="cells", owner_path="/scrapers", budget_s=_PARSING_BUDGET_S),
    # Owner: the per-portal page (value quality is a portal parser's debt; no page renders it).
    Check("data_quality", check_data_quality, "data_quality", "gauge", _WORKER, _SIX_HOURLY,
          "Data quality", unit="cells", owner_path="/scrapers"),
    Check("text_extraction", check_text_extraction, "data_quality", "gauge", _WORKER, _HOURLY,
          "Description reading", unit="cells", owner_path="/costs"),
    Check("migration_drift", check_migration_drift, "platform", "alarm", _GITHUB, _HOURLY,
          "Migration drift", unit="files"),
    # LAST of the alarms: the check that leaves the database, so a lane that runs out of budget
    # drops the outbound probe before any database alarm.
    Check("outbound_url_conformance", check_outbound_url_conformance, "data_quality", "alarm",
          _GITHUB, _WEEKLY, "Portal links open the right page", unit="cells",
          budget_s=_OUTBOUND_BUDGET_S),
)


# The hosts this harness projects: those with a Check (`project_registry` refuses any other).
# pg_cron's registry row is the watcher's own (migration 620); projecting that host would retire it.
_HARNESS_HOSTS = tuple(dict.fromkeys(c.host for c in CHECKS))

# --- the harness ---------------------------------------------------------------

# Only rows since the key's declared_at count, as for the page and the ladder: a newly registered
# or re-declared key is due in the very run that projects it, not a cadence later.
_DUE_SQL = """
select k.key,
       (select max(r.run_at) from pipeline_check_results r
         where r.check_key = k.key
           and r.run_at >= coalesce(h.declared_at, '-infinity'::timestamptz))
  from unnest(%(keys)s::text[]) as k(key)
  left join health_checks h on h.key = k.key
"""

_HEALTH_CHECKS_ROWS_SQL = """
select key, host, cadence_s, declared_at
  from health_checks
 where key = any(%(keys)s::text[])
"""

_HEALTH_CHECKS_UPSERT_SQL = """
insert into health_checks
       (key, domain, class, host, cadence_s, staleness_budget, label, unit, owner_path,
        sort, declared_at)
values (%(key)s, %(domain)s, %(class)s, %(host)s, %(cadence_s)s, %(staleness_budget)s,
        %(label)s, %(unit)s, %(owner_path)s, %(sort)s, %(declared_at)s)
on conflict (key) do update
   set domain = excluded.domain, class = excluded.class, host = excluded.host,
       cadence_s = excluded.cadence_s, staleness_budget = excluded.staleness_budget,
       label = excluded.label, unit = excluded.unit, owner_path = excluded.owner_path,
       sort = excluded.sort, declared_at = excluded.declared_at
"""

_HEALTH_CHECKS_RETIRE_SQL = """
delete from health_checks
 where host = %(host)s and not (key = any(%(keys)s::text[]))
"""


def due_checks(conn: Any, host: str, now: _dt.datetime) -> list[Check]:
    """This host's checks, in registry order, whose newest result since their declared_at (any
    status) is absent or at least `due_after_s` old."""
    mine = [c for c in CHECKS if c.host == host]
    if not mine:
        return []
    with conn.cursor() as cur:
        cur.execute(_DUE_SQL, {"keys": [c.key for c in mine]})
        newest = {key: run_at for key, run_at in cur.fetchall()}
    return [c for c in mine
            if newest.get(c.key) is None
            or (now - newest[c.key]).total_seconds() >= c.due_after_s]


def project_registry(conn: Any, host: str, now: _dt.datetime) -> dict[str, int]:
    """Upsert this host's CHECKS into health_checks; delete its rows that no Check declares."""
    if host not in _HARNESS_HOSTS:
        raise ValueError(f"no Check is hosted on {host!r}: its registry rows (the watcher's, "
                         "migration 620) are not this harness's to retire")
    declared = [(sort, c) for sort, c in enumerate(CHECKS) if c.host == host]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_HEALTH_CHECKS_ROWS_SQL, {"keys": [c.key for _, c in declared]})
        prior = {key: (row_host, cadence_s, declared_at)
                 for key, row_host, cadence_s, declared_at in cur.fetchall()}
        rows: list[dict[str, Any]] = []
        for sort, c in declared:
            old = prior.get(c.key)
            # declared_at anchors "not measured yet" on the page, so only a new key or a
            # changed host or cadence restarts it; a re-projection keeps the clock.
            kept = old is not None and old[0] == c.host and old[1] == c.cadence_s
            rows.append({
                "key": c.key, "domain": c.domain, "class": c.cls, "host": c.host,
                "cadence_s": c.cadence_s, "staleness_budget": c.staleness_budget,
                "label": c.label, "unit": c.unit, "owner_path": c.owner_path,
                "sort": sort, "declared_at": old[2] if kept else now,
            })
        if rows:
            cur.executemany(_HEALTH_CHECKS_UPSERT_SQL, rows)
        cur.execute(_HEALTH_CHECKS_RETIRE_SQL,
                    {"host": host, "keys": [c.key for c in CHECKS]})
        deleted = cur.rowcount
    return {"upserted": len(rows), "deleted": max(deleted or 0, 0)}


def _is_timeout_error(exc: BaseException) -> bool:
    """A statement cancelled by our own per-check budget, vs a genuinely broken check."""
    name = type(exc).__name__
    if name in ("QueryCanceled", "QueryCanceledError"):
        return True
    return "canceling statement due to statement timeout" in str(exc)


def _timed_out_result(key: str, budget_s: float, elapsed_s: float,
                      exc: BaseException) -> dict[str, Any]:
    """A check cancelled by its wall clock measured nothing: unknown, never fail.

    "I could not measure this" is a different claim from "I measured this and it is broken",
    and a lane under DB pressure would otherwise manufacture a wall of false reds at the
    exact moment the operator needs to read the real one."""
    return {
        "check_key": key,
        "status": "unknown",
        "value": None,
        "details": {"cells": [], "timed_out": True, "budget_seconds": round(budget_s, 1),
                    "elapsed_seconds": round(elapsed_s, 1), "error": str(exc)},
        "message": (
            f"Check '{key}' ran out of its {budget_s:.0f}s budget after {elapsed_s:.0f}s and "
            "was cancelled, so it is Not measured this run (never healthy). Usually DB "
            "pressure; if it persists, the check's query needs work."
        ),
    }


def _errored_result(key: str, exc: BaseException) -> dict[str, Any]:
    """A check that raised measured nothing: unknown, with the error kept."""
    return {
        "check_key": key,
        "status": "unknown",
        "value": None,
        "details": {"cells": [], "errored": True, "error": str(exc)},
        "message": f"Check '{key}' raised an error, so it is Not measured this run: {exc}",
    }


_VERDICTS = ("ok", "warn", "fail")


def _carries_cells(result: dict[str, Any]) -> bool:
    """At least one judged cell: all the ladder reads (wave 6 deleted its implicit cell)."""
    cells = (result.get("details") or {}).get("cells")
    return isinstance(cells, list) and any(
        isinstance(c, dict) and c.get("status") in _VERDICTS for c in cells)


def _uncelled_result(key: str, status: str) -> dict[str, Any]:
    """A verdict with no judged cell would read on the page and reach no incident: it is stored
    Not measured instead, and the harness logs it as the check's defect."""
    reason = f"the check returned {status} without a judged cell"
    return {
        "check_key": key,
        "status": "unknown",
        "value": None,
        "details": {"cells": [], "skipped": reason},
        "message": f"Check '{key}' is Not measured this run: {reason}.",
    }


def lane_budget_for(checks: Sequence[Check]) -> float:
    """A lane owes every check it runs that check's own budget, so none can starve another."""
    return float(sum(c.budget_s for c in checks))


# A check fits the pass while its budget is at most what the pass has left plus this slack. The
# lane's clock runs from `start`, so a check due alone (its lane IS its budget) is weighed a few
# ns short of its budget and, with no slack, was deferred on every tick; one second also lets a
# check before it overrun its own budget by the statement floor (clock.statement_timeout_ms).
_FIT_SLACK_S = 1.0


def run_checks(
    conn: Any, thresholds: dict[str, Any], *, checks: Sequence[Check],
    on_result: Callable[[dict[str, Any]], None] | None = None,
    lane_budget_s: float | None = None, defer_unfit: bool = False,
) -> list[dict[str, Any]]:
    """Run each check in isolation on its own wall clock, handing on_result each result as it
    lands; a check the lane budget does not reach gets no result, so it stays due. With
    `defer_unfit` a check whose budget no longer fits what the lane has left (plus
    `_FIT_SLACK_S`), weighed before it starts, is not started either: deferred to the next pass,
    never cut short into a Not measured row that holds it a cadence."""
    results: list[dict[str, Any]] = []
    _LANE.reset()
    _PASS.start()
    if lane_budget_s is not None:
        _LANE.start(lane_budget_s)
    try:
        for i, check in enumerate(checks):
            left = _LANE.remaining()
            if left is not None and left <= 0:
                LOG.warning("lane budget %.0fs spent; not reached, so still due: %s",
                            lane_budget_s or 0.0, ", ".join(c.key for c in checks[i:]))
                break
            if defer_unfit and left is not None and check.budget_s > left + _FIT_SLACK_S:
                LOG.warning("DEFER %s: its %.0fs budget does not fit the %.0fs the pass has left; "
                            "due next pass", check.key, check.budget_s, left)
                continue
            budget = _LANE.arm_for_check(check.budget_s)
            started = _time.monotonic()
            try:
                result = check.fn(conn, thresholds)
            except Exception as exc:  # noqa: BLE001 - one broken check never ends the lane
                if _is_timeout_error(exc):
                    spent = _time.monotonic() - started
                    LOG.error("check %s timed out after %.1fs of its %.0fs budget",
                              check.key, spent, budget)
                    result = _timed_out_result(check.key, budget, spent, exc)
                else:
                    LOG.exception("check %s errored", check.key)
                    result = _errored_result(check.key, exc)
            if result.get("status") in _VERDICTS and not _carries_cells(result):
                LOG.error("check %s returned %s without a judged cell; stored Not measured: %s",
                          check.key, result["status"], str(result.get("message") or "")[:200])
                result = _uncelled_result(check.key, result["status"])
            elapsed = _time.monotonic() - started
            if not isinstance(result.get("details"), dict):
                result["details"] = {}
            if not isinstance(result["details"].get("cells"), list):
                result["details"]["cells"] = []
            result["details"]["elapsed_ms"] = int(elapsed * 1000)
            results.append(result)
            # Persisted per check, so a job killed mid-lane keeps everything it measured.
            if on_result is not None:
                try:
                    on_result(result)
                except Exception:  # noqa: BLE001 - persisting one result must not end the lane
                    LOG.exception("could not persist result for check %s", check.key)
    finally:
        _LANE.reset()
        _PASS.reset()
    return results


def insert_result(conn: Any, result: dict[str, Any], run_at: _dt.datetime) -> None:
    """Persist ONE result; its message rides inside `details`, beside any cells it carries."""
    details = dict(result.get("details") or {})
    if result.get("message"):
        details["message"] = result["message"]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pipeline_check_results (run_at, check_key, status, value, details) "
            "VALUES (%s, %s, %s, %s, %s::jsonb)",
            (run_at, result["check_key"], result["status"],
             result.get("value"), json.dumps(details)),
        )


def run_due(
    conn: Any, thresholds: dict[str, Any], host: str, *, now: _dt.datetime,
    on_result: Callable[[Any, dict[str, Any], _dt.datetime], None] = insert_result,
    lane_budget_s: float | None = None,
) -> list[dict[str, Any]]:
    """Measure this host's due checks, alarms before gauges, storing each result as it lands.
    `lane_budget_s` caps the pass (the worker lane's 500 s) and defers a check that no longer
    fits it to the next pass; uncapped (the canary), the lane is the due budgets' sum."""
    # Due-ness is read from the stored results, so a restarted lane re-measures nothing, and
    # the sort is stable: registry order holds inside each class.
    due = sorted(due_checks(conn, host, now), key=lambda c: c.cls != "alarm")
    if due:
        LOG.info("DUE host=%s %d of %d checks: %s", host, len(due),
                 sum(1 for c in CHECKS if c.host == host), ", ".join(c.key for c in due))
    budget = lane_budget_for(due)
    if lane_budget_s is not None:
        budget = min(budget, lane_budget_s)
    return run_checks(conn, thresholds, checks=due,
                      on_result=lambda result: on_result(conn, result, now),
                      lane_budget_s=budget, defer_unfit=lane_budget_s is not None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Select and compute the due checks and log them; write nothing "
                             "(no registry rows, no result rows).")
    parser.add_argument("--host", default=_GITHUB, choices=(_GITHUB,),
                        help="The host whose registry rows this run projects and whose due "
                             "checks it measures: GitHub's only (the worker's health lane "
                             "calls run_due itself).")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    run_at = _dt.datetime.now(_dt.timezone.utc)
    written = 0
    with connect() as conn:
        # A dispatch from a branch measures with that branch's CHECKS but never projects them:
        # its registry would delete main's keys from the page until main's next run.
        ref = os.environ.get("GITHUB_REF_NAME", "main")
        if not args.dry_run and ref != "main":
            LOG.info("REGISTRY host=%s not projected: this run is from %s, not main",
                     args.host, ref)
        elif not args.dry_run:
            projected = project_registry(conn, args.host, run_at)
            LOG.info("REGISTRY host=%s upserted=%d deleted=%d",
                     args.host, projected["upserted"], projected["deleted"])

        def _persist(db: Any, result: dict[str, Any], at: _dt.datetime) -> None:
            """Log this result and, unless dry-running, store it the moment it completes."""
            nonlocal written
            LOG.info(
                "CHECK %s status=%s value=%s",
                result["check_key"], result["status"], result.get("value"),
            )
            if args.dry_run:
                return
            insert_result(db, result, at)
            written += 1

        results = run_due(conn, THRESHOLDS, args.host, now=run_at, on_result=_persist)
    LOG.info("verify_pipeline host=%s measured %d checks, wrote %d rows",
             args.host, len(results), written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
