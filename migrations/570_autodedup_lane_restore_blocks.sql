-- 570_autodedup_lane_restore_blocks.sql
--
-- AUTODEDUP production sprint W5 "one lane", gate step 5 (PR #1623 body): the scope's blocks
-- come back after the G3 prediction was recorded (a dry run of `rt` over the three trial
-- blocks). From this row on, the lane's reconcile (autodedup/reconcile.py) merges inside the
-- trial area on its own clock — sales and rentals in Jablonec nad Nisou (town:563510), Turnov
-- (town:577626) and Praha-Vysočany (quarter:490245) — and its first ledger rows
-- (`autodedup.applied_merges`, generation 'rt', run_id 'rt:<holder>') must equal that
-- prediction (G3). The interval row is untouched (60 since migration 569).
--
-- Brake: interval 0 on /settings, then mode=unapply. W6 deletes the scope row.
-- Data, not schema: one row updated; migration 020's trigger archives the previous value.

set lock_timeout = '5s';

do $$
begin
  update public.app_settings
     set value = '{"category_types": ["prodej", "pronajem"], "blocks": ["town:563510", "town:577626", "quarter:490245"], "listing_ids": null, "all_blocks": false, "max_clusters_per_run": 600}'::jsonb,
         updated_at = now(),
         updated_by = 'migration 570'
   where key = 'autodedup_apply_scope';
  if not found then
    raise exception 'app_settings.autodedup_apply_scope is missing: apply migration 558 first';
  end if;
end
$$;

reset lock_timeout;
