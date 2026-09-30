-- 580_broker_one_count_book.sql
-- Broker Unify W4: ONE count book. Exact cells for every filter shape the fast
-- path serves, a national level, nested chips counted once, and the duplicate
-- CZ-totals book deleted.
--
-- WHAT WAS WRONG (measured 2026-09-30):
--   * The matview stored only concrete (category_main, category_type) cells and
--     no national level, so every "Vše" and every national query SUMMED
--     count(DISTINCT ...) across cells — non-additive when one property's
--     listings straddle cells (multi-portal geocode splits, sale+rent pairs):
--     556 brokers carried an inflated national property_count.
--   * A kraj chip + an okres chip inside it counted the shared inventory twice,
--     and a test enshrined it (the ==17 assertion, retired in this PR).
--   * brokers.cz_* was a SECOND book of the same concept, recomputed on a
--     different clock — it disagreed with the matview for 19 of the top 100
--     brokers.
--   * The default national read re-aggregated ~99k region rows per request
--     (5.4 s / 97k buffers measured) instead of reading one precomputed row.
--
-- THE FIX:
--   * The matview gains a 'cz' geo level (geo_id 0) and GROUPING SETS over the
--     category pair — cells (m,t), (m,'*'), ('*',t), ('*','*') — each computed
--     EXACTLY (count(DISTINCT property_key) inside the cell, never summed).
--     The fast branch becomes ONE arm: every filter shape resolves to exact
--     cells; multi-chip selections still sum, but only across DISJOINT geos
--     (properties are additive there), because...
--   * ...an okres chip whose kraj is also selected (and an obec chip whose
--     okres/kraj is) is suppressed IN SQL via admin_boundaries parent lookups,
--     so nested selections count each property once. Count-once is the
--     operator's ruling (2026-09-30); the live (price/subtype) branch already
--     counted once via its per-listing OR and is unchanged.
--   * brokers_public serves its cz_* columns (same names, same positions —
--     consumers unchanged) from the matview's ('cz', 0, '*', '*') cell, and the
--     four brokers.cz_* columns are DROPPED — one book. recompute_brokers
--     (same PR) stops computing them, which also removes its listing_location
--     join: the domestic predicate now lives in EXACTLY one place, this file.
--   * broker_identities.agency_name captures idnes's firm label at attribution
--     time (backfilled once below), so _FIRM_DISPLAY_NAMES stops re-reading
--     every idnes raw_json from TOAST daily (mean 200 s, max 858 s).
--
-- DESTRUCTIVE (operator pre-approved 2026-09-30, dump-first):
--   * DROP + CREATE of the matview — fully derived, rebuilt WITH DATA here.
--   * DROP of brokers.cz_* (4 columns) — derived values; a pre-apply snapshot
--     of (id, cz_*) is saved by the applying session.
--
-- RUN ORDER — DEPLOY THE CODE FIRST, THEN APPLY THIS FILE (508's rule): the
-- pre-W4 recompute writes brokers.cz_* on every pass, so dropping the columns
-- first would break the drain within minutes. The W4 recompute neither reads
-- nor writes them and runs fine while they still exist.

set local statement_timeout = '900s';
set local lock_timeout = '15s';

drop view if exists broker_geo_options;
drop materialized view if exists broker_region_type_stats;

create materialized view broker_region_type_stats as
 with attributed as (
         select b.id as broker_id,
            ll.kraj_kod  as region_id,
            ll.okres_kod as okres_id,
            ll.obec_kod  as obec_id,
            coalesce(l.category_main, ''::text) as category_main,
            coalesce(l.category_type, ''::text) as category_type,
            coalesce(l.property_id, (- l.id)) as property_key,
            (l.is_active and (l.last_seen_at > (now() - '7 days'::interval))) as is_live
           from (((listings l
             join broker_identities bi on ((bi.id = l.broker_identity_id)))
             join brokers b on (((b.id = bi.broker_id) and (b.status = 'active'::text))))
             join listing_location ll on ((ll.listing_id = l.id)))
          where (ll.obec_kod is not null)
        ), per_level as (
         select 'cz'::text as geo_level,
            0::bigint as geo_id,
            attributed.broker_id,
            attributed.category_main,
            attributed.category_type,
            attributed.property_key,
            attributed.is_live
           from attributed
        union all
         select 'region'::text,
            attributed.region_id,
            attributed.broker_id,
            attributed.category_main,
            attributed.category_type,
            attributed.property_key,
            attributed.is_live
           from attributed
          where (attributed.region_id is not null)
        union all
         select 'okres'::text,
            attributed.okres_id,
            attributed.broker_id,
            attributed.category_main,
            attributed.category_type,
            attributed.property_key,
            attributed.is_live
           from attributed
          where (attributed.okres_id is not null)
        union all
         select 'obec'::text,
            attributed.obec_id,
            attributed.broker_id,
            attributed.category_main,
            attributed.category_type,
            attributed.property_key,
            attributed.is_live
           from attributed
          where (attributed.obec_id is not null)
        )
 select broker_id,
    geo_level,
    geo_id,
    -- Inside a grouping set the rolled-up column is NULL; '*' marks the rollup
    -- cell. A genuinely unknown category is already '' (coalesced above), so
    -- '' and '*' can never collide.
    coalesce(category_main, '*'::text) as category_main,
    coalesce(category_type, '*'::text) as category_type,
    count(*) as listing_count,
    count(distinct property_key) as property_count,
    count(*) filter (where is_live) as active_listing_count,
    count(distinct property_key) filter (where is_live) as active_property_count
   from per_level
  group by broker_id, geo_level, geo_id,
           grouping sets ((category_main, category_type), (category_main), (category_type), ());

create unique index broker_region_type_stats_pk on broker_region_type_stats
  using btree (broker_id, geo_level, geo_id, category_main, category_type);
create index broker_region_type_stats_rank_idx on broker_region_type_stats
  using btree (geo_level, geo_id, category_main, category_type, active_property_count desc)
  include (broker_id, listing_count, property_count, active_listing_count);
revoke all on broker_region_type_stats from anon, authenticated;

comment on materialized view broker_region_type_stats is
  'Exact per-cell broker inventory (Broker Unify W4): geo levels cz(0)/region/'
  'okres/obec x category grouping sets (m,t)/(m,*)/(*,t)/(*,*). Every cell''s '
  'property_count is an exact count(DISTINCT) — never sum cells that can '
  'overlap. Republished hourly-if-stale by the broker drain via '
  'public.refresh_matview.';

-- broker_geo_options: 396's body, verbatim — re-created only because its
-- matview was dropped. count(distinct broker_id) is unaffected by the new
-- rollup cells, and the level filter already excludes 'cz'.
create view broker_geo_options as
select s.geo_level, s.geo_id, ab.name, ab.parent_id,
       count(distinct s.broker_id) as broker_count
from broker_region_type_stats s
join admin_boundaries ab on ab.id = s.geo_id
where s.geo_level in ('region', 'okres')
group by s.geo_level, s.geo_id, ab.name, ab.parent_id;
revoke all on broker_geo_options from anon, authenticated;

-- The leaderboard: 508's body with the fast branch rebuilt on exact cells.
-- ONE fast arm (was four): the filter shape picks its cell — no geo -> the
-- national 'cz' cell, a category left NULL -> that dimension's '*' rollup —
-- and a nested chip (okres under a selected kraj, obec under a selected
-- okres/kraj) is suppressed so overlapping inventory counts once. Sums remain
-- only across the DISJOINT geos of a multi-chip selection, where properties
-- are additive. The live branch (price/subtype) is 508's, verbatim: it always
-- counted each listing once via its per-listing OR. CTE names, gates, the
-- shared MATERIALIZED active_brokers, per-branch LIMIT-before-hydration and
-- the final explicit ORDER BY are all preserved — the offline contract and
-- the live plan-shape rails pin them.
create or replace function public.broker_leaderboard(
  p_region_ids bigint[] default null::bigint[],
  p_okres_ids bigint[] default null::bigint[],
  p_obec_ids bigint[] default null::bigint[],
  p_category_main text default null::text,
  p_category_type text default null::text,
  p_metric text default 'active_property_count'::text,
  p_limit integer default 100,
  p_firm_ids bigint[] default null::bigint[],
  p_min_price_czk integer default null::integer,
  p_include_unpriced boolean default false,
  p_subtypes text[] default null::text[],
  p_include_unknown_subtype boolean default false
)
returns table(broker_id bigint, display_name text, primary_email text, primary_phone text,
              firm_id bigint, firm_name text, firm_domain text,
              listing_count bigint, property_count bigint,
              active_listing_count bigint, active_property_count bigint)
language sql
stable
as $function$
  with active_brokers as materialized (
    select b.id
    from brokers b
    where b.status = 'active'
      and (p_firm_ids is null or b.primary_firm_id = any(p_firm_ids))
  ),

  -- FAST BRANCH: exact precomputed cells (W4). Gated on "no live-only filter".
  fast_raw as (
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where p_min_price_czk is null and p_subtypes is null
      and s.category_main = coalesce(p_category_main, '*')
      and s.category_type = coalesce(p_category_type, '*')
      and (
        (coalesce(array_length(p_region_ids, 1), 0)
           + coalesce(array_length(p_okres_ids, 1), 0)
           + coalesce(array_length(p_obec_ids, 1), 0) = 0
         and s.geo_level = 'cz')
        or (s.geo_level = 'region'
            and s.geo_id = any(coalesce(p_region_ids, '{}'::bigint[])))
        or (s.geo_level = 'okres'
            and s.geo_id = any(coalesce(p_okres_ids, '{}'::bigint[]))
            and not exists (
              select 1 from admin_boundaries ab
              where ab.id = s.geo_id
                and ab.parent_id = any(coalesce(p_region_ids, '{}'::bigint[]))))
        or (s.geo_level = 'obec'
            and s.geo_id = any(coalesce(p_obec_ids, '{}'::bigint[]))
            and not exists (
              select 1 from admin_boundaries ab
              left join admin_boundaries pa on pa.id = ab.parent_id
              where ab.id = s.geo_id
                and (ab.parent_id = any(coalesce(p_okres_ids, '{}'::bigint[]))
                     or pa.parent_id = any(coalesce(p_region_ids, '{}'::bigint[])))))
      )
  ),
  fast_agg as (
    select r.broker_id,
           sum(r.listing_count)::bigint          as listing_count,
           sum(r.property_count)::bigint         as property_count,
           sum(r.active_listing_count)::bigint   as active_listing_count,
           sum(r.active_property_count)::bigint  as active_property_count
    from fast_raw r
    join active_brokers ab on ab.id = r.broker_id
    group by r.broker_id
  ),
  fast_top as (
    select a.broker_id, a.listing_count, a.property_count,
           a.active_listing_count, a.active_property_count
    from fast_agg a
    order by case p_metric
               when 'listing_count'        then a.listing_count
               when 'property_count'       then a.property_count
               when 'active_listing_count' then a.active_listing_count
               else                             a.active_property_count
             end desc,
             a.broker_id
    limit greatest(1, least(p_limit, 2000))
  ),

  -- LIVE BRANCH: reads `listings` directly, for the filters the matview cannot
  -- express (price since 448, subtype since 469). 508's body, verbatim.
  live_priced as (
    select l.broker_identity_id,
           coalesce(l.property_id, -l.id) as property_key,
           (l.is_active and l.last_seen_at > now() - interval '7 days') as is_live
    from listings l
    join listing_location ll on ll.listing_id = l.id
    where (p_min_price_czk is not null or p_subtypes is not null)
      and l.broker_identity_id is not null
      and ll.obec_kod is not null
      and (p_category_main is null or l.category_main = p_category_main)
      and (p_category_type is null or l.category_type = p_category_type)
      and (
        coalesce(array_length(p_region_ids, 1), 0)
          + coalesce(array_length(p_okres_ids, 1), 0)
          + coalesce(array_length(p_obec_ids, 1), 0) = 0
        or ll.kraj_kod  = any(coalesce(p_region_ids, '{}'::bigint[]))
        or ll.okres_kod = any(coalesce(p_okres_ids,  '{}'::bigint[]))
        or ll.obec_kod  = any(coalesce(p_obec_ids,   '{}'::bigint[]))
      )
      and (
        p_min_price_czk is null
        or l.price_czk >= p_min_price_czk
        or (l.price_czk is null and p_include_unpriced)
      )
      and (
        p_subtypes is null
        or l.subtype = any(p_subtypes)
        or (l.subtype is null and p_include_unknown_subtype)
      )
  ),
  live_agg as (
    select bi.broker_id,
           count(*)                                                as listing_count,
           count(distinct p.property_key)                          as property_count,
           count(*) filter (where p.is_live)                       as active_listing_count,
           count(distinct p.property_key) filter (where p.is_live) as active_property_count
    from live_priced p
    join broker_identities bi on bi.id = p.broker_identity_id
    join active_brokers ab on ab.id = bi.broker_id
    group by bi.broker_id
  ),
  live_top as (
    select a.broker_id, a.listing_count, a.property_count,
           a.active_listing_count, a.active_property_count
    from live_agg a
    order by case p_metric
               when 'listing_count'        then a.listing_count
               when 'property_count'       then a.property_count
               when 'active_listing_count' then a.active_listing_count
               else                             a.active_property_count
             end desc,
             a.broker_id
    limit greatest(1, least(p_limit, 2000))
  ),

  combined as (
    select t.broker_id, b.display_name, b.primary_email, b.primary_phone,
           b.primary_firm_id      as firm_id,
           f.display_name         as firm_name,
           f.canonical_domain     as firm_domain,
           t.listing_count, t.property_count,
           t.active_listing_count, t.active_property_count
    from fast_top t
    join brokers b on b.id = t.broker_id
    left join firms f on f.id = b.primary_firm_id
    where p_min_price_czk is null and p_subtypes is null
    union all
    select t.broker_id, b.display_name, b.primary_email, b.primary_phone,
           b.primary_firm_id      as firm_id,
           f.display_name         as firm_name,
           f.canonical_domain     as firm_domain,
           t.listing_count, t.property_count,
           t.active_listing_count, t.active_property_count
    from live_top t
    join brokers b on b.id = t.broker_id
    left join firms f on f.id = b.primary_firm_id
    where p_min_price_czk is not null or p_subtypes is not null
  )
  -- Explicit final ORDER BY, even though each branch already emits its rows in the right
  -- order and exactly one branch is ever non-empty: an incidental guarantee is not the
  -- same promise as an explicit one (migration 435's tiebreaker rationale).
  select * from combined
  order by case p_metric
             when 'listing_count'        then listing_count
             when 'property_count'       then property_count
             when 'active_listing_count' then active_listing_count
             else                             active_property_count
           end desc,
           broker_id
$function$;

revoke execute on function public.broker_leaderboard(
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[], integer, boolean,
  text[], boolean) from public;
revoke execute on function public.broker_leaderboard(
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[], integer, boolean,
  text[], boolean) from anon;
revoke execute on function public.broker_leaderboard(
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[], integer, boolean,
  text[], boolean) from authenticated;

-- brokers_public: same columns, same order, same names — but the cz_* block is
-- served from the matview's national all-category cell instead of the second
-- book on brokers. DROP + CREATE because a view cannot lose its reference to
-- the columns dropped below via CREATE OR REPLACE. Plain view, not
-- security_invoker: owner rights read through the matview's revokes, and the
-- view has been service-role only since migration 299.
drop view if exists brokers_public;
create view brokers_public as
 select b.id as broker_id,
    b.display_name,
    b.primary_email,
    b.primary_phone,
    b.primary_firm_id as firm_id,
    f.canonical_domain as firm_domain,
    f.display_name as firm_name,
    f.is_franchise as firm_is_franchise,
    b.source_count,
    b.distinct_source_count,
    b.listing_count,
    b.property_count,
    b.active_listing_count,
    b.active_property_count,
    b.first_seen_at,
    b.last_seen_at,
    coalesce(cz.listing_count, 0)::bigint        as cz_listing_count,
    coalesce(cz.property_count, 0)::bigint       as cz_property_count,
    coalesce(cz.active_listing_count, 0)::bigint as cz_active_listing_count,
    coalesce(cz.active_property_count, 0)::bigint as cz_active_property_count
   from brokers b
     left join firms f on f.id = b.primary_firm_id
     left join broker_region_type_stats cz
       on cz.broker_id = b.id and cz.geo_level = 'cz' and cz.geo_id = 0
      and cz.category_main = '*' and cz.category_type = '*'
  where b.status = 'active'::text;
revoke all on brokers_public from anon, authenticated;

-- One book: the duplicate CZ totals leave `brokers`. DESTRUCTIVE, pre-approved.
alter table brokers
  drop column if exists cz_listing_count,
  drop column if exists cz_property_count,
  drop column if exists cz_active_listing_count,
  drop column if exists cz_active_property_count;

-- idnes's firm label, captured once at attribution instead of re-derived from
-- every idnes raw_json daily. Backfilled from the latest sighting per identity;
-- the attribution upsert keeps it fresh from here (latest-wins, NULL-preserving).
alter table broker_identities add column if not exists agency_name text;

update broker_identities bi
   set agency_name = x.name
  from (
    select distinct on (l.broker_identity_id)
           l.broker_identity_id,
           l.raw_json->'broker'->>'agency_name' as name
      from listings l
     where l.source = 'idnes'
       and l.broker_identity_id is not null
       and coalesce(l.raw_json->'broker'->>'agency_name', '') <> ''
     order by l.broker_identity_id, l.last_seen_at desc nulls last
  ) x
 where x.broker_identity_id = bi.id;
