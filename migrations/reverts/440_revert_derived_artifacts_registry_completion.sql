-- 440_revert_derived_artifacts_registry_completion.sql
--
-- REVERT for 440_derived_artifacts_registry_completion.sql. SHIPPED UNAPPLIED.
--
-- Lives in migrations/reverts/ for the same two verified reasons as every revert in this
-- build: the CI schema replay applies `ls migrations/*.sql | sort` (NOT recursive), so a
-- revert beside its forward migration would be applied right after it and silently undo the
-- change in every replayed environment; and tests/test_migration_numbers.py globs the same
-- way and forbids a duplicate number above 304.
--
-- WHAT APPLYING THIS COSTS, stated plainly so it is never done casually:
--   * it takes the platform back to a freshness registry that describes ONE artifact
--     honestly and two by adaptation, and thirteen not at all -- which is the exact
--     condition Corollary E exists to end;
--   * it re-introduces the singleton-with-a-column-pair-per-artifact shape, so registering
--     the next artifact needs a migration that ALTERs a table rather than an INSERT;
--   * tests/test_derived_artifacts_registry.py goes RED unless `_W7_BACKLOG` is restored in
--     the same git revert -- the rail is doing its job, do not "fix" it by re-adding names.
-- Prefer fixing forward.
--
-- ORDER IS INVERTED ON PURPOSE and matters as much here as in the forward migration: the
-- table must exist again BEFORE either rebuild function is pointed back at it, or the first
-- tick after this script raises inside `exception when others then pg_advisory_unlock(...);
-- raise;`, rolls back the whole rebuild, and stops republishing browse_list.
--
-- The two function bodies below are the LIVE 2026-08-26 bodies with the stamp block put
-- back exactly as it was; their pre-440 `md5(prosrc)` values are
--   rebuild_browse_list        e2b5e3220f8bb5d81ef3f09bcc379f7c
--   rebuild_properties_map_mv  9997436666360ab044d9a56fabcd14b4
-- so applying this script and re-hashing is a complete check that the revert is exact.

begin;

set local lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 1. The singleton comes back (migration 276's shape), and is re-seeded from
--    the registry rows that replaced it -- so no freshness reading is lost.
-- ---------------------------------------------------------------------------

create table if not exists browse_read_model_state (
  id               smallint primary key default 1 check (id = 1),
  list_rebuilt_at  timestamptz,
  list_duration_ms integer,
  list_rows        bigint,
  map_rebuilt_at   timestamptz,
  map_duration_ms  integer,
  map_rows         bigint
);

insert into browse_read_model_state (id) values (1) on conflict (id) do nothing;

update browse_read_model_state s
   set list_rebuilt_at  = l.last_succeeded_at,
       list_duration_ms = l.last_duration_ms,
       list_rows        = l.last_rows,
       map_rebuilt_at   = m.last_succeeded_at,
       map_duration_ms  = m.last_duration_ms,
       map_rows         = m.last_rows
  from (select * from public.derived_artifacts where name = 'browse_list') l,
       (select * from public.derived_artifacts where name = 'properties_map_mv') m
 where s.id = 1;

alter table browse_read_model_state enable row level security;
revoke all on browse_read_model_state from public, anon, authenticated;

create or replace view browse_read_model_state_public as
  select list_rebuilt_at, list_duration_ms, list_rows,
         map_rebuilt_at, map_duration_ms, map_rows
    from browse_read_model_state;
grant select on browse_read_model_state_public to anon, authenticated;

-- ---------------------------------------------------------------------------
-- 2. Both producers point back at the singleton
-- ---------------------------------------------------------------------------

create or replace function rebuild_browse_list()
returns void
language plpgsql
security definer
set search_path = public
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
    execute $q$
      create unlogged table browse_list_next as
      select * from browse_projection
      order by category_main, category_type, first_seen_at
    $q$;
    execute 'create unique index browse_list_next_pk on browse_list_next (property_id)';
    execute 'create index browse_list_next_cat_first_seen_idx on browse_list_next (category_main, category_type, first_seen_at desc, property_id desc)';
    execute 'create index browse_list_next_obec_price_idx on browse_list_next (obec_id, category_type, price_czk, property_id, category_main, subtype, disposition, area_m2, is_active) where obec_id is not null';
    execute 'create index browse_list_next_okres_price_idx on browse_list_next (okres_id, category_type, price_czk, property_id, category_main, subtype, disposition, area_m2, is_active) where okres_id is not null';
    execute 'create index browse_list_next_region_price_idx on browse_list_next (region_id, category_type, price_czk, property_id, category_main, subtype, disposition, area_m2, is_active) where region_id is not null';
    execute 'analyze browse_list_next';
    execute 'select count(*) from browse_list_next' into n;

    execute 'drop table if exists browse_list';
    execute 'alter table browse_list_next rename to browse_list';
    execute 'alter index browse_list_next_pk rename to browse_list_pk';
    execute 'alter index browse_list_next_cat_first_seen_idx rename to browse_list_cat_first_seen_idx';
    execute 'alter index browse_list_next_obec_price_idx rename to browse_list_obec_price_idx';
    execute 'alter index browse_list_next_okres_price_idx rename to browse_list_okres_price_idx';
    execute 'alter index browse_list_next_region_price_idx rename to browse_list_region_price_idx';
    execute 'grant select on browse_list to authenticated';
    execute 'revoke insert, update, delete, truncate on browse_list from anon, authenticated';

    if has_table_privilege('anon', 'browse_list', 'SELECT') then
      raise exception 'rebuild_browse_list: anon must never hold SELECT on browse_list -- refusing to publish this rebuild (see migration 374)';
    end if;

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

create or replace function rebuild_properties_map_mv()
returns void
language plpgsql
security definer
set search_path = public
as $fn$
declare
  t0 timestamptz := clock_timestamp();
  n  bigint;
begin
  if not pg_try_advisory_lock(hashtext('rebuild_properties_map_mv')) then
    raise notice 'rebuild_properties_map_mv: previous run still active, skipping tick';
    return;
  end if;
  begin
    execute 'drop materialized view if exists properties_map_mv_next';
    execute $q$
      create materialized view properties_map_mv_next as
      select * from browse_projection
      where lat is not null and lng is not null
      order by category_main, category_type, lat, lng
    $q$;
    execute 'create unique index properties_map_mv_next_pk on properties_map_mv_next (property_id)';
    execute $q$
      create index properties_map_mv_next_cover on properties_map_mv_next
        (category_main, category_type, lat, lng)
        include (sreality_id, price_czk, disposition, subtype, area_m2, district,
                 last_seen_at, first_seen_at, is_active)
    $q$;
    execute 'analyze properties_map_mv_next';
    execute 'select count(*) from properties_map_mv_next' into n;

    execute 'drop materialized view if exists properties_map_mv';
    execute 'alter materialized view properties_map_mv_next rename to properties_map_mv';
    execute 'alter index properties_map_mv_next_pk rename to properties_map_mv_pk';
    execute 'alter index properties_map_mv_next_cover rename to properties_map_mv_cover';
    execute 'grant select on properties_map_mv to authenticated';

    if has_table_privilege('anon', 'properties_map_mv', 'SELECT') then
      raise exception 'rebuild_properties_map_mv: anon must never hold SELECT on properties_map_mv -- refusing to publish this rebuild (see migration 374)';
    end if;

    update browse_read_model_state
       set map_rebuilt_at  = now(),
           map_duration_ms = (extract(epoch from clock_timestamp() - t0) * 1000)::integer,
           map_rows        = n
     where id = 1;
    perform pg_notify('pgrst', 'reload schema');
  exception when others then
    perform pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));
    raise;
  end;
  perform pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));
end
$fn$;

-- ---------------------------------------------------------------------------
-- 3. The adapter goes back into derived_artifacts_public, and the thirteen
--    registry rows this wave added come out.
-- ---------------------------------------------------------------------------
--
-- `llm_cost_hour_rollup` is NOT deleted -- it is migration 437's row, not this wave's.
-- The delete is spelled as "everything except 437's row" rather than as a name list so it
-- cannot drift out of step with the seed above.

create or replace view public.derived_artifacts_public as
  select a.name, a.producer, a.host, a.cadence, a.staleness_budget,
         a.complete_through, a.last_succeeded_at,
         a.last_duration_ms, a.last_rows, a.is_serving
    from public.derived_artifacts a
  union all
  select v.name, v.producer, 'pg_cron', v.cadence, v.staleness_budget,
         v.rebuilt_at, v.rebuilt_at,
         v.duration_ms, v.n_rows, true
    from public.browse_read_model_state b
    cross join lateral (values
      ('browse_list', 'rebuild_browse_list', '*/15 * * * *', interval '45 minutes',
        b.list_rebuilt_at, b.list_duration_ms, b.list_rows),
      ('properties_map_mv', 'rebuild_properties_map_mv', '7,37 * * * *', interval '90 minutes',
        b.map_rebuilt_at, b.map_duration_ms, b.map_rows)
    ) as v(name, producer, cadence, staleness_budget, rebuilt_at, duration_ms, n_rows);

grant select on public.derived_artifacts_public to authenticated;

delete from public.derived_artifacts where name <> 'llm_cost_hour_rollup';

commit;
