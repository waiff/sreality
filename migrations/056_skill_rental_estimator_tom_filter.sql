-- 056_skill_rental_estimator_tom_filter.sql
--
-- Update rental_estimator_full_v1's system prompt to:
--
--   1. Build the first cohort from delisted listings with tom_days_max=180
--      (active included only if it materially enlarges the sample). These
--      "recently-cleared" comparables are the strongest evidence of the
--      market's pricing.
--   2. Compute market velocity on that cohort and prune by p75 of TOM —
--      excluding listings whose asking prices reflect friction rather
--      than demand.
--   3. Repurpose step 6 (was "CONSIDER DEMAND") to focus on the
--      recent-vs-older trend split for confidence-tiering, since velocity
--      is now mandatory in step 1.
--
-- Both the cohort filter AND the velocity tool needed to learn about
-- `tom_days_max`; the agent.py changes in the same commit add it to
-- find_comparables_relaxed and compute_market_velocity input_schemas
-- and to the velocity handler's per-call overrides.
--
-- Surgical replace() against the exact live system_prompt anchors. The
-- DB version uses `--` ASCII double-hyphen (drift from the canonical
-- `—` em-dash in skills/rental_estimator_full_v1/SKILL.md from a prior
-- migration); this anchor matches the DB. The trigger from migration
-- 029 writes a skills_history row automatically.

begin;

update skills
   set system_prompt = replace(
         system_prompt,
         E'1. START BROAD. Your first tool call is almost always `find_comparables_relaxed`\n'
         '   with the target''s lat/lng, area, disposition, and a sensible radius (1000m\n'
         '   in Prague, 1500m in regional cities). The "relaxed" variant automatically\n'
         '   widens area_band_pct / disposition_match until it has enough comparables,\n'
         '   so you don''t need to guess thresholds.',
         E'1. BUILD THE FIRST COHORT FROM RECENTLY-CLEARED COMPARABLES. Your first\n'
         '   tool call is `find_comparables_relaxed` with the target''s lat/lng, area,\n'
         '   disposition, a sensible radius (1000m in Prague, 1500m in regional\n'
         '   cities), AND two TOM-narrowing filters: `population="delisted"` and\n'
         '   `tom_days_max=180`. This restricts the cohort to listings that have\n'
         '   already cleared the market within the last ~6 months -- the strongest\n'
         '   "the market priced at this point" evidence we have. The "relaxed"\n'
         '   variant widens area_band_pct / disposition_match until it has enough\n'
         '   comparables.\n'
         '\n'
         '   If the delisted-only cohort is thin (<= ~10 listings), re-run once with\n'
         '   `population="all"` keeping `tom_days_max=180`. Adopt the wider cohort\n'
         '   only if it materially enlarges the sample (~2x or more); otherwise stick\n'
         '   with delisted-only.\n'
         '\n'
         '   Immediately after the first cohort settles, call\n'
         '   `compute_market_velocity` once with the same `population` and\n'
         '   `tom_days_max` you used in the find call -- this measures TOM stats on\n'
         '   the same cohort. Read `data.tom_stats.p75`, then re-run\n'
         '   `find_comparables_relaxed` with `tom_days_max=<that p75 value>` (every\n'
         '   other filter unchanged). This prunes the longer-tail listings whose\n'
         '   asking prices reflect friction rather than demand; the pruned cohort\n'
         '   is your working cohort for the rest of the run. If the prune would\n'
         '   shrink the cohort below ~10, skip it and add a warning\n'
         '   ("velocity-based TOM prune skipped -- would have left an unreliable\n'
         '   sample size").'
       ),
       updated_by = 'seed'
 where name = 'rental_estimator_full_v1';

update skills
   set system_prompt = replace(
         system_prompt,
         E'6. CONSIDER DEMAND. If the cohort price spread is wide (iqr/median > 0.35),\n'
         '   call `compute_market_velocity` once. A median TOM > 60 days or a sharp\n'
         '   recent-vs-older slowdown is grounds to nudge confidence down one tier\n'
         '   and add a warning. Skip it for tight cohorts -- TOM tells you nothing\n'
         '   you don''t already know from a narrow IQR.',
         E'6. READ THE VELOCITY TREND FOR CONFIDENCE. The `compute_market_velocity`\n'
         '   call you already made in step 1 returned a trend split. Inspect\n'
         '   `data.trend.recent.median_tom_days` vs `data.trend.older.median_tom_days`:\n'
         '   a sharp slowdown (recent materially higher than older) means the market\n'
         '   is cooling and the p75 you used to prune in step 1 was set during a\n'
         '   more buoyant period -- nudge confidence down one tier and add a warning\n'
         '   ("market slowing; recent median TOM Xd vs older Yd"). A sharp speed-up\n'
         '   is informational only -- don''t upgrade confidence above what spread and\n'
         '   sample size justify.'
       ),
       updated_by = 'seed'
 where name = 'rental_estimator_full_v1';

commit;
