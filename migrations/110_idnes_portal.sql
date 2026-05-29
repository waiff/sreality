-- 110_idnes_portal.sql
--
-- Register reality.idnes.cz as a scraper portal (Phase 4 portal framework).
-- iDNES already had an on-demand URL PARSER row ('idnes_reality', migration 100)
-- for pasting a single listing URL; this adds the CRAWLER portal ('idnes') — its
-- own listings.source / scrape_runs.source key and Health-dashboard row, parallel
-- to bazos. The two rows reflect two real capabilities over the same site (an
-- on-demand parser AND a scheduled crawler).
--
-- Purely additive: one INSERT carrying the operational config (migration 107
-- columns). A partial HTML crawl, so supports_complete_walk=false (the runner
-- never marks listings inactive — architectural rule #3) and no split_threshold.
-- ON CONFLICT keeps it idempotent if the row already exists.

insert into portals
  (source, label, kind, stage, home_url, sort_order,
   supports_complete_walk, categories, split_threshold)
values
  ('idnes', 'iDNES Reality', 'scraper', 'pilot', 'https://reality.idnes.cz', 25,
   false,
   '[{"sale_type": "prodej", "category": "byty"}]'::jsonb,
   null)
on conflict (source) do nothing;
