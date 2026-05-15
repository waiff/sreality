-- 052_estimation_default_filters.sql
--
-- Expose the hardcoded filter defaults used by /estimations as
-- operator-tunable app_settings rows. Until now `_DEFAULT_RADIUS_M`
-- (1000), `_DEFAULT_AREA_BAND_PCT` (0.20), `_DEFAULT_DISPOSITION_MATCH`
-- ("exact"), `_DEFAULT_ACTIVE_ONLY` (true), the rent/sale max_age_days
-- split (7/30), and the find_comparables_relaxed min_results (5) were
-- Python constants in `api/estimation_runs.py`. Operators couldn't edit
-- them without a deploy.
--
-- After this migration the constants become fallback values only — the
-- live defaults live in app_settings, edited via the Settings page (or
-- PUT /admin/app_settings/{key}) and preserved in app_settings_history
-- via the migration 020 trigger.
--
-- The agent ALSO gets a broader find_comparables_relaxed input_schema
-- (commit alongside this migration) so it can tune every ComparableFilters
-- field per round, not just the original five. The defaults below are
-- the values the agent sees in round 1 of every run when the caller
-- didn't override them in the request body.

begin;

insert into app_settings (key, value, description, updated_by) values
  (
    'default_radius_m',
    '1000'::jsonb,
    'Default ST_DWithin radius in metres for comparable lookups. The '
    'agent overrides per round via find_comparables_relaxed.radius_m; '
    'this is the round-1 seed.',
    'seed'
  ),
  (
    'default_area_band_pct',
    '0.20'::jsonb,
    'Default area band as a fraction of the target''s area_m2. 0.20 = '
    '±20%. The agent overrides per round via '
    'find_comparables_relaxed.area_band_pct.',
    'seed'
  ),
  (
    'default_disposition_match',
    '"exact"'::jsonb,
    'Default disposition match mode for comparable lookups. One of '
    '"exact" (only target''s disposition), "loose" (+/-1 from the '
    'target''s, e.g. 2+kk groups with 2+1), or "any" (no disposition '
    'filter). Agent overrides per round.',
    'seed'
  ),
  (
    'default_active_only',
    'true'::jsonb,
    'Whether to restrict comparable lookups to listings whose '
    '`is_active=true` AND `last_seen_at` within max_age_days. Set '
    'false to also consider delisted listings. Agent inherits this '
    'as the base; it can pass `population` per round to override.',
    'seed'
  ),
  (
    'default_max_age_days_rent',
    '7'::jsonb,
    'Default freshness window (days) for rent-estimate comparables. '
    'Listings older than this drop out of the active cohort. Agent '
    'overrides per round via find_comparables_relaxed.max_age_days.',
    'seed'
  ),
  (
    'default_max_age_days_sale',
    '30'::jsonb,
    'Default freshness window (days) for sale-estimate comparables. '
    'Sales turn over more slowly than rentals so the window is wider. '
    'Agent overrides per round via find_comparables_relaxed.max_age_days.',
    'seed'
  ),
  (
    'default_min_results',
    '5'::jsonb,
    'Default min_results threshold for find_comparables_relaxed. The '
    'relaxation ladder widens filters until the cohort hits this size '
    'or the ladder is exhausted. Agent overrides per round.',
    'seed'
  );

commit;
