-- 117_mmreality_portal.sql
--
-- Register mmreality.cz as a scraper portal (Phase 4 portal framework). Adds the
-- 'mmreality' source's Health-dashboard row + operational config, parallel to
-- bazos / idnes / bezrealitky. Its listings.source / scrape_runs.source key is
-- 'mmreality'.
--
-- M&M Reality is server-rendered HTML, but every detail page embeds a COMPLETE
-- structured estate object (a Vue `:property` prop), so the parser decodes JSON
-- rather than scraping markup — precise coordinates, typed condition/construction
-- /ownership, normalised to the canonical cross-portal labels.
--
-- A single MIXED-category index (`/nemovitosti/?page=N`, no per-category slice),
-- so it can't be gated per-(category_main, category_type) the way the
-- source-scoped mark_inactive requires: supports_complete_walk=false (the bazos
-- posture — the runner never flips its listings inactive from index absence,
-- architectural rule #3). One category descriptor walks everything; each
-- listing's real category is read from its own detail JSON.
--
-- Purely additive: one INSERT carrying the operational config (migration 107
-- columns) + cadence (114) + operational_limits (115). ON CONFLICT keeps it
-- idempotent. Pilot cadence (6h), so the cadence-aware Health thresholds scale
-- liveness/freshness to match (migration 114).

insert into portals
  (source, label, kind, stage, home_url, sort_order,
   supports_complete_walk, categories, split_threshold,
   scrape_cadence_minutes, operational_limits)
values
  ('mmreality', 'M&M Reality', 'scraper', 'pilot', 'https://www.mmreality.cz', 35,
   false,
   '[{"index": "nemovitosti"}]'::jsonb,
   null,
   360,
   '{"index_rate": 1.0, "detail_workers": 4, "detail_rate": 2.0}'::jsonb)
on conflict (source) do nothing;
