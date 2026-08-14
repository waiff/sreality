-- 408_location_w2a_contract_sha256_governs_extraction.sql
--
-- Location-data program, Wave W2a-3e (review follow-up). `contract_sha256` stops
-- covering the two blocks of a portal contract that are NOT extraction, and the
-- volatile profiles stop borrowing `contract_version` as their identity.
--
-- Adds no relation, column, index or grant. It restates nine existing hashes and
-- corrects three column comments migration 407 wrote in the same PR, before the
-- review that changed the label. Both files land in one merge.
--
-- WHY THE HASH MOVED. A hash covers what it governs. `contract_sha256` is the
-- immutability gate on portal_contract_entries: a mismatch means "bump
-- contract_version", and contract_version is what location_claims.extractor_version
-- (`contract:<portal>@N`) and contract_entry_id name. Both feed
-- location_claim_fingerprint (mig 386), whose UNIQUE index (mig 382) is what stops an
-- incremental re-walk from re-inserting a claim it already has. So while the hash was
-- taken over the WHOLE file, editing archive configuration -- `persistence:`, i.e. the
-- volatile paths the payload archive normalises with and the retention cap -- forced a
-- new contract_version, and a new contract_version re-inserts the CLAIMS corpus. On
-- 2026-08-14 that corpus was 5,135,469 rows / 2,625 MB, append-only and never pruned,
-- in a subsystem with roughly 4 GB of allowance left. A selector edit could spend most
-- of it, and every future edit another copy.
--
-- `location_data.contracts.contract_body_hash` therefore now hashes the file minus
-- `shadow:` (already excluded, mig 404: operational state) and minus the whole
-- `persistence:` block (archive configuration). The nine contracts keep the
-- contract_version they had; the profiles they now declare are identified by their own
-- digest instead (below).
--
-- WHY THESE NINE ROWS MUST BE RESTATED. The rows below were projected with a hash over
-- the whole file. The deploy that carries this migration computes the governed hash, so
-- `project()` would read the difference as "the bytes changed under a loaded version"
-- and refuse the contract -- correctly, since it cannot tell a redefinition from a real
-- edit. This is that one-time restatement, and it is conditional on the OLD value: a row
-- holding anything else is reported and left alone rather than overwritten, because an
-- unexplained hash is exactly what the gate exists to surface.
--
-- Both hashes are reproducible from git:
--   old = sha256(file bytes, minus any `shadow:` line)          -- the pre-408 rule
--   new = python -m location_data.contracts --check             -- prints `sha256` per portal
--
-- ORDERING. Apply this migration in the same window as the merge. Between the two, the
-- contract-load step of location_claims_intake.yml fails loudly (`… is already loaded
-- with a different sha256 …`, which now names this migration) and that hour's intake is
-- skipped; the batch watermark is a keyset cursor, so the next run resumes where it
-- stopped. Nothing is written, lost or half-written either way.
--
-- SUPERSEDED VERSIONS ARE LEFT ALONE. portal_contracts keeps every version ever
-- projected (remax@1, ceskereality@1/@2, …). Those rows hold a whole-file hash of the
-- bytes they were projected from, which is the honest record of that artefact, and
-- nothing ever compares them again -- only the version on disk is ever re-projected.
--
-- WHAT REPLACES contract_version AS THE PROFILE'S IDENTITY. The cohort label
-- `normalizer_version` now reads `payload_norm@3+profile@<8 hex>`, where the digest is
-- taken over the RESOLVED volatile profile (the declared rules on their base) by
-- location_data.payload_norm.profile_digest. It moves iff the projection moves: a
-- locator fix that bumps contract_version leaves the churn cohort intact, and an edit to
-- volatile_paths opens a clean one even though no version moved. Under the old form
-- (`+contract@N`) every extraction-only bump would have orphaned that surface's counters
-- and restarted the readout at fetches=1 -- the same waste payload_norm refuses on the
-- engine axis by not bumping NORMALIZER_VERSION for an output that did not move.
--
-- Backend/service-role only, unchanged: RLS is on and the anon/authenticated ACL is
-- revoked on every relation named here (382, 402, 403); re-asserted at the foot.

begin;

------------------------------------------------------------------
-- 1. Restate the nine live contract hashes into the governed dialect.
------------------------------------------------------------------

do $$
declare
  target record;
  restated int := 0;
  already int := 0;
  never_projected int := 0;
  unmatched text[] := '{}';
begin
  for target in
    select * from (values
      ('bazos',        1, '9dd1a17670639c5045dd666b29b6e24f337ae6f854f5269376419691e8bd4770',
                          '4cd371edb293caf925621e94ca029b7b48ead69883f7ae469376cbbb44c885b4'),
      ('bezrealitky',  1, '542082966c334e97c5e722a7ce4845a21dcab04abcdb03f5d98e2ecacaad34bf',
                          '06dea09a3e8db1e77661a5fe0c29e30cc6ebd00404f634d96d97e2c393bcb1c6'),
      ('ceskereality', 3, '26a722159d7e76608adc676e2ad135698a6ceadbe7b6f4930e0f064435e25219',
                          '45db104c2384922ae4d3ce92acd313fbe9e4c63ca94ebdb4d54ce09b5bd5d181'),
      ('idnes',        1, '7a2ed37f7dfad21091ba87fcc1acd51b812956c1e4a34e8bb22d5be37bcdb048',
                          '85f9f201cdbe251726834973dc6a17c7a137eca5e1eeb6fdd1888dc3443acfc6'),
      ('maxima',       1, 'd733530a5a321d36ba15d58fd887039672257ae0d5ddb503364ab21a170f6c96',
                          '6e31edcb67c4ffc03c8b819b7d278a6eefeab0ad13930d40e6080b84cc72cae0'),
      ('mmreality',    1, '2f15c8589b59a2026cbf5f336e13ed3ad0f3b1a0bdfcb4b2ebebed0b70835a48',
                          '4901c13f6153d8e7a24b1bcb9dd421377b8efb9bdb13f6efe331cb46ca23a6b8'),
      ('realitymix',   3, '7bcac827c953762d4ad8d745e489fab41ab82d447316102a473f3c897e5b2ca6',
                          '4cdb35f38b53256b18187a316c11ca4c7e9131cd7567e2002ec46a6171bc8185'),
      ('remax',        2, 'af7d90d5768bee8cf695a1e37bc9892f49296967ce1c0d55b4337f19e7f505f6',
                          '3cbde78ca3c5c308a3775d1157699fe3a3d97b9016e51e117f76ee5fc454ce5f'),
      ('sreality',     1, '75253e3a52858b82366f57cc610a05ad2a5b1ce39a2c76bc823c6dcca581e526',
                          'eb465adf36e21cc49e1edd4b12a5c9aa72d942a4ae13b77347a3679a1dde0a29')
    ) as t(source, version, whole_file_sha, governed_sha)
  loop
    update portal_contracts
       set contract_sha256 = decode(target.governed_sha, 'hex')
     where source = target.source
       and version = target.version
       and contract_sha256 = decode(target.whole_file_sha, 'hex');

    if found then
      restated := restated + 1;
    elsif exists (
      select 1 from portal_contracts
       where source = target.source
         and version = target.version
         and contract_sha256 = decode(target.governed_sha, 'hex')
    ) then
      already := already + 1;   -- re-run of this migration; nothing to do.
    elsif not exists (
      select 1 from portal_contracts
       where source = target.source and version = target.version
    ) then
      -- The normal state on a from-zero schema replay (CI) and on any environment
      -- where the contracts have never been loaded: there is nothing to restate.
      never_projected := never_projected + 1;
    else
      unmatched := unmatched || format('%s@%s', target.source, target.version);
    end if;
  end loop;

  raise notice '408: contract_sha256 restated=% already_governed=% never_projected=% '
    'unmatched=%', restated, already, never_projected,
    coalesce(array_length(unmatched, 1), 0);

  if array_length(unmatched, 1) is not null then
    -- Not an exception: the other rows are correct and worth keeping. A warning plus a
    -- refusing `--load` is a better place to reconcile from than a rolled-back migration.
    raise warning '408: % projected row(s) hold neither the pre-408 nor the governed '
      'hash: %. Compare them against `python -m location_data.contracts --check`, which '
      'prints each portal''s governed sha256, before the next contract load.',
      array_length(unmatched, 1), array_to_string(unmatched, ', ');
  end if;
end $$;

comment on column portal_contracts.contract_sha256 is
  'sha256 of the contract file''s GOVERNED bytes: the YAML on disk minus its `shadow:` '
  'line (operational state, mig 404) and minus its whole `persistence:` block (archive '
  'configuration, mig 408) - location_data.contracts.contract_body_hash, printed per '
  'portal by `python -m location_data.contracts --check`. NOT `sha256sum <file>`. It is '
  'the immutability gate on portal_contract_entries: projecting different governed bytes '
  'under a loaded version is refused, because a change to what a contract EXTRACTS is a '
  'new contract_version (02 section 2.1.8) - and a new contract_version re-stamps '
  'extractor_version and contract_entry_id, i.e. re-inserts every claim the next '
  'incremental scan re-walks (location_claim_fingerprint, mig 386). Excluding the two '
  'non-extraction blocks is what stops archive configuration from spending that. Rows '
  'projected before 408 that are still on disk were restated above; superseded versions '
  'keep the whole-file hash of the bytes they were projected from and are never '
  're-compared.';

------------------------------------------------------------------
-- 2. The cohort label: `+profile@<digest>`, not `+contract@<version>`.
--    Restates the three comments 407 wrote for the earlier form.
------------------------------------------------------------------

comment on column portal_payload_churn.normalizer_version is
  'The normaliser cohort this counter row belongs to: '
  'location_data.payload_norm.resolve_normalisation(source, page_kind) at write time. '
  'Part of the PK, never an in-place stamp - a profile change starts a NEW row rather '
  'than relabelling accumulated counters. It names both axes that can move a '
  'normalised byte: the ENGINE (''payload_norm@N'', that module''s algorithm) and the '
  'PROFILE. Three suffixes are defined and each means "measured by a different '
  'instrument, do not average with the others": ''+profile@<8 hex>'' - the first 8 hex of '
  'payload_norm.profile_digest over the resolved volatile profile the contract declares '
  'for this (source, page_kind), so the cohort breaks IFF the projection moves and an '
  'extraction-only contract_version bump does NOT orphan these counters; the portal is '
  'not repeated because `source` is already in this PK, and two portals declaring the '
  'same rules honestly share a digest. ''+base'' - no contract declares that surface, so '
  'only the generic portal-agnostic base was stripped; the rate is an upper bound on '
  'that surface, not a verdict on a profile. ''+probe'' - written by '
  'scripts/location_payload_refetch_probe.py, whose minutes-apart cadence would '
  'otherwise drag the passive readout''s change rate and refetch interval down; it '
  'COMPOSES onto whichever of the first two applies.';

comment on column portal_raw_payloads.normalizer_version is
  'The normaliser that ACTUALLY produced this row''s payload_sha256 - by construction, '
  'not by convention: location_data.payloads.append_payload takes the profile and this '
  'label as one value (payload_norm.resolve_normalisation(source, page_kind)), and a '
  'caller supplying its own volatile profile MUST supply the label naming it or the '
  'append is refused. So this column can never describe an instrument other than the '
  'one applied. payload_sha256 is the content ADDRESS, so a profile change moves it for '
  'unchanged content and appends one row per artefact - this column is what makes that '
  'cohort identifiable afterwards rather than indistinguishable from real churn. '
  '''+profile@<8 hex>'' digests the volatile profile that was applied (declared in '
  'contracts/portals/<source>.yaml under persistence.volatile_paths, which git is the '
  'store of record for - 02 section 2.1.8; it is read from the deployed artefact, never '
  'from the projection in portal_contracts, so a permanent content address can never '
  'depend on whether the contract-load job had run). To trace a digest back to the '
  'artefact that declares it today, run `python -m location_data.contracts --check`. '
  '''+base'' means no contract declares this row''s SURFACE and only the generic base was '
  'stripped, which is the honest state for every page_kind except detail.';

comment on column portal_contracts.fetch_config is
  'The contract''s non-extraction blocks, projected verbatim from the YAML: fetch, '
  'persistence, precision_caps, regressions, extractor_runtime. Verbatim on purpose - a '
  'normalised-on-the-way-in copy would be a second dialect of one fact. '
  '`persistence.volatile_paths` is a mapping of page_kind -> {base, json_pointers, '
  'css_selectors, strip_attributes} and is the ONLY part of a contract read at scrape '
  'time (location_data.payload_norm, from the deployed FILE - this column is the copy an '
  'operator reads in psql): it decides the projection payload_sha256 addresses. A '
  'page_kind absent from it is not declared and is normalised with the base profile '
  'alone. `persistence` is OUTSIDE contract_sha256 (mig 408), so changing what a portal '
  'strips is a reviewed diff and a clean churn cohort but NOT a contract_version bump - '
  'it governs the payload archive, not the claims - and this column is refreshed in '
  'place by the next contract load rather than pinned to the version that first carried '
  'it. Everything else here is hashed, so it can only change with a version bump.';

------------------------------------------------------------------
-- No relation, column or grant changed above; re-asserting the posture these
-- tables already carry keeps it readable from this file alone.
------------------------------------------------------------------

revoke all on portal_payload_churn from anon, authenticated;
revoke all on portal_raw_payloads from anon, authenticated;
revoke all on portal_contracts from anon, authenticated;
revoke all on portal_contract_entries from anon, authenticated;

commit;
