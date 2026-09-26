-- 572_autodedup_lane_resume_after_reseed.sql
--
-- AUTODEDUP W5 "one lane": resume the worker's lane at 60 s after the w31 re-seed
-- (`rt_seed fresh=true settings=w31,model=w6_gold`, which wrote `rt_seed_version:rt` and
-- set `rt_bootstrap:rt` = true). The lane now rebuilds the live generation under w31 while
-- the scope's blocks are still empty (migration 569): the reconcile merges nothing until
-- migration 570 restores the three trial blocks after the G3 prediction is recorded.
--
-- Brake: interval 0 on /settings, then mode=unapply.
-- Data, not schema: one row updated; migration 020's trigger archives the previous value.

set lock_timeout = '5s';

do $$
begin
  update public.app_settings
     set value = '60'::jsonb,
         updated_at = now(),
         updated_by = 'migration 572'
   where key = 'realtime_autodedup_interval_seconds';
  if not found then
    raise exception 'app_settings.realtime_autodedup_interval_seconds is missing: apply migration 557 first';
  end if;
end
$$;

reset lock_timeout;
