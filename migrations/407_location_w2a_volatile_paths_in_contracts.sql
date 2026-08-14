-- 407_location_w2a_volatile_paths_in_contracts.sql
--
-- Location-data program, Wave W2a: the volatile profiles move OUT of Python and
-- into the portal contracts, and the cohort label stops naming a table that no
-- longer exists.
--
-- Design: design/final/02-portal-contracts.md section 2.3.2 P1 ("payload_sha256 is
-- computed over a normalised projection of the body, never over the bytes as
-- fetched") and 06 W2a-3b. Migration 405's own comment anticipated this exactly:
-- "from W2a-3b on, the contract's own persistence.volatile_paths rather than this
-- module's table". This is that step.
--
-- This migration adds NO relation, column, index or grant. It is the SQL-side half
-- of a code change, for the same reason 405 was: the VALUE SPACE of two text columns
-- moved, and those columns are read straight out of psql by whoever signs the storage
-- projection. A value an operator meets in a readout and cannot explain from the
-- schema is the confusion this wave keeps paying for.
--
-- WHAT CHANGED IN THE CODE.
--
-- 1. `location_data.payload_norm.MEASURED_VOLATILE_PROFILES` is RETIRED. Each
--    portal's rules now live in contracts/portals/<portal>.yaml under
--    `persistence.volatile_paths`, keyed BY page_kind:
--
--      persistence:
--        volatile_paths:
--          detail:
--            base: html            -- or `none`; the portal-agnostic floor
--            css_selectors: [...]  -- what a DETAIL diff actually saw move
--            json_pointers: [...]  -- RFC 6901, '-' = every array element
--            strip_attributes: [...]
--
--    Keyed by page_kind and not flat, because a detail page is one property and an
--    index page is a LIST of properties: a flat list is the collapse migration 405
--    fixed in Python, re-introduced one layer up. A page_kind ABSENT from the
--    mapping is not declared and is normalised with the base profile alone.
--    `base:` is stated per surface with no default, so no body acquires a floor by
--    accident. The projection of that YAML into portal_contracts.fetch_config is
--    verbatim, and the runtime reads the same key out of the same files.
--
-- 2. `normalizer_version` gains a third documented form. `payload_norm@N` on its own
--    named a profile TABLE inside that module; there is no such table now, so a row
--    stamped with the bare version would name an instrument that does not exist.
--
-- WHY THE LABEL NAMES BOTH AXES. Two independent things can move a normalised byte:
-- the ENGINE (payload_norm's algorithm, versioned `payload_norm@N`) and the PROFILE
-- (a portal's declaration, versioned by `contract_version`). Naming only one would
-- let the other move a permanent content address with no cohort break to show for
-- it, which is precisely the failure migration 402's PK exists to prevent. The
-- portal itself is NOT repeated into the label: `source` is already a column of both
-- tables below (and part of portal_payload_churn's PK), and a second copy of a key
-- is a thing that can disagree with the first.
--
-- WHY THE DETAIL COHORTS RESET AND THE INDEX COHORTS DO NOT. The projection is
-- byte-identical across this move - proved fixture by fixture against digests
-- computed under the retired code (26 committed bodies, 8 portals, 0 differing
-- bytes), which is also why NORMALIZER_VERSION stays at payload_norm@3. But the
-- instrument's IDENTITY did change: the same bytes are now produced by a reviewed,
-- retractable contract version rather than by a Python constant, and 402's discipline
-- is that a cohort is never relabelled in place. So every portal's detail surface
-- opens one clean cohort (`payload_norm@3` -> `payload_norm@3+contract@N`) and keeps
-- its old rows readable beside it. The index surfaces do NOT move: they were and
-- remain `payload_norm@3+base`, because the base profile belongs to the normaliser
-- and is identical under every contract version.
--
-- Backend/service-role only, unchanged: every relation touched below already has RLS
-- on and its anon/authenticated ACL revoked (382, 402, 403). The revokes are
-- re-asserted at the foot of the file, the same cheap insurance 403 and 405 carry.

begin;

------------------------------------------------------------------
-- portal_payload_churn - the live instrument's cohort key.
------------------------------------------------------------------

comment on column portal_payload_churn.normalizer_version is
  'The normaliser cohort this counter row belongs to: '
  'location_data.payload_norm.resolve_normalisation(source, page_kind) at write time. '
  'Part of the PK, never an in-place stamp - a profile change starts a NEW row rather '
  'than relabelling accumulated counters. It names both axes that can move a '
  'normalised byte: the ENGINE (''payload_norm@N'', this module''s algorithm) and the '
  'PROFILE. Three suffixes are defined and each means "measured by a different '
  'instrument, do not average with the others": ''+contract@N'' - the portal contract '
  'at contract_version N declared persistence.volatile_paths for this (source, '
  'page_kind), and that declaration is what was stripped; the portal is not repeated '
  'because `source` is already in this PK. ''+base'' - no contract declares that '
  'surface, so only the generic portal-agnostic base was stripped; the rate is an '
  'upper bound on that surface, not a verdict on a profile. ''+probe'' - written by '
  'scripts/location_payload_refetch_probe.py, whose minutes-apart cadence would '
  'otherwise drag the passive readout''s change rate and refetch interval down; it '
  'COMPOSES onto whichever of the first two applies.';

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
  'unchanged content and appends one row per artefact - this column is what makes that '
  'cohort identifiable afterwards rather than indistinguishable from real churn. '
  '''+contract@N'' names the portal contract version whose persistence.volatile_paths '
  'was applied (contracts/portals/<source>.yaml in git is the store of record, 02 '
  'section 2.1.8; it is read from the deployed artefact, never from the projection '
  'below, so a permanent content address can never depend on whether the contract-load '
  'job had run). ''+base'' means no contract declares this row''s SURFACE and only the '
  'generic base was stripped, which is the honest state for every page_kind except '
  'detail.';

------------------------------------------------------------------
-- portal_contracts.fetch_config - where the declaration is reviewable in psql.
------------------------------------------------------------------

comment on column portal_contracts.fetch_config is
  'The contract''s non-extraction blocks, projected verbatim from the YAML: fetch, '
  'persistence, precision_caps, regressions, extractor_runtime. Verbatim on purpose - '
  'contract_sha256 is taken over those same bytes, so a normalised-on-the-way-in copy '
  'would be a second dialect of one fact. `persistence.volatile_paths` is a mapping of '
  'page_kind -> {base, json_pointers, css_selectors, strip_attributes} and is the ONLY '
  'part of a contract read at scrape time (location_data.payload_norm): it decides the '
  'projection payload_sha256 addresses. A page_kind absent from it is not declared and '
  'is normalised with the base profile alone. Entries are immutable per contract_version '
  '(02 section 2.1.8), so changing what a portal strips is a version bump, a reviewed '
  'diff and a clean churn cohort - never an edit.';

------------------------------------------------------------------
-- No relation, column or grant changed above; re-asserting the posture these
-- tables already carry keeps it readable from this file alone.
------------------------------------------------------------------

revoke all on portal_payload_churn from anon, authenticated;
revoke all on portal_raw_payloads from anon, authenticated;
revoke all on portal_contracts from anon, authenticated;
revoke all on portal_contract_entries from anon, authenticated;

commit;
