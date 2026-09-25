-- 562_autodedup_trial_scope.sql
--
-- AUTODEDUP production sprint W2 (decision 1): the apply scope names the trial.
--
-- `app_settings.autodedup_apply_scope` is the ONE rollout control of the apply path (lane mode
-- `apply`, autodedup/apply.py). Migration 558 seeded it with sales and NO area, so nothing
-- could merge. It now names the trial: sales (`prodej`) in Jablonec nad Nisou
-- (town:563510), Turnov (town:577626) and Praha-Vysocany (quarter:490245), spelled as the
-- export lane and `autodedup.apply.Scope` spell a block. Every key the scope parser reads is
-- written out: no advert list, no whole-country flag, and the seeded run cap of 200 groups.
--
-- Nothing merges on this alone: a live run still needs a dispatch with dry_run=0, and
-- emptying the area on /settings stops a run between two groups (E39 / E904). With
-- `retire_legacy=1` the same dispatch first undoes the old engine's intact merges inside
-- these blocks (autodedup/legacy_retire.py, temporary, deleted in W5).
--
-- Decision 1 of 2026-09-25 is the authority for this value. W6 deletes the row, when every
-- area and category merges (decision 6).
-- Migration 561 was taken by the sibling one-property-view branch; this is the next free number.
--
-- Data, not schema: one row updated. Reversible: migration 020's trigger archives the
-- previous value into app_settings_history. Fails loudly when 558 has not been applied.

set lock_timeout = '5s';

do $$
begin
  update public.app_settings
     set value = '{"category_types": ["prodej"], "blocks": ["town:563510", "town:577626", "quarter:490245"], "listing_ids": null, "all_blocks": false, "max_clusters_per_run": 200}'::jsonb,
         updated_at = now(),
         updated_by = 'migration 562'
   where key = 'autodedup_apply_scope';
  if not found then
    raise exception 'app_settings.autodedup_apply_scope is missing: apply migration 558 first';
  end if;
end
$$;

reset lock_timeout;
