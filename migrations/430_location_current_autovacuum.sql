-- 430_location_current_autovacuum.sql
-- Cardinality Doctrine W0a / F2 — autovacuum on the tables the maintenance jobs scan.
--
-- Both tables are rewritten every 15 minutes by the location resolver drain, and both
-- had drifted far past the default 20% scale factor before a vacuum fired. Measured
-- 2026-08-25, live:
--
--     listing_location_current   687,495 live / 112,136 dead = 14.0%, last autovacuum 08-13
--     property_location_current  569,533 live / 106,535 dead = 15.8%, last autovacuum 08-13
--
-- At the default 0.2 the threshold sits ~137k/114k rows behind, so a table rewritten 96x
-- a day waits nearly two weeks. Every scan of these tables — including F1's new index
-- path and every seq scan that still exists — reads ~15% dead rows for nothing, which
-- lands straight back in the buffer-pool eviction picture F1 exists to fix.
--
-- 0.02 puts the threshold at ~14k/11k dead rows, i.e. roughly hourly at the current
-- write rate, at a cost proportional to what actually changed.
--
-- Rollback:
--   alter table public.listing_location_current  reset (autovacuum_vacuum_scale_factor);
--   alter table public.property_location_current reset (autovacuum_vacuum_scale_factor);
alter table public.listing_location_current
  set (autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02);

alter table public.property_location_current
  set (autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02);
