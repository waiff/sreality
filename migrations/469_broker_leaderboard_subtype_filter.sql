-- 469_broker_leaderboard_subtype_filter.sql
-- Brokers UI (Makléři): filter the leaderboard by property SUBTYPE — "who holds the most
-- warehouses / offices / cottages", not just the most of a whole category_main.
--
-- REUSES migration 448's live branch rather than adding a matview dimension. 448 split
-- broker_leaderboard into a precomputed fast path (broker_region_type_stats) and a live
-- path (reads `listings` directly), gated so the planner prunes whichever branch cannot
-- apply. Subtype is the second filter to need the live path, and it generalises the split:
-- the rule is no longer "price is special", it is "a filter the matview cannot express
-- routes to the live branch". Adding it here instead of to the matview because:
--   * subtype is meaningful for only 2 of 5 category_main values (dum, komercni — see
--     toolkit/filter_registry's SUBTYPE_OPTIONS), so a matview dimension would be mostly
--     empty rows on top of a table refreshed by an already-long daily sweep;
--   * the matview sums `count(distinct property_key)` ACROSS its group-by keys, which is
--     only correct because categories are disjoint per property (migration 190's own
--     note). Subtype is NOT reliably disjoint per property — two portals can label the
--     same building `vila` and `rodinny_dum` — so a subtype group-by key would make the
--     unfiltered totals silently over-count. The live branch filters rows and counts
--     distinct properties ONCE, so the question never arises;
--   * measured: a komercni + 2-subtype live call is ~1.0s, cheaper than the byt/prodej
--     price shape 448 already accepted (~2.6-2.9s cold), because dum (~66k) and komercni
--     (~50k) active attributed rows are far smaller sets than byt.
--
-- DATA-QUALITY CAVEAT, MEASURED AND DELIBERATELY SURFACED IN THE UI. `listings.subtype`
-- coverage is a PORTAL gap, not random sparsity — measured live on active, attributed,
-- CZ-resolved rows:
--     sreality      dum   0.0% null    komercni   0.0% null
--     idnes         dum  29.4% null    komercni   7.6% null
--     remax         dum  99.6% null    komercni  43.5% null
--     ceskereality  dum   100% null    komercni   100% null
--     realitymix    dum   100% null    komercni   100% null
--     mmreality     dum   100% null    komercni   100% null
-- Half of all active attributed houses (32,853 of 65,974) and 39% of commercial carry no
-- subtype at all. Because brokers cluster by portal, that bias is not noise: of the top 15
-- brokers by unfiltered active commercial inventory, TWO (428 and 325 listings) have zero
-- subtype-labelled rows and disappear entirely from a subtype-filtered ranking, while most
-- of the rest are labelled on only 51-66% of their book. A subtype-filtered leaderboard is
-- therefore a ranking of "who lists on the portals that publish a subtype", not purely of
-- who holds the most of that subtype. `p_include_unknown_subtype` (the exact analogue of
-- 448's `p_include_unpriced`) lets the operator count unlabelled rows as matching, and the
-- Brokers page states the caveat inline whenever the filter is active — a silently biased
-- ranking would be worse than no filter at all.
--
-- THE GATE IS REPEATED INLINE, NOT FACTORED INTO A CTE — load-bearing, do not "clean up".
-- Branch pruning works because `p_min_price_czk is null and p_subtypes is null` is a
-- plan-time-foldable expression over bound parameters, so the planner proves one branch
-- dead and drops its relations from the plan entirely (448 verified this live, including
-- under real bound parameters via PREPARE/EXECUTE). Hoisting the gate into a CTE would
-- turn it into a runtime InitPlan scalar, which the planner cannot fold — both branches
-- would then be planned AND executed, and every unfiltered call would start paying for a
-- full `listings` scan. The two gates must also stay exact logical complements, or a call
-- could match both branches (double-counted rows) or neither (an empty leaderboard);
-- tests/test_broker_leaderboard_contract.py pins that.
--
-- BUG THIS MIGRATION ALSO FIXES, introduced latent by 448 and reachable only once a
-- SECOND live-path filter exists: 448's price predicate was written as
--     (l.price_czk >= p_min_price_czk or (l.price_czk is null and p_include_unpriced))
-- with no "no price filter" escape, because in 448 the live branch ran ONLY when
-- p_min_price_czk was non-null. With a subtype-only call the live branch now runs with
-- p_min_price_czk NULL, `l.price_czk >= NULL` is NULL, and the whole predicate would be
-- false for every row — a silently EMPTY leaderboard. Both predicates now carry the same
-- `p_x is null or ...` escape and are symmetric.
--
-- DROP + CREATE, not CREATE OR REPLACE: two new parameters change the signature, and
-- Postgres treats (name, argument types) as a function's identity — CREATE OR REPLACE
-- would silently create a THIRD overload instead of replacing (448's header records
-- hitting exactly that, live). The REVOKEs below are therefore load-bearing, not
-- belt-and-braces: a freshly CREATEd function grants EXECUTE to PUBLIC by default under
-- this project's Supabase ACL posture, and this function returns primary_email /
-- primary_phone. Both new parameters are TRAILING and defaulted, so api/outreach.py's
-- bare 7-positional-argument call stays legal and unchanged.

drop function if exists public.broker_leaderboard(
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[], integer, boolean);

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
  -- Shared by both branches: computed once regardless of which branch's gate is true,
  -- since it references neither broker_region_type_stats nor listings (448's header
  -- measures what duplicating it per branch cost).
  with active_brokers as materialized (
    select b.id
    from brokers b
    where b.status = 'active'
      and (p_firm_ids is null or b.primary_firm_id = any(p_firm_ids))
  ),

  -- FAST BRANCH: the precomputed matview. Gated on "no live-only filter is set" — every
  -- arm repeats the full gate so the planner can prove the whole branch dead when one is.
  fast_raw as (
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where p_min_price_czk is null and p_subtypes is null
      and coalesce(array_length(p_region_ids, 1), 0)
              + coalesce(array_length(p_okres_ids, 1), 0)
              + coalesce(array_length(p_obec_ids, 1), 0) = 0
      and s.geo_level = 'region'
      and (p_category_main is null or s.category_main = p_category_main)
      and (p_category_type is null or s.category_type = p_category_type)
    union all
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where p_min_price_czk is null and p_subtypes is null
      and coalesce(array_length(p_region_ids, 1), 0) > 0
      and s.geo_level = 'region'
      and s.geo_id = any(coalesce(p_region_ids, '{}'::bigint[]))
      and (p_category_main is null or s.category_main = p_category_main)
      and (p_category_type is null or s.category_type = p_category_type)
    union all
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where p_min_price_czk is null and p_subtypes is null
      and coalesce(array_length(p_okres_ids, 1), 0) > 0
      and s.geo_level = 'okres'
      and s.geo_id = any(coalesce(p_okres_ids, '{}'::bigint[]))
      and (p_category_main is null or s.category_main = p_category_main)
      and (p_category_type is null or s.category_type = p_category_type)
    union all
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where p_min_price_czk is null and p_subtypes is null
      and coalesce(array_length(p_obec_ids, 1), 0) > 0
      and s.geo_level = 'obec'
      and s.geo_id = any(coalesce(p_obec_ids, '{}'::bigint[]))
      and (p_category_main is null or s.category_main = p_category_main)
      and (p_category_type is null or s.category_type = p_category_type)
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

  -- LIVE BRANCH: reads `listings` directly, for the filters the matview cannot express
  -- (price since 448, subtype since 469). Mirrors the matview's own base predicate
  -- (obec_id is not null = migration 396's domestic scope, is_active + 7-day window =
  -- "active"), then adds those filters. Both are written `p_x is null or <match>` so each
  -- is inert when unset — the branch now runs whenever EITHER is set.
  --
  -- `listings` carries region_id/okres_id/obec_id directly on every row, so matching "any
  -- selected admin unit at any level" is a plain OR across three columns here — no need
  -- for the matview's per-level UNION ALL explosion above, which exists only because THAT
  -- table stores one row per (broker, level, id).
  live_priced as (
    select l.broker_identity_id,
           coalesce(l.property_id, -l.id) as property_key,
           (l.is_active and l.last_seen_at > now() - interval '7 days') as is_live
    from listings l
    where (p_min_price_czk is not null or p_subtypes is not null)
      and l.broker_identity_id is not null
      and l.obec_id is not null
      and (p_category_main is null or l.category_main = p_category_main)
      and (p_category_type is null or l.category_type = p_category_type)
      and (
        coalesce(array_length(p_region_ids, 1), 0)
          + coalesce(array_length(p_okres_ids, 1), 0)
          + coalesce(array_length(p_obec_ids, 1), 0) = 0
        or l.region_id = any(coalesce(p_region_ids, '{}'::bigint[]))
        or l.okres_id  = any(coalesce(p_okres_ids,  '{}'::bigint[]))
        or l.obec_id   = any(coalesce(p_obec_ids,   '{}'::bigint[]))
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

  -- Hydration for both branches (each ..._top is already <= p_limit rows), then one
  -- combined result. The two guards are exact logical complements, so exactly one branch
  -- ever contributes rows — the union selects between two complete answers, it does not
  -- merge two partial ones.
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
