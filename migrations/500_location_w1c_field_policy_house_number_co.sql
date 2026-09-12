-- 500_location_w1c_field_policy_house_number_co.sql
--
-- Location-data programme, wave W1-c: the ONE survivorship rung migration 499
-- missed. Additive, data-only, re-runnable.
--
-- 499 seeded `regex_text` (rank 550) for the five fields the slim contracts were
-- expected to produce through a pattern over the portal's own markup: obec_name,
-- cast_obce_name, kraj_name, house_number_cp, psc. The nine redrafted contracts
-- produce a sixth, measured after the redraft rather than predicted before it:
-- realitymix's `rm.det.og_house_number_co` reads the orientation half out of the
-- SAME `og:title` capture as the čp half, so `house_number_cp` arrives ranked and
-- `house_number_co` - its pair, never an alternative - would be declined at S7
-- with `_best_policy` returning None. A čp with no čo is the exact hole the pair
-- exists to close.
--
-- 499 is applied (or queued) as written; migrations are append-only, so the row
-- is added here rather than by widening that file's array.
--
-- Same rung, same flags, same argument as 499 and 470 - an anchored pattern over
-- the portal's own markup, carrying a quote and a byte span into a
-- content-addressed archived body, is stronger than a model reading prose (900)
-- and weaker than a typed payload field (300) or a named element read whole
-- (400). Rows go into the EXISTING 'v1'; a new `policy_version` is one of the
-- five resolution-identity columns and would invalidate every stored resolution
-- corpus-wide.
--
-- No re-resolve section: no claim of this shape exists yet (the contract that
-- emits it lands with this file), so every claim this row governs is new, and a
-- new claim set is a new `claim_set_hash` - a re-resolution the drain does on
-- its own.

begin;

insert into location_field_policy
  (policy_version, field, source_pattern, method_pattern, rank,
   min_confidence, may_fill_null, may_overwrite_non_null, requires_independent_agreement)
select 'v1', f.field, 'portal:*', 'regex_text', 550,
       null::match_confidence, true, false, false
from unnest(array['house_number_co']::location_claim_type[]) as f(field)
on conflict (policy_version, field, source_pattern, method_pattern) do nothing;

commit;
