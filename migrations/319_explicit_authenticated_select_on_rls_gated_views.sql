-- 319: make the `authenticated` SELECT grant on the migration-316/318 views EXPLICIT.
--
-- CI caught this, not a live gap: production already has `authenticated` SELECT on
-- every one of these views (verified live) -- it arrived implicitly via Supabase's
-- pre-299 default ACL (every new table/view auto-granted to anon+authenticated) at
-- the time each view was originally created (migrations 022-282, all long before 299
-- closed that default). scripts/ci_db_bootstrap.sql does NOT replicate that default
-- ACL (documented caveat on the Phase 0 CI gate), so the CI schema-replay DB never
-- granted `authenticated` anything on these views -- exposed for the first time by
-- migration 318's new live regression tests, which are the first ones in the suite
-- to actually SELECT a _public/admin-ops view as `authenticated` rather than the
-- base table. Not a security change: RLS (316) / the embedded is_platform_admin()
-- filter (318) already do 100% of the access-control work here -- this migration
-- only makes CI's schema-replay match what production has always had, and hardens
-- against ever needing to rely on an implicit, undocumented default-ACL timing
-- window again (a real disaster-recovery rebuild from migrations should not depend
-- on hitting that window in the same order).
--
-- `anon` deliberately gets nothing here -- Phase 0's settled decision is fully
-- login-gated (anon revoked to ~nothing); these views only ever needed to serve
-- `authenticated`.

begin;

grant select on
  public.collections_public,
  public.property_pipeline_public,
  public.pipeline_stages_public,
  public.property_notes_public,
  public.property_tags_public,
  public.tags_public,
  public.collection_properties_public,
  public.property_estimates_public,
  public.data_quality_by_source,
  public.dedup_engine_flow_public,
  public.dedup_engine_runs_public,
  public.dedup_funnel_resolutions_public,
  public.dedup_label_events,
  public.dedup_llm_cost_by_category_public,
  public.dedup_queue_snapshot_public,
  public.dedup_recency_backlog,
  public.dedup_scan_state_public,
  public.dedup_vision_bakeoff_results_public,
  public.detail_latency_recent,
  public.image_border_cases_public,
  public.image_tag_annotations_public,
  public.image_training_examples_public,
  public.listing_detail_queue_public,
  public.listing_fetch_failures_public,
  public.llm_cost_daily_public,
  public.llm_cost_hourly_public,
  public.parsed_url_activity,
  public.phash_pair_notes_public,
  public.pipeline_check_history_public,
  public.pipeline_checks_public,
  public.publication_gate_health_public
  to authenticated;

commit;
