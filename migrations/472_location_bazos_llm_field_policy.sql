-- 472_location_bazos_llm_field_policy.sql
--
-- Location-data program, wave W2-10: the survivorship rungs without which the
-- bazos free-text LLM lane writes correct, evidence-quoted claims that can never
-- win a field.
--
-- WHAT SHIPS WITH THIS. `contracts/portals/bazos.yaml` bumps to @3 and, for the
-- first time in the fleet, names `llm_location_text` on sixteen entries - eight
-- output fields read from the ad's DESCRIPTION and the same eight from its
-- TITLE. `location_data/claims_llm.py` was merged INERT precisely because no
-- contract named its reader; these are its policy rows and the bump is the other
-- half of the same change. Without both, a bazos LLM claim is stored, live and
-- auditable, and silently declined at S7 forever.
--
-- WHICH FIELDS. The entries emit eight claim types; only SIX of them are fields
-- `resolver.core.SURVIVORSHIP_FIELDS` arbitrates, and only those get rows:
--
--     obec_name  cast_obce_name  psc  street_name
--     house_number_cp  house_number_co
--
-- `landmark` and `address_line_verbatim` are deliberately absent. Both are
-- storable claim types that no field is ever won from (`is_admin_bearing` is
-- FALSE on them permanently), so a policy row for either would be dead config of
-- exactly the kind 383's inert `coordinate` / `address_point_id` rows already
-- are, and this migration does not add more of them.
--
-- RANK 350, AND WHY IT SITS WHERE IT DOES. Lower wins. The shipped ladder around
-- it, after 383/388/400 and 470:
--
--      50 operator_manual         (400)
--     100 registry_derived        (383)
--     300 portal_structured_field (383)
--     350 llm_text, portal:bazos   HERE
--     400 html_selector_parse     (383)
--     450 breadcrumb_parse        (470)
--     500 url_slug_parse          (470)
--     550 regex_text              (470)
--     600 legacy_column           (470)
--     900 llm_text, generic       (383)
--
-- 350 is between the two structured-portal rungs, and it is a statement about
-- THIS PORTAL rather than about language models. The generic ladder assumes a
-- portal's own structured fields are the better instrument; on bazos they are
-- not, and that is measured, not asserted:
--   * the town anchor's TEXT is the OKRES, not the municipality - 29,546 active
--     rows share only 90 distinct `locality` values (76 okresy + 22 Praha +
--     "Zahranici");
--   * `postal_town` (the raw_json `locality` mirror) disagrees with the
--     geo-derived obec on 57.0% of rows, and is `is_admin_bearing = FALSE`
--     permanently for that reason;
--   * the maps pin is no substitute either: the pin-derived obec is itself wrong
--     on measured rows (Zbinohy stored as Vetrny Jenikov, Moravany/Platenice as
--     Dasice);
--   * bazos `raw_json` carries NO description at all, so the street, cast obce
--     and house number an ad states in prose reach nothing except this lane.
-- The free text is the only true carrier of a street-grade address on this
-- portal, so it must outrank the okres-grade structured reads (400/500/600) that
-- would otherwise decide the field. It stays BELOW 300 because a genuinely typed
-- field in a portal's own payload - which bazos does not have for these fields -
-- would still be the stronger instrument, and the ladder should not have to be
-- re-argued if bazos ever grows one.
--
-- The rows are `source_pattern = 'portal:bazos'`, never `'llm_text'` or
-- `'portal:*'`. `survivorship._best_policy` takes the MINIMUM over every matching
-- row, so a per-portal row can only ever LOWER the effective rank - which is why
-- this is expressible at all, and equally why it cannot be undone by adding a
-- higher-numbered row later. Scoping it to bazos is what keeps the generic
-- rank-900 `('llm_text','llm_text')` row governing every other portal's future
-- LLM claims.
--
-- THE FOUR GUARD FLAGS, stated rather than defaulted:
--   * min_confidence = 'medium'. The lane maps the MODEL's own confidence onto
--     `claim_confidence` ('high' -> high, 'medium' -> medium) and refuses 'low'
--     outright, recording it as a `stated_but_ambiguous` absence. So this admits
--     everything the lane will actually write and is a rail against a future
--     relaxation of that mapping, not a filter that fires today.
--   * may_fill_null = true. The whole point: these fields are NULL on this
--     portal today.
--   * may_overwrite_non_null = FALSE. D7's graded write-back is kept: a
--     differing non-NULL incumbent becomes a `write_back_blocked_non_null`
--     contradiction for a human, never a silent replacement. A new instrument
--     with no measured error rate may fill a hole; it may not correct a record.
--   * requires_independent_agreement = FALSE. This is the deliberate one - a
--     relaxation of the generic llm_text rung's TRUE, and the argument for it is:
--
--         bazos free text is the ONLY carrier for these fields; requiring a
--         second source makes single-source claims permanently unusable.
--
--     `_independently_agreed` counts DISTINCT `claim.source` values, narrowed
--     from (source, method) precisely because `location_claim_fingerprint`
--     hashes `surface` and one portal re-mined from a second substrate would
--     otherwise corroborate ITSELF. bazos is one source reading its own page:
--     there is no second source that could ever agree, so requiring one does not
--     make these claims safer, it makes them indistinguishable from claims we
--     never wrote. What buys the corroboration back is not policy but the lane:
--     every admin-bearing value is checked against the pinned RUIAN registry
--     before it is written (an obec/cast obce must name a real admin unit, a
--     street must be a `ruian_streets.name_norm` INSIDE the candidate obec, a
--     house number must resolve to a real `ruian_address_points` row), every
--     claim carries a verbatim quote and a byte span the D7 CHECKs
--     (`loc_claim_text_evidence`, `loc_claim_evidence_payload`,
--     `loc_claim_llm_model`) refuse to store without, and `_gazetteer_validate`
--     runs again at S7 because `llm_text` is in `survivorship.TEXT_METHODS`.
--     Plus may_overwrite_non_null = false above.
--
-- DESCRIPTION vs TITLE IS NOT IN HERE, and cannot be. `location_field_policy`
-- matches on (source, extraction_method) alone - `claim.surface` is invisible to
-- `survivorship.matches` except the one `surface = 'registry'` special case - so
-- two `llm_text` claims from one portal are unrankable as data. The operator's
-- "free text beats the headline on conflict" is therefore an EXTRACTOR decision,
-- taken in `claims_llm` where it is expressible: one call asks for
-- `from_description` and `from_title` separately and the lane emits exactly ONE
-- claim per output field per listing, description-first, with the title as a
-- fallback rung. There is nothing left for policy to rank, which is the point -
-- the alternative (splitting the two blocks across two extraction_methods, or
-- stamping the title family a grade lower) would have been a rank game dressed
-- as a provenance.
--
-- POLICY VERSION. Rows go into the EXISTING 'v1', per the 388 / 400 / 470
-- precedent, with `on conflict ... do nothing` so the file is re-runnable. A NEW
-- `policy_version` is one of the five resolution-identity columns
-- (claim_set_hash, resolver_version, registry_version_id, policy_version,
-- collision_epoch_id) and would invalidate every stored resolution corpus-wide.
-- No DDL: this is a data migration that ships as a numbered file because 388,
-- 400 and 470 did.
--
-- NO RE-RESOLVE SECTION, and its absence is a decision rather than an omission.
-- 470 had to enqueue `dirty_locations` because `legacy_column` claims already
-- existed and were being skipped, so its rows changed the winner over an
-- EXISTING claim set. Here the opposite holds: ZERO `llm_text` claims exist on
-- this corpus - the lane has never written one, because it returned
-- `outcome='inert'` before opening a batch row while no contract named its
-- reader. Every claim these rows will govern is new, a new claim gives a new
-- `claim_set_hash` and therefore a new resolution, and the lane's own writes
-- enqueue their listings with reason `claim_insert`. Enqueueing anything here
-- would be dirtying the corpus for work that cannot exist yet.
--
-- NOT TOUCHED: `tests/location_data/mini_mirror.py`'s `v1_field_policy_fields()`
-- globs `38*_location_w1_*.sql` and checks FIELDS only. This migration adds no
-- new survivorship FIELD - all six already have rows - so that gate stays green
-- and its glob stays as it is.

begin;

------------------------------------------------------------------
-- The bazos llm_text rung.
------------------------------------------------------------------

insert into location_field_policy
  (policy_version, field, source_pattern, method_pattern, rank,
   min_confidence, may_fill_null, may_overwrite_non_null, requires_independent_agreement)
select 'v1', f.field, 'portal:bazos', 'llm_text', 350,
       'medium'::match_confidence, true, false, false
from unnest(array[
       'obec_name', 'cast_obce_name', 'psc', 'street_name',
       'house_number_cp', 'house_number_co'
     ]::location_claim_type[]) as f(field)
on conflict (policy_version, field, source_pattern, method_pattern) do nothing;

commit;
