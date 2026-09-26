-- 573_portal_contract_leftovers_floor_area.sql
-- The stored values the per-portal ingest contract now declines and that NO ingest path
-- can clear: the idnes floor placeholder, out-of-band storeys and storey counts, and a
-- dwelling headline area outside the dwelling band. DATA ONLY, DESTRUCTIVE (a value
-- becomes NULL), every cleared cell backed up first in this same statement.
--
-- APPLY ORDER (the coordinator's runbook, not this file): only AFTER the rails PR
-- (fix/portal-contracts-floor-area) is merged AND deployed. Before that, a live idnes /
-- sreality / ... refetch re-writes a structured cell (`floor = EXCLUDED.floor`) with the
-- old parser's value, and a new bazos row can still land a sub-area headline.
--
-- THE RAILS (the parser half, same PR). Each predicate below is exactly one of them, so
-- the file NULLs only rows whose stored value the current parser would decline:
--   F1  idnes floor = 20: the top option of the portal's select, "20. patro a vyšší", a
--       broker-feed placeholder declared a contract sentinel
--       (scraper/attribute_contract.py, idnes `floor`). idnes has no option above it,
--       so every stored idnes 20 is "20 or higher, unknown which": NULL for all of them.
--   F2  floor outside -3..40: scraper.floor.floor_from_portal's bare-number arm now
--       bounds like its word arm (sreality 161 / 126 / 139, realitymix 1,002,
--       ceskereality 126).
--   T1  total_floors outside 1..40: scraper.floor.total_floors_from_portal on every
--       structured read + the text lane (bezrealitky 731,463,379 / 113, mmreality 0).
--   A0  byt / dum / komercni area_m2 < 5 (MIN_AREA_M2, the 2026-08-25 rail): PRE-rail
--       leftovers — bazos Mechová 3+1 stored its cellar's 2 m².
--   A1  byt / dum area_m2 < 8 m² x N for a stated N+kk / N+1 (MIN_AREA_PER_ROOM_M2):
--       bazos's first m² figure was a room, a balcony, a cellar.
--   A2  byt area_m2 >= 1,000 (MAX_FLAT_AREA_M2): a project's site area or a typo.
--
-- WHY A MIGRATION. A parser that declines returns NULL, and a NULL does not reach the
-- stored row for these populations: `text` / `none` cells (bazos area_m2 + area_basis,
-- bazos floor / total_floors) are preserve-if-null (R4, scraper/db.py
-- `_preserved_columns`), so a live refetch KEEPS the old value; inactive rows are never
-- refetched; an unchanged advert may not be refetched for weeks; and scripts/reparse.py
-- never blanks (R9, `_merged`). R4 names the remedy: "removing a stale preserved value
-- is a deliberate hand-written UPDATE". This is that UPDATE.
--
-- COUNTS. No database is reachable from the build session, so the rows are counted from
-- the investigation's exports (A4, 2026-09-26, the trial + cohorts 17 / 18 = 40,514
-- listings): F1 8, F2 9, T1 6, A0 46, A1 32, A2 6 = 107 (trial 9). Corpus-wide the
-- measured figures are F1 2,995 rows (1,736 active) on 2026-09-23 and F2 86 (33 active)
-- before the W8 heal. RUN THE COUNT BELOW FIRST (read-only) and record it with the apply.
--
--   select rail, count(*) filter (where is_active) as active, count(*) as total from (
--     select case when source = 'idnes' and floor = 20 then 'F1' else 'F2' end as rail,
--            is_active
--       from listings where (source = 'idnes' and floor = 20) or floor < -3 or floor > 40
--     union all
--     select 'T1', is_active from listings where total_floors < 1 or total_floors > 40
--     union all
--     select case when category_main in ('byt','dum','komercni') and area_m2 < 5 then 'A0'
--                 when category_main = 'byt' and area_m2 >= 1000 then 'A2'
--                 else 'A1' end, is_active
--       from listings
--      where (category_main in ('byt','dum','komercni') and area_m2 < 5)
--         or (category_main in ('byt','dum')
--             and area_m2 < 8 * (case when disposition ~ '^[1-9]\+(kk|1)$'
--                                     then left(disposition, 1)::int end))
--         or (category_main = 'byt' and area_m2 >= 1000)
--   ) r group by rail order by rail;
--
-- Expected AFTER: no rows (re-run the file if a row a concurrent drain held was skipped).
--
-- HOW. One statement per column: a `hit` CTE (FOR UPDATE SKIP LOCKED — a row a drain is
-- writing right now is left for a re-run instead of waiting on it) -> copy into
-- backup_a4.listing_cells -> compare-and-set UPDATE to NULL (`area_basis` moves with
-- `area_m2`: never a basis on a NULL number, scraper/db.py `_AREA_BASIS_FOLLOWS`) ->
-- every touched property enqueued for maintenance (rule 20). The heal rules of 554 / R9:
-- no listing_snapshots row (the sanctioned rule-2 exception for correcting our OWN
-- reading of what is already stored — each healed live row on a parsed-hash portal
-- appends one genuine snapshot at its next detail fetch; sreality hashes the raw payload
-- and appends none), and no last_seen_at. Idempotent: a second run selects nothing, and
-- the backup keeps the FIRST value it saw per (listing_id, column_name).
--
-- RESTORE (per column; only where the cell is still NULL, so a newer value wins):
--   update listings l set floor = b.old_value::int
--     from backup_a4.listing_cells b
--    where b.listing_id = l.id and b.column_name = 'floor' and l.floor is null;
--   (total_floors the same; area_m2 = b.old_value, area_basis = b.old_basis.)
-- The operator drops schema backup_a4 once the heal has been live for a month.
--
-- AFTER APPLY (runbook): reparse.yml per portal with fields=area_m2 (dry run, then write
-- with the snapshot deferral) lands the NULL -> value moves the resolver now makes (the
-- next measure after a declined one; dotted thousands "1.139 m2" -> 1,139). It never
-- blanks, so it cannot undo this file. The autodedup lane's `changed` feed reads
-- listing_snapshots only and this file writes none: re-seed the lane to see the moves.

set lock_timeout = '5s';
set statement_timeout = '900s';

create schema if not exists backup_a4;
revoke all on schema backup_a4 from anon, authenticated;

create table if not exists backup_a4.listing_cells (
  listing_id   bigint      not null,
  column_name  text        not null,
  rail         text        not null,
  old_value    numeric,
  old_basis    text,
  backed_up_at timestamptz not null default now(),
  primary key (listing_id, column_name)
);
alter table backup_a4.listing_cells enable row level security;
revoke all on backup_a4.listing_cells from anon, authenticated;

-- 1. floor: the idnes placeholder (F1) and any storey outside -3..40 (F2).
with hit as (
  select l.id, l.floor,
         case when l.source = 'idnes' and l.floor = 20 then 'F1' else 'F2' end as rail
    from listings l
   where (l.source = 'idnes' and l.floor = 20) or l.floor < -3 or l.floor > 40
     for update of l skip locked
), saved as (
  insert into backup_a4.listing_cells (listing_id, column_name, rail, old_value)
  select id, 'floor', rail, floor from hit
  on conflict (listing_id, column_name) do nothing
), cleared as (
  update listings l
     set floor = null
    from hit
   where l.id = hit.id
     and l.floor is not distinct from hit.floor
  returning l.property_id
)
insert into dirty_properties (property_id)
select distinct property_id from cleared where property_id is not null
on conflict (property_id) do update set marked_at = now();

-- 2. total_floors outside 1..40 (T1).
with hit as (
  select l.id, l.total_floors
    from listings l
   where l.total_floors < 1 or l.total_floors > 40
     for update of l skip locked
), saved as (
  insert into backup_a4.listing_cells (listing_id, column_name, rail, old_value)
  select id, 'total_floors', 'T1', total_floors from hit
  on conflict (listing_id, column_name) do nothing
), cleared as (
  update listings l
     set total_floors = null
    from hit
   where l.id = hit.id
     and l.total_floors is not distinct from hit.total_floors
  returning l.property_id
)
insert into dirty_properties (property_id)
select distinct property_id from cleared where property_id is not null
on conflict (property_id) do update set marked_at = now();

-- 3. the dwelling headline outside the dwelling band (A0 / A1 / A2), area_basis with it.
with hit as (
  select l.id, l.area_m2, l.area_basis,
         case when l.category_main in ('byt','dum','komercni') and l.area_m2 < 5 then 'A0'
              when l.category_main = 'byt' and l.area_m2 >= 1000 then 'A2'
              else 'A1' end as rail
    from listings l
   where (l.category_main in ('byt','dum','komercni') and l.area_m2 < 5)
      or (l.category_main in ('byt','dum')
          and l.area_m2 < 8 * (case when l.disposition ~ '^[1-9]\+(kk|1)$'
                                    then left(l.disposition, 1)::int end))
      or (l.category_main = 'byt' and l.area_m2 >= 1000)
     for update of l skip locked
), saved as (
  insert into backup_a4.listing_cells (listing_id, column_name, rail, old_value, old_basis)
  select id, 'area_m2', rail, area_m2, area_basis from hit
  on conflict (listing_id, column_name) do nothing
), cleared as (
  update listings l
     set area_m2 = null, area_basis = null
    from hit
   where l.id = hit.id
     and l.area_m2 is not distinct from hit.area_m2
  returning l.property_id
)
insert into dirty_properties (property_id)
select distinct property_id from cleared where property_id is not null
on conflict (property_id) do update set marked_at = now();
