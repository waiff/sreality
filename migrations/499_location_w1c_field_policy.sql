-- 499_location_w1c_field_policy.sql
--
-- Location-data programme, wave W1-c: the survivorship rungs the SLIM contracts
-- need. Additive, data-only, re-runnable.
--
-- THE GAP. `location_field_policy` is evaluated by one function,
-- `survivorship.evaluate_field`, and a claim whose (source, extraction_method)
-- matches NO row is not "unranked" - `_best_policy` returns None, the claim is
-- SKIPPED, and the field lands in `resolution.survivorship_blocked` forever.
-- Migration 470 seeded `regex_text` for two fields only (street_name,
-- okres_name), because those were the only two the W2 activation emitted through
-- it. W1-c rewrites the nine contracts around ONE entry per claim type over the
-- stored page body, and `html_regex` / `html_attr_regex` (method `regex_text`)
-- is the shape that reads a whole `Lokalita` row or a URL slug - so the town
-- itself, the quarter, the kraj, the house number and the PSC now arrive through
-- that producer on portals where nothing else states them.
--
-- Without these rows those claims are written correctly, carry their evidence
-- span, and are declined at S7 - which on `obec_name` is exactly the
-- town-coverage hole `location_town_coverage` is red for (rule 25).
--
-- WHICH PAIRS. The five (regex_text x claim_type) pairs the slim contracts
-- produce that have no v1 row: obec_name, cast_obce_name, kraj_name,
-- house_number_cp, psc. `street_name` and `okres_name` already have theirs from
-- 470 and are not restated.
--
-- RANK 550, the row 470 established for this method, and the argument is
-- unchanged: an anchored pattern over the portal's own markup, carrying a quote
-- and a byte span into a content-addressed archived body that the D7 CHECKs
-- refuse to store without, is a stronger instrument than a model reading prose
-- (900) and a weaker one than a typed payload field (300) or a named element
-- read whole (400).
--
-- THE GUARD FLAGS, stated rather than defaulted and identical to 470's:
--   * may_fill_null = true - the whole point; these fields are NULL today.
--   * may_overwrite_non_null = FALSE - D7's graded write-back: a differing
--     non-NULL incumbent becomes a `write_back_blocked_non_null` contradiction
--     for a human, not a silent replacement.
--   * requires_independent_agreement = FALSE - C7's "one portal, one voice":
--     `_independently_agreed` counts DISTINCT claim.source, and every claim
--     these rows govern comes from ONE portal reading its OWN page. There is no
--     second source that could ever agree, so requiring one would not make the
--     claims safer, it would make them permanently unusable. `_gazetteer_
--     validate` runs on `regex_text` by construction (survivorship.TEXT_METHODS).
--
-- POLICY VERSION. Rows go into the EXISTING 'v1', per the 388/400/470
-- precedent, with `on conflict ... do nothing` so the file is re-runnable. A new
-- `policy_version` is one of the five resolution-identity columns and would
-- invalidate every stored resolution corpus-wide.
--
-- NO RE-RESOLVE SECTION, unlike 470. The pairs below have no claims today: the
-- contracts that will emit them are not written yet (the rewrites land on top of
-- this), so every claim they govern will be new, and a new claim set is a new
-- `claim_set_hash` - i.e. a re-resolution the drain does on its own. Enqueuing
-- the corpus for rows that can change no existing decision would be a multi-
-- million-row write that buys nothing.

begin;

-- regex_text (rank 550) - a pattern over the portal's own markup, which is how
-- the slim contracts read a `Lokalita` row (bazos), a title line
-- (ceskereality, mmreality), a description's "okres X" (maxima) and a URL slug.
insert into location_field_policy
  (policy_version, field, source_pattern, method_pattern, rank,
   min_confidence, may_fill_null, may_overwrite_non_null, requires_independent_agreement)
select 'v1', f.field, 'portal:*', 'regex_text', 550,
       null::match_confidence, true, false, false
from unnest(array[
       'obec_name', 'cast_obce_name', 'kraj_name', 'house_number_cp', 'psc'
     ]::location_claim_type[]) as f(field)
on conflict (policy_version, field, source_pattern, method_pattern) do nothing;

commit;
