-- 125_drop_min_completeness_limit.sql
--
-- Remove the operator-tunable `min_completeness` scrape limit. The completeness
-- bar that gates mark_inactive (architectural rule #3) is now HARDCODED at 100%
-- in each portal's *_main.py (INDEX_MIN_COMPLETENESS = 1.0): a listing is only
-- inferred delisted after a FULL index walk, and that is deliberately no longer
-- tunable (a partial walk must never be allowed to delist live listings).
--
-- The knob was, in fact, never read by the scraper — the completeness check uses
-- the module constant, not portals.operational_limits.min_completeness — so this
-- migration only strips the now-dead JSONB keys to keep the DB in sync with the
-- code (PortalLimits no longer has the field) and the Scrapers dashboard (the
-- field is gone). Data-only cleanup; no schema change.
--
-- The before-update history triggers (portal_limits_history, app_settings_history)
-- snapshot the pre-cleanup value, so the removed numbers stay auditable.

update portals
   set operational_limits = operational_limits - 'min_completeness',
       operational_limits_updated_by = 'migration_125'
 where operational_limits ? 'min_completeness';

update app_settings
   set value = value - 'min_completeness',
       updated_by = 'migration_125'
 where key = 'scraper_limits_global'
   and value ? 'min_completeness';
