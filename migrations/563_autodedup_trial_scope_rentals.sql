-- 563_autodedup_trial_scope_rentals.sql
--
-- AUTODEDUP production sprint, checkpoint 1: the trial scope adds rentals.
--
-- Migration 562 named the trial as sales (`prodej`) in Jablonec nad Nisou (town:563510),
-- Turnov (town:577626) and Praha-Vysočany (quarter:490245). Reading Browse on 2026-09-25 the
-- operator found the same duplicate rows among rentals and ruled that rentals join the trial
-- now ("update the json line yourself, so leases are in the eval too"): `category_types` gains
-- `pronajem`, and the run cap rises from 200 to 600 groups so a re-apply of one generation
-- (about 300 sales groups already done, up to 567 rental groups) takes two dispatches, not
-- five. Rentals never merge with sales (rule 15, the `category_type_mix` refusal): the wider
-- scope only lets rental groups through the same gate, and `retire_legacy=1` now also undoes
-- the old engine's intact rental merges inside the blocks.
--
-- Nothing merges on this alone: a live run still needs a dispatch with dry_run=0.
-- Data, not schema: one row updated; migration 020's trigger archives the previous value into
-- app_settings_history. Fails loudly when 558 has not been applied.

set lock_timeout = '5s';

do $$
begin
  update public.app_settings
     set value = '{"category_types": ["prodej", "pronajem"], "blocks": ["town:563510", "town:577626", "quarter:490245"], "listing_ids": null, "all_blocks": false, "max_clusters_per_run": 600}'::jsonb,
         updated_at = now(),
         updated_by = 'migration 563'
   where key = 'autodedup_apply_scope';
  if not found then
    raise exception 'app_settings.autodedup_apply_scope is missing: apply migration 558 first';
  end if;
end
$$;

reset lock_timeout;
