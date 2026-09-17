-- 534_plot_area_m2.sql
--
-- W21 of the area program: ONE definition of "the plot area", and the direct
-- detector for the defect W21 fixed in the mmreality parser.
--
-- ---------------------------------------------------------------------------
-- 1. WHY A NAMED MEASURE FOR THE PLOT
-- ---------------------------------------------------------------------------
-- `listings.area_m2` is POLYMORPHIC by design (rule 23): the interior measure for
-- byt / dum / komercni, THE PARCEL for `pozemek`. `estate_area` is the side column
-- that carries a DWELLING's plot beside its floor area.
--
-- Every reader of "the plot area" spelled that as `estate_area` and nothing else,
-- which is right for a house and silently wrong for land -- on land the plot IS
-- `area_m2`, and `estate_area` is whatever the page happened to also state. Five
-- portals do state it (sreality, bezrealitky, idnes, ceskereality, maxima); the
-- other four do not. Measured live 2026-09-17 over ACTIVE `pozemek` rows:
--
--     bazos        15 846 land rows   estate_area NULL on 15 846
--     realitymix   11 472             NULL on 11 470
--     mmreality     3 653             NULL on 3 653
--     remax         1 649             NULL on 1 649
--     ceskereality 11 407             NULL on 5
--     idnes        27 199             NULL on 3
--     ---------------------------------------------------------------
--     32 626 of 101 021 active land rows (32.3%) carry NO estate_area,
--     31 613 of them carry the parcel in `area_m2`
--
-- So `min_estate_area = 500` -- an operator asking for "plots of at least 500 m2",
-- which the filter registry describes as covering "houses and land" -- dropped a
-- third of the country's land inventory without a word. Not because the data is
-- missing: because the reader was asking the wrong column for that category.
--
-- THE FIX IS A NAME, NOT A COLUMN. Filling `estate_area` for land in the writers
-- would duplicate `area_m2` into a second column on 32k rows, put two numbers
-- where the page states one, and leave every future portal to remember the rule.
-- `plot_area_m2(category_main, area_m2, estate_area)` says it once, and the five
-- portals that DO publish a parcel cell on a land page keep publishing it --
-- faithful to their pages, and identical to `area_m2` there anyway.
--
-- Declaration style is `measure_price_per_m2`'s (migration 425), deliberately:
-- single expression, IMMUTABLE, PARALLEL SAFE, NO `SET search_path`. A SET clause
-- blocks inlining and turns every predicate on the measure into a per-row function
-- call. STRICT is NOT declared either -- a NULL `estate_area` on a dwelling is the
-- answer (no plot stated), not a reason to skip the CASE.
--
-- ---------------------------------------------------------------------------
-- 2. WHY THE PLAUSIBILITY VIEW GAINS AN ARM
-- ---------------------------------------------------------------------------
-- W21's mmreality defect was a SIDE column carrying a DERIVED SUM: the parser read
-- `landArea or plotArea or totalArea`, and on 14 417 stored rows `landArea` and
-- `plotArea` carry a value on exactly ZERO of them, so `estate_area` was always
-- `totalArea` -- which the page computes as `parcelArea + usableArea` (4 133 stored
-- rows carry all three and agree). A sum is not a measurement. `data_quality_by_source`
-- cannot see it (the column is populated on every row) and `area_vs_usable_divergence`
-- cannot either (it watches the HEADLINE against `usable_area`, and mmreality's
-- headline was already the interior).
--
-- `estate_sum_share` is that missing arm, stated as an invariant rather than as a
-- portal: `estate_area` must not equal `area_m2 + usable_area`. Measured live over
-- every cell carrying all three areas, the share within 1% of that sum:
--
--     mmreality  dum/pronajem   0.0545   (55 rows -- below the 100-row floor)
--     mmreality  dum/prodej     0.0258   (1 123 rows -- the worst SCORED cell)
--     bezrealitky dum/pronajem  0.0200   (50 rows)
--     every other cell         <= 0.0135
--
-- Read that honestly: the relation is a WEAK fingerprint of the live mmreality
-- shape, not a smoking gun. mmreality's headline is `usableArea` and its
-- `estate_area` was `parcelArea + usableArea`, so the two coincide only where the
-- parcel happens to equal the interior -- which is why the top cell is 2.6% and not
-- 100%. What the arm buys is the FORWARD guard: a portal that starts writing a
-- derived total into a side column shows up as a cell, and mmreality is already the
-- top two rows of the ranking, so the arm is calibrated on a real signal rather than
-- on noise. Thresholds (verify_pipeline `estate_sum_share_warn/fail` = 0.05 / 0.10)
-- sit at ~2x and ~4x the worst scored cell, above a <= 1.4% background.
--
-- The view is extended by APPENDING two columns; `create or replace view` permits
-- that and nothing else, so no existing column's name, type or position moves and
-- every reader of the current shape keeps working. Body otherwise VERBATIM from
-- migration 427, which is still the live definition.
--
-- Additive: one new function, one view replaced. No table is written, no backfill.

begin;

set local lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 1. THE PLOT MEASURE.
-- ---------------------------------------------------------------------------
create or replace function public.plot_area_m2(
  p_category_main text,
  p_area_m2 numeric,
  p_estate_area numeric
)
 RETURNS numeric
 LANGUAGE sql
 IMMUTABLE PARALLEL SAFE
AS $function$
    SELECT CASE
        WHEN p_category_main = 'pozemek' THEN p_area_m2
        ELSE p_estate_area END
$function$;

comment on function public.plot_area_m2(text, numeric, numeric) is
  'THE plot-area measure: the m2 of LAND a listing offers. For category_main = '
  '''pozemek'' that is area_m2 itself -- the headline is polymorphic by design '
  '(rule 23) and for a parcel it IS the parcel; for every other category it is '
  'estate_area, the side column carrying a dwelling''s lot beside its floor area. '
  'Exists because reading estate_area directly drops 32 626 of 101 021 active land '
  'rows (32.3%, measured 2026-09-17) whose portal states the parcel only as the '
  'headline: bazos, realitymix, mmreality and remax publish no separate parcel cell '
  'on a land page. Do NOT close that gap by filling estate_area for land in the '
  'writers -- that duplicates area_m2 into a second column and leaves every future '
  'portal to remember the rule. Returns NULL when the row states no plot, which is '
  'a visible gap and never a guess. Python face: toolkit.measures.plot_area_m2 / '
  'plot_area_sql. No SET search_path: a SET clause blocks inlining and turns every '
  'predicate on the measure into a per-row function call.';

revoke all on function public.plot_area_m2(text, numeric, numeric) from public;
grant execute on function public.plot_area_m2(text, numeric, numeric)
  to anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 2. THE PLAUSIBILITY VIEW, + the derived-sum arm (two APPENDED columns).
--    Migration 427's body verbatim; only the two `estate_*` lines are new.
-- ---------------------------------------------------------------------------
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
           (l.area_m2 > 0 and l.usable_area > 0)          as area_pair,
           -- W21: the three-area rows the derived-sum arm can score, and the
           -- relation itself. Within 1% because the columns are numeric(*,1) and a
           -- page's own rounding moves the sum by a decimetre, not by a percent.
           (l.area_m2 > 0 and l.usable_area > 0 and l.estate_area > 0)
             as area_triple,
           (l.estate_area > 0 and l.area_m2 > 0 and l.usable_area > 0
            and abs(l.estate_area - (l.area_m2 + l.usable_area))
                <= 0.01 * greatest(l.estate_area, l.area_m2 + l.usable_area))
             as estate_is_sum
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
           as area_divergence_share_7d,
         -- COVERAGE. Every column above is a ratio over rows that HAVE the inputs, so
         -- a cell with no inputs at all scores no arm and is skipped -- which reads
         -- exactly like a clean one. The four columns below are the denominators that
         -- make "nothing to measure" say so. `n_area_valued` / `n_ppm2_valued` are the
         -- SUPPORT of the two medians (percentile_cont ignores NULLs), never n_active:
         -- bezrealitky pozemek/prodej has 1 643 active rows and 9 areas, so its median
         -- rests on 9 values and must not be gated on 1 643.
         (count(area_m2))::bigint                                  as n_area_valued,
         (count(price_per_m2))::bigint                             as n_ppm2_valued,
         (count(*) filter (where is_recent))::bigint               as n_active_7d,
         -- The share of active rows for which the measure has NO INPUT -- no price, or
         -- no positive area. Deliberately NOT `price_per_m2 is null`: a row the basis
         -- floor rejected has its inputs and is already indicted by floor_null_share,
         -- and pooling the two would bill one portal twice for one defect. NULL when
         -- the basis itself is undecidable, because there the absent measure is the
         -- specified answer (charter: a visible gap, never a guess) and not a gap.
         --
         -- IT IS `total MINUS eligible`, NEVER `filter (where not floor_eligible)`, and
         -- the difference is the whole column. `floor_eligible` is `price is not null
         -- AND area_m2 > 0 AND ...`; with `area_m2` NULL the comparison is NULL, so the
         -- conjunction is NULL, and `not NULL` is NULL -- which a FILTER clause drops.
         -- The rows this column exists to count are exactly the rows with a NULL area,
         -- so the negated spelling reports 0.484 where the truth is 1.000 and hides all
         -- 27 174 sreality land rows. Measured both ways against production before this
         -- line was written this way.
         case when measure_price_per_m2_basis(category_main, category_type) is null
              then null
              else (count(*) - count(*) filter (where floor_eligible))::numeric
                   / count(*)
         end                                                       as measure_input_gap_share,
         case when measure_price_per_m2_basis(category_main, category_type) is null
              then null
              else (count(*) filter (where is_recent)
                    - count(*) filter (where floor_eligible and is_recent))::numeric
                   / nullif(count(*) filter (where is_recent), 0)
         end                                                       as measure_input_gap_share_7d,
         -- W21, APPENDED (create or replace view permits new columns only at the end).
         -- The share of three-area rows whose `estate_area` is the SUM of the other two
         -- rather than a measurement of its own -- the shape mmreality's parser wrote
         -- for years by reading a page-derived `totalArea` as the parcel. Scored over
         -- n_estate_triples, never n_active: a portal that publishes one of the three
         -- fields is silent here, not clean.
         (count(*) filter (where area_triple))::bigint             as n_estate_triples,
         (count(*) filter (where estate_is_sum))::numeric
           / nullif(count(*) filter (where area_triple), 0)        as estate_sum_share
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
  'so divergence there is expected and the check skips those cells. Also publishes the '
  'DENOMINATORS every one of those ratios is blind to: n_area_valued / n_ppm2_valued are '
  'the support of the two medians (gate a median on its own support, never on n_active), '
  'and measure_input_gap_share is the share of active rows the measure has no input for -- '
  'the arm that makes a cell with nothing to measure say so instead of reading clean. '
  'W21 appended estate_sum_share: the share of rows carrying all three areas whose '
  'estate_area equals area_m2 + usable_area within 1%, i.e. a side column holding a '
  'DERIVED SUM instead of a measurement -- the class of defect mmreality shipped by '
  'reading a page-computed totalArea as the parcel, which neither a null-check nor the '
  'headline-vs-usable arm can see. '
  '~12 s sequential scan: read it once per run, never per-row.';

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

-- VERIFICATION (measured live 2026-09-17, BEFORE the W21 parser fix + heal):
--
--   -- The plot measure exposes the land the estate_area reader drops. Measured at
--   -- apply time: plot_rows 100 035 against estate_rows 68 418 on active land
--   -- (the active corpus churns hourly, so read the GAP -- ~32k -- not the totals).
--   select count(plot_area_m2(category_main, area_m2, estate_area)) as plot_rows,
--          count(estate_area)                                       as estate_rows
--     from listings where is_active and category_main = 'pozemek';
--
--   -- ... and changes NOTHING for a dwelling: expect zero rows.
--   select count(*) from listings
--    where is_active and category_main is distinct from 'pozemek'
--      and plot_area_m2(category_main, area_m2, estate_area)
--          is distinct from estate_area;
--
--   -- The new arm. Expect mmreality dum/prodej top of the scored cells at
--   -- ~0.0258 over 1 123 triples, every other scored cell <= 0.0135.
--   select source, category_main, category_type, n_estate_triples, estate_sum_share
--     from measure_plausibility_by_source
--    where n_estate_triples >= 100
--    order by estate_sum_share desc nulls last limit 5;
--
--   -- The stale calibration this replaces: area_vs_usable_divergence's only
--   -- non-zero cell today is mmreality dum/prodej at 0.5709 over 2 638 pairs
--   -- (the 1 515 pre-W1 rows still carrying the sum as their headline), and its
--   -- 7d arm is 0.0000 over 150 pairs -- the live parser has been right since W1.
--   -- After the W21 heal both should read 0.0000.
--   select source, category_main, category_type,
--          n_area_pairs, area_divergence_share, n_area_pairs_7d, area_divergence_share_7d
--     from measure_plausibility_by_source
--    where category_main is distinct from 'pozemek' and n_area_pairs >= 100
--    order by area_divergence_share desc nulls last limit 5;
