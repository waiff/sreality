-- 400_location_w1v_operator_field_policy.sql
--
-- Location-data program, Wave W1v: survivorship rows for OPERATOR claims.
--
-- S7 ranks operator arbitration first (03 section 3.9), and the schema has
-- carried every needed vocabulary since W1 (`extraction_method =
-- 'operator_manual'`, `surface = 'operator_input'`, `licence_class =
-- 'operator'`, `dirty_locations.reason = 'operator_edit'`) - but the v1
-- `location_field_policy` seed (383, extended by 388) has NO operator row, and
-- survivorship.evaluate_field SKIPS any claim without a matching policy row.
-- W1 could not observe the gap: no operator write path existed, so no operator
-- claim was ever evaluated. W1v ships that write path, and without these rows
-- an operator correction would win position/candidates (which are policy-free)
-- while silently losing every survivorship FIELD - the correction would move
-- the pin and not the street.
--
-- Rank 50: strictly ahead of registry (100) - "derived never overwrites
-- claimed", and an operator statement is the strongest claim the system
-- accepts. may_overwrite_non_null = true is deliberate and is exactly D7's
-- graded write-back: the OPERATOR is the one producer allowed to overwrite a
-- non-NULL value; the llm_text lane stays fill-NULL-only.
--
-- Rows are inserted into the EXISTING policy_version 'v1' (the 388 precedent):
-- a NEW policy_version is one of the five resolution-identity columns and
-- would invalidate every stored resolution corpus-wide; adding producer rows
-- to v1 changes nothing for claims that have no operator producer, which today
-- is all of them.

begin;

insert into location_field_policy
  (policy_version, field, source_pattern, method_pattern, rank,
   min_confidence, may_fill_null, may_overwrite_non_null, requires_independent_agreement)
select 'v1', f.field, 'operator', 'operator_manual', 50,
       null::match_confidence, true, true, false
from unnest(array[
       -- the 383 seed's contended fields
       'coordinate', 'address_point_id', 'street_name', 'house_number_cp', 'house_number_co',
       'psc', 'obec_name', 'cast_obce_name', 'okres_name', 'kraj_name',
       -- the 388 additions
       'evidencni', 'postal_town', 'development_name', 'cadastral_territory_name',
       'parcel_number'
     ]::location_claim_type[]) as f(field)
on conflict (policy_version, field, source_pattern, method_pattern) do nothing;

commit;
