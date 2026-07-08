-- 282: read models for the dedup funnel (on /dedup) and the dedup LLM-cost
-- grouping/category breakdown (on /costs).
--
-- The two aggregating reads (pair-audit resolutions × category, vision-cache
-- cost × category) join listings per row over ~50k+ rows — ~24 s live on this
-- instance, far over the anon 3 s statement_timeout. They are therefore
-- MATERIALIZED views refreshed by pg_cron every 15 min (the health-matview
-- pattern); each embeds refreshed_at. The flow counters (dedup_engine_runs,
-- small) and the open-queue snapshot (must be live for the operator) stay
-- plain views.
--
-- Semantics the two pages must not blur (labels rely on this):
--   * dedup_pair_audit rows = terminal RESOLUTIONS (one per pair decision);
--     counts from it are distinct pairs. Queued pairs are NOT audited (the
--     candidate row is the record) and rule-C rejects exist only as run
--     counters.
--   * dedup_engine_runs counters = WORK (pair evaluations); full/dirty/
--     candidate runs re-scan the same groups, so sums over a window are
--     evaluation volume, never distinct pairs. `eligible` is a market
--     GAUGE — never summed; read from the latest full-scan row.
--   * costs come from the vision caches, whose rows link llm_call_id —
--     classification rows carry the call cost pre-split per image, the
--     pair caches are 1:1 with calls, so SUM(cost_usd) matches llm_calls.

create index if not exists dedup_pair_audit_run_at_idx
  on dedup_pair_audit (run_at desc);
create index if not exists listing_visual_matches_created_at_idx
  on listing_visual_matches (created_at desc);
create index if not exists listing_floor_plan_matches_created_at_idx
  on listing_floor_plan_matches (created_at desc);
create index if not exists listing_site_plan_matches_created_at_idx
  on listing_site_plan_matches (created_at desc);
create index if not exists image_room_classifications_created_at_idx
  on image_room_classifications (created_at desc);

drop view if exists dedup_funnel_resolutions_public;
drop view if exists dedup_engine_flow_public;
drop view if exists dedup_llm_cost_by_category_public;
drop view if exists dedup_queue_snapshot_public;
drop materialized view if exists dedup_funnel_resolutions_mv;
drop materialized view if exists dedup_llm_cost_by_category_mv;

-- 1) Terminal resolutions per funnel step × category (materialized).
create materialized view dedup_funnel_resolutions_mv as
select
  coalesce(a.source, 'engine') as source,
  a.stage,
  a.outcome,
  coalesce(a.category_main, 'ostatni') as category_main,
  case when l.category_type in ('prodej', 'pronajem')
       then l.category_type else 'ostatni' end as category_type,
  (count(distinct a.id) filter (where a.run_at >= now() - interval '7 days'))::int as pairs_7d,
  count(distinct a.id)::int as pairs_30d,
  (count(distinct s.property_id) filter (where a.run_at >= now() - interval '7 days'))::int as properties_7d,
  count(distinct s.property_id)::int as properties_30d,
  (count(distinct s.sreality_id) filter (where a.run_at >= now() - interval '7 days'))::int as listings_7d,
  count(distinct s.sreality_id)::int as listings_30d,
  now() as refreshed_at
from dedup_pair_audit a
left join listings l on l.sreality_id = a.left_sreality_id
cross join lateral (values
  (a.left_property_id,  a.left_sreality_id),
  (a.right_property_id, a.right_sreality_id)
) as s(property_id, sreality_id)
where a.run_at >= now() - interval '30 days'
group by 1, 2, 3, 4, 5
with no data;

create unique index dedup_funnel_resolutions_mv_key
  on dedup_funnel_resolutions_mv (source, stage, outcome, category_main, category_type);

create view dedup_funnel_resolutions_public as
select * from dedup_funnel_resolutions_mv;

grant select on dedup_funnel_resolutions_public to anon, authenticated;

-- 2) Engine flow counters (WORK, not distinct pairs). Live view; the
--    market gauges come from the latest full-scan row, never summed.
create view dedup_engine_flow_public as
with latest_full as (
  select eligible, flagged_location, flagged_disposition
  from dedup_engine_runs
  where eligible is not null
  order by id desc
  limit 1
), agg as (
  select
    count(*) filter (where started_at >= now() - interval '7 days')::int  as runs_7d,
    count(*)::int as runs_30d,
    coalesce(sum(pairs_considered) filter (where started_at >= now() - interval '7 days'), 0)::bigint as pairs_considered_7d,
    coalesce(sum(pairs_considered), 0)::bigint as pairs_considered_30d,
    coalesce(sum(rejected) filter (where started_at >= now() - interval '7 days'), 0)::bigint as rejected_7d,
    coalesce(sum(rejected), 0)::bigint as rejected_30d,
    coalesce(sum(queued) filter (where started_at >= now() - interval '7 days'), 0)::bigint as queued_7d,
    coalesce(sum(queued), 0)::bigint as queued_30d,
    coalesce(sum(clip_cosine_calls) filter (where started_at >= now() - interval '7 days'), 0)::bigint as clip_cosine_calls_7d,
    coalesce(sum(clip_cosine_calls), 0)::bigint as clip_cosine_calls_30d,
    coalesce(sum(routed_haiku) filter (where started_at >= now() - interval '7 days'), 0)::bigint as routed_haiku_7d,
    coalesce(sum(routed_haiku), 0)::bigint as routed_haiku_30d,
    coalesce(sum(routed_sonnet) filter (where started_at >= now() - interval '7 days'), 0)::bigint as routed_sonnet_7d,
    coalesce(sum(routed_sonnet), 0)::bigint as routed_sonnet_30d,
    coalesce(sum(floor_plan_deferred) filter (where started_at >= now() - interval '7 days'), 0)::bigint as floor_plan_deferred_7d,
    coalesce(sum(floor_plan_deferred), 0)::bigint as floor_plan_deferred_30d,
    coalesce(sum(clip_deferred) filter (where started_at >= now() - interval '7 days'), 0)::bigint as clip_deferred_7d,
    coalesce(sum(clip_deferred), 0)::bigint as clip_deferred_30d,
    coalesce(sum(skipped_unresolved) filter (where started_at >= now() - interval '7 days'), 0)::bigint as skipped_unresolved_7d,
    coalesce(sum(skipped_unresolved), 0)::bigint as skipped_unresolved_30d,
    coalesce(sum(vision_calls) filter (where started_at >= now() - interval '7 days'), 0)::bigint as vision_calls_7d,
    coalesce(sum(vision_calls), 0)::bigint as vision_calls_30d,
    coalesce(sum(vision_errors) filter (where started_at >= now() - interval '7 days'), 0)::bigint as vision_errors_7d,
    coalesce(sum(vision_errors), 0)::bigint as vision_errors_30d
  from dedup_engine_runs
  where started_at >= now() - interval '30 days'
)
select
  (select eligible from latest_full)            as eligible_market,
  (select flagged_location from latest_full)    as flagged_location_market,
  (select flagged_disposition from latest_full) as flagged_disposition_market,
  agg.*
from agg;

grant select on dedup_engine_flow_public to anon, authenticated;

-- 3) The open operator queue right now, by tier × category. Live (small).
create view dedup_queue_snapshot_public as
select
  c.tier,
  coalesce(cat.category_main, 'ostatni') as category_main,
  case when cat.category_type in ('prodej', 'pronajem')
       then cat.category_type else 'ostatni' end as category_type,
  count(*)::int as pairs
from property_identity_candidates c
left join lateral (
  select l.category_main, l.category_type
  from listings l
  where l.property_id = c.left_property_id
  order by l.sreality_id
  limit 1
) as cat on true
where c.status = 'proposed'
group by 1, 2, 3;

grant select on dedup_queue_snapshot_public to anon, authenticated;

-- 4) Dedup LLM spend attributed to category via the vision caches
--    (materialized). The /costs page shows (dedup group total − sum here)
--    as an explicit "unattributed" row, so the two tabs can never
--    silently disagree.
create materialized view dedup_llm_cost_by_category_mv as
with linked as (
  select 'compare_listings_visually'::text as called_for,
         v.created_at, v.llm_call_id, v.cost_usd,
         l.category_main, l.category_type, v.sreality_id_a as sreality_id
  from listing_visual_matches v
  left join listings l on l.sreality_id = v.sreality_id_a
  where v.created_at >= now() - interval '30 days'
  union all
  select 'compare_listing_floor_plans',
         f.created_at, f.llm_call_id, f.cost_usd,
         l.category_main, l.category_type, f.sreality_id_a
  from listing_floor_plan_matches f
  left join listings l on l.sreality_id = f.sreality_id_a
  where f.created_at >= now() - interval '30 days'
  union all
  select 'compare_listing_site_plans',
         sp.created_at, sp.llm_call_id, sp.cost_usd,
         l.category_main, l.category_type, sp.sreality_id_a
  from listing_site_plan_matches sp
  left join listings l on l.sreality_id = sp.sreality_id_a
  where sp.created_at >= now() - interval '30 days'
  union all
  select 'classify_listing_images',
         c.created_at, c.llm_call_id, c.cost_usd,
         l.category_main, l.category_type, i.sreality_id
  from image_room_classifications c
  join images i on i.id = c.image_id
  left join listings l on l.sreality_id = i.sreality_id
  where c.created_at >= now() - interval '30 days'
)
select
  k.called_for,
  coalesce(k.category_main, 'ostatni') as category_main,
  case when k.category_type in ('prodej', 'pronajem')
       then k.category_type else 'ostatni' end as category_type,
  (count(distinct k.llm_call_id) filter (where k.created_at >= now() - interval '7 days'))::int as calls_7d,
  count(distinct k.llm_call_id)::int as calls_30d,
  round(coalesce(sum(k.cost_usd) filter (where k.created_at >= now() - interval '7 days'), 0)::numeric, 4) as cost_7d,
  round(coalesce(sum(k.cost_usd), 0)::numeric, 4) as cost_30d,
  (count(distinct k.sreality_id) filter (where k.created_at >= now() - interval '7 days'))::int as listings_7d,
  count(distinct k.sreality_id)::int as listings_30d,
  now() as refreshed_at
from linked k
group by 1, 2, 3
with no data;

create unique index dedup_llm_cost_by_category_mv_key
  on dedup_llm_cost_by_category_mv (called_for, category_main, category_type);

create view dedup_llm_cost_by_category_public as
select * from dedup_llm_cost_by_category_mv;

grant select on dedup_llm_cost_by_category_public to anon, authenticated;

-- pg_cron refresh every 15 min (the health-matview cadence family). The
-- first refresh after this migration must be non-concurrent (matviews are
-- created WITH NO DATA); done by the migration runner right after apply.
do $$
begin
  perform cron.unschedule('dedup-funnel-mv-refresh');
exception when others then
  null;
end $$;

select cron.schedule(
  'dedup-funnel-mv-refresh',
  '*/15 * * * *',
  $$refresh materialized view concurrently dedup_funnel_resolutions_mv;
    refresh materialized view concurrently dedup_llm_cost_by_category_mv;$$
);
