-- 569_autodedup_lane_shadow_build.sql
--
-- AUTODEDUP production sprint W5 "one lane", gate step 2 (PR #1623 body; PROGRAM.md E908–E915):
-- the worker's autodedup lane starts its shadow build. Two app_settings rows change together:
--
--   1. `autodedup_apply_scope` keeps sales + rentals and the run cap but names NO blocks, so
--      the reconcile finds every group out of scope (`scope_closed`) while the lane bootstraps
--      its `rt` generation and its rate is measured (G2 reads only passes after the blocks are
--      restored, review B3).
--   2. `realtime_autodedup_interval_seconds` 0 → 60: the lane runs every minute. The reconcile
--      merges nothing until `rt_seed_version:rt` = 'w5' (written by rt_seed run 36233648037),
--      `rt_bootstrap:rt` is false AND the scope names blocks — migration 570 restores them
--      after the G3 prediction is recorded.
--
-- Brake: interval 0 on /settings (this row), then mode=unapply. The batch modes apply/unapply
-- refuse while the interval is above 0 (E913 fix, review A3).
-- Data, not schema: two rows updated; migration 020's trigger archives the previous values.
-- Fails loudly when either row is missing.

set lock_timeout = '5s';

do $$
begin
  update public.app_settings
     set value = '{"category_types": ["prodej", "pronajem"], "blocks": [], "listing_ids": null, "all_blocks": false, "max_clusters_per_run": 600}'::jsonb,
         updated_at = now(),
         updated_by = 'migration 569'
   where key = 'autodedup_apply_scope';
  if not found then
    raise exception 'app_settings.autodedup_apply_scope is missing: apply migration 558 first';
  end if;

  update public.app_settings
     set value = '60'::jsonb,
         updated_at = now(),
         updated_by = 'migration 569'
   where key = 'realtime_autodedup_interval_seconds';
  if not found then
    raise exception 'app_settings.realtime_autodedup_interval_seconds is missing: apply migration 557 first';
  end if;
end
$$;

reset lock_timeout;
