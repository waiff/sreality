-- 359_readmodel_views_listing_id.sql
--
-- Gate-2 wave-5 tail, lead 6: read-model matviews/views identity.
--
-- Four DB objects still resolve listing identity through sreality_id (or a
-- sreality_id-derived surrogate) with no fallback to the R2 surrogate
-- `listings.id`. Post-Gate-2-flip, a brand-new non-sreality-portal row inserts
-- with `sreality_id = NULL` and vanishes from every one of these:
--
--   1. dedup_funnel_resolutions_mv / dedup_llm_cost_by_category_mv (materialized
--      views) — both LEFT JOIN listings on sreality_id to classify a pair/call by
--      category, and both `count(DISTINCT sreality_id)` for the listings_7d/30d
--      columns. A NULL-sreality row joins to nothing (falls into the 'ostatni'
--      bucket) and count(DISTINCT) silently drops it from the listings count.
--
--   2. broker_region_type_stats (materialized view) — `COALESCE(l.property_id,
--      -l.sreality_id)` as the per-listing dedup key for count(DISTINCT). For an
--      unmerged (property_id NULL) NULL-sreality row this is COALESCE(NULL, NULL)
--      = NULL, and a NULL GROUP/count(DISTINCT) key silently drops the row from
--      property_count/active_property_count instead of counting it as its own
--      singleton.
--
--   3. manual_rental_estimates_public — the underlying `manual_rental_estimates`
--      table has carried `listing_id` since migration 327 (dual-written
--      alongside sreality_id), but the read-only view never exposed it, so a
--      consumer joining the public view back onto a NULL-sreality listing has no
--      key to join on.
--
-- FIX, verified against the LIVE definitions (pg_get_viewdef / pg_matviews, not
-- the historical migration files, which are stale — current bodies span
-- migrations 282/318/331 for the two dedup matviews and 187/189/190/299 for
-- broker_region_type_stats):
--
--   * dedup_funnel_resolutions_mv: the category-join tolerates either id
--     (`l.sreality_id = a.left_sreality_id OR l.id = a.left_listing_id`); the
--     listings_7d/30d count resolves EACH side (left AND right) through its own
--     LEFT JOIN onto `listings` and counts DISTINCT on that resolved id (with a
--     'sr:'-prefixed sreality_id fallback only when truly unresolvable) —
--     NOT on dedup_pair_audit's own left_listing_id/right_listing_id columns
--     directly. Those columns are backfilled inconsistently across rows for the
--     historical (pre-negative-sreality-id-era) tail: the SAME sreality_id shows
--     up with listing_id NULL on one audit row and populated on another (2
--     independent live audit_id pairs verified, e.g. sreality_id -161339 →
--     listing_id NULL on audit_id 32426 vs listing_id 262501 on audit_id 61308).
--     Trusting the raw column directly inflates the distinct count (verified
--     live: naive COALESCE(listing_id, sreality_id) gave 116808 vs the correct
--     116623 total across all buckets — a live-caught bug in the first draft of
--     this migration, not a hypothetical). Resolving through the `listings` join
--     every time sidesteps the inconsistency and is a proven no-op (0 row/value
--     mismatches vs a freshly-computed copy of today's formula, full outer join
--     on every output column, both dedup_pair_audit windows).
--
--   * dedup_llm_cost_by_category_mv: sreality_id_a/listing_id_a (and
--     images.sreality_id/listing_id) are 1:1 and listing_id_a/images.listing_id
--     already carry a `NOT NULL` CHECK (migrations 308/309/310/350) — no
--     inconsistency risk, so the join + the listings_7d/30d count repoint
--     directly onto listing_id_a / images.listing_id. Verified live: 0 row
--     mismatches (fp/sp/visual/image_room_classifications resolve identically
--     via either id), 0 distinct-count mismatches.
--
--   * broker_region_type_stats: `-l.sreality_id` becomes `-l.id` — `listings.id`
--     is NEVER NULL (the PK), so the dedup key can never collapse to NULL again.
--     The literal key VALUE changes (it is never selected, only used inside
--     count(DISTINCT)), so this is a pure no-op today: verified live, 0 row
--     mismatches across all 437,153 (broker, geo_level, geo_id, category) rows.
--
--   * manual_rental_estimates_public: add `listing_id` to the SELECT list.
--
-- Both matviews + broker_region_type_stats are DROP + CREATE (a stored rule
-- CREATE OR REPLACE cannot rewrite the join). broker_geo_options and the two
-- *_public admin-gate wrapper views depend on them and must be dropped first,
-- recreated last, VERBATIM (their own column lists are untouched — only the
-- matview bodies they read from change). manual_rental_estimates_public is a
-- plain CREATE OR REPLACE VIEW (no dependents, ACL untouched by the replace).
--
-- ACL: a fresh matview/view inherits this project's default privileges, which
-- grant anon + authenticated the FULL relation privilege set (migration 354's
-- header; verified via pg_default_acl). Live posture (migrations 299/318/331):
-- both dedup matviews and broker_region_type_stats + broker_geo_options are
-- fully DARK to anon/authenticated (admin/service-role only, reached — for the
-- two dedup ones — through the is_platform_admin()-gated *_public wrapper
-- views); the two wrapper views themselves grant `authenticated: SELECT` only.
-- Every recreate below re-issues the matching revoke/grant explicitly so the
-- live ACL is reproduced exactly, not left to the default. `-- ci-allow-ungated`
-- annotates the two matview creates: tests/test_migration_rls_grants.py flags
-- any create naming an admin-only relation (here, the matview's own name, which
-- is in _ADMIN_ONLY_RELATIONS) without an embedded is_platform_admin() gate — a
-- materialized view cannot carry one; the revoke below is its protection
-- (identical reasoning + pattern to migration 354).

begin;

set local lock_timeout = '5s';

-- ===========================================================================
-- 1. manual_rental_estimates_public — plain CREATE OR REPLACE, ACL preserved.
-- ===========================================================================
-- CREATE OR REPLACE VIEW only permits appending columns, not inserting them
-- mid-list (42P16) -- listing_id goes at the end, not next to sreality_id.
create or replace view manual_rental_estimates_public as
  select id, sreality_id, rent_czk, author, source_kind, notes,
         created_at, updated_at, listing_id
  from manual_rental_estimates;

-- ===========================================================================
-- 2. dedup_funnel_resolutions_mv + dedup_llm_cost_by_category_mv, and their
--    is_platform_admin()-gated wrapper views.
-- ===========================================================================
drop view dedup_funnel_resolutions_public;
drop view dedup_llm_cost_by_category_public;
drop materialized view dedup_funnel_resolutions_mv;
drop materialized view dedup_llm_cost_by_category_mv;

-- ci-allow-ungated: dedup_funnel_resolutions_mv admin-only ops matview; kept dark to browser roles by the revoke below, a matview cannot embed is_platform_admin().
create materialized view dedup_funnel_resolutions_mv as
SELECT COALESCE(a.source, 'engine'::text) AS source,
    a.stage,
    a.outcome,
    COALESCE(a.category_main, 'ostatni'::text) AS category_main,
        CASE
            WHEN (l.category_type = ANY (ARRAY['prodej'::text, 'pronajem'::text])) THEN l.category_type
            ELSE 'ostatni'::text
        END AS category_type,
    (count(DISTINCT a.id) FILTER (WHERE (a.run_at >= (now() - '7 days'::interval))))::integer AS pairs_7d,
    (count(DISTINCT a.id))::integer AS pairs_30d,
    (count(DISTINCT s.property_id) FILTER (WHERE (a.run_at >= (now() - '7 days'::interval))))::integer AS properties_7d,
    (count(DISTINCT s.property_id))::integer AS properties_30d,
    (count(DISTINCT COALESCE(s.listing_id::text, ('sr:'::text || s.sreality_id::text))) FILTER (WHERE (a.run_at >= (now() - '7 days'::interval))))::integer AS listings_7d,
    (count(DISTINCT COALESCE(s.listing_id::text, ('sr:'::text || s.sreality_id::text))))::integer AS listings_30d,
    now() AS refreshed_at
   FROM (((dedup_pair_audit a
     LEFT JOIN listings l ON (((l.sreality_id = a.left_sreality_id) OR (l.id = a.left_listing_id))))
     LEFT JOIN listings rl ON (((rl.sreality_id = a.right_sreality_id) OR (rl.id = a.right_listing_id))))
     CROSS JOIN LATERAL ( VALUES (a.left_property_id,a.left_sreality_id,l.id), (a.right_property_id,a.right_sreality_id,rl.id)) s(property_id, sreality_id, listing_id))
  WHERE (a.run_at >= (now() - '30 days'::interval))
  GROUP BY COALESCE(a.source, 'engine'::text), a.stage, a.outcome, COALESCE(a.category_main, 'ostatni'::text),
        CASE
            WHEN (l.category_type = ANY (ARRAY['prodej'::text, 'pronajem'::text])) THEN l.category_type
            ELSE 'ostatni'::text
        END;

create unique index dedup_funnel_resolutions_mv_key on dedup_funnel_resolutions_mv
  using btree (source, stage, outcome, category_main, category_type);
revoke all on dedup_funnel_resolutions_mv from anon, authenticated;

-- ci-allow-ungated: dedup_llm_cost_by_category_mv admin-only ops matview; kept dark to browser roles by the revoke below, a matview cannot embed is_platform_admin().
create materialized view dedup_llm_cost_by_category_mv as
 WITH linked AS (
         SELECT 'compare_listings_visually'::text AS called_for,
            v.created_at,
            v.llm_call_id,
            v.cost_usd,
            l.category_main,
            l.category_type,
            v.listing_id_a AS listing_id
           FROM (listing_visual_matches v
             LEFT JOIN listings l ON ((l.id = v.listing_id_a)))
          WHERE (v.created_at >= (now() - '30 days'::interval))
        UNION ALL
         SELECT 'compare_listing_floor_plans'::text,
            f.created_at,
            f.llm_call_id,
            f.cost_usd,
            l.category_main,
            l.category_type,
            f.listing_id_a
           FROM (listing_floor_plan_matches f
             LEFT JOIN listings l ON ((l.id = f.listing_id_a)))
          WHERE (f.created_at >= (now() - '30 days'::interval))
        UNION ALL
         SELECT 'compare_listing_site_plans'::text,
            sp.created_at,
            sp.llm_call_id,
            sp.cost_usd,
            l.category_main,
            l.category_type,
            sp.listing_id_a
           FROM (listing_site_plan_matches sp
             LEFT JOIN listings l ON ((l.id = sp.listing_id_a)))
          WHERE (sp.created_at >= (now() - '30 days'::interval))
        UNION ALL
         SELECT 'classify_listing_images'::text,
            c.created_at,
            c.llm_call_id,
            c.cost_usd,
            l.category_main,
            l.category_type,
            i.listing_id
           FROM ((image_room_classifications c
             JOIN images i ON ((i.id = c.image_id)))
             LEFT JOIN listings l ON ((l.id = i.listing_id)))
          WHERE (c.created_at >= (now() - '30 days'::interval))
        )
 SELECT called_for,
    COALESCE(category_main, 'ostatni'::text) AS category_main,
        CASE
            WHEN (category_type = ANY (ARRAY['prodej'::text, 'pronajem'::text])) THEN category_type
            ELSE 'ostatni'::text
        END AS category_type,
    (count(DISTINCT llm_call_id) FILTER (WHERE (created_at >= (now() - '7 days'::interval))))::integer AS calls_7d,
    (count(DISTINCT llm_call_id))::integer AS calls_30d,
    round(COALESCE(sum(cost_usd) FILTER (WHERE (created_at >= (now() - '7 days'::interval))), (0)::numeric), 4) AS cost_7d,
    round(COALESCE(sum(cost_usd), (0)::numeric), 4) AS cost_30d,
    (count(DISTINCT listing_id) FILTER (WHERE (created_at >= (now() - '7 days'::interval))))::integer AS listings_7d,
    (count(DISTINCT listing_id))::integer AS listings_30d,
    now() AS refreshed_at
   FROM linked k
  GROUP BY called_for, COALESCE(category_main, 'ostatni'::text),
        CASE
            WHEN (category_type = ANY (ARRAY['prodej'::text, 'pronajem'::text])) THEN category_type
            ELSE 'ostatni'::text
        END;

create unique index dedup_llm_cost_by_category_mv_key on dedup_llm_cost_by_category_mv
  using btree (called_for, category_main, category_type);
revoke all on dedup_llm_cost_by_category_mv from anon, authenticated;

-- Wrapper views, reproduced VERBATIM (migration 318) — is_platform_admin() gate unchanged.
create view dedup_funnel_resolutions_public as
select * from (SELECT source,
    stage,
    outcome,
    category_main,
    category_type,
    pairs_7d,
    pairs_30d,
    properties_7d,
    properties_30d,
    listings_7d,
    listings_30d,
    refreshed_at
   FROM dedup_funnel_resolutions_mv
) __admin_gate
where is_platform_admin();
revoke all on dedup_funnel_resolutions_public from anon, authenticated;
grant select on dedup_funnel_resolutions_public to authenticated;

create view dedup_llm_cost_by_category_public as
select * from (SELECT called_for,
    category_main,
    category_type,
    calls_7d,
    calls_30d,
    cost_7d,
    cost_30d,
    listings_7d,
    listings_30d,
    refreshed_at
   FROM dedup_llm_cost_by_category_mv
) __admin_gate
where is_platform_admin();
revoke all on dedup_llm_cost_by_category_public from anon, authenticated;
grant select on dedup_llm_cost_by_category_public to authenticated;

-- ===========================================================================
-- 3. broker_region_type_stats, and its dependent broker_geo_options.
-- ===========================================================================
drop view broker_geo_options;
drop materialized view broker_region_type_stats;

create materialized view broker_region_type_stats as
 WITH attributed AS (
         SELECT b.id AS broker_id,
            l.region_id,
            l.okres_id,
            l.obec_id,
            COALESCE(l.category_main, ''::text) AS category_main,
            COALESCE(l.category_type, ''::text) AS category_type,
            COALESCE(l.property_id, (- l.id)) AS property_key,
            (l.is_active AND (l.last_seen_at > (now() - '7 days'::interval))) AS is_live
           FROM ((listings l
             JOIN broker_identities bi ON ((bi.id = l.broker_identity_id)))
             JOIN brokers b ON (((b.id = bi.broker_id) AND (b.status = 'active'::text))))
        ), per_level AS (
         SELECT 'region'::text AS geo_level,
            attributed.region_id AS geo_id,
            attributed.broker_id,
            attributed.category_main,
            attributed.category_type,
            attributed.property_key,
            attributed.is_live
           FROM attributed
          WHERE (attributed.region_id IS NOT NULL)
        UNION ALL
         SELECT 'okres'::text,
            attributed.okres_id,
            attributed.broker_id,
            attributed.category_main,
            attributed.category_type,
            attributed.property_key,
            attributed.is_live
           FROM attributed
          WHERE (attributed.okres_id IS NOT NULL)
        UNION ALL
         SELECT 'obec'::text,
            attributed.obec_id,
            attributed.broker_id,
            attributed.category_main,
            attributed.category_type,
            attributed.property_key,
            attributed.is_live
           FROM attributed
          WHERE (attributed.obec_id IS NOT NULL)
        )
 SELECT broker_id,
    geo_level,
    geo_id,
    category_main,
    category_type,
    count(*) AS listing_count,
    count(DISTINCT property_key) AS property_count,
    count(*) FILTER (WHERE is_live) AS active_listing_count,
    count(DISTINCT property_key) FILTER (WHERE is_live) AS active_property_count
   FROM per_level
  GROUP BY broker_id, geo_level, geo_id, category_main, category_type;

create unique index broker_region_type_stats_pk on broker_region_type_stats
  using btree (broker_id, geo_level, geo_id, category_main, category_type);
create index broker_region_type_stats_rank_idx on broker_region_type_stats
  using btree (geo_level, geo_id, category_main, category_type, active_property_count desc);
revoke all on broker_region_type_stats from anon, authenticated;

create view broker_geo_options as
select s.geo_level, s.geo_id, ab.name, ab.parent_id,
       count(distinct s.broker_id) as broker_count
from broker_region_type_stats s
join admin_boundaries ab on ab.id = s.geo_id
where s.geo_level in ('region', 'okres')
group by s.geo_level, s.geo_id, ab.name, ab.parent_id;
revoke all on broker_geo_options from anon, authenticated;

commit;
