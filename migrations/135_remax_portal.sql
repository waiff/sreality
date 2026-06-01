-- 135_remax_portal.sql
--
-- Promote remax-czech.cz from an on-demand URL parser to a scheduled scraper
-- portal (Phase 4 portal framework). RE/MAX is a national franchise catalogue
-- (~7,900 listings) served as a single server-rendered search index (no JSON
-- API, no per-category URL) split only by an offer-type flag (sale=1 prodej /
-- sale=2 pronájem). It lands in the same listings/listing_snapshots contract as
-- bazos/idnes/bezrealitky/mmreality/maxima, tagged source='remax', via its own
-- fetcher (scraper/remax_client.py) + parser (remax_parser.py) + the shared
-- portal_runner.
--
-- The 'remax' source already had a portals row as a 'parser' (kind='parser',
-- stage='on_demand', migration 100) for the LLM URL-parser the estimation
-- preview uses (scraper/source_parsers/remax.py, source_kind='remax'). That
-- on-demand path is UNCHANGED and keeps working — it routes by domain in
-- source_dispatcher, independent of this row's `kind`. This migration converts
-- the single 'remax' row to a scraper so the Health dashboard tracks the
-- scheduled crawl; its on-demand parse counts still join on the same key, so the
-- card surfaces both facets.
--
-- INSERT ... ON CONFLICT DO UPDATE so this is idempotent and also seeds a fresh
-- rebuild (where the parser row exists from migration 100). Purely additive to
-- schema (operational columns from migrations 107/114/115).
--
-- A PILOT: supports_complete_walk=false, so the runner never marks listings
-- inactive from index-absence (architectural rule #3) — remax reports a per-
-- AGENDA total and the per-category slice is title-derived, so a safe per-(cm,ct)
-- completeness check isn't available. A gone detail (404/410 or a redirect off
-- the detail path) still flips that one listing inactive. Promotion to a
-- complete-walk delisting sweep is a deliberate later migration once proven.
--
-- categories are per (category_main, category_type, sale): each descriptor walks
-- (or reuses, via the agenda cache) its offer-type's mixed index and keeps the
-- title-derived slice for its category. 6h pilot cadence (the cadence-aware
-- Health thresholds scale liveness/freshness to match, migration 114).

insert into portals
  (source, label, kind, stage, home_url, sort_order, is_enabled,
   supports_complete_walk, categories, split_threshold,
   scrape_cadence_minutes, operational_limits)
values
  ('remax', 'RE/MAX', 'scraper', 'pilot', 'https://www.remax-czech.cz', 28, true,
   false,
   '[
      {"category_main": "byt",      "category_type": "prodej",   "sale": 1},
      {"category_main": "dum",      "category_type": "prodej",   "sale": 1},
      {"category_main": "pozemek",  "category_type": "prodej",   "sale": 1},
      {"category_main": "komercni", "category_type": "prodej",   "sale": 1},
      {"category_main": "ostatni",  "category_type": "prodej",   "sale": 1},
      {"category_main": "byt",      "category_type": "pronajem", "sale": 2},
      {"category_main": "dum",      "category_type": "pronajem", "sale": 2},
      {"category_main": "pozemek",  "category_type": "pronajem", "sale": 2},
      {"category_main": "komercni", "category_type": "pronajem", "sale": 2},
      {"category_main": "ostatni",  "category_type": "pronajem", "sale": 2}
    ]'::jsonb,
   null,
   360,
   '{"index_rate": 1.0, "detail_workers": 4, "detail_rate": 2.0, "max_detail_per_run": 3000}'::jsonb)
on conflict (source) do update set
  label                  = excluded.label,
  kind                   = excluded.kind,
  stage                  = excluded.stage,
  home_url               = excluded.home_url,
  sort_order             = excluded.sort_order,
  is_enabled             = excluded.is_enabled,
  supports_complete_walk = excluded.supports_complete_walk,
  categories             = excluded.categories,
  split_threshold        = excluded.split_threshold,
  scrape_cadence_minutes = excluded.scrape_cadence_minutes,
  operational_limits     = excluded.operational_limits;
