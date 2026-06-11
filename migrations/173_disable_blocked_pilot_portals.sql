-- Disable the two pilot portal registry rows that cannot produce data today.
--
-- mmreality: the scraper code + selectors are correct (verified from a
-- residential IP), but Cloudflare 403-blocks GitHub-hosted runner IPs, so the
-- portal has produced ZERO listings since its first run. The 6h cron is
-- removed from scrape_mmreality.yml; workflow_dispatch is kept for a retry
-- once non-datacenter egress exists. Re-enable the row together with the cron.
--
-- ceskereality: registry row seeded into prod by an applied-but-untracked
-- migration (116_ceskereality_portal) from the unmerged branch
-- feature/ceskereality-scraper, whose number collides with main's
-- 116_maxima_portal. No scraper code exists on main. If that branch is
-- revived it needs renumbering plus an explicit re-enable.
--
-- is_enabled is read only by the health surfaces (migrations 100/136/169) and
-- the /admin/portals display, so this flip removes the dead Health lanes and
-- changes no scraping behavior.

UPDATE portals
SET is_enabled = false
WHERE source IN ('mmreality', 'ceskereality');
