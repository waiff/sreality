-- 582_autodedup_apply_scope_share_sales.sql
--
-- AUTODEDUP E927 (PR #1655): a share-sale advert (`podil`, sreality's "Podíly" section) merges
-- with the sale advert (`prodej`) of the same property at one stated price (operator ruling
-- 2026-09-30). On 2026-10-01 the operator answered N9: admit `podil` into the live apply scope
-- in the same release. The scope row (558 -> 562 -> 563 -> 570) names `prodej, pronajem`, and
-- `Scope.group_outside` refuses a WHOLE group when any member is outside it, so without this
-- row a sale group that absorbs its `podil` twin stops merging (`category_outside_scope`): two
-- trial groups today, about 1,400 nationally as the scope widens. `category_types` gains
-- `podil`; the blocks and the 600 cap are migration 570's, unchanged.
--
-- Applied TOGETHER with the release that carries E927 (apply_migration.yml), never before: the
-- scope admits whatever the running engine groups, and the engine measured with `podil` in
-- scope is E927's (under the base engine every deal-type gate keeps a share apart from a sale,
-- and a `podil`-only group would merge unmeasured). The release's `rt_seed fresh=true` re-seed
-- is what reaches the stored `podil` listings.
-- Data, not schema: one row updated (idempotent); migration 020's trigger archives the previous
-- value into app_settings_history. Fails loudly when 558 has not been applied.

set lock_timeout = '5s';

do $$
begin
  update public.app_settings
     set value = '{"category_types": ["prodej", "pronajem", "podil"], "blocks": ["town:563510", "town:577626", "quarter:490245"], "listing_ids": null, "all_blocks": false, "max_clusters_per_run": 600}'::jsonb,
         updated_at = now(),
         updated_by = 'migration 582'
   where key = 'autodedup_apply_scope';
  if not found then
    raise exception 'app_settings.autodedup_apply_scope is missing: apply migration 558 first';
  end if;
end
$$;

reset lock_timeout;
