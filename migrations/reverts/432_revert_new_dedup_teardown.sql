-- 432_revert_new_dedup_teardown.sql
--
-- REVERT for 432_new_dedup_teardown.sql. SHIPPED UNAPPLIED.
--
-- Lives in migrations/reverts/ for two verified reasons: the CI replay applies
-- `ls migrations/*.sql | sort` (NOT recursive), so a revert beside its forward migration
-- would be applied right after it and silently undo the teardown in every replayed
-- environment; and tests/test_migration_numbers.py forbids a duplicate number above 304.
--
-- WHAT THIS RESTORES, AND WHAT IT CANNOT.
--   * The four dedup objects: restored EXACTLY. The block below is migrations/361 lines
--     116-254 verbatim — text that has already been applied to production once. Verified
--     against the live pg_get_viewdef/pg_indexes on 2026-08-25: identical modulo
--     PostgreSQL's paren normalization. No drift.
--   * property_sources_mv: the DEFINITION is restored; the CONTENT is not, and is not
--     wanted. The dropped copy was a frozen 2026-08-05 snapshot (511,946 rows, max
--     property_id 616,619 against a live max of 686,428 — ~69,800 properties missing),
--     never refreshed since creation. Recreating it populates TODAY's answer, which is
--     strictly better than what was dropped.
--   * The 35 dedup_funnel_resolutions_mv rows: NOT restored by a REFRESH, ever. Their
--     definition is 30-day-windowed over dedup_pair_audit, whose last write was
--     2026-08-06. They are preserved as real rows in public.dedup_funnel_resolutions_archive,
--     created by the forward migration before the drop. That is the recovery path.
--
-- ORDER MATTERS: recreate the objects FIRST, then cron.schedule — otherwise the first
-- tick can fire against objects that do not exist yet.

begin;

set local lock_timeout = '5s';

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

-- ---------------------------------------------------------------------------
-- property_sources_mv had NO create statement in any numbered migration — it drifted
-- into production out-of-band (verified: zero rows in supabase_migrations.schema_migrations
-- mention it). This is the definition captured from the live catalog on 2026-08-25.
--
-- The grant reproduces the live ACL exactly. It was never explicit: it came from
-- pg_default_acl, which grants `authenticated=r` on every table `postgres` creates in
-- public. Stated here so the revert is faithful rather than accidentally tighter.
-- ---------------------------------------------------------------------------
create materialized view public.property_sources_mv as
 SELECT property_id,
    array_agg(DISTINCT source) AS all_sources,
    array_agg(DISTINCT source) FILTER (WHERE is_active) AS active_sources
   FROM listings l
  WHERE property_id IS NOT NULL
  GROUP BY property_id;

create unique index property_sources_mv_pk
  on public.property_sources_mv using btree (property_id);

grant select on public.property_sources_mv to authenticated;

-- ---------------------------------------------------------------------------
-- The pg_cron job, byte-identical to the live row (md5 28ce0cee4afbe70f1204f785f1bb0126,
-- matching migrations/371 lines 207-213). Whitespace is load-bearing: each newline inside
-- the command is followed by exactly six spaces, there is no trailing newline, and the
-- matview names are UNQUALIFIED.
--
-- nodename/nodeport/database/username/active are pg_cron's defaults for a local job and
-- are reproduced automatically by the 3-argument form.
--
-- CAVEAT: the jobid will NOT be 8 again — it is sequence-assigned. Anything keying on
-- `jobid = 8` must key on jobname instead.
-- ---------------------------------------------------------------------------
select cron.schedule(
  'dedup-funnel-mv-refresh',
  '*/15 * * * *',
  $$set statement_timeout='300s';
      refresh materialized view concurrently dedup_funnel_resolutions_mv;
      refresh materialized view concurrently dedup_llm_cost_by_category_mv;$$
);

commit;
