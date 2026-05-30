-- 116_maxima_portal.sql
--
-- Register nemovitosti.maxima.cz as a scraper portal (Phase 4 portal framework).
-- Maxima is a single real-estate agency that publishes its whole catalogue
-- (~220 listings) as one server-rendered WordPress index (no JSON API, no
-- per-category URL). It lands in the same listings/listing_snapshots contract as
-- bazos/idnes/bezrealitky, tagged source='maxima', via its own fetcher
-- (scraper/maxima_client.py) + parser (maxima_parser.py) + the shared portal_runner.
--
-- Purely additive: one INSERT carrying the operational config (migration 107
-- columns) + per-portal limits (114) + the 6h pilot cadence (114). A PILOT, so
-- supports_complete_walk=false (the runner never marks listings inactive from
-- index-absence — architectural rule #3). The whole-catalogue walk IS complete
-- (the index reports a total), so promotion to complete-walk + a delisting sweep
-- is a deliberate later migration (as bazos got in 113) once the pilot is proven.
-- categories carries the single mixed-catalogue descriptor; the parser derives
-- each listing's category from its native-id prefix + title verb, so no
-- per-category encoding is needed. ON CONFLICT keeps it idempotent.

insert into portals
  (source, label, kind, stage, home_url, sort_order,
   supports_complete_walk, categories, split_threshold,
   scrape_cadence_minutes, operational_limits)
values
  ('maxima', 'Maxima Reality', 'scraper', 'pilot',
   'https://nemovitosti.maxima.cz', 26,
   false,
   '[{"label": "all"}]'::jsonb,
   null,
   360,
   '{
      "index_rate": 1.0,
      "detail_workers": 2,
      "detail_rate": 1.0,
      "min_completeness": 0.9
    }'::jsonb)
on conflict (source) do nothing;
