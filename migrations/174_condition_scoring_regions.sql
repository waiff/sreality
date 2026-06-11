-- 174_condition_scoring_regions.sql
--
-- Condition scoring: operator-scoped kraje + permanent cross-portal score reuse.
--
-- 1. `listings.condition_levels_propagated_from` — provenance marker for
--    propagated condition levels. Non-NULL means this listing's
--    building_condition_level / apartment_condition_level were COPIED from
--    that sibling listing (same property_id, the one that actually paid the
--    LLM). Cleared (set back to NULL) the moment the listing earns its own
--    genuine score, so a genuine score is never mistaken for a copy.
--    `toolkit.condition_scoring.propagate_condition_levels` is the only
--    writer of non-NULL values.
--
-- 2. Seed `app_settings.condition_scoring_enabled_region_ids` — the
--    Settings-editable list of admin_boundaries kraj ids the scorer targets.
--    Insert-only (ON CONFLICT DO NOTHING) so a later operator edit survives
--    a re-run. Seed = Středočeský (27), Plzeňský (43), Královéhradecký (86),
--    Pardubický (94), Kraj Vysočina (108).

alter table listings
  add column condition_levels_propagated_from bigint;

comment on column listings.condition_levels_propagated_from is
  'Provenance for propagated condition levels: non-NULL = building_condition_level / apartment_condition_level were copied from this sibling listing (same property_id) by propagate_condition_levels, not scored by the LLM for this row. NULL = own genuine score (or not yet scored). Cleared when the listing earns its own score.';

insert into app_settings (key, value, description)
values (
  'condition_scoring_enabled_region_ids',
  '[27,43,86,94,108]'::jsonb,
  'admin_boundaries kraj ids whose active listings the condition scorer targets (backfill, batch submit, and the LLM health check all read this list). Listings outside these kraje — or with region_id NULL — are parked, not selected. Empty array pauses condition scoring entirely. Edited from the Settings page.'
)
on conflict (key) do nothing;
