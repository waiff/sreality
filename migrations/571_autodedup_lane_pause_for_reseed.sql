-- 571_autodedup_lane_pause_for_reseed.sql
--
-- AUTODEDUP W5 "one lane": pause the worker's lane so `rt_seed fresh=true` can take the lane
-- lease. The engine wave S15b (settings w31, PR #1619) merged after migration 569 started the
-- shadow build under the w5 seed (settings w29); the live generation must be rebuilt under
-- w31 before the first reconcile. A fresh seed takes the same lease the lane's passes hold
-- (E914: one writer), so the interval goes to 0 first; the running pass ends within its 1,050 s
-- bound and releases the lease; then the seed runs; migration 572 resumes the lane at 60.
--
-- The scope's blocks stay empty (migration 569): nothing merges in either state.
-- Data, not schema: one row updated; migration 020's trigger archives the previous value.

set lock_timeout = '5s';

do $$
begin
  update public.app_settings
     set value = '0'::jsonb,
         updated_at = now(),
         updated_by = 'migration 571'
   where key = 'realtime_autodedup_interval_seconds';
  if not found then
    raise exception 'app_settings.realtime_autodedup_interval_seconds is missing: apply migration 557 first';
  end if;
end
$$;

reset lock_timeout;
