-- 557_realtime_autodedup_lane_settings.sql — the always-on worker's autodedup lane knobs
-- (AUTODEDUP rollout §7.3, "true real-time").
--
-- The lane (`scraper/realtime_worker.py`, `autodedup`) runs ONE bounded pass of the dedup
-- engine's real-time SHADOW lane (`autodedup.incremental_lane.run_incremental`, the same
-- function `autodedup_realtime.yml` runs) every interval. It writes only the `rt` generation
-- inside schema `autodedup` — never a production merge.
--
-- Seeded with the flag OFF, i.e. the lane SHIPS DARK: the worker reads the flag on every wake
-- and idles while it is false (an absent row reads as false too). The rows exist so the
-- operator can switch the lane on from /settings without a deploy — `PUT
-- /admin/app_settings/{key}` 404s on a key that has no row, and the page renders
-- `description` as the hint beside it. The estimation lane's flag was never seeded, never
-- set, and is invisible to every monitor as a result; this lane does not repeat that.
--
-- Data-only and idempotent: `on conflict do nothing` (an operator's value is never
-- overwritten), no schema change, no new table (so nothing for the RLS rule to cover). No
-- `--single-transaction`, so every statement is re-runnable from statement 1
-- (.github/workflows/apply_migration.yml).

set lock_timeout = '5s';

insert into app_settings (key, value, description)
values
  (
    'realtime_autodedup_enabled',
    'false'::jsonb,
    'Runs the duplicate-finding engine inside the always-on worker, so a new or changed '
    'advert is compared with its neighbours within minutes instead of whenever GitHub''s '
    'ten-minute schedule happens to fire. It still only proposes groups on the review '
    'pages — it never merges anything in Browse. Needs the real-time generation to be seeded '
    'first; until then every pass simply skips. Set to true to turn it on, false to turn it off.'
  ),
  (
    'realtime_autodedup_interval_seconds',
    '60'::jsonb,
    'How long the worker waits between two duplicate-finding passes, in seconds (only while '
    'realtime_autodedup_enabled is true). 0 pauses the lane. A pass only looks at adverts '
    'that are at least five minutes old, so going below 60 does not make decisions faster.'
  ),
  (
    'realtime_autodedup_max_listings',
    '100'::jsonb,
    'The most adverts one duplicate-finding pass may take on (1 to 500). The engine also '
    'limits itself by how fast its last passes ran, and uses the smaller of the two.'
  )
on conflict (key) do nothing;

reset lock_timeout;
