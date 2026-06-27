-- 249_ceskereality_portal_live.sql
--
-- Bring ceskereality.cz live as a full scraper portal (Phase 4 framework).
--
-- History: a `ceskereality` portals row was seeded into prod by the unmerged
-- branch feature/ceskereality-scraper (its migration 116 collided with main's
-- 116_maxima_portal and never landed), then DISABLED by migration 173 because no
-- scraper code existed on main. This migration revives it: the scraper code now
-- lands (scraper/ceskereality_{client,parser,main}.py + portal.py config), so we
-- RE-ENABLE the row and set its FULL operational config.
--
-- ceskereality is a STRUCTURED HTML portal, like idnes: each detail page carries
-- a schema.org JSON-LD product block (clean price + a stable broker identity from
-- the /realitni-makleri/…-{id}/ contact anchor), an i-info spec list, precise
-- per-listing coordinates (data-coord-lat/lng — no geocode), and an image gallery.
-- Per-category search pages carry a result total ("Máme tady N…") with no deep-
-- pagination cap, so a per-category walk is provable-complete → supports_complete_
-- walk=true and the runner marks delisted listings inactive under the completeness
-- guard (rule #3), source-scoped (rule #15). The detail URL carries the category,
-- so the drain derives each listing's category from its own URL — one config walks
-- all 12 (cm × ct) descriptors.
--
-- Coverage: byty + rodinné domy + chaty-chalupy + pozemky + komerční-prostory +
-- ostatní, both sale types (the branch's original config omitted domy + pozemky).
-- Polite operational limits — the site disallows generic bots in robots.txt, so
-- the client crawls slowly (≈0.7 req/s, 2 detail workers) with an honest UA.
--
-- Purely additive: an idempotent upsert that RE-ENABLES + overwrites the
-- operational config (the row already exists from migration 173's era). 6h cadence
-- matches the scheduled pilot; the workflow ships dispatch-only until a validation
-- run confirms the runners aren't anti-bot-blocked, then gains a cron.

insert into portals
  (source, label, kind, home_url, sort_order, is_enabled,
   supports_complete_walk, categories, split_threshold,
   scrape_cadence_minutes, operational_limits)
values
  ('ceskereality', 'Českéreality', 'scraper',
   'https://www.ceskereality.cz', 27, true,
   true,
   '[
     {"sale_type": "prodej",   "category": "byty"},
     {"sale_type": "pronajem", "category": "byty"},
     {"sale_type": "prodej",   "category": "rodinne-domy"},
     {"sale_type": "pronajem", "category": "rodinne-domy"},
     {"sale_type": "prodej",   "category": "chaty-chalupy"},
     {"sale_type": "pronajem", "category": "chaty-chalupy"},
     {"sale_type": "prodej",   "category": "pozemky"},
     {"sale_type": "pronajem", "category": "pozemky"},
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
     "max_detail_per_run": 1500
   }'::jsonb)
on conflict (source) do update set
  label                  = excluded.label,
  kind                   = excluded.kind,
  home_url               = excluded.home_url,
  sort_order             = excluded.sort_order,
  is_enabled             = excluded.is_enabled,
  supports_complete_walk = excluded.supports_complete_walk,
  categories             = excluded.categories,
  split_threshold        = excluded.split_threshold,
  scrape_cadence_minutes = excluded.scrape_cadence_minutes,
  operational_limits     = excluded.operational_limits;
