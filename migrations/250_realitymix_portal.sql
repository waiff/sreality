-- 250_realitymix_portal.sql
--
-- Bring realitymix.cz live as a full scraper portal (Phase 4 framework).
--
-- realitymix.cz is a Centrum.cz agency-feed AGGREGATOR served as STRUCTURED
-- server-rendered HTML, like idnes/ceskereality: each detail page carries a
-- schema.org BreadcrumbList JSON-LD (the category path — the drain's category
-- source, since the detail URL /detail/{obec}/{slug}-{id}.html does NOT encode
-- it), a <li class="detail-information__data-item"> spec list, precise per-listing
-- coordinates + a structured street (<div id="print-map" data-gps-lat/-lon
-- data-address>), an st.realitymix.cz image gallery, and a stable broker/agency
-- identity. Per-category search pages (/reality/{family}/{sale}) carry a result
-- total ("z celkem N nalezených") with offset paging (?stranka=N) and NO deep-
-- pagination cap, so a per-category walk is provable-complete →
-- supports_complete_walk=true and the runner marks delisted listings inactive
-- under the completeness guard (rule #3), source-scoped (rule #15). Coordinates
-- come from the page, so no MAPY_CZ_API_KEY is needed.
--
-- Coverage: byty + domy + chaty + pozemky + komerce + ostatni, both offer types
-- (≈48k listings, ~2400 index pages — idnes-scale, so the workflow is cadence-
-- split). It is an aggregator, so it overlaps heavily with sreality/idnes; the
-- dedup engine merges the overlap (street + disposition + precise coords + the
-- free same-photo pHash fast-path are all available). 6h cadence matches the
-- scheduled pilot; the workflow ships with the cron live but the operator can
-- flip is_enabled / tune limits in SQL without a workflow edit.
--
-- Purely additive: an idempotent upsert (the row may already exist from a prior
-- on-conflict re-run).

insert into portals
  (source, label, kind, home_url, sort_order, is_enabled,
   supports_complete_walk, categories, split_threshold,
   scrape_cadence_minutes, operational_limits)
values
  ('realitymix', 'RealityMix', 'scraper',
   'https://realitymix.cz', 31, true,
   true,
   '[
     {"sale_type": "prodej",   "category": "byty"},
     {"sale_type": "pronajem", "category": "byty"},
     {"sale_type": "prodej",   "category": "domy"},
     {"sale_type": "pronajem", "category": "domy"},
     {"sale_type": "prodej",   "category": "chaty"},
     {"sale_type": "pronajem", "category": "chaty"},
     {"sale_type": "prodej",   "category": "pozemky"},
     {"sale_type": "pronajem", "category": "pozemky"},
     {"sale_type": "prodej",   "category": "komerce"},
     {"sale_type": "pronajem", "category": "komerce"},
     {"sale_type": "prodej",   "category": "ostatni"},
     {"sale_type": "pronajem", "category": "ostatni"}
   ]'::jsonb,
   null,
   360,
   '{
     "index_rate": 1.0,
     "detail_workers": 4,
     "detail_rate": 1.5,
     "max_detail_per_run": 2000
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
