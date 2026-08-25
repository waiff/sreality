-- 427_measure_plausibility_by_source.sql
-- W9 of the per-m2 measure program: the plausibility gate.
--
-- WHY A NEW HEALTH SURFACE AT ALL. `data_quality_by_source` (318) tests 29
-- fields for `IS NOT NULL` and nothing else, and `scraper_health_checks_mv`
-- (354) counts rows and ages. BOTH defects this program fixed produce 100%
-- non-NULL, perfectly-shaped values:
--   * mmreality wrote the PLOT area into the floor-area column -- `area_m2` is
--     populated on every row, it is just the wrong physical quantity;
--   * four portals wrote a per-m2 UNIT price into `price_czk` -- 136 Kc is a
--     number, it is just not the price of the thing.
-- A null-check cannot see either one. Measured live on 2026-08-25, BEFORE the
-- W2 backfill: mmreality dum/prodej median `area_m2` 905.0 against
-- median `usable_area` 130.0 on the same 3 588 rows, while every other portal's
-- dum/prodej pair agrees on 0.0% of rows -- 100.0% divergence versus 0.0%. The
-- signal was always there; nothing was looking at it.
--
-- THE GRAIN IS (source, category_main, category_type) and that is load-bearing.
-- Per-source alone pools sale flats with monthly rentals and land; per-category
-- alone pools a broken portal with eight healthy ones and the median hides it.
-- Only the pair isolates "this portal, this basis" -- which is the same tuple
-- `measure_price_per_m2_basis` resolves a label from, so a cell has exactly one
-- unit and the medians in it are comparable.
--
-- WHAT THE VIEW DELIBERATELY DOES NOT DO: compare one portal against the others.
-- That was the obvious reading of "plausibility" and the data refutes it.
-- Measured live across every cell with >= 200 active rows and >= 3 peer sources,
-- the ratio of a cell's median to its peers' median median:
--     realitymix ostatni/prodej   area 19.4x   (a junk-drawer category, not a bug)
--     idnes      pozemek/prodej   ppm2  8.5x   (rural plot mix; 455 vs 3 849 Kc/m2)
--     mmreality  dum/prodej       ppm2  8.1x   (THE BUG)
-- The bug does not stand out from the legitimate mix differences at any
-- threshold, so a cross-portal arm would be a permanent false alarm on land and
-- `ostatni` while adding nothing the two direct detectors below do not already
-- catch. Portals differ; that is not a defect.
--
-- MATERIAL DIVERGENCE IS > 10%, NOT `IS DISTINCT FROM`. The charter specified
-- `area_m2 IS DISTINCT FROM usable_area`; taken literally that is two separate
-- mistakes. (a) With `usable_area` NULL -- bazos populates it on 0 of 10 409
-- dum/prodej rows -- `IS DISTINCT FROM` is TRUE, so a portal that simply does not
-- publish the second field would score 100% "divergent" and the check would fire
-- on every portal that has nothing to compare. Hence `n_area_pairs`: only rows
-- carrying BOTH are counted, and a cell with no pairs reports NULL, not 0 and not
-- 100. (b) Exact inequality also counts rounding and balcony conventions:
-- realitymix byt/prodej diverges on 21.9% of pairs but at a MEDIAN relative
-- difference of 9.6% -- a different field convention, not a different quantity.
-- mmreality dum/prodej diverges at a median relative difference of 85.0%. The
-- 10% band separates "the same area, measured slightly differently" from "a
-- different physical thing", and it is the difference between a check the
-- operator trusts and one they learn to dismiss.
--
-- `pozemek` IS NOT EXEMPTED HERE, only in the check. Under Option A `area_m2` is
-- polymorphic: it is the PLOT for land by design, so land divergence from
-- `usable_area` is correct and expected. The view still publishes the number --
-- suppressing it in SQL would hide the one place a future session could verify
-- the claim -- and `scripts/verify_pipeline.py` skips those cells by name.
--
-- FLOOR SHARE IS AN ABSOLUTE LEVEL, NOT A WEEK-OVER-WEEK JUMP. The charter asked
-- for "fail when the share NULLed by the basis floor jumps". A jump detector is
-- blind to a stationary defect, and the unit-price masquerade has been stationary
-- for the entire life of the four portals that carry it -- there is no week in
-- which it jumped. The level is what indicts it, measured live:
--     ceskereality komercni/pronajem  20.0%  (1 133 of 5 656)
--     realitymix   komercni/pronajem  19.0%
--     realitymix   pozemek/pronajem   15.7%
--     remax        komercni/pronajem  11.3%
--     bazos        komercni/pronajem   6.7%
--   versus idnes 0.5% and sreality 0.5% on the same cell.
-- Those are exactly the portals the program charter names as having no per-area
-- price guard (ceskereality, realitymix, bazos) or a weaker one (remax). The
-- week-over-week arm still exists -- it lives in `ppm2_median_shift`, on the
-- medians, where its job is to catch the NEXT regression on the day it ships
-- rather than to re-derive one that is already standing.
--
-- THE 7-DAY ARMS EXIST FOR DETECTION LATENCY. A regression introduced today only
-- reaches the stock share as fast as the corpus churns -- weeks. The same
-- regression is ~100% of the rows that arrived since it shipped. Live proof that
-- the fresh arm is the sharp one: mmreality dum/prodej is 100.0% divergent over
-- the 113 pairs first seen in the trailing 7 days. Both arms are published; the
-- check alarms on the worse of the two.
--
-- ADMIN-GATED, ANON DARK, exactly like `data_quality_by_source`: this is
-- operational data, and the body is a ~12 s sequential scan of 386k active
-- listings. `anon` carries a 3 s statement timeout and no business here.
-- `verify_pipeline` connects as a rolbypassrls role with no JWT claims, which is
-- the branch of `is_platform_admin()` that returns true.
--
-- Catalog-only. No table is written, no backfill, nothing to roll back but the
-- view itself.

begin;

set local lock_timeout = '5s';

create or replace view public.measure_plausibility_by_source as
select * from (
  with rows as (
    select l.source,
           l.category_main,
           l.category_type,
           l.area_m2::numeric                             as area_m2,
           l.usable_area::numeric                         as usable_area,
           (l.first_seen_at > now() - interval '7 days')  as is_recent,
           measure_price_per_m2(l.price_czk::numeric, l.area_m2::numeric,
                                l.category_main, l.category_type) as price_per_m2,
           -- Eligible for the floor: a row that HAS a price, HAS a positive area
           -- and HAS a decidable basis. Anything else is a coverage gap, not a
           -- floor rejection, and pooling the two would let a portal that stops
           -- publishing prices masquerade as a portal publishing bad ones.
           (l.price_czk is not null and l.area_m2 > 0
            and measure_price_per_m2_basis(l.category_main, l.category_type) is not null)
             as floor_eligible,
           (l.area_m2 > 0 and l.usable_area > 0)          as area_pair
      from listings l
     where l.is_active
  )
  select source,
         category_main,
         category_type,
         measure_price_per_m2_basis(category_main, category_type) as price_per_m2_basis,
         count(*)::bigint                                          as n_active,
         (percentile_cont(0.5) within group (order by area_m2))::numeric
           as median_area_m2,
         (percentile_cont(0.5) within group (order by usable_area))::numeric
           as median_usable_area,
         (percentile_cont(0.5) within group (order by price_per_m2))::numeric
           as median_price_per_m2,
         (count(*) filter (where floor_eligible))::bigint          as n_floor_eligible,
         (count(*) filter (where floor_eligible and price_per_m2 is null))::numeric
           / nullif(count(*) filter (where floor_eligible), 0)     as floor_null_share,
         (count(*) filter (where floor_eligible and is_recent))::bigint
           as n_floor_eligible_7d,
         (count(*) filter (where floor_eligible and is_recent and price_per_m2 is null))::numeric
           / nullif(count(*) filter (where floor_eligible and is_recent), 0)
           as floor_null_share_7d,
         (count(*) filter (where area_pair))::bigint               as n_area_pairs,
         (count(*) filter (where area_pair
             and abs(area_m2 - usable_area) / greatest(area_m2, usable_area) > 0.10))::numeric
           / nullif(count(*) filter (where area_pair), 0)          as area_divergence_share,
         (count(*) filter (where area_pair and is_recent))::bigint as n_area_pairs_7d,
         (count(*) filter (where area_pair and is_recent
             and abs(area_m2 - usable_area) / greatest(area_m2, usable_area) > 0.10))::numeric
           / nullif(count(*) filter (where area_pair and is_recent), 0)
           as area_divergence_share_7d
    from rows
   group by source, category_main, category_type
) __admin_gate
where is_platform_admin();

comment on view public.measure_plausibility_by_source is
  'Per (source, category_main, category_type) plausibility of the per-m2 measure over '
  'ACTIVE listings: the medians, the share of measurable rows the basis floor NULLs, and '
  'the share of rows whose area_m2 diverges from usable_area by more than 10%. Read by '
  'scripts/verify_pipeline.py for the ppm2_median_shift / ppm2_basis_floor_share / '
  'area_vs_usable_divergence checks. Complements data_quality_by_source, which tests '
  'presence only and is structurally blind to a populated-but-wrong value. Divergence is '
  'measured ONLY over rows carrying both areas (n_area_pairs) and only above a 10% '
  'relative band; for category_main = ''pozemek'' area_m2 is the PLOT by design (Option A) '
  'so divergence there is expected and the check skips those cells. ~12 s sequential scan: '
  'read it once per run, never per-row.';

-- Live ACL target, identical to data_quality_by_source and pipeline_checks_public:
-- authenticated SELECT (behind the in-body admin gate), service_role full, anon dark.
-- The `anon` revoke is not redundant defence: applied as `postgres` the default ACL
-- gives anon nothing, but applied as `supabase_admin` it grants anon ALL -- and this
-- view is a 12 s scan against a role carrying a 3 s statement timeout.
revoke all on public.measure_plausibility_by_source from public;
revoke all on public.measure_plausibility_by_source from anon;
grant select on public.measure_plausibility_by_source to authenticated;
grant all    on public.measure_plausibility_by_source to service_role;

commit;

-- VERIFICATION (measured live 2026-08-25, pre-W2-backfill; these are the numbers
-- the three checks are sized against and the ones the PR body records):
--
--   -- The mmreality area-basis defect. Expect 1 row: mmreality / dum / prodej,
--   -- area_divergence_share 0.997 over 3 588 pairs and 1.000 over the 113 pairs
--   -- first seen in the trailing week. Every other portal's dum/prodej: 0.000.
--   SELECT source, n_area_pairs, round(area_divergence_share, 3),
--          round(median_area_m2, 1), round(median_usable_area, 1)
--     FROM measure_plausibility_by_source
--    WHERE category_main = 'dum' AND category_type = 'prodej'
--    ORDER BY area_divergence_share DESC NULLS LAST;
--
--   -- The unit-price masquerade. Expect ceskereality/realitymix komercni+pronajem
--   -- around 0.20 and 0.19, realitymix pozemek/pronajem 0.157, remax 0.113,
--   -- bazos 0.067 -- against idnes and sreality at 0.005 on the same cell.
--   SELECT source, category_main, category_type, n_floor_eligible,
--          round(floor_null_share, 3)
--     FROM measure_plausibility_by_source
--    WHERE n_floor_eligible >= 100 AND floor_null_share >= 0.05
--    ORDER BY floor_null_share DESC;
--
--   -- After the W2 backfill heals mmreality, the same cell must read
--   -- median_area_m2 ~130, median_price_per_m2 ~42 500 (from 905 and 5 701) and
--   -- area_divergence_share 0.000 -- and ppm2_median_shift must have FIRED on the
--   -- run that followed the backfill: 6.96x on area, 7.45x on the measure, both
--   -- far above the 3.0x fail ratio. A silent heal means the shift check is inert.
