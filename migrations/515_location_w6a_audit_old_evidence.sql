-- 515_location_w6a_audit_old_evidence.sql
--
-- W6-a of the location simplification sprint. `location_claims` rows written
-- under a RETIRED contract version (`portal_contracts.is_active = false`) are
-- DELETED from the store -- ~9.5 M of ~13.2 M rows, the large majority of a
-- 5.8 GB relation. The resolver has never read them: `resolve_db._CLAIMS_SELECT`
-- admits a claim only when its contract entry belongs to an ACTIVE contract, or
-- when it is an operator claim (no entry at all). So no verdict moves, nothing
-- re-resolves, and NOTHING IS ENQUEUED INTO `dirty_locations` -- which is exactly
-- what separates this from `contracts.py --retract`, the mechanism that withdraws
-- a contract version's evidence BECAUSE it was wrong and must re-resolve every
-- listing it touched.
--
-- The delete itself is not here. It is 9.5 M rows across seven indexes, which is
-- a job, not a migration: `.github/workflows/location_claims_retire.yml` dumps
-- the doomed rows to a gzipped CSV artifact (rule 1's backup) and then runs
-- `scripts/location_claims_retire.py` in bounded id-keyset batches.
--
-- THIS FILE IS THE ONE READER THAT LOSES SOMETHING. `location_pin_audit_mv`
-- (migration 514 § 4) carries `old_evidence` -- 'legacy' / 'archived' / 'none',
-- a three-way summary of the SUPERSEDED claims a listing still holds, so that
-- "no live evidence" was never read as "nothing was ever there". Once the
-- superseded rows are gone the column is the constant 'none' on every row, and a
-- column that can only say "zadna" is worse than no column: the operator reads it
-- as a finding. It goes, and its evidence lateral goes with it.
--
-- WHAT REPLACES THE LATERAL. `claims_now` (evidence under an ACTIVE contract)
-- stays -- it is the column that tells the operator whether the lane has anything
-- left to consume -- but it no longer needs a three-aggregate lateral over every
-- claim of the listing. It becomes one EXISTS, which stops at the first admissible
-- claim instead of counting them all. Measured on production with EXPLAIN
-- (estimates on the same cohort, both forms planned side by side 2026-09-13; the
-- matview is not rebuilt to measure it): total cost **6,623,913 -> 1,665,599, a
-- 75 % cut** on the hourly refresh -- and that is BEFORE the delete shrinks the
-- relation the probe walks. The saving is structural: counting needs every claim
-- of the listing and its contract header (~22 rows a listing, aggregated),
-- EXISTS needs one.
--
-- A MATVIEW CANNOT DROP A COLUMN IN PLACE, so this is a DROP + CREATE of the
-- relation, its unique index (REFRESH CONCURRENTLY needs it), its six filter
-- indexes and its grants -- 514 § 4 verbatim minus the column. The two functions
-- are untouched: `location_pin_audit_summary()` never named `old_evidence`, and
-- `refresh_location_pin_audit_mv()` is late-bound plpgsql. Neither is dropped by
-- the CASCADE (a string-bodied SQL function records no dependency on the relations
-- it reads), so the cron job keeps pointing at a live function throughout.
--
-- THE CRON IS LEFT ARMED, deliberately, unlike 513's retirement of v1. The window
-- between DROP and CREATE is one populate of a ~44 k-row relation; a :25 tick that
-- lands inside it fails once in the pg_cron log and the next hour is clean. The
-- alternative -- unschedule first, reschedule last -- leaves the hourly refresh
-- silently gone if the file is interrupted in between, which is the worse failure.
-- The `cron.schedule` at the end is still here because it is idempotent and because
-- a replay container that never had the job needs it.
--
-- ORDER. Additive to the schema and independent of the delete: applying it before
-- the rows go simply makes the page stop showing a column whose values are about to
-- become uniform, and applying it after would leave a window in which it shows
-- 'zadna' on every row. Apply through `apply_migration.yml` on the PR branch.

set lock_timeout = '5s';
set statement_timeout = '900s';

-- ---------------------------------------------------------------------------
-- 1. The matview, 514 § 4 minus `old_evidence` and its lateral.
-- ---------------------------------------------------------------------------

drop materialized view if exists location_pin_audit_mv cascade;

create materialized view location_pin_audit_mv as
with candidates as materialized (
  -- Arm 1: the store HAS a row for this listing and it is not an answer.
  select ll.listing_id
    from listing_location ll
   where ll.geom is null
     and ll.country_status <> 'foreign'
  union
  -- Arm 2: the store has NO row at all for an active property's display listing.
  select p.repr_listing_ref_id
    from properties p
   where p.status = 'active'
     and p.repr_listing_ref_id is not null
     and not exists (select 1 from listing_location ll
                      where ll.listing_id = p.repr_listing_ref_id)
),
cohort as materialized (
  select
    l.id                                           as listing_id,
    l.property_id,
    l.sreality_id,
    l.source,
    l.source_id_native,
    l.source_url,
    l.category_main,
    l.category_type,
    l.disposition,
    l.area_m2,
    l.price_czk,
    l.is_active,
    l.first_seen_at,
    l.last_seen_at,
    -- The ONE label (migration 503). Mostly NULL here by construction -- that is
    -- the finding, not a defect -- but a row that knows its town without knowing
    -- where it stands says so.
    location_display_label(ll.street_name, ll.house_number_cp, ll.house_number_co,
                           ll.obec_name, ll.cast_obce_name, ll.country_code,
                           ll.country_status)      as display_label,
    ll.country_status::text                        as country_status,
    ll.granularity::text                           as granularity,
    ll.match_confidence::text                      as match_confidence,
    ll.resolver_version,
    ll.resolved_at,
    (ll.listing_id is not null)                    as has_row,
    -- The saved verdict consumed evidence: a non-empty claim set. `claim_set_hash`
    -- is NOT NULL on every row and the resolver hashes the CONSUMED claim list, so
    -- an empty consumption is the digest of the empty list, not a NULL
    -- (location_data/resolver/serialize.py::claim_set_hash -> digest([])).
    (ll.claim_set_hash is not null
       and ll.claim_set_hash <> sha256('[]'::bytea)) as has_claims
  from candidates c
       join listings l on l.id = c.listing_id
       left join listing_location ll on ll.listing_id = l.id
  -- The served set, the same constant the resolver sweep and the claim lane use:
  -- location_data.claims_common.SERVED_LISTING_PREDICATE (alias `l` is part of
  -- that contract). History nobody resolves is not an audit finding.
  where (l.is_active
        OR EXISTS (SELECT 1 FROM properties pr
                    WHERE pr.repr_listing_ref_id = l.id AND pr.status = 'active'))
)
select
  c.*,
  -- Evidence under an ACTIVE contract -- the whole question now that the claims
  -- under retired versions are deleted (W6-a). EXISTS, not a count: the page asks
  -- "is there any", and the probe stops at the first admissible claim. The join to
  -- the contract header is the same two primary-key hops the resolver's own read
  -- predicate makes (`resolve_db._ACTIVE_CONTRACT_ENTRY`); it is NOT dropped just
  -- because every surviving claim is expected to be active, because an operator
  -- claim carries no contract entry at all and must not count as portal evidence.
  exists (select 1
            from location_claims cl
                 join portal_contract_entries pce on pce.id = cl.contract_entry_id
                 join portal_contracts pc         on pc.id  = pce.contract_id
           where cl.listing_id = c.listing_id
             and pc.is_active)                     as claims_now,
  sib.found                                        as sibling_has_pin,
  -- The bucket the page filters on: live/delisted x "did the saved verdict
  -- consume anything". `has_claims` is a plain boolean by here (no row yields
  -- false, never NULL), so the CASE is total.
  case
    when c.is_active and not c.has_claims          then 'active_no_claims'
    when c.is_active                               then 'active_unresolved'
    when not c.has_claims                          then 'delisted_no_claims'
    else                                                'delisted_unresolved'
  end                                              as quality,
  now()                                            as refreshed_at
from cohort c
     left join lateral (
       select exists (
         select 1
         from listings sib
              join listing_location sll on sll.listing_id = sib.id
         where sib.property_id = c.property_id
           and sib.id <> c.listing_id
           and sll.geom is not null
       ) as found
     ) sib on true;

-- REFRESH ... CONCURRENTLY requires a unique index. Listing-grain, so the PK of
-- `listings` is the key.
create unique index if not exists location_pin_audit_mv_pk
  on location_pin_audit_mv (listing_id);

-- The page's filter axes, and its two keyset lanes ((col, id) btrees, the shape
-- frontend/src/lib/keyset.ts pages against). 514's index set, unchanged.
create index if not exists location_pin_audit_mv_source
  on location_pin_audit_mv (source);
create index if not exists location_pin_audit_mv_category
  on location_pin_audit_mv (category_main);
create index if not exists location_pin_audit_mv_quality
  on location_pin_audit_mv (quality);
create index if not exists location_pin_audit_mv_sibling
  on location_pin_audit_mv (listing_id) where sibling_has_pin;
create index if not exists location_pin_audit_mv_last_seen
  on location_pin_audit_mv (last_seen_at, listing_id);
create index if not exists location_pin_audit_mv_first_seen
  on location_pin_audit_mv (first_seen_at, listing_id);

revoke all on location_pin_audit_mv from anon;
grant select on location_pin_audit_mv to authenticated;

-- Corollary E (migrations 437/440): the registry row survives a DROP of the
-- relation (it is a data row, not a dependency), but the insert is kept so a
-- replay that reaches this file first still registers the artifact.
insert into public.derived_artifacts
  (name, producer, host, cadence, staleness_budget, is_serving)
values ('location_pin_audit_mv', 'refresh_location_pin_audit_mv', 'pg_cron',
        '25 * * * *', interval '130 minutes', true)
on conflict (name) do nothing;

-- ---------------------------------------------------------------------------
-- 2. The cron, re-asserted. `cron.schedule` on an existing jobname UPDATES it,
--    so this is idempotent; the guard is 136 / 274 / 510 / 514's -- the CI
--    schema-replay container has no pg_cron and this file must still apply there.
-- ---------------------------------------------------------------------------

do $cron$
begin
  create extension if not exists pg_cron;
  perform cron.schedule(
    'refresh-location-pin-audit',
    '25 * * * *',
    $$set statement_timeout='900s'; select public.refresh_location_pin_audit_mv();$$
  );
exception when others then
  raise notice 'pg_cron unavailable; location pin audit refresh not scheduled (%). Refresh via refresh_location_pin_audit_mv() on another scheduler.', sqlerrm;
end
$cron$;

reset statement_timeout;
reset lock_timeout;

-- ---------------------------------------------------------------------------
-- 3. The proof.
-- ---------------------------------------------------------------------------

do $$
declare
  cols text[];
begin
  if to_regclass('public.location_pin_audit_mv') is null then
    raise exception '515: location_pin_audit_mv missing';
  end if;

  select array_agg(attname order by attnum) into cols
    from pg_attribute
   where attrelid = 'public.location_pin_audit_mv'::regclass
     and attnum > 0 and not attisdropped;

  if 'old_evidence' = any(cols) then
    raise exception '515: location_pin_audit_mv still carries old_evidence';
  end if;
  if not ('claims_now' = any(cols)) then
    raise exception '515: location_pin_audit_mv lost claims_now (only old_evidence goes)';
  end if;

  if not exists (
    select 1 from pg_class where relname = 'location_pin_audit_mv_pk' and relkind = 'i'
  ) then
    raise exception '515: location_pin_audit_mv_pk missing (REFRESH CONCURRENTLY needs it)';
  end if;

  -- The two functions the CASCADE must NOT have taken with it.
  if to_regprocedure('public.location_pin_audit_summary()') is null
     or to_regprocedure('public.refresh_location_pin_audit_mv()') is null then
    raise exception '515: the pin-audit functions are missing';
  end if;

  if not exists (select 1 from public.derived_artifacts
                  where name = 'location_pin_audit_mv') then
    raise exception '515: location_pin_audit_mv not registered in derived_artifacts';
  end if;

  if exists (select 1 from pg_extension where extname = 'pg_cron') then
    if not exists (select 1 from cron.job where jobname = 'refresh-location-pin-audit') then
      raise exception '515: the hourly pin-audit refresh is not scheduled';
    end if;
  else
    raise notice '515: pg_cron unavailable, cron assertion skipped';
  end if;

  raise notice '515: pin-audit surface rebuilt without old_evidence';
end
$$;
