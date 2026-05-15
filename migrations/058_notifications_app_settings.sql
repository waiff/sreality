-- 058_notifications_app_settings.sql
--
-- Phase U2.7: Operator-tunable knobs for the new-listing notification
-- matcher. Lives in `app_settings` so the worker cadence and watermark
-- can be inspected and adjusted without a redeploy. Same pattern as
-- the LLM-cost knobs from migration 020.
--
-- - notifications_matcher_interval_seconds: how often the FastAPI
--   lifespan-spawned async worker wakes up. Default 300 seconds
--   (5 minutes) — matches the operator-proposed Phase U2.7 cadence.
--   Setting to 0 disables the loop.
-- - notifications_watermark_first_seen_at: ISO-8601 timestamp; the
--   worker only considers listings with first_seen_at strictly greater
--   than this value. Bumped to max(first_seen_at) of the processed
--   window after each successful run. Initialised to now() at seed
--   time so the first matcher run after deploy does NOT blast the
--   feed with every historical listing — the operator hand-edits this
--   row (or leaves the worker to advance organically) if backfill is
--   desired.
-- - notifications_match_window_listings: hard cap on how many fresh
--   listings the matcher inspects per wake-up. Defends against a
--   future scenario where the scrape cadence grows wildly. Default 1000.

begin;

insert into app_settings (key, value, description, updated_by)
values
    (
        'notifications_matcher_interval_seconds',
        to_jsonb(300),
        'How often the new-listing notification matcher wakes up, in '
        'seconds. Set to 0 to disable the loop entirely. Read once at '
        'service boot; SIGHUP / restart required for changes to take '
        'effect.',
        'migration_058'
    ),
    (
        'notifications_watermark_first_seen_at',
        to_jsonb(now()),
        'Watermark cursor for the new-listing notification matcher. '
        'Worker only inspects listings with first_seen_at > this value. '
        'Seeded at now() so the first run after deploy does not flood '
        'the feed with every historical listing.',
        'migration_058'
    ),
    (
        'notifications_match_window_listings',
        to_jsonb(1000),
        'Hard cap on listings inspected per matcher wake-up. Defends '
        'against scrape-cadence spikes. Worker walks oldest first so '
        'a backlog is drained deterministically across consecutive runs.',
        'migration_058'
    )
on conflict (key) do nothing;

commit;
