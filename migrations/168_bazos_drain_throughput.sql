-- 168_bazos_drain_throughput.sql
--
-- The bazos detail-drain was capped at max_detail_per_run=350, so each hourly
-- run stopped after 350 claims using only ~17 of its 40-min time budget — far
-- too slow to clear the ~14.5k backlog the 14-category expansion (#364) created.
-- The drain already bounds itself cleanly via --max-seconds (40 min) before the
-- 50-min job timeout, so the tight claim cap is redundant throttling.
--
-- Raise the cap so the TIME budget governs (~1.4k/run at 0.6 req/s × 40 min) and
-- bump detail_workers 2→4 so per-listing geocoding/DB latency overlaps and the
-- drain actually reaches the 0.6 req/s ceiling. detail_rate stays 0.6 — workers
-- share one rate limiter, so this does NOT increase bazos's request rate (the
-- politeness knob); it just uses the validated ceiling for the full budget.

update portals
set operational_limits = coalesce(operational_limits, '{}'::jsonb)
      || '{"max_detail_per_run": 1500, "detail_workers": 4}'::jsonb,
    operational_limits_updated_by = 'migration_168_bazos_drain_throughput'
where source = 'bazos';
