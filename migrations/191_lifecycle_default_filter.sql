-- 191_lifecycle_default_filter.sql
--
-- Unify the cohort lifecycle filter. The comparable-filter layer used to
-- carry TWO knobs for one concept: a legacy `active_only` bool and a
-- `population` enum (active|delisted|all), with the enum overriding the
-- bool. Both are now collapsed into a single `lifecycle` selector
-- (renamed from `population`); `active_only` is deleted from the codebase.
--
-- This migration moves the estimation default to match: the old
-- `default_active_only` (true) app_setting becomes `default_lifecycle`
-- ('active'), which `_build_filters` reads to seed round-1 of every run.
-- 'active' reproduces the prior active_only=true baseline exactly (the
-- is_active gate plus the max_age_days recency window).
--
-- Additive: seeds the new key (no-op if already present) and removes the
-- now-dead `default_active_only` row. The deleted value is preserved in
-- app_settings_history via the migration 020 trigger, so this is
-- reversible.

begin;

insert into app_settings (key, value, description, updated_by) values
  (
    'default_lifecycle',
    '"active"'::jsonb,
    'Default cohort lifecycle for comparable lookups. One of "active" '
    '(is_active=true plus the max_age_days freshness window), "delisted" '
    '(is_active=false — closed deals, a transacted-price proxy), or '
    '"all" (no is_active gate). The agent inherits this as the round-1 '
    'base and can override per round via find_comparables_relaxed.lifecycle.',
    'seed'
  )
on conflict (key) do nothing;

delete from app_settings where key = 'default_active_only';

-- Drop the now-orphaned operator visibility overrides for the retired
-- filter ids (migration 059 seeded `active_only`; `population` was renamed
-- to `lifecycle`). Both filter ids no longer exist in the registry, so
-- these rows are dead. The renamed `lifecycle` filter needs no row —
-- `available_filters` treats a missing visibility row as enabled.
delete from filter_visibility where filter_id in ('active_only', 'population');

commit;
