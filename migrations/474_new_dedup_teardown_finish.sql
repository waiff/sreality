-- 474_new_dedup_teardown_finish.sql
-- NEW DEDUP Wave 0 (PR-3) — finish the teardown docs/design/new-dedup/CUTOFF.md §4 specifies.
--
-- WHY THIS EXISTS. Wave 0 was recorded closed on 2026-08-25. It was not. Migration 432
-- dropped the two funnel/cost matviews and their `_public` views and unscheduled one cron
-- job; every other object in CUTOFF §4, and the whole of §3 step 2 (the publication gate),
-- was left standing. A 2026-09-05 program review found them still live. This migration is
-- the rest of that one runbook step — no new decision, no new rule, nothing added to the
-- design. The corrected history is in docs/design/new-dedup/PROGRAM.md.
--
-- WHAT IS BEING REMOVED. The legacy dedup DECISION ENGINE's private state: the candidate
-- queue and its archive, the dirty-set and scan cursors it swept with, the LLM batch
-- bookkeeping, the per-run ledger, the six admin views over them, and one unused partial
-- index on the hot `listings` table. The engine's CODE was deleted in PR-1/PR-2; nothing in
-- scraper/, toolkit/, api/, frontend/, location_data/ or chrome-extension/ reads any of
-- these (verified by grep, 2026-09-05) and `listings_dedup_eligible_idx` shows idx_scan = 0.
--
-- Live row counts at drop time, all captured to R2 by scripts/backup_new_dedup_teardown_tables.py
-- under backups/new-dedup-teardown/2026-09-05/ (the destructive-migration gate, CLAUDE.md rule 1):
--   property_identity_candidates         159,260      dedup_batches                 265
--   property_identity_candidates_archive   5,542      dedup_batch_requests       18,250
--   dedup_dirty_properties                15,357      dedup_engine_runs           9,932
--   dedup_scan_state                           3      (112 MB reclaimed in total)
--
-- WHAT IS DELIBERATELY KEPT, and must stay kept: dedup_pair_audit (the legacy decision
-- ledger), dedup_decision_feedback, dedup_golden_*, dedup_vision_bakeoff_*,
-- dedup_model_compare_sets, listing_visual_matches, listing_site_plan_matches,
-- listing_floor_plan_matches, image_room_classifications, listing_image_comparisons and
-- dedup_funnel_resolutions_archive. Those are FROZEN, not dead: paid verdict caches and the
-- historical record. The blocking keys street_name_key / street_source / geo_cell_key stay
-- too — the new Level 0 reuses street_name_key.
--
-- THE PUBLICATION GATE (CUTOFF §3). Since migration 273 a new property stayed invisible in
-- Browse/map/stats/watchdogs until something stamped `published_at`, and the only stamper for
-- an ordinary new property was the engine being removed. Step 1 of the removal (M-0) set
-- `dedup_publication_gate_enabled = false` back in August, so the gate has been INERT ever
-- since — every property is already visible and this migration changes no row's visibility.
-- What is left is step 2: taking the now-dead predicate out of the three views that still
-- carry it, then dropping the function and its health view. The predicate is identical in all
-- three (`and (not (select publication_gate_enabled()) or <x>.published_at is not null)`) and
-- its removal is the ONLY edit to each definition — the select lists below are migration 425's
-- verbatim, re-checked column-for-column against the live views before this file was written.
--
-- `properties.published_at` and `publish_reason` are KEPT (CUTOFF §3 step 5): dropping them
-- would force wide view churn across every read model for no benefit, and they are a
-- historical record of what the old engine published and why.
--
-- THE LEGACY STAMP (CUTOFF §4, decision Q1/Q4). `property_merge_events.generation` marks
-- every merge made before the rebuild as 'legacy'; the future production engine (Wave 8)
-- writes 'v2'. DB-only, no UI badge. The column is nullable with no default on purpose —
-- 'legacy' is a statement about the 124,363 rows that exist TODAY, not a value new rows
-- should inherit by accident.

set lock_timeout = '5s';  -- listings is hot; lose the race and abort cleanly rather than queue

-- ---------------------------------------------------------------- 1. views over the queues

-- Ordered before the tables: dedup_label_events / dedup_queue_snapshot_public /
-- dedup_recency_backlog read property_identity_candidates, the two flow views read
-- dedup_engine_runs, dedup_scan_state_public reads dedup_scan_state. No CASCADE anywhere in
-- this file — if some object outside the audited set depends on one of these, the migration
-- must fail loudly instead of quietly taking that object with it.
drop view if exists dedup_label_events;
drop view if exists dedup_queue_snapshot_public;
drop view if exists dedup_recency_backlog;
drop view if exists dedup_engine_flow_public;
drop view if exists dedup_engine_runs_public;
drop view if exists dedup_scan_state_public;

-- ------------------------------------------------------------------------- 2. the queues

-- One statement, all seven: Postgres resolves the foreign keys among them (batch requests ->
-- batches) when they are dropped together, so no drop order has to be maintained by hand.
drop table if exists
  property_identity_candidates,
  property_identity_candidates_archive,
  dedup_dirty_properties,
  dedup_scan_state,
  dedup_batches,
  dedup_batch_requests,
  dedup_engine_runs;

-- ------------------------------------------------------- 3. the eligibility index (mig 127)

-- Partial index built to find listings the engine should consider. idx_scan = 0 in
-- pg_stat_user_indexes; 6 MB of write amplification on every listings INSERT/UPDATE for a
-- reader that no longer exists.
drop index if exists listings_dedup_eligible_idx;

-- ------------------------------------------------------------------- 4. the legacy stamp

alter table property_merge_events add column if not exists generation text;
update property_merge_events set generation = 'legacy' where generation is null;
comment on column property_merge_events.generation is
  'Which dedup engine made this merge: ''legacy'' = the pre-2026-08 engine removed in NEW DEDUP '
  'Wave 0 (backfilled by migration 474), ''v2'' = the rebuilt engine from Wave 8 onward. NULL '
  'means an operator merge made in between. No default: ''legacy'' is a fact about rows that '
  'existed on 2026-09-05, not a value new rows should inherit.';

-- --------------------------------------------------- 5. the publication gate leaves the views

-- Each of the three is migration 425's definition with the gate line removed and nothing else
-- touched. CREATE OR REPLACE (not drop/create) so the dependents ride through untouched:
-- properties_map_mv reads browse_projection, pipeline_board_public reads properties_public.
-- Postgres refuses a REPLACE that changes the output columns, which makes it its own check
-- that these select lists really are unchanged. Grants are preserved by REPLACE and re-stated
-- below anyway, matching 425 line for line.

create or replace view properties_public as
select
    p.id as property_id,
    p.repr_listing_id as sreality_id,
    p.first_seen_at,
    p.last_seen_at,
    p.is_active,
    p.category_main,
    p.category_type,
    p.current_price_czk as price_czk,
    l.price_unit,
    p.area_m2,
    p.disposition,
    p.locality,
    p.district,
    p.locality_district_id,
    p.locality_region_id,
    p.lat,
    p.lng,
    l.floor,
    l.total_floors,
    p.has_balcony,
    p.has_parking,
    p.has_lift,
    p.building_type,
    p.condition,
    p.energy_rating,
    p.estate_area,
    p.usable_area,
    p.garden_area,
    p.category_sub_cb,
    p.furnished,
    p.terrace,
    p.cellar,
    p.garage,
    p.parking_lots,
    p.ownership,
    l.broker_name,
    null::text as broker_email,
    null::text as broker_phone,
    case
        when p.is_active then greatest(0, floor(extract(epoch from now() - p.first_seen_at) / 86400::numeric)::integer)
        else greatest(0, floor(extract(epoch from p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
    end as tom_days,
    measure_price_per_m2(p.current_price_czk::numeric, p.area_m2, p.category_main, p.category_type) as price_per_m2,
    p.building_condition_level,
    p.apartment_condition_level,
    l.description,
    p.source_count,
    p.distinct_site_count,
    p.price_drop_count,
    p.price_rise_count,
    p.max_price_drop_pct,
    p.stats_computed_at,
    p.source,
    coalesce(p.street, l.street) as street,
    p.mf_reference_rent_czk,
    p.mf_gross_yield_pct,
    p.obec,
    p.okres,
    p.region,
    p.home_obec_pop,
    p.near_pop_5km,
    p.near_pop_15km,
    p.near_jobs_5km,
    p.near_jobs_15km,
    p.near_youth_5km,
    p.near_youth_15km,
    p.near_overall_5km,
    p.near_overall_15km,
    p.subtype,
    p.last_change_at,
    p.obec_id,
    p.okres_id,
    p.region_id,
    p.price_change_count,
    p.price_change_count_30d,
    p.price_change_count_90d,
    p.price_change_count_365d,
    p.total_price_change_pct,
    concat_ws(', '::text, p.street, p.locality) as place_search_text,
    p.asset_id,
    p.mf_reference_rent,
    p.published_at,
    p.repr_listing_ref_id as listing_id,
    l.source_id_native,
    p.home_city_id,
    measure_price_per_m2_basis(p.category_main, p.category_type) as price_per_m2_basis
from properties p
     left join listings l on l.id = p.repr_listing_ref_id
where p.status = 'active'::text;

revoke all on properties_public from anon;
grant select on properties_public to authenticated;
create or replace view browse_projection as
select
    id as property_id,
    repr_listing_id as sreality_id,
    first_seen_at,
    last_seen_at,
    is_active,
    category_main,
    category_type,
    current_price_czk as price_czk,
    area_m2,
    disposition,
    locality,
    district,
    locality_district_id,
    locality_region_id,
    lat,
    lng,
    has_balcony,
    has_parking,
    has_lift,
    building_type,
    condition,
    energy_rating,
    estate_area,
    usable_area,
    garden_area,
    category_sub_cb,
    furnished,
    terrace,
    cellar,
    garage,
    parking_lots,
    ownership,
    case
        when is_active then greatest(0, floor(extract(epoch from now() - first_seen_at) / 86400::numeric)::integer)
        else greatest(0, floor(extract(epoch from last_seen_at - first_seen_at) / 86400::numeric)::integer)
    end as tom_days,
    measure_price_per_m2(current_price_czk::numeric, area_m2, category_main, category_type) as price_per_m2,
    building_condition_level,
    apartment_condition_level,
    source,
    street,
    mf_reference_rent_czk,
    mf_gross_yield_pct,
    obec,
    okres,
    region,
    home_obec_pop,
    near_pop_5km,
    near_pop_15km,
    near_jobs_5km,
    near_jobs_15km,
    near_youth_5km,
    near_youth_15km,
    near_overall_5km,
    near_overall_15km,
    subtype,
    last_change_at,
    obec_id,
    okres_id,
    region_id,
    price_change_count,
    price_change_count_30d,
    price_change_count_90d,
    price_change_count_365d,
    total_price_change_pct,
    concat_ws(', '::text, street, locality) as place_search_text,
    asset_id,
    repr_listing_ref_id as listing_id,
    (select l.source_id_native from listings l where l.id = p.repr_listing_ref_id) as source_id_native,
    all_sources,
    active_sources,
    home_city_id,
    measure_price_per_m2_basis(category_main, category_type) as price_per_m2_basis
from properties p
where status = 'active'::text;

revoke all on browse_projection from anon;
grant select on browse_projection to authenticated;
create or replace view listing_feed_public as
select
  l.id,
  l.source,
  l.source_id_native,
  l.sreality_id,                          -- legacy compat only; NULL post-Gate-2 for non-sreality
  l.category_main,
  l.category_type,
  l.category_sub_cb,
  l.subtype,
  l.disposition,
  l.price_czk,
  l.price_unit,
  l.area_m2,
  l.locality,
  l.district,
  l.obec,
  l.okres,
  l.region,
  l.obec_id,
  l.okres_id,
  l.region_id,
  st_y(l.geom::geometry) as lat,
  st_x(l.geom::geometry) as lng,
  l.floor,
  l.total_floors,
  l.has_balcony,
  l.has_parking,
  l.has_lift,
  l.building_type,
  l.condition,
  l.energy_rating,
  l.estate_area,
  l.usable_area,
  l.garden_area,
  l.furnished,
  l.terrace,
  l.cellar,
  l.garage,
  l.parking_lots,
  l.ownership,
  l.building_condition_level,
  l.apartment_condition_level,
  l.description,
  l.street,
  l.house_number,
  l.is_active,
  l.first_seen_at,
  l.last_seen_at,
  l.published_at,
  l.discovery_seq,
  case when l.source in ('bazos', 'ceskereality') then l.published_at end as portal_date,
  -- Qualified `l.` (369 left these bare): the `properties` join added below
  -- brings its own area_m2/price_czk into scope, so the unqualified form is now
  -- ambiguous. Same expression, same output column — only the resolution is pinned.
  measure_price_per_m2(l.price_czk::numeric, l.area_m2::numeric, l.category_main, l.category_type) as price_per_m2,
  -- ---- appended by migration 370 ----
  -- `listing_id` is a deliberate alias of `id`, not redundancy: every Browse
  -- SELECT list, the React key, the maplibre feature-state id, the hover-sync
  -- set and the card-image batch all key on `listing_id` because that is what
  -- browse_list / properties_map_mv call the same surrogate (migration 343).
  -- Aliasing it here means the frontend's column contract is IDENTICAL across
  -- both read models and the single-portal swap touches only the relation name.
  l.id as listing_id,
  -- Carried so operator curation (tags, collections, pipeline, merge) keeps
  -- working from a mirrored card, and as the detail-link fallback. NULLABLE by
  -- rule #19 (a freshly drained listing is attached out-of-band), which is why
  -- it must NOT be the keyset tiebreak in this mode — `listing_id` is.
  l.property_id,
  (
    lpad(
      greatest(0, floor(extract(epoch from
        (case when l.source in ('bazos', 'ceskereality') then l.published_at end)
          at time zone 'UTC'
      )))::bigint::text,
      12, '0'
    )
    || lpad(coalesce(l.discovery_seq, 0)::text, 19, '0')
  ) collate "C" as portal_sort_key,
  -- Listing-grain equivalents of two browse_projection expressions. Both are
  -- pure functions of columns already on the row, so they are exact at this
  -- grain rather than the property-grain approximation browse_list carries.
  concat_ws(', '::text, l.street, l.locality) as place_search_text,
  case when l.is_active
       then greatest(0, floor(extract(epoch from now() - l.first_seen_at) / 86400::numeric)::integer)
       else greatest(0, floor(extract(epoch from l.last_seen_at - l.first_seen_at) / 86400::numeric)::integer)
  end as tom_days,
  l.mf_gross_yield_pct,
  -- Property-grain precomputes. These exist ONLY so the corresponding Browse
  -- filters keep working unchanged in single-portal mode; none of them is
  -- displayed on a card, so root cause 3's golden-record leakage (a DISPLAYED
  -- field lifted from a different portal's sibling listing) cannot occur
  -- through them. They are genuinely property-grain facts by construction:
  -- city proximity/population is a function of location, and the price-change
  -- and last-change series are maintained per property.
  p.last_change_at,
  p.home_obec_pop,
  p.near_pop_5km, p.near_pop_15km,
  p.near_jobs_5km, p.near_jobs_15km,
  p.near_youth_5km, p.near_youth_15km,
  p.near_overall_5km, p.near_overall_15km,
  p.price_change_count,
  p.price_change_count_30d,
  p.price_change_count_90d,
  p.price_change_count_365d,
  p.total_price_change_pct,
  measure_price_per_m2_basis(l.category_main, l.category_type) as price_per_m2_basis
from listings l
join properties p on p.id = l.property_id
where p.status = 'active';

revoke all on listing_feed_public from anon;
grant select on listing_feed_public to authenticated;

-- ------------------------------------------------- 6. the gate function and its health view

-- Ordered after the redefinitions above: while any view still called it, the drop would fail.
-- publication_gate_health_public counted unpublished properties for the Health page's gate
-- section, which PR-2 deleted; it reads `properties` and never called the function itself.
drop view if exists publication_gate_health_public;
drop function if exists publication_gate_enabled();

-- ---------------------------------------------------------------- 7. settings the engine owned

-- Every key here configured a decision path that no longer exists: the merge/dismiss switches
-- and thresholds, the batch warmer, the worker's dedup lane interval, and the four paid
-- vision comparators' model+prompt pairs (llm_visual_match / llm_site_plan_match /
-- llm_floor_plan_match / llm_room_classify). Their VERDICT CACHES stay — the tables listed in
-- the header — because the money is already spent; only the settings that would drive new
-- calls go. Deliberately NOT deleted: clip_tagging_priority_region_ids (the tagging lane keeps
-- running) and everything under the image_* / tag_* labeling programme.
--
-- CUTOFF §4 said "the four realtime_dedup_* worker keys"; only one still exists.
-- Literal names, never a LIKE pattern: `dedup_sim_*` (Wave 1's simulation schema) and any
-- future dedup key would match `dedup_%` and be swept away by a pattern.
delete from app_settings where key in (
  'dedup_auto_merge_enabled',
  'dedup_batch_warmer_enabled',
  'dedup_byt_geo_enabled',
  'dedup_clip_cosine_enabled',
  'dedup_cosine_haiku_min',
  'dedup_defer_incomplete_downloads',
  'dedup_facade_dismiss_enabled',
  'dedup_geo_area_max_pct',
  'dedup_geo_enabled',
  'dedup_nonbyt_attr_merge_enabled',
  'dedup_nonbyt_cosine_merge_min',
  'dedup_nonbyt_phash_single_enabled',
  'dedup_prefer_clip_tags',
  'dedup_publication_gate_enabled',
  'dedup_visual_match_model_haiku',
  'realtime_dedup_interval_seconds',
  'llm_visual_match_model',
  'llm_visual_match_prompt',
  'llm_site_plan_match_model',
  'llm_site_plan_match_prompt',
  'llm_floor_plan_match_model',
  'llm_floor_plan_match_prompt',
  'llm_room_classify_model',
  'llm_room_classify_prompt'
);

-- pipeline_check_thresholds is a LIVE key (scripts/verify_pipeline.py, toolkit/ops_incidents.py,
-- toolkit/system_alerts.py read it and merge it over their code defaults), so the row stays and
-- only the eleven dedup-engine checks come out of it. Each of the eleven has zero references
-- left anywhere in the repo; the six that remain — llm_error_rate_warn, llm_spend_24h_warn_usd,
-- ops_incident_log_excerpt_bytes, ops_incident_max_age_hours, ops_incident_min_failures,
-- verification_stale_hours — are still code defaults and must survive this.
update app_settings
   set value = value
         - 'candidate_age_p95_warn_days'   -- age of the candidate queue that is now gone
         - 'cycle_age_fail_hours'          -- engine sweep cycle
         - 'dirty_age_p95_warn_hours'      -- dedup_dirty_properties backlog
         - 'geo_debt_area_pct'
         - 'geo_debt_price_pct'
         - 'merge_p95_warn_hours'          -- automatic merge latency
         - 'precision_sample_n'            -- dedup precision sampling
         - 'street_debt_fail'
         - 'street_debt_price_pct'
         - 'street_debt_warn'
         - 'unpublished_overdue_fail'      -- the publication gate's own check
     , updated_at = now()
     , updated_by = 'migration_474'
 where key = 'pipeline_check_thresholds';

reset lock_timeout;
