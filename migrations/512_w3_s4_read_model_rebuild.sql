-- 512_w3_s4_read_model_rebuild.sql
--
-- Finishes migration 506 under a bigger budget. No schema change of its own.
--
-- WHAT HAPPENED. 506 §8 rebuilds both read models in autocommit under a 900 s
-- statement_timeout, sized off a 270 s average. Applied 2026-09-13 10:40 the
-- rebuild took longer than that and was CANCELLED, which left the two halves of
-- 506 out of step: the views were already narrowed and committed, while
-- `browse_list` kept its eight now-dropped columns and `properties_map_mv` -- which
-- §1 drops and §8 recreates -- did not exist at all. The */15 and 7,37 cron ticks
-- that normally close such a gap could not: they carry a 600 s budget and were
-- cancelled twice in a row against the same load.
--
-- WHY IT IS SLOWER THAN 270 s. W3 added two joins to `browse_projection`
-- (`listing_location`, ~756k rows, plus the granularity lookup), and the apply
-- landed while the W4 backup copy (migration 511, 830k rows plus 325 MB of
-- tables) had just evicted most of the buffer cache. Every wait in
-- `pg_stat_activity` during the failed runs was `IO / DataFileRead`. The work is
-- unchanged in shape -- it simply needs more than ten minutes on a cold cache.
--
-- A NEW FILE RATHER THAN AN EDIT TO 506. Migrations are append-only (CLAUDE.md
-- rule 1): 506 is merged and partly applied, and editing it would rewrite what
-- the database already ran. This file re-runs only the part that was cancelled.
-- It is idempotent and safe to run at any time -- both functions are blue-green
-- rebuilds that swap at the end -- so it is also the thing to re-run if a future
-- rebuild is ever cancelled the same way.
--
-- THE BUDGET IS 3600 s, not 900: the point is a ceiling that a cold-cache
-- rebuild cannot reach, since the cost of a cancelled rebuild is a half-applied
-- read model and the cost of a long one is only a long one. Both rebuilds hold
-- an advisory lock, so a cron tick that fires during this one SKIPS with a
-- notice and does not duplicate the work.
--
-- The assertions are 506 §9's, unchanged, because they are exactly the question
-- this file exists to answer.

set statement_timeout = '3600s';
set lock_timeout = '30s';

select rebuild_browse_list();
select rebuild_properties_map_mv();

reset statement_timeout;
reset lock_timeout;

begin;

set local lock_timeout = '5s';

do $$
declare v_left text;
begin
  select string_agg(format('%s.%s', t, a), ', ') into v_left from (
    select 'browse_list' as t, a.attname as a
      from pg_attribute a
     where a.attrelid = 'public.browse_list'::regclass
       and not a.attisdropped and a.attnum > 0
       and a.attname in ('locality', 'district', 'street', 'obec', 'okres',
                         'region', 'place_search_text', 'home_city_id')
    union all
    select 'properties_map_mv', a.attname
      from pg_attribute a
     where a.attrelid = 'public.properties_map_mv'::regclass
       and not a.attisdropped and a.attnum > 0
       and a.attname in ('locality', 'district', 'street', 'obec', 'okres',
                         'region', 'place_search_text', 'home_city_id')
  ) s;
  if v_left is not null then
    raise exception
      'read model(s) still carry the dropped W3 columns (%) -- a concurrent '
      'rebuild held the advisory lock and this one skipped; re-run this file', v_left;
  end if;
end $$;

do $$
declare v_missing text;
begin
  select string_agg(t, ', ') into v_missing from (
    select 'browse_list' as t where not exists (
      select 1 from pg_attribute a
       where a.attrelid = 'public.browse_list'::regclass
         and a.attname = 'display_label' and not a.attisdropped)
    union all
    select 'properties_map_mv' where not exists (
      select 1 from pg_attribute a
       where a.attrelid = 'public.properties_map_mv'::regclass
         and a.attname = 'granularity_rank' and not a.attisdropped)
  ) s;
  if v_missing is not null then
    raise exception 'read model(s) % lost the W3 S1 columns', v_missing;
  end if;
end $$;

do $$
declare v_left integer;
begin
  if current_setting('server_version_num')::int < 170000 then
    return;
  end if;
  select count(*) into v_left
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public' and c.relkind in ('r', 'm', 'p')
     and (has_table_privilege('authenticated', c.oid, 'MAINTAIN')
       or has_table_privilege('anon', c.oid, 'MAINTAIN'));
  if v_left > 0 then
    raise exception
      'browse rebuild re-granted MAINTAIN to a browser role on % relation(s)', v_left;
  end if;
end $$;

commit;
