-- 405_location_w2a_normalizer_cohort_by_surface.sql
--
-- Location-data program, Wave W2a: `normalizer_version` gains a documented
-- suffix, because a volatile profile belongs to a (source, page_kind) and not
-- to a portal.
--
-- Design: design/final/02-portal-contracts.md section 2.3.2 P1 ("payload_sha256
-- is computed over a normalised projection of the body, never over the bytes as
-- fetched" - and the gate: "Measure churn before P2 is enabled"). This migration
-- adds NO relation, column, index or grant. It is the SQL-side half of a code
-- change, and it exists because that code change widened the VALUE SPACE of two
-- text columns that are read directly in psql by whoever signs off the storage
-- projection. A value an operator meets in a readout and cannot explain from the
-- schema is the confusion this wave keeps paying for.
--
-- WHAT CHANGED IN THE CODE. Every profile in location_data.payload_norm was
-- derived by diffing DETAIL pages (W2a-3b, W2a-3c). They were selected by SOURCE
-- alone, so they were also applied to INDEX bodies - a page that is a LIST of
-- properties rather than one property. Profiles are now keyed by
-- (source, page_kind); a surface nobody has diffed falls back to a generic base
-- profile carrying only portal-agnostic, content-free rules (third-party
-- analytics loaders matched by src, page chrome, CSRF material).
--
-- WHY A SUFFIX AND NOT A VERSION BUMP. `normalizer_version` is in
-- portal_payload_churn's PRIMARY KEY (migration 402) precisely so a profile change
-- opens a clean cohort, and 402's own header states the failure a bump avoids:
-- relabelling in place "would blend @1-era counters into the @2 readout and
-- register one phantom change per key on its first @2 fetch". A GLOBAL bump would
-- have avoided that on the index surfaces at the cost of discarding the DETAIL
-- evidence accumulating under payload_norm@3 across all nine portals - and the
-- detail normalisation is byte-for-byte unchanged by this work, so there is
-- nothing about it to re-measure. The suffix is the per-surface answer: the
-- surfaces whose bytes moved get a clean cohort, the surface whose bytes did not
-- keeps its history.
--
-- It also maintains itself. The suffix is derived from "does a measured profile
-- exist for this (source, page_kind)", not stamped by hand - so the day an index
-- profile is measured and added, that surface leaves the '+base' cohort and opens
-- its own clean one with nobody remembering to bump anything.
--
-- Backend/service-role only, unchanged: both relations already have RLS on and
-- their anon/authenticated ACL revoked (402, 382/403). The revokes are re-asserted
-- at the foot of the file, the same cheap insurance 403 carries, so the grant
-- posture stays readable from this file alone.

begin;

------------------------------------------------------------------
-- portal_payload_churn - the live instrument's cohort key.
------------------------------------------------------------------

comment on column portal_payload_churn.normalizer_version is
  'The normaliser cohort this counter row belongs to: '
  'location_data.payload_norm.normalizer_version_for(source, page_kind) at write '
  'time. Part of the PK, never an in-place stamp - a profile change starts a NEW '
  'row rather than relabelling accumulated counters. Two suffixes are defined and '
  'both mean "measured by a different instrument, do not average with the bare '
  'version": ''+base'' - no volatile paths have been measured for this '
  '(source, page_kind), so only the generic portal-agnostic base was stripped; the '
  'rate is an upper bound on that surface, not a verdict on a profile. ''+probe'' - '
  'written by scripts/location_payload_refetch_probe.py, whose minutes-apart cadence '
  'would otherwise drag the passive readout''s change rate and refetch interval down.';

------------------------------------------------------------------
-- portal_raw_payloads - the same label, on the store whose IDENTITY it explains.
------------------------------------------------------------------

comment on column portal_raw_payloads.normalizer_version is
  'The normaliser that ACTUALLY produced this row''s payload_sha256 - by construction, '
  'not by convention: location_data.payloads.append_payload takes the profile and this '
  'label as one value (payload_norm.resolve_normalisation(source, page_kind)), and a '
  'caller supplying its own volatile profile MUST supply the label naming it or the '
  'append is refused. So this column can never describe an instrument other than the '
  'one applied. payload_sha256 is the content ADDRESS, so a profile change moves it for '
  'unchanged content and appends one row per artefact - this column is what makes '
  'that cohort identifiable afterwards rather than indistinguishable from real '
  'churn. A ''+base'' suffix means no profile has been measured for this row''s '
  'SURFACE and only the generic base was stripped, which is the honest state for '
  'every page_kind except detail (see 405''s header). A value matching no '
  'payload_norm@N form at all is a caller-declared projection - from W2a-3b on, the '
  'contract''s own persistence.volatile_paths rather than this module''s table.';

------------------------------------------------------------------
-- No relation, column or grant changed above; re-asserting the posture both
-- tables already carry keeps it readable from this file alone.
------------------------------------------------------------------

revoke all on portal_payload_churn from anon, authenticated;
revoke all on portal_raw_payloads from anon, authenticated;
revoke all on sequence portal_raw_payloads_id_seq from anon, authenticated;

commit;
