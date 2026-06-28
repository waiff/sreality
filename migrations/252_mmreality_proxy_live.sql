-- 252_mmreality_proxy_live.sql
--
-- Bring mmreality.cz live as a scheduled scraper portal.
--
-- History: the mmreality scraper code + selectors are correct (registered by
-- migration 117, verified from a residential IP), but mmreality's Cloudflare edge
-- HARD-403s our datacenter (GitHub-Actions) IP on the FIRST request — so the
-- portal ingested ZERO listings across its first 101 runs and migration 173
-- DISABLED its registry row. We now route every request through the residential
-- proxy in SCRAPER_PROXY_URL (MmRealityClient.USE_PROXY = True), the same egress
-- the sister CF-blocked portal (ceskereality) uses; with a residential exit IP the
-- site returns 200. This migration RE-ENABLES the row (reverses 173 for mmreality)
-- and restores normal crawl rates now that the throttle is gone — mirroring
-- ceskereality's migration 251.
--
-- index_rate 1.0 -> 2.0 (a residential exit lifts the politeness floor that the
-- raw datacenter IP needed; matches ceskereality's post-proxy 2.0). detail_workers
-- and detail_rate are already 4 / 2.0. The portal stays supports_complete_walk
-- =false (single mixed index, no result-total — rule #3 unchanged); delistings
-- surface via the gone-detail flip + the 7-day staleness rule.
--
-- Operational/data only (updates the existing registry row from migration 117);
-- no schema change. The scheduled cron lands together with this migration in
-- scrape_mmreality.yml.

update portals
set is_enabled = true,
    operational_limits = '{
      "index_rate": 2.0,
      "detail_workers": 4,
      "detail_rate": 2.0
    }'::jsonb
where source = 'mmreality';
