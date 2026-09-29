-- 577: the control draw's operator sample (B-j) — 100 pairs into `autodedup.eval_samples`.
--
-- The Judge page (`/autodedup/judge`, PR #1644, PROGRAM.md E922) lists every pair of a sealed
-- random draw in `autodedup.eval_samples` under "Náhodný vzorek pro vás" and counts it on the
-- `judge` progress strip (the first 100 of each stratum, in the seeded order). This file seals
-- the first such draw: 100 pairs picked WITHOUT the engine's ranking, so the operator's binary
-- reads measure the judge and the engine on pairs the engine did not choose.
--
-- The draw is pre-registered (seed 20260929; w15/control_draw/PREREGISTRATION.md, script
-- control_draw.py): 500 pairs of the post-heal trial, cohort 17 and cohort 18 exports, in nine
-- strata (the stamps on autodedup/pairs/g2_control_{trial,c17,c18}.json): pairs of one
-- advertiser in one town and one category, from cells of 2-6 adverts (`adv_small`) and 7+
-- (`adv_large`), and uniform stored candidate pairs (`cand`). Inside each stratum a second
-- seeded order picks the operator's share: 10 + 10 per cohort and 14 / 13 / 13 candidates.
-- `sampling_rate` is that share over the stratum's population after the exclusions (pairs
-- already ruled, judged, listed or kept apart), so a read can be weighted back to the market.
-- gold_label, engine_zone and engine_score stay NULL: the operator's word lives in
-- `autodedup.verdicts`, and the engine's view is read live from the generation.
--
-- ADDITIVE, data only: 100 inserted rows, no schema change. IDEMPOTENT: a pair already sealed
-- under the same stratum is left as it is (`on conflict do nothing`), and the block RAISES if a
-- pair of this draw is sealed under another stratum or the draw does not end up whole.
-- ORDER: apply after 576 and after PR #1644's code is live (that is the page that reads it;
-- before it, nothing reads these rows). Undo (destructive, operator OK first):
-- delete from autodedup.eval_samples where stratum like 'g2:control\_%';

set lock_timeout = '5s';

do $$
declare
  inserted int;
  already int;
  elsewhere int;
begin
  with draw (stratum, listing_lo, listing_hi, sampling_rate) as (
    values
      ('g2:control_adv_large_c17'::text, 902::bigint, 15688::bigint, 7.082655e-04::double precision),
      ('g2:control_adv_large_c17', 96153, 96513, 7.082655e-04),
      ('g2:control_adv_large_c17', 143990, 209188, 7.082655e-04),
      ('g2:control_adv_large_c17', 254919, 256937, 7.082655e-04),
      ('g2:control_adv_large_c17', 348309, 15424446, 7.082655e-04),
      ('g2:control_adv_large_c17', 404485, 17583676, 7.082655e-04),
      ('g2:control_adv_large_c17', 509768, 16943041, 7.082655e-04),
      ('g2:control_adv_large_c17', 10952940, 10952941, 7.082655e-04),
      ('g2:control_adv_large_c17', 16884544, 18585304, 7.082655e-04),
      ('g2:control_adv_large_c17', 17128253, 18805591, 7.082655e-04),
      ('g2:control_adv_large_c18', 140356, 13165362, 6.107243e-04),
      ('g2:control_adv_large_c18', 291497, 291530, 6.107243e-04),
      ('g2:control_adv_large_c18', 334012, 334090, 6.107243e-04),
      ('g2:control_adv_large_c18', 379435, 18784661, 6.107243e-04),
      ('g2:control_adv_large_c18', 417090, 17215787, 6.107243e-04),
      ('g2:control_adv_large_c18', 10130198, 10130205, 6.107243e-04),
      ('g2:control_adv_large_c18', 18685508, 18700253, 6.107243e-04),
      ('g2:control_adv_large_c18', 18685821, 18686059, 6.107243e-04),
      ('g2:control_adv_large_c18', 18921386, 18921393, 6.107243e-04),
      ('g2:control_adv_large_c18', 18984005, 18984007, 6.107243e-04),
      ('g2:control_adv_large_trial', 79287, 341755, 1.221598e-03),
      ('g2:control_adv_large_trial', 82625, 13568669, 1.221598e-03),
      ('g2:control_adv_large_trial', 323232, 485993, 1.221598e-03),
      ('g2:control_adv_large_trial', 399647, 416898, 1.221598e-03),
      ('g2:control_adv_large_trial', 412530, 413594, 1.221598e-03),
      ('g2:control_adv_large_trial', 413587, 413590, 1.221598e-03),
      ('g2:control_adv_large_trial', 506206, 13462905, 1.221598e-03),
      ('g2:control_adv_large_trial', 13850605, 18922915, 1.221598e-03),
      ('g2:control_adv_large_trial', 13850718, 15706501, 1.221598e-03),
      ('g2:control_adv_large_trial', 15601101, 18754086, 1.221598e-03),
      ('g2:control_adv_small_c17', 83939, 84062, 1.500150e-03),
      ('g2:control_adv_small_c17', 133627, 13210092, 1.500150e-03),
      ('g2:control_adv_small_c17', 165186, 165776, 1.500150e-03),
      ('g2:control_adv_small_c17', 173093, 17097639, 1.500150e-03),
      ('g2:control_adv_small_c17', 285201, 285222, 1.500150e-03),
      ('g2:control_adv_small_c17', 321743, 505846, 1.500150e-03),
      ('g2:control_adv_small_c17', 345884, 365020, 1.500150e-03),
      ('g2:control_adv_small_c17', 393310, 397044, 1.500150e-03),
      ('g2:control_adv_small_c17', 477462, 481553, 1.500150e-03),
      ('g2:control_adv_small_c17', 18588061, 18896413, 1.500150e-03),
      ('g2:control_adv_small_c18', 49436, 19049171, 1.490535e-03),
      ('g2:control_adv_small_c18', 53344, 53373, 1.490535e-03),
      ('g2:control_adv_small_c18', 294034, 294161, 1.490535e-03),
      ('g2:control_adv_small_c18', 432482, 18970138, 1.490535e-03),
      ('g2:control_adv_small_c18', 457453, 19006362, 1.490535e-03),
      ('g2:control_adv_small_c18', 15864820, 18673701, 1.490535e-03),
      ('g2:control_adv_small_c18', 17941108, 18647815, 1.490535e-03),
      ('g2:control_adv_small_c18', 18568864, 18697890, 1.490535e-03),
      ('g2:control_adv_small_c18', 18617945, 18811245, 1.490535e-03),
      ('g2:control_adv_small_c18', 18699568, 18699912, 1.490535e-03),
      ('g2:control_adv_small_trial', 16439, 16441, 5.216484e-03),
      ('g2:control_adv_small_trial', 80801, 80818, 5.216484e-03),
      ('g2:control_adv_small_trial', 83190, 83247, 5.216484e-03),
      ('g2:control_adv_small_trial', 158802, 170928, 5.216484e-03),
      ('g2:control_adv_small_trial', 257267, 257419, 5.216484e-03),
      ('g2:control_adv_small_trial', 261471, 18922018, 5.216484e-03),
      ('g2:control_adv_small_trial', 285571, 285578, 5.216484e-03),
      ('g2:control_adv_small_trial', 328875, 10712579, 5.216484e-03),
      ('g2:control_adv_small_trial', 447744, 447747, 5.216484e-03),
      ('g2:control_adv_small_trial', 18642117, 18781906, 5.216484e-03),
      ('g2:control_cand_c17', 26219, 119651, 1.517982e-04),
      ('g2:control_cand_c17', 51603, 12243550, 1.517982e-04),
      ('g2:control_cand_c17', 87776, 428813, 1.517982e-04),
      ('g2:control_cand_c17', 100572, 18988560, 1.517982e-04),
      ('g2:control_cand_c17', 104052, 18683772, 1.517982e-04),
      ('g2:control_cand_c17', 106740, 18955122, 1.517982e-04),
      ('g2:control_cand_c17', 109684, 323229, 1.517982e-04),
      ('g2:control_cand_c17', 387877, 18587428, 1.517982e-04),
      ('g2:control_cand_c17', 441742, 475174, 1.517982e-04),
      ('g2:control_cand_c17', 467789, 18684465, 1.517982e-04),
      ('g2:control_cand_c17', 540758, 541414, 1.517982e-04),
      ('g2:control_cand_c17', 10873067, 18953231, 1.517982e-04),
      ('g2:control_cand_c17', 18830687, 18833100, 1.517982e-04),
      ('g2:control_cand_c18', 2969, 12585369, 2.060908e-04),
      ('g2:control_cand_c18', 112874, 548607, 2.060908e-04),
      ('g2:control_cand_c18', 142532, 319025, 2.060908e-04),
      ('g2:control_cand_c18', 342084, 18760907, 2.060908e-04),
      ('g2:control_cand_c18', 441370, 13785585, 2.060908e-04),
      ('g2:control_cand_c18', 469213, 469865, 2.060908e-04),
      ('g2:control_cand_c18', 469847, 17422760, 2.060908e-04),
      ('g2:control_cand_c18', 479220, 513060, 2.060908e-04),
      ('g2:control_cand_c18', 541655, 13426414, 2.060908e-04),
      ('g2:control_cand_c18', 11372506, 18834130, 2.060908e-04),
      ('g2:control_cand_c18', 13213603, 13705927, 2.060908e-04),
      ('g2:control_cand_c18', 13994900, 13994904, 2.060908e-04),
      ('g2:control_cand_c18', 18762225, 18780130, 2.060908e-04),
      ('g2:control_cand_trial', 8018, 18563672, 7.117076e-04),
      ('g2:control_cand_trial', 10117, 276571, 7.117076e-04),
      ('g2:control_cand_trial', 20101, 338550, 7.117076e-04),
      ('g2:control_cand_trial', 24074, 18852788, 7.117076e-04),
      ('g2:control_cand_trial', 54787, 83275, 7.117076e-04),
      ('g2:control_cand_trial', 65585, 15427866, 7.117076e-04),
      ('g2:control_cand_trial', 77417, 395437, 7.117076e-04),
      ('g2:control_cand_trial', 105737, 18675912, 7.117076e-04),
      ('g2:control_cand_trial', 194031, 17973016, 7.117076e-04),
      ('g2:control_cand_trial', 498228, 18987502, 7.117076e-04),
      ('g2:control_cand_trial', 519905, 12601662, 7.117076e-04),
      ('g2:control_cand_trial', 11645894, 13380591, 7.117076e-04),
      ('g2:control_cand_trial', 12123552, 14217487, 7.117076e-04),
      ('g2:control_cand_trial', 12369730, 12381029, 7.117076e-04)
  ), prior as (
    select d.stratum, e.stratum as sealed
      from draw d
      join autodedup.eval_samples e
        on e.listing_lo = d.listing_lo and e.listing_hi = d.listing_hi
  ), ins as (
    insert into autodedup.eval_samples (stratum, listing_lo, listing_hi, sampling_rate)
    select d.stratum, d.listing_lo, d.listing_hi, d.sampling_rate from draw d
    on conflict (listing_lo, listing_hi) do nothing
    returning 1
  )
  select (select count(*) from ins),
         (select count(*) from prior where sealed = stratum),
         (select count(*) from prior where sealed <> stratum)
    into inserted, already, elsewhere;

  if elsewhere > 0 then
    raise exception 'migration 577: % pair(s) of the control draw are already sealed under another stratum', elsewhere;
  end if;
  if inserted + already <> 100 then
    raise exception 'migration 577: % inserted + % already sealed is not the 100 pairs of the draw', inserted, already;
  end if;
  raise notice 'migration 577: % inserted, % already sealed', inserted, already;
end
$$;

reset lock_timeout;
