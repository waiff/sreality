-- 448_broker_leaderboard_value_filter.sql
-- Brokers UI (Makléři): filter the leaderboard by property value, so the ranking can
-- answer "who has the most listings priced at least X" instead of only "who has the
-- most listings" — plus an explicit toggle for whether a listing with no price counts.
--
-- WHY A LIVE QUERY, NOT A NEW MATVIEW DIMENSION. broker_region_type_stats exists
-- because geo x category is small and enumerable (a few dozen regions/okresy x a
-- handful of category_main/category_type pairs) and gets hit on every single page
-- load, so precomputing it once a day and summing rows is a clear win (migrations 414,
-- 435). A minimum price is neither: it is a continuous, operator-typed number. Making
-- it fast the same way would mean either (a) picking a fixed set of price bands and
-- losing exactness (an "at least 5 000 000 Kc" filter that is actually "at least the
-- nearest band boundary"), or (b) exploding the matview by a price-bucket dimension
-- that still has to be re-picked and re-migrated as the CZ market inflates — the
-- opposite of low-maintenance. Browse's own price_czk_min/max filter
-- (toolkit.comparables._shared_filter_where) is already exact, not banded, over the
-- same listings.price_czk column — matching that keeps one meaning for "price filter"
-- across the app instead of Brokers quietly using a coarser one.
--
-- ONE SQL FUNCTION, TWO GATED BRANCHES, NOT A PROCEDURAL IF/ELSE — this was tried and
-- reverted during development, for a concrete, measured reason. `language sql` bodies
-- get INLINED by the planner (this function always has been `language sql`, since 187),
-- which is what lets EXPLAIN show its internal shape and is exactly what
-- tests/test_broker_leaderboard_plan_shape.py depends on to prove the LIMIT sits below
-- the hydration join. Rewriting the branch as `language plpgsql` with `if ... then
-- return query ... else return query ... end if` compiles and returns identical rows,
-- but PL/pgSQL is NEVER inlined — the function becomes an opaque "Function Scan" node
-- to any caller's EXPLAIN, with no visible Limit/Join/CTE Scan nodes at all. Verified
-- live against production: that plpgsql version made every one of
-- test_broker_leaderboard_plan_shape.py's assertions fail (no Limit node found at all).
--
-- The fix, also verified live: keep `language sql`, and instead of a runtime IF, gate
-- EACH branch's own base CTE with `where p_min_price_czk is null` / `is not null` and
-- UNION ALL the two hydrated results together. Postgres's planner inlines the CTEs,
-- constant-folds the gate against the ACTUAL bound parameter value (confirmed via
-- `PREPARE ... EXECUTE` against production, which mirrors how psycopg calls this with
-- prepare_threshold=None — i.e. a fresh custom plan per call, not a generic one), and
-- PRUNES the entire inactive branch out of the plan — verified: the unfiltered call's
-- EXPLAIN shows no trace of `listings`, and the price-filtered call's EXPLAIN shows no
-- trace of `broker_region_type_stats`. Same performance and observability as if only
-- one branch existed at a time; the other costs nothing beyond planning.
--
-- MEASURED, HONESTLY (this project's convention for a perf-sensitive migration — see
-- migration 429's "HOW THIS WAS ACTUALLY APPLIED" and 435's cold/warm split). The live
-- branch (no geo filter, category_main='byt', category_type='prodej' — the busiest
-- single shape and this page's default) costs ~2.6-2.9s COLD: ~50k qualifying rows each
-- need a heap visit to check price_czk/obec_id, and unlike broker_region_type_stats (a
-- matview, effectively static between daily refreshes), `listings` is under constant
-- scraper write pressure across nine portals, so its visibility map is rarely clean and
-- an Index Only Scan degrades to a heap fetch on almost every row regardless of which
-- columns are indexed. A purpose-built covering index — (category_main, category_type)
-- INCLUDE (broker_identity_id, price_czk, property_id, region_id, okres_id, obec_id,
-- is_active, last_seen_at, id) WHERE broker_identity_id IS NOT NULL AND obec_id IS NOT
-- NULL — was built and measured live against production: NO improvement (2.9s, Heap
-- Fetches: 47879/50529, for exactly the visibility-map reason above), so it was dropped
-- rather than left as a ~53 MB permanent write tax on the platform's hottest table for a
-- filter this page uses occasionally, not on every load. Accepted because: this branch
-- only runs when the operator has typed a value (the unfiltered default path is
-- untouched — still the sub-100ms matview aggregate); this is a single-operator tool
-- with no concurrent-load risk; and an exact live threshold is worth more here than
-- shaving seconds off an opt-in query (CLAUDE.md: "don't design for hypothetical future
-- requirements").
--
-- "cena na vyzadani" (price on request) IS `listings.price_czk IS NULL` — confirmed
-- against the frontend's own `hasPrice` convention (OriginPropertyPanel.tsx,
-- ListingOverview.tsx, ListingCards.tsx all render "Cena na vyzadani" exactly when
-- price_czk is null) and scraper/realitymix_parser.py's header comment ("Cena na
-- vyzadani" / "Rezervovano" / "info v RK" all collapse to no price_czk). There is no
-- separate flag or sentinel anywhere in the schema — one predicate covers every
-- "price unavailable" case on all nine portals. Measured live against production:
-- 7.8% of active broker-attributed byt/prodej listings and 19.8% of byt/pronajem carry
-- no price, so the include/exclude choice is a real, non-cosmetic decision.
--
-- DROP THE OLD 8-ARG SIGNATURE, THEN CREATE OR REPLACE THE NEW 10-ARG ONE — both are
-- needed HERE, in the file, regardless of what already happened on production during
-- development. Postgres treats (name, argument types) as a function's identity, so
-- widening the parameter COUNT (8 -> 10) is a genuinely different object: a first
-- live attempt at CREATE OR REPLACE with 2 new trailing parameters, with nothing to
-- replace yet, silently created a SECOND, overloaded function instead — confirmed by
-- every short positional/named call (including api/outreach.py's bare 7-arg one, and
-- tests/test_broker_leaderboard_live.py's named-arg calls) becoming ambiguous ("is not
-- unique") until the old overload was dropped by hand.
--
-- REAL MISTAKE MADE AND CAUGHT HERE, kept in the history rather than quietly fixed:
-- an earlier version of this file OMITTED the DROP, reasoning that "the signature
-- widening already happened during development, live on production" — true of prod's
-- CURRENT state, and irrelevant to what THIS FILE must do. CI's schema-replay lane
-- (`.github/workflows/migrations.yml`) applies every migration in order against an
-- EMPTY database, where migration 435 still leaves the OLD 8-arg function — so a
-- from-scratch replay hit the exact overload bug above, caught by
-- tests/test_broker_leaderboard_live.py's named-arg calls going "AmbiguousFunction".
-- Ad hoc production state is not a substitute for what the migration file itself
-- does when replayed from nothing — that is the entire point of the replay lane.
-- CREATE OR REPLACE alone is still correct for the BODY/LANGUAGE change (this file
-- also rewrote plpgsql back to plain SQL — see below — with the signature held
-- constant across that rewrite), preserving the ACL exactly as migration 435
-- documents; only the parameter-count widening needs the DROP.
--
-- Also re-learned live: a freshly CREATEd function grants EXECUTE to PUBLIC by default
-- (this project's standing default-ACL gotcha — see the database skill). The new
-- 10-arg overload created during the earlier signature-widening step was briefly
-- anon/authenticated-executable in production before being caught and revoked by hand
-- within the same development session — the REVOKE statements below are not
-- belt-and-braces, they are load-bearing.
--
-- Deferred hydration (rank on cheap columns, LIMIT, hydrate only the top p_limit rows —
-- migration 435) is preserved in BOTH branches: each has its own `..._top` CTE that
-- ranks-then-limits BEFORE its own `join brokers` / `left join firms`, so a
-- price-filtered call still only hydrates display columns for the rows it will
-- actually return, not the whole matching set.
--
-- ONE `active_brokers` CTE, SHARED, not one per branch — also measured, not assumed.
-- The first working version of this migration gave each branch its own identically-
-- defined `_active_brokers` CTE (mirroring how the two branches otherwise don't share
-- state). A MATERIALIZED CTE is eagerly evaluated once for every reference in the query
-- regardless of which branch a reference sits inside — Postgres does not (currently)
-- prune a materialized CTE's OWN evaluation just because every place that reads it is
-- itself inside a dead branch. So two identically-defined materialized CTEs meant
-- `brokers` (~22,775 active rows) was scanned TWICE on every single call, including the
-- default unfiltered one — measured live: 4,607 of the unfiltered call's 9,056 total
-- cost units, essentially half. `active_brokers` doesn't reference `listings` or
-- `broker_region_type_stats` at all, so nothing about the branch-pruning above depends
-- on it being duplicated — sharing one instance (defined before either branch, computed
-- once, joined into both `fast_agg` and `live_agg`) dropped the unfiltered call's total
-- cost to 7,010 with no change to the pruning behaviour, reverified live the same way.

drop function if exists public.broker_leaderboard(
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[]);

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
  p_include_unpriced boolean default false
)
returns table(broker_id bigint, display_name text, primary_email text, primary_phone text,
              firm_id bigint, firm_name text, firm_domain text,
              listing_count bigint, property_count bigint,
              active_listing_count bigint, active_property_count bigint)
language sql
stable
as $function$
  -- Shared by both branches (see the header's "ONE active_brokers CTE" note) — computed
  -- once regardless of which branch's gate is true, since it references neither
  -- broker_region_type_stats nor listings.
  with active_brokers as materialized (
    select b.id
    from brokers b
    where b.status = 'active'
      and (p_firm_ids is null or b.primary_firm_id = any(p_firm_ids))
  ),

  -- FAST BRANCH (byte-for-byte migration 435, plus the `p_min_price_czk is null` gate
  -- added to every base-CTE arm so the planner can prune this whole branch when a value
  -- filter IS active). Ranks on broker_region_type_stats alone.
  fast_raw as (
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where p_min_price_czk is null
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
    where p_min_price_czk is null
      and coalesce(array_length(p_region_ids, 1), 0) > 0
      and s.geo_level = 'region'
      and s.geo_id = any(coalesce(p_region_ids, '{}'::bigint[]))
      and (p_category_main is null or s.category_main = p_category_main)
      and (p_category_type is null or s.category_type = p_category_type)
    union all
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where p_min_price_czk is null
      and coalesce(array_length(p_okres_ids, 1), 0) > 0
      and s.geo_level = 'okres'
      and s.geo_id = any(coalesce(p_okres_ids, '{}'::bigint[]))
      and (p_category_main is null or s.category_main = p_category_main)
      and (p_category_type is null or s.category_type = p_category_type)
    union all
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where p_min_price_czk is null
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

  -- LIVE BRANCH (448): only "runs" (survives planner pruning) when p_min_price_czk is
  -- set. Reads `listings` directly, because price is the one dimension nothing here
  -- precomputes. Mirrors the matview's own base predicate (obec_id is not null =
  -- migration 396's domestic scope, is_active + 7-day window = "active"), adds the
  -- price predicate, keeps deferred hydration.
  --
  -- `listings` carries region_id/okres_id/obec_id directly on every row, so matching
  -- "any selected admin unit at any level" is a plain OR across three columns here —
  -- no need for the matview's per-level UNION ALL explosion above, which exists only
  -- because THAT table stores one row per (broker, level, id): exploding this path the
  -- same way would double- or triple-count a listing that has all three ids.
  live_priced as (
    select l.broker_identity_id,
           coalesce(l.property_id, -l.id) as property_key,
           (l.is_active and l.last_seen_at > now() - interval '7 days') as is_live
    from listings l
    where p_min_price_czk is not null
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
        l.price_czk >= p_min_price_czk
        or (l.price_czk is null and p_include_unpriced)
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

  -- Hydration (both branches, still above the union — each ..._top is already <=
  -- p_limit rows, so this join is cheap in both cases) then a single combined result.
  -- Exactly one of the two WHERE guards is ever true per call, so exactly one branch
  -- ever contributes rows — the union does not merge two partial answers, it selects
  -- between two complete ones.
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
    where p_min_price_czk is null
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
    where p_min_price_czk is not null
  )
  -- Explicit final ORDER BY, even though each branch already emits its rows in the
  -- right order and exactly one branch is ever non-empty: an incidental guarantee
  -- (UNION ALL of one real stream and one empty one preserves order) is not the same
  -- promise as an explicit one, and this project has hit exactly that class of "silent
  -- wrong order" bug before (migration 435's own tiebreaker rationale).
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
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[], integer, boolean) from public;
revoke execute on function public.broker_leaderboard(
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[], integer, boolean) from anon;
revoke execute on function public.broker_leaderboard(
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[], integer, boolean) from authenticated;
