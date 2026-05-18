-- 069_filter_visibility_condition.sql
--
-- Seed visibility rows so the `condition_match` filter (already in
-- the registry and used by the analytical surfaces) becomes available
-- on Browse, Watchdog, Neighborhood, and Defaults — matching the
-- universal coverage of `furnished` / `ownership`.
--
-- The Settings → agenda × filter matrix renders one toggle cell per
-- (agenda, filter_id) pair in this table. Seeding `enabled=true` here
-- means a fresh deploy shows the new toggles ON; an operator can flip
-- any of them off without losing their setting on the next deploy
-- (ON CONFLICT DO NOTHING preserves existing rows).
--
-- Browse RPC plumbing for the new parameter ships in migration 068.
-- Watchdog matcher plumbing ships in api/notifications.py.

begin;

insert into filter_visibility (agenda, filter_id, enabled, updated_by) values
    ('browse',       'condition_match', true, 'migration_069'),
    ('watchdog',     'condition_match', true, 'migration_069'),
    ('neighborhood', 'condition_match', true, 'migration_069'),
    ('defaults',     'condition_match', true, 'migration_069')
on conflict (agenda, filter_id) do nothing;

commit;
