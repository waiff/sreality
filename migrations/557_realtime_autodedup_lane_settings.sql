-- 557_realtime_autodedup_lane_settings.sql — the always-on worker's autodedup lane interval
-- (AUTODEDUP rollout §7.3, "true real-time").
--
-- The lane (`scraper/realtime_worker.py`, `autodedup`) runs ONE bounded pass of the dedup
-- engine's real-time SHADOW lane (`autodedup.incremental_lane.run_incremental`, the same
-- function `autodedup_realtime.yml` runs) every interval. It writes only the `rt` generation
-- inside schema `autodedup` — never a production merge.
--
-- ONE row, the worker's convention (the sold_comps lane's, migration 544): the interval is the
-- cadence AND the kill switch, 0 = stopped, no boolean flag. Seeded at 0, so the lane SHIPS
-- DARK; the row exists so the operator can start it from /settings without a deploy — `PUT
-- /admin/app_settings/{key}` 404s on a key that has no row, and the page renders
-- `description` as the hint beside it. The claim is sized by the engine itself (its measured
-- rate under the worker's pass budget), so there is no count to set here.
--
-- Data-only and idempotent: `on conflict do nothing` (an operator's value is never
-- overwritten), no schema change, no new table (so nothing for the RLS rule to cover). No
-- `--single-transaction`, so every statement is re-runnable from statement 1
-- (.github/workflows/apply_migration.yml).

set lock_timeout = '5s';

insert into app_settings (key, value, description)
values (
  'realtime_autodedup_interval_seconds',
  '0'::jsonb,
  'Seconds between two duplicate-finding passes inside the always-on worker; 0 stops the lane. '
  'A pass compares new and changed adverts with their neighbours and only proposes groups on '
  'the review pages — it never merges anything in Browse. 60 is a sensible running value: a '
  'pass only looks at adverts at least five minutes old, so going lower does not make '
  'decisions faster.'
)
on conflict (key) do nothing;

reset lock_timeout;
