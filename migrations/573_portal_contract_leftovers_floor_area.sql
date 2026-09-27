-- 573_portal_contract_leftovers_floor_area.sql
-- The stored values the per-portal ingest contract now declines and that NO ingest path
-- can clear: the idnes floor placeholder, out-of-band storeys and storey counts, and a
-- dwelling headline area outside the dwelling band. DATA ONLY, DESTRUCTIVE (a value
-- becomes NULL), every cleared cell backed up first in this same statement.
--
-- APPLY ORDER. This file is its OWN PR (#1633), stacked on the sale-only per-room rail
-- (#1638, fix/area-per-room-rail-sales-only), which follows the prose-only narrowing
-- (#1637) and the rails PR #1630, so it can be applied from its branch while it is still
-- open (apply_migration.yml's contract: a merged file is never unapplied):
--   1. #1630 merged AND deployed (done: b2454fa1), #1637 merged AND deployed (merged:
--      35c7f3ea), and #1638 merged AND deployed (Railway status + the next Actions scrape
--      on main). Before #1630, a live idnes / sreality / ... refetch re-writes a structured
--      cell (`floor = EXCLUDED.floor`) with the old parser's value; before #1637, the
--      ingest rail still NULLs a structured room rental's area at its next fetch; before
--      #1638, it still NULLs a bazos RENTAL's prose figure — both populations this file's
--      A1 now leaves alone.
--   2. The COUNT QUERY below, read-only, re-run and compared with the 2026-09-27 COUNTS;
--      the stop conditions below checked.
--   3. The operator's OK (destructive).
--   4. apply_migration.yml with --ref this branch.
--   5. Merge this PR.
--
-- THE RAILS (#1630 as narrowed by #1637 and #1638, the parser half). Each predicate
-- below is exactly one of them, so the file NULLs only rows whose stored value the
-- current parser would decline. The literals are pinned to the Python constants by
-- tests/test_migration_573_rails.py (static) and executed over seeded hits and misses by
-- tests/test_migration_573_live.py (CI replay):
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
--   A1  BAZOS SALE byt / dum area_m2 < 8 m² x N for a stated N+kk / N+1
--       (MIN_AREA_PER_ROOM_M2, the resolver's `prose` arm since #1637, on a sale alone —
--       `category_type = 'prodej'`, PER_ROOM_CATEGORY_TYPE — since #1638): a sale's first
--       m² figure was a room, a balcony, a cellar. bazos is the one portal whose area_m2
--       contract cell is `text` — its whole area is that prose figure — so
--       `source = 'bazos'` IS the prose arm. A structured cell or title figure is never
--       held to the per-room floor, and neither is a rental's prose figure.
--   A2  byt area_m2 >= 1,000 (MAX_FLAT_AREA_M2): a project's site area or a typo.
-- NOT HERE: #1630 also holds a HOUSE's unlabelled figure (area_basis 'unknown') under
-- 1,000 m², because bazos's first prose m² on a dum is its parcel. Its stored leftovers
-- (157 bazos dum rows of 1,000 m² or more in the A4 union, spaced parcels stored before
-- the ceiling) are NOT cleared by this file: a named residual, the operator's call.
-- NOT HERE EITHER: the 280 structured rows under the per-room floor (COUNTS below) — room
-- rentals, a real value — and the handful of genuine structured typos among them (one
-- broker's "prodej bytu 3+kk 8 m²", Benátky nad Jizerou, on ceskereality 18628455 / idnes
-- 18628153 / realitymix 18629569; idnes 12539595, 2+kk 9 m²): a named residual.
-- NOR the bazos RENTALS under the per-room floor (247: 222 inactive, 25 active; COUNTS
-- below): room rentals whose prose figure is the room's real size, mixed with genuine
-- defects the rule cannot tell apart — a named residual, listed in the verdicts below.
--
-- WHY A MIGRATION. A parser that declines returns NULL, and a NULL does not reach the
-- stored row for these populations: `text` / `none` cells (bazos area_m2 + area_basis,
-- bazos floor / total_floors) are preserve-if-null (R4, scraper/db.py
-- `_preserved_columns`), so a live refetch KEEPS the old value; inactive rows are never
-- refetched; an unchanged advert may not be refetched for weeks; and scripts/reparse.py
-- never blanks (R9, `_merged`). R4 names the remedy: "removing a stale preserved value
-- is a deliberate hand-written UPDATE". This is that UPDATE.
--
-- COUNTS. The count query below, run read-only on production on 2026-09-27 (~07:20 UTC),
-- by rail, portal and liveness (inactive / active). That run carried the corpus-wide A1;
-- its bazos arm is the A1 row here, and its structured arm is listed as EXCLUDED:
--   F1  idnes 1,322 / 1,694                                                      = 3,016
--   F2  bezrealitky 1/0, ceskereality 5/7, realitymix 20/9, remax 2/1,
--       sreality 36/16                                                           =    97
--   T1  bazos 15/0, bezrealitky 4/2, idnes 41/11, mmreality 35/65, realitymix 3/5,
--       remax 0/1, sreality 39/15                                                =   236
--   A0  bazos 693/88, bezrealitky 16/0, ceskereality 56/11, idnes 88/51,
--       realitymix 31/34, remax 0/1, sreality 54/26                              = 1,149
--   A1  bazos 506/154, every deal type (the sale-only split: BY DEAL TYPE below) =   660
--   A2  bazos 40/17, bezrealitky 1/0, ceskereality 8/8, idnes 15/16,
--       realitymix 9/3, remax 0/5, sreality 14/12                                =   148
--   F1-other: none. In all 5,306 cells, 2,252 of them on active rows.
--   EXCLUDED (the old A1 outside bazos): idnes 105/15, ceskereality 80/13,
--       bezrealitky 34/5, sreality 13/3, realitymix 8/4                          =   280
-- HAND-READ VERDICTS, 2026-09-27 (active rows on the structured portals):
--   A0  (30 read) 1 m² placeholders, every one: idnes 1+kk rentals at 1.0 (147146,
--       147086, 147310, ...), komerční at 1 m² on sreality (334185 sklad, 83486 virtuální
--       kancelář, ...), idnes, ceskereality and realitymix, remax 13590429 dům. NULL.
--   A2  (25 read) the same advert on several portals with the same impossible figure, a
--       broker feed's site area or typo: 1,225 m² 1+kk Blažimská (idnes / sreality /
--       remax), 3,184 m² 2+kk Sedlecká (four portals), 4,970 m² 2+kk Křenová, 11,938 m²
--       2+kk Radimova. NULL keeps the copies matching each other and stops the number
--       reaching users.
--   A1 outside bazos (30 read) ROOM RENTALS listed under the whole flat's disposition:
--       ceskereality "pronájem bytu 5+1 a více" at 11-38 m² (Praha rooms; the same rooms
--       on idnes as 5+kk 11-12 m²), bezrealitky 3+1 / 2+kk / 4+1 rooms at 15-23 m²,
--       realitymix "pronájem pokoje 20 m² ve sdíleném bytě 3+1". The area is the room's
--       real size: EXCLUDED here, and #1637 stops the ingest rail NULLing it.
-- The first count, on the investigation's exports (A4, 2026-09-26, 40,514 listings, 107
-- rows), held none of those rentals, which is why #1630 first applied A1 everywhere.
-- BAZOS A1 BY DEAL TYPE, a second read-only read on production, 2026-09-27 (inactive /
-- active): prodej 617 / 160 = 777, pronajem 222 / 25 = 247.
--   A1 bazos rentals (all 25 active read) a MIXED population. ROOM RENTALS whose figure
--       is the room's real size: 16757869 "pronájem pokoje 20m2 ve sdíleném bytě 3+1",
--       18625955 "pronájem pokoje ve sdíleném bytě 3+1", 18718428 "pronájem pokojů v
--       rodinném domě", 18798750 "dva pokoje o velikostech cca 20m2 a 15m2", 18850769
--       "pronájem pokoje 20m2", 18938443 "pronájem zařízeného pokoje, spolubydlení v 3+1",
--       18998832 "pronájem lůžka v pokojích", 19016712 "pronájem pokoje v domě", 18677344
--       "3+1 pro spolubydlení", 13336828 "pronájem jednotlivých pokojů". Beside them,
--       GENUINE DEFECTS: 18565661 2+kk 6.7 m², 18677626 3+kk 5 m², 19006537 2+kk 5 m²,
--       17940680 2+1 1.5 m², 18907479 1+1 6 m², 19034313 1+1 5 m², 18677321 and 18702811
--       2+kk 10 m², 18701470 2+kk 15 m². The per-room rule cannot tell the two apart, so
--       on a rental it does not fire (#1638): EXCLUDED here, and the rental defects are a
--       named residual (17940680's 1.5 m² is still A0's).
-- EXPECTED A1 NOW: the bazos sales, 777 (617 inactive, 160 active). NOTE: that read
-- counted the per-room predicate on its own, while the count query below files a row
-- under 5 m² as A0 first — which is the likely reason its all-deal-type bazos A1 above
-- reads 660 (506 / 154) against 1,024 (839 / 185) here. The re-run's A1 line should
-- therefore read at or under 777; a bazos sale under 5 m² appears on the A0 line.
-- RE-RUN THE COUNT BELOW FIRST (read-only), by rail, portal and liveness, and record it
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
--         or (source = 'bazos' and category_type = 'prodej' and category_main in ('byt','dum')
--             and area_m2 < 8 * (case when disposition ~ '^[1-9]\+(kk|1)$'
--                                     then left(disposition, 1)::int end))
--         or (category_main = 'byt' and area_m2 >= 1000)
--   ) r group by rail, source, is_active order by rail, source, is_active;
--
-- STOP CONDITIONS — do not apply; hand-read the rows first (and report them) when:
--   1. the A1 total is over 1,000 (777 bazos sales expected), OR any A1 row is not a bazos
--      sale. A1 outside bazos sales is 0 by construction (the predicate names the source
--      and the deal type), so such a row means the predicate moved; a jump is a bazos
--      population nobody has read.
--   2. any 'F1-other' row exists (an idnes 20 whose raw podlaží is not the placeholder).
--   3. the F1 total is far from the 3,016 counted on 2026-09-27 (under 2,000 or over
--      4,000): the predicate or the data moved since the measurement.
--   4. any A0 / A2 count on a structured portal is more than 50 above its 2026-09-27
--      figure in COUNTS. Those populations were read on 2026-09-27 (placeholders,
--      cross-portal feed figures); the excess would be one that was not.
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
      or (l.source = 'bazos' and l.category_type = 'prodej' and l.category_main in ('byt','dum')
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
