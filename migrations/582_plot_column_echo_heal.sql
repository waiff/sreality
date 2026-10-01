-- 582_plot_column_echo_heal.sql
-- The stored `estate_area` values the plot rule now declines and that NO ingest path can
-- clear: a flat's "plot" (every one), and a commercial unit's "plot" that merely repeats
-- its own floor figure. DATA ONLY, DESTRUCTIVE (a value becomes NULL), every cleared cell
-- backed up first in this same statement.
--
-- THE RULING. General ruling 3 (2026-09-30) + the operator's answers of 2026-10-01: on 14
-- of 14 live pages opened (idnes, realitymix, mmreality; ceskereality 403 — read from its
-- stored pages) the portal ITSELF prints one figure in the plot box and the floor-area box.
-- Not our scraping and not our key maps: the missing rule was that "a plot equal to the
-- floor area on a commercial unit is not a plot", and that "a flat never carries a plot at
-- all". That rule is `scraper.area.stated_plot` (PR #1668, fix/plot-echo-rail), called
-- BEFORE the content hash at the contract boundary for every portal.
--
-- APPLY ORDER. This file is its OWN PR, stacked on PR #1668, so it can be applied from its
-- branch while it is still open (apply_migration.yml's contract: a merged file is never
-- unapplied):
--   1. PR #1668 merged AND deployed (Railway status + the next Actions scrape on main).
--      Before that, a live detail refetch RE-WRITES the echo: `estate_area` is a
--      `structured` cell, so the old parser's value lands again on the next write.
--   2. The COUNT QUERY below, read-only, run and compared with the 2026-09-30 CENSUS; the
--      stop conditions checked; a stratified hand-read of P2 hits by portal x komerční
--      subtype (garáž, sklad, výroba/areál, ubytování, činžovní dům, zemědělský) — the
--      #1630 -> #1637 lesson: a rail whose samples were chosen as hits was refuted by
--      migration 573's production count.
--   3. The operator's OK (destructive).
--   4. The worker's autodedup lane PAUSED (interval 0, the migration 571 pattern), so a
--      fresh seed can take the lane's lease afterwards.
--   5. apply_migration.yml with --ref this branch.
--   6. `rt_seed fresh=true`, then resume the lane (the migration 572 pattern — the sequence
--      used after 573 on 2026-09-27). This file writes NO snapshot, so the lane's `changed`
--      feed (listing_snapshots) would never see the healed rows without the re-seed.
--   7. Merge this PR — only once applied, and only after the next engine release: this
--      file moves live-lane inputs (`plot_area_rel_diff` / `plot_area_exact` read the echo
--      today) and the inputs of the sealed cohort 20, which is not exported yet.
--
-- THE RAILS. Each predicate below is exactly the parser rule, so the file NULLs only rows
-- whose stored value `scraper.area.stated_plot` declines today. The literals are pinned to
-- the Python constants by tests/test_migration_582_rails.py (static: the category sets, the
-- labelled bases, and the predicate replayed against `stated_plot` over a grid) and executed
-- over seeded hits and misses by tests/test_migration_582_live.py (CI replay):
--   P1  byt with any estate_area (`PLOT_FREE_CATEGORIES`): a flat NEVER carries a plot. On
--       ceskereality the 3,185 active flat "plots" were the doubled floor figure (987), the
--       placeholder 1, the whole building's parcel (355, 575, 1,661, 3,470) or a near-floor
--       figure (fixture b1: plot 64 beside užitná 59) — none the unit's own land. idnes 22,
--       bezrealitky 2, the rest 0-2 (sreality publishes none on a flat).
--   P2  komercni whose estate_area equals the usable measure, OR equals the headline
--       `area_m2` when `area_basis` is a LABELLED interior measure ('usable' / 'floor' /
--       'total' = `PLOT_ECHO_BASES`). NOT compared, by the ruling: a title / prose fallback
--       (basis 'unknown' — "Prodej areálu 3 400 m²" IS the site, and the real parcel would
--       go), zastavěná plocha (a building may cover its whole parcel), and a HOUSE (the
--       operator kept the dum rule as it is; a garage's parcel really is its footprint).
--       Equality is the column's: numeric(9,1) against numeric(9,1) / numeric(7,1), which
--       is what the Python rule compares too (0.1 m², half up).
-- NOT HERE: dum (idnes carries 2,077 echoes, 7.1 %; ceskereality 172, realitymix 124,
-- mmreality 63 — the mmreality-dum residue check against the stored page's `parcelArea` is
-- owed separately); pozemek (area_m2 IS the plot); ostatni.
-- UNDER-HEAL, NAMED (not a false hit): an INACTIVE komerční echo that predates the basis
-- stamp (area_basis NULL — the c17/c18 exports: idnes 247151, 485605, 247227) is caught by
-- the usable arm when `usable_area` carries the figure, but a pre-stamp row of the
-- realitymix "Plocha" shape (usable NULL, basis NULL) is MISSED. If the count shows many
-- such rows, add an arm on the stored page's own params in a follow-up, e.g.
-- `raw_json->'params'->>'plocha parcely' = raw_json->'params'->>'plocha'`.
--
-- WHY A MIGRATION. `scripts/reparse.py` never blanks (R9, `_merged`: a re-derive that yields
-- None keeps the stored value), so on exactly these rows a `fields=estate_area` reparse
-- reports changed=0; inactive rows are never refetched; and nothing needs moving, because
-- the figure already sits in `area_m2` / `usable_area`. R11 ("never NULL a stated fact") is
-- honoured by the ruling itself: the figure is ruled not to be a plot, it stays stored as the
-- floor area, and the page's own statement stays in `portal_raw_pages.html` /
-- `raw_json.params`.
--
-- CENSUS (2026-09-30, ACTIVE rows, read-only; inactive rows were NOT counted):
--   P1  byt with a plot: ceskereality 3,185, idnes 22, bezrealitky 2, others 0-2  ~= 3,211
--   P2  plot = area_m2 / = usable, of plots set: ceskereality 1,377 / 1,383 of 3,023,
--       idnes 644 / 651 of 2,886, realitymix 427 / 95 of 1,976 (basis mostly 'total'),
--       mmreality 53 / 53 of 762, remax 4 of 185, maxima 1 of 3, sreality 0 of 0 (publishes
--       no plot on komerční)                                     upper bound ~= 2,519
--   The realitymix 427 did not condition on `area_basis`, and this file excludes basis
--   'unknown', so the P2 count reads AT OR UNDER the bound. Total active ~= 5,730, plus the
--   inactive rows, uncounted.
-- RUN THE COUNT BELOW FIRST (read-only), by rail, portal, category, liveness and basis, and
-- record it with the apply:
--
--   select rail, source, category_main, is_active, area_basis, count(*) as n from (
--     select case when category_main = 'byt' then 'P1' else 'P2' end as rail,
--            source, category_main, is_active, area_basis
--       from listings
--      where estate_area is not null
--        and (category_main = 'byt'
--             or (category_main = 'komercni'
--                 and (estate_area = usable_area
--                      or (estate_area = area_m2
--                          and area_basis in ('usable','floor','total')))))
--   ) r group by rail, source, category_main, is_active, area_basis
--   order by rail, source, category_main, is_active, area_basis;
--
-- STOP CONDITIONS — do not apply; hand-read the rows first (and report them) when:
--   1. any row's category_main is not 'byt' or 'komercni'. Impossible by construction (the
--      predicate names both), so such a row means the predicate moved.
--   2. any P2 row carries area_basis = 'plot': a dwelling stamped as land is a row
--      re-categorised after a land-era derive — OUR residue, not the advertiser's.
--   3. any ACTIVE row carries area_basis NULL: the resolver stamps every live write, so an
--      active unstamped row was never re-derived — hand-read before clearing. (Inactive
--      NULL-basis rows are the pre-stamp exports and are expected.)
--   4. the ACTIVE P2 total is over 3,500 (~= 2,519 expected) or the ACTIVE P1 total is over
--      4,000 (~= 3,211 expected): the predicate or the data moved since the census.
--   5. more than 10 P1 rows on sreality (it publishes no plot on a flat: a category mapping
--      moved) or ANY P2 row on sreality (0 of 19,365 komerční carry a plot).
--
-- Expected AFTER: the count query returns no rows (re-run the file if a row a concurrent
-- drain held was skipped).
--
-- HOW. One statement (one column): a `hit` CTE (FOR UPDATE SKIP LOCKED — a row a drain is
-- writing right now is left for a re-run instead of waiting on it) -> copy into
-- backup_a4.listing_cells, column_name 'estate_area' (no collision with 573's floor /
-- total_floors / area_m2 rows; its own rail tags P1 / P2; `old_basis` stays NULL because
-- `area_basis` is NOT touched — it belongs to `area_m2`, which is correct on every hit) ->
-- compare-and-set UPDATE to NULL -> every touched property enqueued for maintenance (rule
-- 20). The heal rules of 554 / R9 / 573: no listing_snapshots row (the sanctioned rule-2
-- exception for correcting our OWN reading of what is already stored — each healed live row
-- appends one genuine snapshot at its next detail fetch; its stored snapshot keeps the old
-- value, the accepted asymmetry), and no last_seen_at. Idempotent: a second run selects
-- nothing, and the backup keeps the FIRST value it saw per (listing_id, column_name). The
-- schema / table DDL below is `if not exists` so the file stands on its own if backup_a4 was
-- already dropped after 573's month.
--
-- RESTORE (only where the cell is still NULL, so a newer value wins):
--   update listings l set estate_area = b.old_value
--     from backup_a4.listing_cells b
--    where b.listing_id = l.id and b.column_name = 'estate_area' and l.estate_area is null;
-- The operator drops schema backup_a4 once the heal has been live for a month.
--
-- AFTER APPLY (runbook): no reparse is needed — nothing moves INTO a column. Re-seed the
-- autodedup lane (step 6 above); `browse_list` / `properties_map_mv` pick the NULL up through
-- `dirty_properties` + the read-model rebuild (`plot_area_m2` reads `estate_area` on komerční).

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

-- 1. estate_area: a flat's plot (P1) and a komerční plot that is the floor figure (P2).
with hit as (
  select l.id, l.estate_area,
         case when l.category_main = 'byt' then 'P1' else 'P2' end as rail
    from listings l
   where l.estate_area is not null
     and (l.category_main = 'byt'
          or (l.category_main = 'komercni'
              and (l.estate_area = l.usable_area
                   or (l.estate_area = l.area_m2
                       and l.area_basis in ('usable','floor','total')))))
     for update of l skip locked
), saved as (
  insert into backup_a4.listing_cells (listing_id, column_name, rail, old_value)
  select id, 'estate_area', rail, estate_area from hit
  on conflict (listing_id, column_name) do nothing
), cleared as (
  update listings l
     set estate_area = null
    from hit
   where l.id = hit.id
     and l.estate_area is not distinct from hit.estate_area
  returning l.property_id
)
insert into dirty_properties (property_id)
select distinct property_id from cleared where property_id is not null
on conflict (property_id) do update set marked_at = now();
