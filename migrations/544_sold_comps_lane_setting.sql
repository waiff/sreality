-- 544_sold_comps_lane_setting.sql — the sold-comps worker lane's one knob (sold-comps W2).
--
-- The lane that fetches registered sales from reas.cz (`scraper/realtime_worker.py`,
-- `sold_comps`) has NO boolean flag and NO env var. `_lane_loop` treats `interval <= 0`
-- as idle-not-dead, so this single integer is the cadence AND the kill switch — strictly
-- less machinery than the estimation and location-resolve lanes, which each carry a
-- boolean setting on top of an interval.
--
-- Seeded at 0, i.e. the lane SHIPS DARK: the worker reads the row every pass and idles
-- while it is 0. The row exists only so the operator can turn the lane on from /settings
-- without a deploy — `PUT /admin/app_settings/{key}` 404s on a key that has no row, and
-- the page renders `description` as the hint beside it.
--
-- Data-only and idempotent: `on conflict do nothing`, no schema change, no new table (so
-- nothing for the RLS rule to cover). No `--single-transaction`, so every statement is
-- re-runnable from statement 1 (.github/workflows/apply_migration.yml).

set lock_timeout = '5s';

insert into app_settings (key, value, description)
values (
  'realtime_sold_comps_interval_seconds',
  '0'::jsonb,
  'How often the real-time worker fetches registered sales (reas.cz) for the towns '
  'where your deal pipeline has a live card, in seconds. 0 turns the lane off — it is '
  'both the schedule and the switch. Each pass takes at most 5 municipalities, stalest '
  'first, and re-asks a municipality only once its last successful look is 35 days old '
  '(6 hours after a failure), so a few hours is a sensible cadence. Sales appear about '
  '30 days after the transfer, so nothing is lost by looking rarely.'
)
on conflict (key) do nothing;

reset lock_timeout;
