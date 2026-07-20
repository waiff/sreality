-- 318_admin_ops_views_platform_admin_gate.sql
-- Follow-up to migration 316. A 29-agent live audit (2026-07-20) classified the
-- remaining Supabase advisor security_definer_view findings not covered by 316.
-- 23 of the 26 flagged views (+ 3 SPA-called SECURITY DEFINER functions with no
-- in-body admin check) wrap admin-only OPERATIONAL data (dedup engine internals,
-- scraper health, LLM cost, image training/labeling state, workflow health) that
-- is reachable ONLY through admin-gated frontend routes (RequireAdmin) and the
-- gated API today -- but nothing stops a future non-admin authenticated Supabase
-- client from reading them directly, since none of them are security_invoker and
-- their owner (postgres) bypasses RLS. UNLIKE migration 316 (per-account tenant
-- data), the fix here is deliberately NOT security_invoker + a base-table RLS
-- policy: several of these views read the shared `listings`/`properties`/`images`
-- tables directly, which have carried RLS-enabled-with-ZERO-policies since early
-- migrations (verified live) precisely so their owner-bypass views (listings_public,
-- properties_public, ...) can serve shared-market reads to every authenticated user
-- -- adding a restrictive policy directly to those tables risks interacting with
-- every other reader of them, for a comparatively low-value, narrowly-scoped fix.
-- Instead, each view/function below is redefined to embed `is_platform_admin()` as
-- a plain boolean filter in its own query -- this is evaluated per-request exactly
-- like the `publication_gate_enabled()` clause properties_public already carries,
-- completely independent of RLS/security_invoker/ownership, so it needs ZERO changes
-- to `listings`/`properties`/`images` or any other base table's grants or policies.
-- A non-admin authenticated caller gets 0 rows through any of these 26 objects; the
-- platform admin (the only account today) sees exactly what they see now.
--
-- 3 views classified `leave_open_no_change` are deliberately NOT touched here:
--   browse_read_model_state_public / portal_listing_counts -- aggregate-only,
--     non-sensitive operational metadata (rebuild timing, per-source counts),
--     no row-level content, consistent with other anon-readable health surfaces.
--   listing_freshness_checks_public -- genuinely non-admin: the Listing Detail
--     page's "verify freshness" button (Phase U2.5) reads this for ANY signed-in
--     user, not just the admin -- locking it would break a real user feature.

begin;

create or replace view public.data_quality_by_source as
select * from (SELECT l.source,
    v.field,
    count(*) AS n_active,
    count(*) FILTER (WHERE v.present) AS n_populated,
    round(100.0 * count(*) FILTER (WHERE v.present)::numeric / count(*)::numeric, 1) AS pct_populated
   FROM listings l
     CROSS JOIN LATERAL ( VALUES ('price_czk'::text,l.price_czk IS NOT NULL), ('area_m2'::text,l.area_m2 IS NOT NULL), ('disposition'::text,l.disposition IS NOT NULL), ('category_main'::text,l.category_main IS NOT NULL), ('category_type'::text,l.category_type IS NOT NULL), ('geom'::text,l.geom IS NOT NULL), ('locality'::text,l.locality IS NOT NULL), ('district'::text,l.district IS NOT NULL), ('locality_district_id'::text,l.locality_district_id IS NOT NULL), ('locality_region_id'::text,l.locality_region_id IS NOT NULL), ('street'::text,l.street IS NOT NULL), ('house_number'::text,l.house_number IS NOT NULL), ('floor'::text,l.floor IS NOT NULL), ('total_floors'::text,l.total_floors IS NOT NULL), ('has_balcony'::text,l.has_balcony IS NOT NULL), ('has_lift'::text,l.has_lift IS NOT NULL), ('has_parking'::text,l.has_parking IS NOT NULL), ('terrace'::text,l.terrace IS NOT NULL), ('cellar'::text,l.cellar IS NOT NULL), ('garage'::text,l.garage IS NOT NULL), ('parking_lots'::text,l.parking_lots IS NOT NULL), ('building_type'::text,l.building_type IS NOT NULL), ('condition'::text,l.condition IS NOT NULL), ('energy_rating'::text,l.energy_rating IS NOT NULL), ('furnished'::text,l.furnished IS NOT NULL), ('ownership'::text,l.ownership IS NOT NULL), ('building_condition_level'::text,l.building_condition_level IS NOT NULL), ('apartment_condition_level'::text,l.apartment_condition_level IS NOT NULL), ('property_grouped'::text,l.property_id IS NOT NULL)) v(field, present)
  WHERE l.is_active
  GROUP BY l.source, v.field
) __admin_gate
where is_platform_admin();

create or replace view public.dedup_engine_flow_public as
select * from (WITH latest_full AS (
         SELECT dedup_engine_runs.eligible,
            dedup_engine_runs.flagged_location,
            dedup_engine_runs.flagged_disposition
           FROM dedup_engine_runs
          WHERE dedup_engine_runs.eligible IS NOT NULL
          ORDER BY dedup_engine_runs.id DESC
         LIMIT 1
        ), agg AS (
         SELECT count(*) FILTER (WHERE dedup_engine_runs.started_at >= (now() - '7 days'::interval))::integer AS runs_7d,
            count(*)::integer AS runs_30d,
            COALESCE(sum(dedup_engine_runs.pairs_considered) FILTER (WHERE dedup_engine_runs.started_at >= (now() - '7 days'::interval)), 0::bigint) AS pairs_considered_7d,
            COALESCE(sum(dedup_engine_runs.pairs_considered), 0::bigint) AS pairs_considered_30d,
            COALESCE(sum(dedup_engine_runs.rejected) FILTER (WHERE dedup_engine_runs.started_at >= (now() - '7 days'::interval)), 0::bigint) AS rejected_7d,
            COALESCE(sum(dedup_engine_runs.rejected), 0::bigint) AS rejected_30d,
            COALESCE(sum(dedup_engine_runs.queued) FILTER (WHERE dedup_engine_runs.started_at >= (now() - '7 days'::interval)), 0::bigint) AS queued_7d,
            COALESCE(sum(dedup_engine_runs.queued), 0::bigint) AS queued_30d,
            COALESCE(sum(dedup_engine_runs.clip_cosine_calls) FILTER (WHERE dedup_engine_runs.started_at >= (now() - '7 days'::interval)), 0::bigint) AS clip_cosine_calls_7d,
            COALESCE(sum(dedup_engine_runs.clip_cosine_calls), 0::bigint) AS clip_cosine_calls_30d,
            COALESCE(sum(dedup_engine_runs.routed_haiku) FILTER (WHERE dedup_engine_runs.started_at >= (now() - '7 days'::interval)), 0::bigint) AS routed_haiku_7d,
            COALESCE(sum(dedup_engine_runs.routed_haiku), 0::bigint) AS routed_haiku_30d,
            COALESCE(sum(dedup_engine_runs.routed_sonnet) FILTER (WHERE dedup_engine_runs.started_at >= (now() - '7 days'::interval)), 0::bigint) AS routed_sonnet_7d,
            COALESCE(sum(dedup_engine_runs.routed_sonnet), 0::bigint) AS routed_sonnet_30d,
            COALESCE(sum(dedup_engine_runs.floor_plan_deferred) FILTER (WHERE dedup_engine_runs.started_at >= (now() - '7 days'::interval)), 0::bigint) AS floor_plan_deferred_7d,
            COALESCE(sum(dedup_engine_runs.floor_plan_deferred), 0::bigint) AS floor_plan_deferred_30d,
            COALESCE(sum(dedup_engine_runs.clip_deferred) FILTER (WHERE dedup_engine_runs.started_at >= (now() - '7 days'::interval)), 0::bigint) AS clip_deferred_7d,
            COALESCE(sum(dedup_engine_runs.clip_deferred), 0::bigint) AS clip_deferred_30d,
            COALESCE(sum(dedup_engine_runs.skipped_unresolved) FILTER (WHERE dedup_engine_runs.started_at >= (now() - '7 days'::interval)), 0::bigint) AS skipped_unresolved_7d,
            COALESCE(sum(dedup_engine_runs.skipped_unresolved), 0::bigint) AS skipped_unresolved_30d,
            COALESCE(sum(dedup_engine_runs.vision_calls) FILTER (WHERE dedup_engine_runs.started_at >= (now() - '7 days'::interval)), 0::bigint) AS vision_calls_7d,
            COALESCE(sum(dedup_engine_runs.vision_calls), 0::bigint) AS vision_calls_30d,
            COALESCE(sum(dedup_engine_runs.vision_errors) FILTER (WHERE dedup_engine_runs.started_at >= (now() - '7 days'::interval)), 0::bigint) AS vision_errors_7d,
            COALESCE(sum(dedup_engine_runs.vision_errors), 0::bigint) AS vision_errors_30d
           FROM dedup_engine_runs
          WHERE dedup_engine_runs.started_at >= (now() - '30 days'::interval)
        )
 SELECT ( SELECT latest_full.eligible
           FROM latest_full) AS eligible_market,
    ( SELECT latest_full.flagged_location
           FROM latest_full) AS flagged_location_market,
    ( SELECT latest_full.flagged_disposition
           FROM latest_full) AS flagged_disposition_market,
    runs_7d,
    runs_30d,
    pairs_considered_7d,
    pairs_considered_30d,
    rejected_7d,
    rejected_30d,
    queued_7d,
    queued_30d,
    clip_cosine_calls_7d,
    clip_cosine_calls_30d,
    routed_haiku_7d,
    routed_haiku_30d,
    routed_sonnet_7d,
    routed_sonnet_30d,
    floor_plan_deferred_7d,
    floor_plan_deferred_30d,
    clip_deferred_7d,
    clip_deferred_30d,
    skipped_unresolved_7d,
    skipped_unresolved_30d,
    vision_calls_7d,
    vision_calls_30d,
    vision_errors_7d,
    vision_errors_30d
   FROM agg
) __admin_gate
where is_platform_admin();

create or replace view public.dedup_engine_runs_public as
select * from (SELECT id,
    started_at,
    ended_at,
    eligible,
    flagged_location,
    flagged_disposition,
    pairs_considered,
    rejected,
    auto_address,
    auto_phash,
    auto_visual,
    queued,
    vision_calls,
    cost_usd,
    auto_dismissed,
    floor_plan_deferred,
    clip_deferred,
    dirty_queue_depth,
    dirty_claimed,
    dirty_cleared,
    dirty_truncated,
    run_kind,
    truncated,
    skipped_unresolved,
    skipped_oversized,
    oversized_groups,
    vision_errors,
    truncated_cause,
    scan_groups_total,
    scan_groups_scanned,
    dirty_age_p95_seconds,
    dirty_pruned,
    runner
   FROM dedup_engine_runs
) __admin_gate
where is_platform_admin();

create or replace view public.dedup_funnel_resolutions_public as
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

create or replace view public.dedup_label_events as
select * from (WITH operator_merge AS (
         SELECT 'merge:'::text || pme.id AS label_id,
            pme.survivor_property_id AS left_property_id,
            pme.retired_property_id AS right_property_id,
            ( SELECT l.sreality_id
                   FROM listings l
                  WHERE l.property_id = pme.survivor_property_id AND l.sreality_id <> pme.listing_id AND NOT (EXISTS ( SELECT 1
                           FROM property_merge_events pme2
                          WHERE pme2.listing_id = l.sreality_id AND pme2.created_at > pme.created_at))
                  ORDER BY (source_trust_rank(l.source)), l.is_active DESC, l.last_seen_at DESC NULLS LAST, l.sreality_id DESC
                 LIMIT 1) AS left_listing_id,
            pme.listing_id AS right_listing_id,
            true AS is_same,
            'operator_merge'::text AS label_source,
            pr.category_main,
            NULL::text AS tier,
            pme.created_at AS labeled_at,
            pme.reason
           FROM property_merge_events pme
             JOIN properties pr ON pr.id = pme.survivor_property_id
          WHERE pme.source = 'operator'::text AND pme.undone_at IS NULL
        ), operator_dismissal AS (
         SELECT 'dismiss:'::text || pic.id AS label_id,
            pic.left_property_id,
            pic.right_property_id,
            pl.repr_listing_id AS left_listing_id,
            pr.repr_listing_id AS right_listing_id,
            false AS is_same,
            'operator_dismissal'::text AS label_source,
            pl.category_main,
            pic.tier,
            pic.reviewed_at AS labeled_at,
            COALESCE(pic.markers_matched ->> 'reason'::text, ''::text) AS reason
           FROM property_identity_candidates pic
             JOIN properties pl ON pl.id = pic.left_property_id
             JOIN properties pr ON pr.id = pic.right_property_id
          WHERE pic.reviewed_action = 'operator'::text AND pic.status = 'dismissed'::text AND pl.status = 'active'::text AND pr.status = 'active'::text
        ), operator_unmerge AS (
         SELECT 'unmerge:'::text || pme.id AS label_id,
            pme.survivor_property_id AS left_property_id,
            pme.retired_property_id AS right_property_id,
            ( SELECT l.sreality_id
                   FROM listings l
                  WHERE l.property_id = pme.survivor_property_id AND l.sreality_id <> pme.listing_id AND NOT (EXISTS ( SELECT 1
                           FROM property_merge_events pme3
                          WHERE pme3.listing_id = l.sreality_id AND pme3.created_at > pme.created_at))
                  ORDER BY (source_trust_rank(l.source)), l.is_active DESC, l.last_seen_at DESC NULLS LAST, l.sreality_id DESC
                 LIMIT 1) AS left_listing_id,
            pme.listing_id AS right_listing_id,
            false AS is_same,
            'operator_unmerge'::text AS label_source,
            pr.category_main,
            NULL::text AS tier,
            pme.undone_at AS labeled_at,
            'unmerge'::text AS reason
           FROM property_merge_events pme
             JOIN properties pr ON pr.id = pme.retired_property_id
          WHERE pme.undone_by = 'operator'::text AND NOT (EXISTS ( SELECT 1
                   FROM listings l2
                  WHERE l2.sreality_id = pme.listing_id AND l2.property_id = pme.survivor_property_id))
        ), decision_feedback AS (
         SELECT 'feedback:'::text || ddf.id AS label_id,
            ddf.left_property_id,
            ddf.right_property_id,
            pl.repr_listing_id AS left_listing_id,
            pr.repr_listing_id AS right_listing_id,
            ddf.expected_outcome = 'should_merge'::text AS is_same,
            'decision_feedback'::text AS label_source,
            ddf.category_main,
            NULL::text AS tier,
            ddf.created_at AS labeled_at,
            COALESCE(ddf.expected_outcome, ''::text) AS reason
           FROM dedup_decision_feedback ddf
             JOIN properties pl ON pl.id = ddf.left_property_id
             JOIN properties pr ON pr.id = ddf.right_property_id
          WHERE ddf.is_incorrect = true AND (ddf.expected_outcome = ANY (ARRAY['should_merge'::text, 'should_dismiss'::text]))
        ), engine_site_plan_verdict AS (
         SELECT 'siteplan:'::text || pic.id AS label_id,
            pic.left_property_id,
            pic.right_property_id,
            pl.repr_listing_id AS left_listing_id,
            pr.repr_listing_id AS right_listing_id,
            false AS is_same,
            'engine_site_plan_verdict'::text AS label_source,
            pl.category_main,
            pic.tier,
            COALESCE(pic.reviewed_at, pic.created_at) AS labeled_at,
            'site_plan_different_unit'::text AS reason
           FROM property_identity_candidates pic
             JOIN properties pl ON pl.id = pic.left_property_id
             JOIN properties pr ON pr.id = pic.right_property_id
          WHERE (pic.markers_matched ->> 'reason'::text) = 'site_plan_different_unit'::text AND (pic.status = ANY (ARRAY['dismissed'::text, 'proposed'::text])) AND (pic.reviewed_action IS NULL OR pic.reviewed_action <> 'operator'::text) AND pl.status = 'active'::text AND pr.status = 'active'::text
        )
 SELECT operator_merge.label_id,
    operator_merge.left_property_id,
    operator_merge.right_property_id,
    operator_merge.left_listing_id,
    operator_merge.right_listing_id,
    operator_merge.is_same,
    operator_merge.label_source,
    operator_merge.category_main,
    operator_merge.tier,
    operator_merge.labeled_at,
    operator_merge.reason
   FROM operator_merge
UNION ALL
 SELECT operator_dismissal.label_id,
    operator_dismissal.left_property_id,
    operator_dismissal.right_property_id,
    operator_dismissal.left_listing_id,
    operator_dismissal.right_listing_id,
    operator_dismissal.is_same,
    operator_dismissal.label_source,
    operator_dismissal.category_main,
    operator_dismissal.tier,
    operator_dismissal.labeled_at,
    operator_dismissal.reason
   FROM operator_dismissal
UNION ALL
 SELECT operator_unmerge.label_id,
    operator_unmerge.left_property_id,
    operator_unmerge.right_property_id,
    operator_unmerge.left_listing_id,
    operator_unmerge.right_listing_id,
    operator_unmerge.is_same,
    operator_unmerge.label_source,
    operator_unmerge.category_main,
    operator_unmerge.tier,
    operator_unmerge.labeled_at,
    operator_unmerge.reason
   FROM operator_unmerge
UNION ALL
 SELECT decision_feedback.label_id,
    decision_feedback.left_property_id,
    decision_feedback.right_property_id,
    decision_feedback.left_listing_id,
    decision_feedback.right_listing_id,
    decision_feedback.is_same,
    decision_feedback.label_source,
    decision_feedback.category_main,
    decision_feedback.tier,
    decision_feedback.labeled_at,
    decision_feedback.reason
   FROM decision_feedback
UNION ALL
 SELECT engine_site_plan_verdict.label_id,
    engine_site_plan_verdict.left_property_id,
    engine_site_plan_verdict.right_property_id,
    engine_site_plan_verdict.left_listing_id,
    engine_site_plan_verdict.right_listing_id,
    engine_site_plan_verdict.is_same,
    engine_site_plan_verdict.label_source,
    engine_site_plan_verdict.category_main,
    engine_site_plan_verdict.tier,
    engine_site_plan_verdict.labeled_at,
    engine_site_plan_verdict.reason
   FROM engine_site_plan_verdict
) __admin_gate
where is_platform_admin();

create or replace view public.dedup_llm_cost_by_category_public as
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

create or replace view public.dedup_queue_snapshot_public as
select * from (SELECT c.tier,
    COALESCE(cat.category_main, 'ostatni'::text) AS category_main,
        CASE
            WHEN cat.category_type = ANY (ARRAY['prodej'::text, 'pronajem'::text]) THEN cat.category_type
            ELSE 'ostatni'::text
        END AS category_type,
    count(*)::integer AS pairs
   FROM property_identity_candidates c
     LEFT JOIN LATERAL ( SELECT l.category_main,
            l.category_type
           FROM listings l
          WHERE l.property_id = c.left_property_id
          ORDER BY l.sreality_id
         LIMIT 1) cat ON true
  WHERE c.status = 'proposed'::text
  GROUP BY c.tier, (COALESCE(cat.category_main, 'ostatni'::text)), (
        CASE
            WHEN cat.category_type = ANY (ARRAY['prodej'::text, 'pronajem'::text]) THEN cat.category_type
            ELSE 'ostatni'::text
        END)
) __admin_gate
where is_platform_admin();

create or replace view public.dedup_recency_backlog as
select * from (SELECT c.tier,
    count(*) FILTER (WHERE GREATEST(pl.first_seen_at, pr.first_seen_at) > (now() - '1 day'::interval)) AS unresolved_lt_1d,
    count(*) FILTER (WHERE GREATEST(pl.first_seen_at, pr.first_seen_at) > (now() - '3 days'::interval)) AS unresolved_lt_3d,
    count(*) FILTER (WHERE GREATEST(pl.first_seen_at, pr.first_seen_at) > (now() - '7 days'::interval)) AS unresolved_lt_7d,
    count(*) AS unresolved_total
   FROM property_identity_candidates c
     JOIN properties pl ON pl.id = c.left_property_id
     JOIN properties pr ON pr.id = c.right_property_id
  WHERE c.status = 'proposed'::text
  GROUP BY ROLLUP(c.tier)
) __admin_gate
where is_platform_admin();

create or replace view public.dedup_scan_state_public as
select * from (SELECT lane,
    cursor_key IS NOT NULL AS mid_cycle,
    cycle_started_at,
    last_cycle_started_at,
    last_cycle_completed_at,
    updated_at
   FROM dedup_scan_state
) __admin_gate
where is_platform_admin();

create or replace view public.dedup_vision_bakeoff_results_public as
select * from (SELECT id,
    run_label,
    set_name,
    check_type,
    lane,
    model,
    sreality_id_a,
    sreality_id_b,
    room_type,
    is_same,
    label_source,
    category_main,
    expected_verdict,
    danger_verdict,
    candidate_verdict,
    is_correct,
    is_dangerous,
    cost_usd,
    created_at
   FROM dedup_vision_bakeoff_results
) __admin_gate
where is_platform_admin();

create or replace view public.detail_latency_recent as
select * from (SELECT source,
    count(*) AS completions,
    percentile_cont(0.5::double precision) WITHIN GROUP (ORDER BY (EXTRACT(epoch FROM completed_at - enqueued_at)::double precision)) AS p50_seconds,
    percentile_cont(0.9::double precision) WITHIN GROUP (ORDER BY (EXTRACT(epoch FROM completed_at - enqueued_at)::double precision)) AS p90_seconds
   FROM detail_queue_completions
  WHERE completed_at > (now() - '24:00:00'::interval)
  GROUP BY source
) __admin_gate
where is_platform_admin();

create or replace view public.image_border_cases_public as
select * from (SELECT image_id,
    created_at
   FROM image_border_cases
) __admin_gate
where is_platform_admin();

create or replace view public.image_tag_annotations_public as
select * from (SELECT image_id,
    tag_flagged,
    render_flagged,
    note,
    updated_at
   FROM image_tag_annotations
) __admin_gate
where is_platform_admin();

create or replace view public.image_training_examples_public as
select * from (SELECT image_id,
    label,
    updated_at
   FROM image_training_examples
) __admin_gate
where is_platform_admin();

create or replace view public.listing_detail_queue_public as
select * from (SELECT source,
    priority,
    enqueued_at,
    claimed_at,
    given_up
   FROM listing_detail_queue
) __admin_gate
where is_platform_admin();

create or replace view public.listing_fetch_failures_public as
select * from (SELECT sreality_id,
    attempts,
    first_failure_at,
    last_failure_at,
    given_up
   FROM listing_fetch_failures
) __admin_gate
where is_platform_admin();

create or replace view public.llm_cost_daily_public as
select * from (SELECT called_at::date AS day,
    called_for,
    provider,
    model,
    count(*)::integer AS calls,
    count(*) FILTER (WHERE error IS NOT NULL)::integer AS error_calls,
    round(sum(cost_usd), 4) AS cost_usd,
    sum(input_tokens) AS input_tokens,
    sum(output_tokens) AS output_tokens,
    sum(cache_read_tokens) AS cache_read_tokens,
    sum(cache_write_tokens) AS cache_write_tokens
   FROM llm_calls l
  GROUP BY (called_at::date), called_for, provider, model
) __admin_gate
where is_platform_admin();

create or replace view public.llm_cost_hourly_public as
select * from (SELECT date_trunc('hour'::text, called_at) AS bucket,
    called_for,
    provider,
    model,
    count(*)::integer AS calls,
    count(*) FILTER (WHERE error IS NOT NULL)::integer AS error_calls,
    round(sum(cost_usd), 4) AS cost_usd,
    sum(input_tokens) AS input_tokens,
    sum(output_tokens) AS output_tokens,
    sum(cache_read_tokens) AS cache_read_tokens,
    sum(cache_write_tokens) AS cache_write_tokens
   FROM llm_calls l
  GROUP BY (date_trunc('hour'::text, called_at)), called_for, provider, model
) __admin_gate
where is_platform_admin();

create or replace view public.parsed_url_activity as
select * from (SELECT source_kind AS source,
    count(*) AS parses_total,
    count(*) FILTER (WHERE parsed_at > (now() - '30 days'::interval)) AS parses_30d,
    max(parsed_at) AS last_parsed_at
   FROM parsed_url_cache
  GROUP BY source_kind
) __admin_gate
where is_platform_admin();

create or replace view public.phash_pair_notes_public as
select * from (SELECT image_id_a,
    image_id_b,
    note,
    updated_at
   FROM phash_pair_notes
) __admin_gate
where is_platform_admin();

create or replace view public.pipeline_check_history_public as
select * from (SELECT check_key,
    run_at,
    status,
    value,
    details
   FROM pipeline_check_results
  WHERE run_at > (now() - '30 days'::interval)
) __admin_gate
where is_platform_admin();

create or replace view public.pipeline_checks_public as
select * from (SELECT DISTINCT ON (check_key) check_key,
    run_at,
    status,
    value,
    details,
    created_at
   FROM pipeline_check_results
  ORDER BY check_key, run_at DESC
) __admin_gate
where is_platform_admin();

create or replace view public.publication_gate_health_public as
select * from (SELECT count(*) AS unpublished,
    min(first_seen_at) AS oldest_unpublished_at,
    ( SELECT count(*) AS count
           FROM properties properties_1
          WHERE properties_1.status = 'active'::text) AS active_total
   FROM properties
  WHERE published_at IS NULL AND status = 'active'::text
) __admin_gate
where is_platform_admin();

-- Functions: same technique, folded into the existing SQL body (no new base-table
-- policy, no change to the existing GRANT EXECUTE ... TO authenticated -- the gate
-- is inside the function body, matching the pattern RLS policies elsewhere already
-- use, e.g. estimation_runs_tenant_read's `... OR (account_id IS NULL AND
-- is_platform_admin())`).

create or replace function public.images_failure_overview()
 returns table(source text, bucket text, detail text, n bigint)
 language sql stable security definer
 set search_path to 'public'
as $function$
  select m.source, m.bucket, m.detail, m.n
  from images_failure_overview_mv m
  where is_platform_admin();
$function$;

create or replace function public.recent_workflow_failures(p_hours integer default 48)
 returns table(workflow_name text, conclusion text, run_started_at timestamp with time zone, html_url text)
 language sql stable security definer
 set search_path to 'public'
as $function$
  select workflow_name, conclusion, run_started_at, html_url
  from workflow_failures
  where recorded_at > now() - make_interval(hours => p_hours)
    and is_platform_admin()
  order by run_started_at desc nulls last
$function$;

create or replace function public.workflow_failure_summary(p_hours integer default 168)
 returns table(workflow_path text, workflow_name text, failure_count bigint, first_failure_at timestamp with time zone, last_failure_at timestamp with time zone, last_conclusion text, last_html_url text, last_success_at timestamp with time zone, consecutive_failures bigint, is_chronic boolean)
 language sql stable security definer
 set search_path to 'public'
as $function$
  with win as (
    select wf.*
    from workflow_failures wf
    where wf.recorded_at > now() - make_interval(hours => p_hours)
      and wf.workflow_path is not null
      and is_platform_admin()
  ),
  grouped as (
    select
      w.workflow_path,
      (array_agg(w.workflow_name order by w.run_started_at desc nulls last))[1] as workflow_name,
      count(*)                                                               as failure_count,
      min(w.run_started_at)                                                  as first_failure_at,
      max(w.run_started_at)                                                  as last_failure_at,
      (array_agg(w.conclusion order by w.run_started_at desc nulls last))[1] as last_conclusion,
      (array_agg(w.html_url   order by w.run_started_at desc nulls last))[1] as last_html_url
    from win w
    group by w.workflow_path
  )
  select
    g.workflow_path,
    g.workflow_name,
    g.failure_count,
    g.first_failure_at,
    g.last_failure_at,
    g.last_conclusion,
    g.last_html_url,
    h.last_success_at,
    streak.consecutive_failures,
    (streak.consecutive_failures >= 3) as is_chronic
  from grouped g
  left join workflow_run_health h on h.workflow_path = g.workflow_path
  cross join lateral (
    select count(*) as consecutive_failures
    from win w2
    where w2.workflow_path = g.workflow_path
      and (h.last_success_at is null or w2.run_started_at > h.last_success_at)
  ) streak
  order by is_chronic desc, streak.consecutive_failures desc, g.last_failure_at desc;
$function$;

commit;