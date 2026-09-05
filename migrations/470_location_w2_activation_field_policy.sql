-- 470_location_w2_activation_field_policy.sql
--
-- Location-data program, wave W2-6..W2-12: the survivorship policy rows the
-- seven-portal contract activation needs in order to be worth anything.
--
-- THE GAP THIS CLOSES. `location_field_policy` is evaluated by exactly one
-- function, `survivorship.evaluate_field`, and a claim whose (source,
-- extraction_method) matches NO row is not "unranked" - `_best_policy` returns
-- None, the claim is SKIPPED, and the field lands in
-- `resolution.survivorship_blocked` forever. The shipped v1 seed is five
-- producer rungs only: ('ruian','registry_derived',100) and
-- ('portal:*','portal_structured_field',300) and
-- ('portal:*','html_selector_parse',400) and ('llm_text','llm_text',900) from
-- 383/388, plus ('operator','operator_manual',50) from 400. That leaves SEVEN
-- of the ten `location_extraction_method` labels with no row at all.
--
-- Until now the gap was invisible because nothing emitted those methods against
-- a survivorship field. The W2-6..W2-12 activation wave (bazos@2,
-- ceskereality@5, idnes@2, maxima@2, mmreality@2, realitymix@4, remax@3) makes
-- four of them live producers, so without these rows the wave would mine seven
-- portals' archived HTML, write correct claims with correct evidence spans, and
-- have every one of them silently declined at S7. The activation and its policy
-- rows are one change.
--
-- WHICH ROWS, AND WHY ONLY THESE. Derived, not guessed: every EXECUTABLE entry
-- of the nine shipped contracts, intersected on
-- `resolver.core.SURVIVORSHIP_FIELDS` (the thirteen fields S7 arbitrates), gives
-- exactly these (method x field) pairs beyond the ones already seeded -
--
--   breadcrumb_parse  cast_obce_name, kraj_name, obec_name, okres_name
--                     (realitymix rm.det.breadcrumb_{quarter,kraj,obec,geo})
--   url_slug_parse    obec_name, psc          (bazos bzs.det.{obec_slug,psc})
--   regex_text        okres_name, street_name (ceskereality cr.det.title_okres,
--                     cr.det.title_line; mmreality mm.det.original_title_street)
--   legacy_column     postal_town, psc, street_name (bazos bzs.det.legacy_psc,
--                     bzs.det.locality_text; ceskereality cr.det.legacy_street;
--                     realitymix rm.det.legacy_street)
--
-- Three methods deliberately get NO rows, and their absence is a decision:
--   * `jsonld_parse` and `map_widget_parse` are live producers after the wave,
--     but every field they emit is a coordinate / uncertainty_geometry /
--     map_zoom / precision_declaration - none is in SURVIVORSHIP_FIELDS. The pin
--     is arbitrated by S4/S6 (position.py, precision.py), where policy rows are
--     inert; 383's own `coordinate` and `address_point_id` rows are already dead
--     config and this migration does not add more of them.
--   * `portal_declared_quality` emits `blur_hint` only, which S6 reads. Same
--     reason.
-- Ranks are reserved for them anyway (jsonld_parse 440, map_widget_parse 460)
-- so the ladder below stays readable if a later contract does emit a
-- survivorship field through one.
--
-- THE RANKS (lower wins). They slot below the two structured-portal rungs and
-- above the llm_text rung, in decreasing order of how much of the value the
-- PORTAL stated versus how much we inferred:
--
--     50 operator_manual        (400)
--    100 registry_derived       (383)
--    300 portal_structured_field(383)  a typed field in the portal's own payload
--    400 html_selector_parse    (383)  a named element, read whole
--    450 breadcrumb_parse       HERE   a named element in an ANCHORED chain
--    500 url_slug_parse         HERE   the portal's own routing key
--    550 regex_text             HERE   a pattern over the portal's own markup
--    600 legacy_column          HERE   our scraper's column, write path unproven
--    900 llm_text               (383)
--
-- `legacy_column` last is the load-bearing one: a legacy column is the only rung
-- where the value did not come from a re-readable document at all - 06 section
-- 6.1.1 caps it at `claim_confidence = 'medium'` and several of these entries
-- declare `legacy_write_path_unknown`. It must lose to anything mined from the
-- page it supposedly came from, and after this migration it does.
--
-- `regex_text` at 550 rather than the 950 the repo prototyped
-- (tests/location_data/test_resolver_survivorship.py's
-- `_policy_with_a_text_mined_rung`, whose docstring says "this is the row W2
-- adds"): that prototype assumed a regex over PROSE and ranked it below the LLM
-- accordingly. What W2 actually ships is different - an anchored pattern over
-- the portal's own structured markup, carrying a quote and a byte span into a
-- content-addressed archived body that the D7 CHECKs (`loc_claim_text_evidence`,
-- `loc_claim_evidence_payload`) refuse to store without. That is a stronger
-- instrument than a model reading free text, so it ranks above it. The prototype
-- row stays a test fixture and is not shipped.
--
-- THE THREE GUARD FLAGS, stated rather than defaulted:
--   * may_fill_null = true. The whole point: these fields are NULL today.
--   * may_overwrite_non_null = FALSE, unlike the 300/400 rungs which may
--     overwrite. D7's graded write-back: a differing non-NULL incumbent becomes
--     a `write_back_blocked_non_null` contradiction for a human, not a silent
--     replacement. These are new instruments with no measured error rate; the
--     first thing they may do is fill a hole, not correct a record.
--   * requires_independent_agreement = FALSE. This is the deliberate one, and it
--     is the C7 "one portal, one voice" rule that forces it. `_independently_
--     agreed` counts DISTINCT `claim.source` values, narrowed from (source,
--     method) precisely because `location_claim_fingerprint` hashes `surface`:
--     one portal re-mined from a second substrate would otherwise corroborate
--     ITSELF. Every claim these four rows govern comes from ONE portal reading
--     its OWN page - a bazos slug, a ceskereality title, a realitymix
--     breadcrumb. There is no second source that could ever agree, so
--     requiring one does not make these claims safer, it makes them permanently
--     unusable, which is indistinguishable from not having activated the
--     contracts at all. The corroboration these rows do NOT get is bought back
--     by may_overwrite_non_null = false above, and by `_gazetteer_validate`,
--     which runs on `regex_text` by construction (survivorship.TEXT_METHODS).
--
-- POLICY VERSION. Rows go into the EXISTING 'v1', per the 388 and 400
-- precedent, with `on conflict ... do nothing` so the file is re-runnable. A NEW
-- `policy_version` is one of the five resolution-identity columns
-- (claim_set_hash, resolver_version, registry_version_id, policy_version,
-- collision_epoch_id) and would invalidate every stored resolution corpus-wide.
-- No schema changes here at all: this is a data migration that ships as a
-- numbered file because 388 and 400 did.
--
-- RE-RESOLVE. Adding a v1 row changes behaviour WITHOUT changing resolution
-- identity, and nothing re-resolves on its own - the drain works off
-- `dirty_locations`. For three of the four methods that is harmless: they had
-- no reader before this wave, so no claim of theirs exists yet and every claim
-- they write will be new (new claim_set_hash => new resolution). `legacy_column`
-- is the exception - those four entries have been running on the W1 lane since
-- W1, their claims are in the table today, and until now every one of them was
-- skipped. Giving them a rung changes the winner over an EXISTING claim set, so
-- this migration enqueues the affected listings itself (the
-- `location_data/contracts.py::_SHADOW_ENQUEUE_SQL` precedent), scoped to the
-- exact (method, field) pairs the rows above govern. `'policy_version'` has been
-- in the `dirty_locations.reason` CHECK since 384.
--
-- NOT TOUCHED: `tests/location_data/mini_mirror.py`'s `v1_field_policy_fields()`
-- globs `38*_location_w1_*.sql` and checks FIELDS only. This migration adds no
-- new survivorship FIELD - all thirteen already have rows - so that gate stays
-- green and its glob stays as it is.

begin;

------------------------------------------------------------------
-- 1. The four new producer rungs.
------------------------------------------------------------------

-- breadcrumb_parse (rank 450) - realitymix's JSON-LD chain, anchored on the
-- kraj slug so it fails closed rather than mis-reading an unanchored list.
insert into location_field_policy
  (policy_version, field, source_pattern, method_pattern, rank,
   min_confidence, may_fill_null, may_overwrite_non_null, requires_independent_agreement)
select 'v1', f.field, 'portal:*', 'breadcrumb_parse', 450,
       null::match_confidence, true, false, false
from unnest(array[
       'obec_name', 'cast_obce_name', 'okres_name', 'kraj_name'
     ]::location_claim_type[]) as f(field)
on conflict (policy_version, field, source_pattern, method_pattern) do nothing;

-- url_slug_parse (rank 500) - bazos' own routing key. The portal built the URL,
-- so the obec and the PSC in it are its statement, not our inference.
insert into location_field_policy
  (policy_version, field, source_pattern, method_pattern, rank,
   min_confidence, may_fill_null, may_overwrite_non_null, requires_independent_agreement)
select 'v1', f.field, 'portal:*', 'url_slug_parse', 500,
       null::match_confidence, true, false, false
from unnest(array[
       'obec_name', 'psc'
     ]::location_claim_type[]) as f(field)
on conflict (policy_version, field, source_pattern, method_pattern) do nothing;

-- regex_text (rank 550) - an anchored pattern over the portal's own markup,
-- carrying the D7 evidence set. `_gazetteer_validate` also runs on this method
-- (survivorship.TEXT_METHODS), so a street that no RUIAN street in the resolved
-- obec matches is blocked with a `text_claim_failed_gazetteer` signal.
insert into location_field_policy
  (policy_version, field, source_pattern, method_pattern, rank,
   min_confidence, may_fill_null, may_overwrite_non_null, requires_independent_agreement)
select 'v1', f.field, 'portal:*', 'regex_text', 550,
       null::match_confidence, true, false, false
from unnest(array[
       'street_name', 'okres_name'
     ]::location_claim_type[]) as f(field)
on conflict (policy_version, field, source_pattern, method_pattern) do nothing;

-- legacy_column (rank 600) - our scraper's own column. Last among the portal
-- rungs on purpose (see the header), and the only one of the four whose claims
-- already exist, which is why section 2 below runs at all.
insert into location_field_policy
  (policy_version, field, source_pattern, method_pattern, rank,
   min_confidence, may_fill_null, may_overwrite_non_null, requires_independent_agreement)
select 'v1', f.field, 'portal:*', 'legacy_column', 600,
       null::match_confidence, true, false, false
from unnest(array[
       'street_name', 'psc', 'postal_town'
     ]::location_claim_type[]) as f(field)
on conflict (policy_version, field, source_pattern, method_pattern) do nothing;

------------------------------------------------------------------
-- 2. Re-resolve what the new rows can now decide differently.
--
-- Scoped to the (extraction_method, claim_type) pairs section 1 governs, so it
-- enqueues the listings whose stored claims were being SKIPPED and are now
-- ranked - not the corpus. Three of the four methods match zero rows today by
-- construction (no reader before this wave); they are named anyway so the file
-- stays correct if it is applied after the archived lane has run once.
--
-- Bounded: "the claims of four contract entries" is still every listing those
-- portals have ever had, and a migration that hangs a pooler backend is worse
-- than one that aborts and is re-run. `on conflict do nothing` keeps the retry
-- free, and a listing already queued for another reason keeps that reason - the
-- drain rebuilds the whole projection row either way, so the reason is a
-- diagnostic label, not a work item.
------------------------------------------------------------------

set local statement_timeout = '300s';

insert into dirty_locations (listing_id, reason)
select distinct c.listing_id, 'policy_version'
  from location_claims c
 where c.extraction_method in
         ('legacy_column', 'url_slug_parse', 'regex_text', 'breadcrumb_parse')
   and c.claim_type in
         ('street_name', 'psc', 'postal_town', 'obec_name', 'cast_obce_name',
          'okres_name', 'kraj_name')
on conflict (listing_id) do nothing;

commit;
