-- 435_broker_leaderboard_deferred_hydration.sql
-- Cardinality Doctrine W4 (item 3) — rank first, hydrate 100.
--
-- THE DEFECT. Today the function joins `brokers_public` (which is `brokers LEFT JOIN firms
-- WHERE status='active'`) to the WHOLE aggregated candidate set, sorts, and only then takes
-- the top 100. Between 87% and 99.2% of every joined-and-decorated row is discarded by the
-- LIMIT. Measured warm, live:
--
--   shape                 blocks   where they go
--   default byt/prodej     3,140   2,067 (67%) = Seq Scan brokers 1,776 + Seq Scan firms 291
--   single region chip    22,952   20,355 (89%) = per-row Index Scans on brokers_pkey
--                                                 (loops=3,941) and firms_pkey (loops=3,941)
--   multi-chip geo        38,405   34,936 (91%) = the same nested loops at loops=6,731
--   firm chip              4,227   3,108 = brokers bitmap 753 + firms_pkey loops=785
--
-- The production authority is pg_stat_statements, not those MCP figures: 42 calls,
-- 5,390 blocks/call, mean 1,081 ms — and 65% of those blocks are shared_READ, with 20,777
-- buffers written back. This RPC does not run warm in production; it evicts. The warm
-- EXPLAINs are a LOWER bound.
--
-- THE FIX. Rank on the matview alone, LIMIT, then join `brokers` and `firms` for hydration
-- only — at most p_limit rows. A LIMIT inside a subquery is a hard optimisation barrier, so
-- the planner CANNOT push the hydration join back below it: the plan shape is structural,
-- not planner whim.
--
-- THE PART THAT IS NOT AN OPTIMISATION — status='active' moves INTO the CTE as a SEMI-JOIN.
-- Pushing the LIMIT under the display join while leaving the activity filter above it is a
-- CORRECTNESS REGRESSION: a broker with surviving stats rows but a merged_away status would
-- consume a top-N slot and then be discarded, silently under-filling the page and shrinking
-- api/outreach.py's limit=2000 candidate pool before its own email filter runs.
-- The doctrine moves INVARIANTS; it does NOT move PREDICATES across a LIMIT.
--
-- This hazard is LIVE, not theoretical. Measured: 5 brokers with status <> 'active' still
-- hold rows in broker_region_type_stats right now. They are benign today only by luck (all
-- carry active_property_count = 0), but of 19,200 merged-away brokers, 717 carry an
-- active_property_count at or above today's byt/prodej cut of 26 and 44 above the
-- all-categories cut, max 1,277. The matview is refreshed only by the daily full sweep
-- (mean gap 25.2 h, max 65.1 h, and 5 of 13 recent sweeps never recorded an end) while
-- inactivation happens continuously via merges — so the two failure modes are CORRELATED
-- and this misbehaves exactly when the pipeline is already degraded.
--
-- BEHAVIOUR CHANGE, STATED. On any input where the matview holds a non-active broker_id the
-- new function returns a DIFFERENT (correct) answer. That is deliberate and is in the PR body.
--
-- THE TIEBREAKER SHIPS HERE, not as a follow-up. `ORDER BY <metric> DESC` has no unique key.
-- Once the LIMIT moves under the join, an unstable sort stops deciding POSITION and starts
-- deciding MEMBERSHIP — which broker appears at all. Measured: at the default byt/prodej
-- limit-100 boundary the value is 26 and SEVEN brokers tie on it. Same class as this
-- project's own `ORDER BY timestamp` ties-reshuffle incident.
--
-- CREATE OR REPLACE, NEVER DROP + CREATE. Replacing preserves the ACL (verified live:
-- {postgres=X, service_role=X}); a DROP resets it to the default EXECUTE TO PUBLIC and would
-- silently re-expose a function returning primary_email/primary_phone to anon, which
-- migration 299 revoked. The signature is byte-identical to 410's, which is what keeps
-- api/outreach.py:123's SEVEN-placeholder call against this EIGHT-parameter signature legal
-- (it relies on p_firm_ids carrying a DEFAULT).
--
-- Rollback: migrations/reverts/435_revert_broker_leaderboard_deferred_hydration.sql
--           (the verbatim migration-414 body; drift-checked against live before this ran).

create or replace function public.broker_leaderboard(
  p_region_ids bigint[] default null::bigint[],
  p_okres_ids bigint[] default null::bigint[],
  p_obec_ids bigint[] default null::bigint[],
  p_category_main text default null::text,
  p_category_type text default null::text,
  p_metric text default 'active_property_count'::text,
  p_limit integer default 100,
  p_firm_ids bigint[] default null::bigint[]
)
returns table(broker_id bigint, display_name text, primary_email text, primary_phone text,
              firm_id bigint, firm_name text, firm_domain text,
              listing_count bigint, property_count bigint,
              active_listing_count bigint, active_property_count bigint)
language sql
stable
as $function$
  -- The four scan arms are mutually exclusive by construction and are UNION ALL-ed BELOW a
  -- single GROUP BY. NEVER give an arm its own GROUP BY: a broker holding both a region and
  -- an okres row would then emit two CTE rows, duplicating every hydrated output row and
  -- doubling its counts. Keeping one GROUP BY above the union also preserves rule 15's
  -- existing cross-region double-count EXACTLY as it is today — correcting that changes
  -- published numbers and is an explicit non-goal of this build.
  with raw as (
    -- arm A (national): fires only when no geo chip is set.
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where coalesce(array_length(p_region_ids, 1), 0)
            + coalesce(array_length(p_okres_ids, 1), 0)
            + coalesce(array_length(p_obec_ids, 1), 0) = 0
      and s.geo_level = 'region'
      and (p_category_main is null or s.category_main = p_category_main)
      and (p_category_type is null or s.category_type = p_category_type)

    union all

    -- arm B1 (region): a single Index Cond, not one arm of a BitmapOr.
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where coalesce(array_length(p_region_ids, 1), 0) > 0
      and s.geo_level = 'region'
      and s.geo_id = any(coalesce(p_region_ids, '{}'::bigint[]))
      and (p_category_main is null or s.category_main = p_category_main)
      and (p_category_type is null or s.category_type = p_category_type)

    union all

    -- arm B2 (okres)
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where coalesce(array_length(p_okres_ids, 1), 0) > 0
      and s.geo_level = 'okres'
      and s.geo_id = any(coalesce(p_okres_ids, '{}'::bigint[]))
      and (p_category_main is null or s.category_main = p_category_main)
      and (p_category_type is null or s.category_type = p_category_type)

    union all

    -- arm B3 (obec)
    select s.broker_id, s.listing_count, s.property_count,
           s.active_listing_count, s.active_property_count
    from broker_region_type_stats s
    where coalesce(array_length(p_obec_ids, 1), 0) > 0
      and s.geo_level = 'obec'
      and s.geo_id = any(coalesce(p_obec_ids, '{}'::bigint[]))
      and (p_category_main is null or s.category_main = p_category_main)
      and (p_category_type is null or s.category_type = p_category_type)
  ),

  -- ONE semi-join carries BOTH invariants. Two separate pushdowns would scan `brokers`
  -- twice (~1,500 blocks instead of ~764); the combined predicate is logically identical.
  --
  -- It keys on broker_id — the GROUP BY key — so evaluating it in WHERE, before the
  -- aggregate, is exactly equivalent to evaluating it after: every row of a group shares
  -- the broker_id, so no group is partially removed and no sum changes.
  --
  -- The firm arm reads `brokers.primary_firm_id`, NOT `firms.id`. brokers_public exposes
  -- `firm_id` as `b.primary_firm_id`, and brokers_primary_firm_id_fkey (ON DELETE SET NULL)
  -- guarantees the column is NULL or a live firm — so this is provably identical to today's
  -- post-join `b.firm_id = any(p_firm_ids)` and needs no `firms` access at all.
  --
  -- `p_firm_ids is null` is the literal spelling ON PURPOSE. An EMPTY array must keep
  -- meaning "no firm matches" (zero rows), which is today's behaviour; rewriting it as
  -- coalesce(array_length(p_firm_ids,1),0) = 0 would silently flip '{}' to mean "all firms".
  agg as (
    select r.broker_id,
           sum(r.listing_count)::bigint          as listing_count,
           sum(r.property_count)::bigint         as property_count,
           sum(r.active_listing_count)::bigint   as active_listing_count,
           sum(r.active_property_count)::bigint  as active_property_count
    from raw r
    where r.broker_id in (
            select b.id
            from brokers b
            where b.status = 'active'
              and (p_firm_ids is null or b.primary_firm_id = any(p_firm_ids))
          )
    group by r.broker_id
  ),

  -- The truncation. This CTE is NOT cosmetic: writing the ORDER BY directly on the
  -- aggregating query is a live wrong-answer trap, because inside a CASE a bare
  -- `listing_count` binds to the INPUT column rather than the aggregate alias and silently
  -- sorts by an arbitrary raw row's value. The extra level makes `a.listing_count`
  -- unambiguously the aggregate. All three CTEs are referenced once and auto-inline.
  --
  -- The tiebreaker is ASCENDING here and ASCENDING in the outer ORDER BY. Mismatched
  -- directions would make membership and display order disagree with no error.
  top as (
    select a.broker_id, a.listing_count, a.property_count,
           a.active_listing_count, a.active_property_count
    from agg a
    order by case p_metric
               when 'listing_count'        then a.listing_count
               when 'property_count'       then a.property_count
               when 'active_listing_count' then a.active_listing_count
               else                             a.active_property_count
             end desc,
             a.broker_id
    -- The clamp is migration 414's, character for character. It is NOT a simplification
    -- opportunity: `greatest(1, least(NULL, 2000))` is NULL, so `p_limit => NULL` means
    -- UNLIMITED — a latent path the API clamps out of reach but the function contract
    -- keeps. A bare `limit p_limit` would also turn today's error on a negative p_limit
    -- into a silent single row.
    limit greatest(1, least(p_limit, 2000))
  )

  -- Hydration only, at most p_limit rows. `brokers` is INNER because the semi-join already
  -- proved existence; `firms` is LEFT, reproducing brokers_public exactly. Both join keys
  -- are primary keys, so neither can multiply rows.
  select t.broker_id,
         b.display_name,
         b.primary_email,
         b.primary_phone,
         b.primary_firm_id      as firm_id,
         f.display_name         as firm_name,
         f.canonical_domain     as firm_domain,
         t.listing_count, t.property_count,
         t.active_listing_count, t.active_property_count
  from top t
  join brokers b on b.id = t.broker_id
  left join firms f on f.id = b.primary_firm_id
  order by case p_metric
             when 'listing_count'        then t.listing_count
             when 'property_count'       then t.property_count
             when 'active_listing_count' then t.active_listing_count
             else                             t.active_property_count
           end desc,
           t.broker_id;
$function$;

-- Belt and braces. CREATE OR REPLACE preserves the ACL, so these are no-ops today — which
-- is exactly the point: they make the posture explicit at the one place a future
-- DROP + CREATE would quietly undo it.
revoke execute on function public.broker_leaderboard(
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[]) from public;
revoke execute on function public.broker_leaderboard(
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[]) from anon;
revoke execute on function public.broker_leaderboard(
  bigint[], bigint[], bigint[], text, text, text, integer, bigint[]) from authenticated;
