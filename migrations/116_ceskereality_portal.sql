-- 116_ceskereality_portal.sql
--
-- Register ceskereality.cz as a scraper portal (Phase 4 portal framework).
-- ceskereality is a server-rendered HTML portal (no public JSON listings API),
-- STRUCTURED like idnes: each detail page carries a schema.org JSON-LD product
-- block (clean price + broker), an i-info spec list, precise per-listing
-- coordinates (data-coord-lat/lng — no geocode needed), and a full image gallery.
--
-- Complete-walk capable: search pages report a result total ("Máme tady N…") and
-- have no deep-pagination cap (deep pages return real listings; the tail is
-- genuinely empty), so a per-category walk is provable-complete and the runner
-- marks delisted listings inactive under the completeness guard (rule #3),
-- source-scoped (rule #15). The detail URL carries the category (/{sale}/{cat}/…),
-- so the drain derives each listing's category from its own URL — one config
-- walks many categories.
--
-- Pilot scope: byty + chaty-chalupy + komerční-prostory + ostatní, both sale
-- types (~26k listings). Polite operational limits (the site disallows generic
-- bots in robots.txt, so we crawl slowly: index 0.7 req/s, 2 detail workers at
-- 0.7 req/s). No scheduled workflow ships in this change — the scrape workflow is
-- DISPATCH-ONLY for an initial validation run; a follow-up adds the cron.
--
-- Purely additive: one INSERT carrying the operational config (migrations
-- 107/114/115 columns). ON CONFLICT keeps it idempotent.

insert into portals
  (source, label, kind, stage, home_url, sort_order,
   supports_complete_walk, categories, split_threshold,
   scrape_cadence_minutes, operational_limits)
values
  ('ceskereality', 'Českéreality', 'scraper', 'pilot',
   'https://www.ceskereality.cz', 27,
   true,
   '[
     {"sale_type": "prodej",   "category": "byty"},
     {"sale_type": "pronajem", "category": "byty"},
     {"sale_type": "prodej",   "category": "chaty-chalupy"},
     {"sale_type": "pronajem", "category": "chaty-chalupy"},
     {"sale_type": "prodej",   "category": "komercni-prostory"},
     {"sale_type": "pronajem", "category": "komercni-prostory"},
     {"sale_type": "prodej",   "category": "ostatni"},
     {"sale_type": "pronajem", "category": "ostatni"}
   ]'::jsonb,
   null,
   360,
   '{
     "index_rate": 0.7,
     "detail_workers": 2,
     "detail_rate": 0.7,
     "max_detail_per_run": 1500,
     "min_completeness": 0.9
   }'::jsonb)
on conflict (source) do nothing;
