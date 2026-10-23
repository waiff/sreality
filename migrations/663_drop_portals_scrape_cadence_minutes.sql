-- 663_drop_portals_scrape_cadence_minutes.sql -- the old health layer's last column: portals.scrape_cadence_minutes
-- (Health dashboard sprint, wave 9 = Health green sprint, wave 6; split out of migration 643).
--
-- ===========================================================================
-- DESTRUCTIVE, with the operator's OK (ruling Q5). Applied right after 643, in the same window and under the same
-- backup (backup_before_drop.yml, label health-legacy-643: portals as source + scrape_cadence_minutes CSV).
-- ===========================================================================
--
-- WHY ITS OWN FILE. 643 must replay before 650 (#1939 drops parsed_url_cache, which 643's parsed_url_activity reads)
-- and this drop must replay after 649 (649_web_portal inserts the web portal's row naming this column): no single
-- number fits both, so the column goes here, after 649 in the CI replay as in production.
-- WHY. Migration 114 added it for the old liveness scaling; since Health wave 8 nothing reads it: no view, matview,
-- function body, pg_depend entry, index or constraint (live, 2026-10-23) and no code in api/, toolkit/, scraper/,
-- scripts/, frontend/src or chrome-extension/src. 649's insert was its last writer (web: 1440).
-- WHAT STAYS. Every other portals column; supports_complete_walk is still read (scraper/portal.py, GET /admin/portals)
-- and the scraper track owns it.
-- APPLY from the PR's branch right after 643 (apply_migration.yml, a dry run, then dry_run=false confirm=APPLY), in a
-- minute no */15 browse-list rebuild is running (:x3-:x4 or :x8-:x9). One catalog change under portals' brief ACCESS
-- EXCLUSIVE lock (the scrapers, the worker and the API read it); a wait past 5 s rolls the file back and the
-- workflow's lock-timeout retry re-runs it. A re-run is a no-op.
-- ROLLBACK: forward only (rule 1); the backup's CSV restores the values.

set lock_timeout = '5s';
set statement_timeout = '60s';

begin;

do $pre$
declare
  v_bad text;
begin
  select string_agg(x, ', ' order by x) into v_bad from (
    select format('view %I.%I', v.schemaname, v.viewname) as x
      from pg_views v
     where v.schemaname not in ('pg_catalog', 'information_schema')
       and v.definition ~ '\mscrape_cadence_minutes\M'
    union all
    select format('materialized view %I.%I', m.schemaname, m.matviewname)
      from pg_matviews m
     where m.definition ~ '\mscrape_cadence_minutes\M'
    union all
    select format('function %s', p.oid::regprocedure)
      from pg_proc p
     where p.pronamespace not in ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
       and p.prosrc ~ '\mscrape_cadence_minutes\M'
    union all
    -- anything on the column but its own default: a view, an index, a constraint, a statistics object
    select pg_describe_object(d.classid, d.objid, d.objsubid)
      from pg_depend d
      join pg_attribute a on a.attrelid = d.refobjid and a.attnum = d.refobjsubid
     where d.refclassid = 'pg_class'::regclass
       and d.refobjid = 'public.portals'::regclass
       and a.attname = 'scrape_cadence_minutes'
       and not a.attisdropped
       and d.classid <> 'pg_attrdef'::regclass
  ) deps;
  if v_bad is not null then
    raise exception '663 refused: % still name(s) or depend(s) on portals.scrape_cadence_minutes', v_bad;
  end if;
end
$pre$;

alter table public.portals drop column if exists scrape_cadence_minutes;

do $post$
begin
  if exists (select 1 from pg_attribute
              where attrelid = 'public.portals'::regclass and attname = 'scrape_cadence_minutes'
                and not attisdropped) then
    raise exception '663 did not land: portals.scrape_cadence_minutes still exists';
  end if;
  if not exists (select 1 from pg_attribute
                  where attrelid = 'public.portals'::regclass and attname = 'supports_complete_walk'
                    and not attisdropped) then
    raise exception '663 did not land: portals.supports_complete_walk is gone (still read; the scraper track owns it)';
  end if;
end
$post$;

notify pgrst, 'reload schema';

commit;

reset statement_timeout;
reset lock_timeout;
