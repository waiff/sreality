-- 283_browse_list_district_price_covering.sql
--
-- Browse read model: restore migration 253's FULL mechanism for the
-- district+price-sort lanes on browse_list.
--
-- Migration 277's lean trio kept 253's key prefix (district_id, category_type,
-- price_czk) but dropped its TRAILING covering key columns. That reopened the
-- exact incident 253 documents: for the operator's "Domy - Praha" preset
-- (kraj chip + price ASC + subtype/area filters) the price-ordered hunt
-- evaluated category_main / subtype / area_m2 / is_active on HEAP tuples —
-- measured live 2026-07-09: Index Scan on browse_list_region_price_idx,
-- "Rows Removed by Filter: 12,655" = ~12.5k random heap fetches, 18.9s.
-- Worse than the old table, structurally: the blue-green rebuild gives
-- browse_list a NEW relfilenode every 5 minutes, so these pages can never
-- stay cached — the lane is permanently cold.
--
-- Fix (the 253-proven mechanism): trailing btree KEY columns are evaluated on
-- INDEX tuples, so non-matching candidates are skipped with zero heap access;
-- property_id as the 4th key makes the index order exactly the keyset
-- ORDER BY (price_czk, property_id) — no Incremental Sort. disposition is
-- included as the common byt refinement alongside this shape. The heap is
-- touched only for the LIMIT-24 result rows. Exact counts on these shapes
-- become index-only (CTAS marks pages all-visible), so the "~N" fallback
-- disappears for district+price cohorts.
--
-- This redefines rebuild_browse_list() verbatim from migration 277 except the
-- three index DDL lines (the swap/rename lines are unchanged — index NAMES
-- stay the same). The next 5-minute cron tick rebuilds browse_list with the
-- widened trio; no reader change. The migration-276 shell indexes are
-- superseded on the first rebuild, as always.

create or replace function rebuild_browse_list()
returns void
language plpgsql
security definer
set search_path = public
set statement_timeout = '600s'
as $fn$
declare
  t0 timestamptz := clock_timestamp();
  n  bigint;
begin
  if not pg_try_advisory_lock(hashtext('rebuild_browse_list')) then
    raise notice 'rebuild_browse_list: previous run still active, skipping tick';
    return;
  end if;
  begin
    execute 'drop table if exists browse_list_next';
    -- UNLOGGED + physical order (category, type, first_seen): every
    -- within-category query reads a contiguous band, which is what lets the
    -- index set stay lean (see migration 276's rationale).
    execute $q$
      create unlogged table browse_list_next as
      select * from browse_projection
      order by category_main, category_type, first_seen_at
    $q$;
    -- Keep in lockstep with migration 276's shell indexes.
    execute 'create unique index browse_list_next_pk on browse_list_next (property_id)';
    execute 'create index browse_list_next_cat_first_seen_idx on browse_list_next (category_main, category_type, first_seen_at desc, property_id desc)';
    -- District+price-sort lanes: the migration-253 mechanism, fully. The
    -- trailing KEY columns (property_id tiebreak + the hot Browse predicates)
    -- are evaluated on INDEX tuples ("Index Cond", zero heap fetch for
    -- non-matches) — without them the price-ordered hunt heap-fetches every
    -- candidate (measured live: "Domy - Praha" preset, 12,655 rows removed by
    -- heap Filter, 18.9s — on a table whose cache resets every 5-min rebuild).
    -- property_id as the 4th key serves the keyset ORDER BY exactly.
    execute 'create index browse_list_next_obec_price_idx on browse_list_next (obec_id, category_type, price_czk, property_id, category_main, subtype, disposition, area_m2, is_active) where obec_id is not null';
    execute 'create index browse_list_next_okres_price_idx on browse_list_next (okres_id, category_type, price_czk, property_id, category_main, subtype, disposition, area_m2, is_active) where okres_id is not null';
    execute 'create index browse_list_next_region_price_idx on browse_list_next (region_id, category_type, price_czk, property_id, category_main, subtype, disposition, area_m2, is_active) where region_id is not null';
    execute 'analyze browse_list_next';
    execute 'select count(*) from browse_list_next' into n;

    -- The swap: short ACCESS EXCLUSIVE window.
    execute 'drop table if exists browse_list';
    execute 'alter table browse_list_next rename to browse_list';
    execute 'alter index browse_list_next_pk rename to browse_list_pk';
    execute 'alter index browse_list_next_cat_first_seen_idx rename to browse_list_cat_first_seen_idx';
    execute 'alter index browse_list_next_obec_price_idx rename to browse_list_obec_price_idx';
    execute 'alter index browse_list_next_okres_price_idx rename to browse_list_okres_price_idx';
    execute 'alter index browse_list_next_region_price_idx rename to browse_list_region_price_idx';
    execute 'grant select on browse_list to anon, authenticated';

    update browse_read_model_state
       set list_rebuilt_at  = now(),
           list_duration_ms = (extract(epoch from clock_timestamp() - t0) * 1000)::integer,
           list_rows        = n
     where id = 1;
    perform pg_notify('pgrst', 'reload schema');
  exception when others then
    perform pg_advisory_unlock(hashtext('rebuild_browse_list'));
    raise;
  end;
  perform pg_advisory_unlock(hashtext('rebuild_browse_list'));
end
$fn$;
