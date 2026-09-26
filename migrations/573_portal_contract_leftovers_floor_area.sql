-- 573_portal_contract_leftovers_floor_area.sql
-- The stored values the per-portal ingest contract now declines and that NO ingest path
-- can clear: the idnes floor placeholder, out-of-band storeys and storey counts, and a
-- dwelling headline area outside the dwelling band. DATA ONLY, DESTRUCTIVE (a value
-- becomes NULL), every cleared cell backed up first in this same statement.
--
-- APPLY ORDER. This file is its OWN PR, stacked on the rails PR #1630
-- (fix/portal-contracts-floor-area), so it can be applied from its branch while that
-- PR is still open (apply_migration.yml's contract: a merged file is never unapplied):
--   1. #1630 merged AND deployed (Railway status + the next Actions scrape on main).
--      Before that, a live idnes / sreality / ... refetch re-writes a structured cell
--      (`floor = EXCLUDED.floor`) with the old parser's value.
--   2. The COUNT QUERY below, read-only, recorded with the apply; the stop conditions
--      below checked.
--   3. The operator's OK (destructive).
--   4. apply_migration.yml with --ref this branch.
--   5. Merge this PR.
--
-- THE RAILS (#1630, the parser half). Each predicate below is exactly one of them, so the
-- file NULLs only rows whose stored value the current parser would decline. The literals
-- are pinned to the Python constants by tests/test_migration_573_rails.py (static) and
-- executed over seeded hits and misses by tests/test_migration_573_live.py (CI replay):
--   F1  idnes floor = 20: the top option of the portal's select, "20. patro a vyšší", a
--       broker-feed placeholder declared a contract sentinel
--       (scraper/attribute_contract.py, idnes `floor`). idnes has no option above it,
--       so every stored idnes 20 is "20 or higher, unknown which": NULL for ALL of them.
--       NOTE the one asymmetry: the ingest rail declines only a label that STARTS with
--       the placeholder, while this file NULLs every idnes 20 whatever its raw text. An
--       idnes 20 stated any other way would re-land at its next fetch; the count query
--       lists such rows as 'F1-other' (stop condition 2), and none are expected.
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
-- NOT HERE: #1630 also holds a HOUSE's unlabelled figure (area_basis 'unknown') under
-- 1,000 m², because bazos's first prose m² on a dum is its parcel. Its stored leftovers
-- (157 bazos dum rows of 1,000 m² or more in the A4 union, spaced parcels stored before
-- the ceiling) are NOT cleared by this file: a named residual, the operator's call.
--
-- WHY A MIGRATION. A parser that declines returns NULL, and a NULL does not reach the
-- stored row for these populations: `text` / `none` cells (bazos area_m2 + area_basis,
-- bazos floor / total_floors) are preserve-if-null (R4, scraper/db.py
-- `_preserved_columns`), so a live refetch KEEPS the old value; inactive rows are never
-- refetched; an unchanged advert may not be refetched for weeks; and scripts/reparse.py
-- never blanks (R9, `_merged`). R4 names the remedy: "removing a stale preserved value
-- is a deliberate hand-written UPDATE". This is that UPDATE.
--
-- COUNTS. No database is reachable from the build session, so the rows were counted only
-- on the investigation's exports (A4, 2026-09-26, the trial + cohorts 17 / 18 = 40,514
-- listings): 107 rows (trial 9), by rail and portal —
--   F1 idnes 8 | F2 sreality 6, realitymix 2, ceskereality 1 | T1 mmreality 4, bezrealitky 2
--   A0 bazos 37, idnes 3, bezrealitky 2, sreality 2, ceskereality 1, realitymix 1
--   A1 bazos 32 (0 of 19,345 dispositioned byt/dum rows on the eight structured portals)
--   A2 bazos 3, idnes 1 (4,095), realitymix 1 (1,800), sreality 1 (5,989).
-- Corpus-wide the only measured figures are F1 2,995 rows (1,736 active) on 2026-09-23 and
-- F2 86 (33 active) before the W8 heal. RUN THE COUNT BELOW FIRST (read-only), by rail, portal and liveness, and record it
-- with the apply:
--
--   select rail, source, is_active, count(*) as n from (
--     select case when source = 'idnes' and floor = 20
--                 then case when starts_with(btrim(raw_json->'params'->>'podlaží'),
--                                            '20. patro a vyšší')
--                           then 'F1' else 'F1-other' end
--                 else 'F2' end as rail, source, is_active
--       from listings where (source = 'idnes' and floor = 20) or floor < -3 or floor > 40
--     union all
--     select 'T1', source, is_active from listings where total_floors < 1 or total_floors > 40
--     union all
--     select case when category_main in ('byt','dum','komercni') and area_m2 < 5 then 'A0'
--                 when category_main = 'byt' and area_m2 >= 1000 then 'A2'
--                 else 'A1' end, source, is_active
--       from listings
--      where (category_main in ('byt','dum','komercni') and area_m2 < 5)
--         or (category_main in ('byt','dum')
--             and area_m2 < 8 * (case when disposition ~ '^[1-9]\+(kk|1)$'
--                                     then left(disposition, 1)::int end))
--         or (category_main = 'byt' and area_m2 >= 1000)
--   ) r group by rail, source, is_active order by rail, source, is_active;
--
-- STOP CONDITIONS — do not apply; hand-read the rows first (and report them) when:
--   1. any A1 row has a source other than 'bazos'. The per-room band was measured to hit
--      no structured-portal row; a hit there is a portal's own area or disposition
--      reading, and NULLing it would destroy a real value.
--   2. any 'F1-other' row exists (an idnes 20 whose raw podlaží is not the placeholder).
--   3. the F1 total is far from the 2,995 measured on 2026-09-23 (under 2,000 or over
--      4,000): the predicate or the data moved since the measurement.
--   4. any A0 / A2 row on a structured portal beyond the handful the union showed (more
--      than 50 per portal): the rail meets a population nobody has read.
--
-- Expected AFTER: the count query returns no rows (re-run the file if a row a concurrent
-- drain held was skipped). An 'F1-other' row the operator chose to apply over re-lands
-- at its next fetch, as the F1 note above says.
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
-- with the snapshot deferral) lands the NULL -> value moves the resolver now makes: the
-- next measure after a declined one, and a dotted-thousands figure ("1.910 m2" -> 1,910)
-- on land, a hall or ostatni. It never blanks, so it cannot undo this file. bazos dum is
-- kept OUT of that heal by the parser itself: a dotted figure is always 1,000 m² or more,
-- which #1630's house ceiling declines on an unlabelled figure (pinned by
-- tests/scraper/test_bazos_parser.py test_parse_detail_a_dotted_parcel_is_not_the_house),
-- so the reparse writes nothing there — the Děčín chata (bazos 204860 / 318121 /
-- 17174350) stays NULL, which is the state the A4 simulation measured it re-join its
-- 24 m² twins in, and does NOT carry the 1,256 m² garden. The autodedup lane's `changed`
-- feed reads listing_snapshots only and this file writes none: re-seed the lane to see
-- the moves.

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
