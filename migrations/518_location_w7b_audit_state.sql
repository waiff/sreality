-- 518_location_w7b_audit_state.sql
--
-- W7-b of the location simplification sprint. The audit page's set holds TWO
-- kinds of row that the operator must never have to tell apart by eye. The
-- operator's words, 2026-09-13: "make sure listings like that (that are in a
-- queue) have their own category on the audit poloh page, as that is a very
-- different set of listings (not really an issue) from the ones that were run
-- and not resolved properly (an issue...)".
--
-- So the relation gains ONE column, `state`, and it is the page's top-level cut:
--
--   'pending'    -- the lane has not finished with this listing yet. Nothing is
--                   wrong; the row is here because the resolver has not spoken,
--                   and it will leave on its own within minutes.
--   'unresolved' -- the lane HAS spoken and still has no location. That is the
--                   issue: a verdict exists, nothing is queued, no evidence has
--                   arrived since, and the listing is still hidden from every
--                   consumer under W5's rule.
--
-- THREE ARMS MAKE 'pending', and each one is a distinct way the lane can still
-- owe the listing an answer:
--   1. NO ROW in `listing_location` -- the resolver has never run for it. (The
--      matview's own arm 2 admits exactly these: a live property's display
--      listing with no store row at all.)
--   2. QUEUED in `dirty_locations` -- the listing is waiting for the drain, or
--      being re-resolved because new claims landed. `dirty_locations` is the
--      one queue (migration 384) and it is MUTABLE: a row is deleted when the
--      drain finishes it, so "is it in there" is exactly "is it still owed".
--   3. A SNAPSHOT NEWER THAN THE VERDICT -- `listing_snapshots.scraped_at` past
--      `listing_location.resolved_at`. The page changed after the resolver last
--      looked, so the standing verdict was formed without the newest evidence.
--      Snapshots are written on CONTENT CHANGE only (rule 2), so this fires on a
--      real change to the ad and not on every re-sighting.
-- Everything else is 'unresolved'. The CASE is total and the arms are ordered
-- cheapest-first; 'pending' wins any tie because "we are not done" is the
-- truthful answer whenever it applies.
--
-- WHY IT IS COMPUTED IN THE REFRESH AND NOT READ LIVE. The page must not join a
-- 13 M-row queue table and a 40 M-row snapshot table per pageview. All three
-- arms are cheap at refresh time: arm 1 is a column the relation already has,
-- arm 2 is a primary-key probe into a queue that is usually EMPTY, and arm 3 is
-- one probe of `listing_snapshots (listing_id, scraped_at DESC)` per cohort row
-- (~44 k probes, an index-only descent each). The cost is an hourly one, and the
-- page reads a plain indexed text column.
--
-- THE PRICE OF AN HOURLY SNAPSHOT, STATED PLAINLY: `state` is as old as the
-- refresh. A listing enqueued at :30 still reads 'unresolved' until :25 -- but by
-- then the drain has almost always finished it and the row has left the relation
-- altogether, which is the same outcome the operator wanted. The failure mode
-- runs one way only: a stale 'unresolved' is a row that is about to disappear,
-- never a real issue hidden inside 'pending'.
--
-- MEASURED ON PRODUCTION, 2026-09-13 18:58 UTC, against the 18:25 refresh:
-- 44,381 rows -> **11 pending** (all of them arm 1, listings the resolver has
-- never run for) and **44,370 unresolved**. `dirty_locations` was EMPTY at the
-- measuring moment and no cohort row carried a snapshot newer than its verdict,
-- which is what a drain that keeps up looks like between ingest bursts. The
-- split is worth having anyway: during a scrape burst or a full sweep the queue
-- carries thousands, and those are precisely the rows the operator must not read
-- as a finding.
--
-- WHAT THE BADGE COUNTS. `!AUDIT POLOH (N)` now counts the 'unresolved' rows
-- only -- the issue. A nav badge is an alarm, and a number that climbs every time
-- the scrapers do their job is an alarm nobody reads.
--
-- A MATVIEW CANNOT ADD A COLUMN IN PLACE, so this is 514 § 4 as 515 left it --
-- DROP + CREATE of the relation, its unique index (REFRESH CONCURRENTLY needs
-- it), its filter indexes and its grants -- plus the one column, one lateral and
-- one index. The summary function is dropped and re-created because its RETURNS
-- TABLE gains `state` (a signature change: CREATE OR REPLACE cannot do it).
-- `refresh_location_pin_audit_mv()` is untouched and is NOT taken by the CASCADE
-- (a string-bodied function records no dependency on what it reads), so the cron
-- job keeps pointing at a live function throughout. The cron is left armed for
-- 515's reason: the DROP-to-CREATE window is one populate of a ~44 k-row
-- relation, and a :25 tick landing inside it fails once in the pg_cron log.
--
-- ADDITIVE. Nothing is deleted and no verdict moves; the only rows that change
-- meaning are the ones the page was already showing. Apply through
-- `apply_migration.yml` on the PR branch.

set lock_timeout = '5s';
set statement_timeout = '900s';

-- ---------------------------------------------------------------------------
-- 1. The matview, 515 § 1 plus `state`.
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
  -- false, never NULL), so the CASE is total. It refines 'unresolved' -- under
  -- 'pending' there is no verdict to explain yet.
  case
    when c.is_active and not c.has_claims          then 'active_no_claims'
    when c.is_active                               then 'active_unresolved'
    when not c.has_claims                          then 'delisted_no_claims'
    else                                                'delisted_unresolved'
  end                                              as quality,
  -- W7-b's top-level cut. Three arms make 'pending' -- no verdict yet, queued
  -- for one, or a page change the standing verdict never saw -- and 'pending'
  -- wins any tie, because "the lane is not done" is the truthful answer whenever
  -- it applies. Everything else is the issue.
  case
    when not c.has_row                             then 'pending'
    when exists (select 1 from dirty_locations d
                  where d.listing_id = c.listing_id)
                                                   then 'pending'
    when snap.scraped_at > c.resolved_at           then 'pending'
    else                                                'unresolved'
  end                                              as state,
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
     ) sib on true
     -- The newest snapshot of this listing, one descent of
     -- `listing_snapshots (listing_id, scraped_at DESC)`. LIMIT 1 and not
     -- max(): the index gives the first row and stops.
     left join lateral (
       select s.scraped_at
         from listing_snapshots s
        where s.listing_id = c.listing_id
        order by s.scraped_at desc
        limit 1
     ) snap on true;

-- REFRESH ... CONCURRENTLY requires a unique index. Listing-grain, so the PK of
-- `listings` is the key.
create unique index if not exists location_pin_audit_mv_pk
  on location_pin_audit_mv (listing_id);

-- The page's filter axes, and its two keyset lanes ((col, id) btrees, the shape
-- frontend/src/lib/keyset.ts pages against). 515's index set plus `state`, which
-- every read of the page now carries (the toggle is not optional) and which the
-- nav badge's head-count reads on its own.
create index if not exists location_pin_audit_mv_state
  on location_pin_audit_mv (state);
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
-- 2. The summary function, grouped by `state` as well. A signature change
--    (RETURNS TABLE gains a column), so DROP + CREATE, not CREATE OR REPLACE.
--    Still ONE payload behind every number on the page: the matrix, the two
--    header counts and the "how many match the current filters" total are all
--    sums over these ~250 rows, so they cannot tell different stories.
-- ---------------------------------------------------------------------------

drop function if exists location_pin_audit_summary();

create function location_pin_audit_summary()
returns table (
  state text,
  source text,
  category_main text,
  quality text,
  sibling_has_pin boolean,
  n bigint,
  refreshed_at timestamptz
)
language sql
stable
security invoker
set search_path = public
as $$
  select a.state, a.source, a.category_main, a.quality, a.sibling_has_pin,
         count(*)::bigint, max(a.refreshed_at)
  from location_pin_audit_mv a
  group by a.state, a.source, a.category_main, a.quality, a.sibling_has_pin
$$;

-- Revoke the default ACL, THEN grant back deliberately. `public` matters most:
-- the default is EXECUTE TO PUBLIC, which anon and authenticated inherit, so
-- naming only the two roles would leave the function callable.
revoke execute on function location_pin_audit_summary()
  from public, anon, authenticated;
grant execute on function location_pin_audit_summary() to authenticated;

-- ---------------------------------------------------------------------------
-- 3. The cron, re-asserted. `cron.schedule` on an existing jobname UPDATES it,
--    so this is idempotent; the guard is 136 / 274 / 510 / 514 / 515's -- the CI
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
-- 4. The proof.
-- ---------------------------------------------------------------------------

do $$
declare
  cols text[];
  bad  bigint;
begin
  if to_regclass('public.location_pin_audit_mv') is null then
    raise exception '518: location_pin_audit_mv missing';
  end if;

  select array_agg(attname order by attnum) into cols
    from pg_attribute
   where attrelid = 'public.location_pin_audit_mv'::regclass
     and attnum > 0 and not attisdropped;

  if not ('state' = any(cols)) then
    raise exception '518: location_pin_audit_mv has no state column';
  end if;
  if not ('quality' = any(cols)) or not ('claims_now' = any(cols)) then
    raise exception '518: location_pin_audit_mv lost a column it must keep';
  end if;
  if 'old_evidence' = any(cols) then
    raise exception '518: old_evidence is back (515 dropped it)';
  end if;

  -- Total and two-valued: every row is one of the two states, never NULL and
  -- never a third spelling the page has no label for.
  select count(*) into bad
    from location_pin_audit_mv
   where state is null or state not in ('pending', 'unresolved');
  if bad > 0 then
    raise exception '518: % rows carry a state outside (pending, unresolved)', bad;
  end if;

  -- A row with no store row at all can only be pending -- there is no verdict to
  -- call unresolved.
  select count(*) into bad
    from location_pin_audit_mv
   where not has_row and state <> 'pending';
  if bad > 0 then
    raise exception '518: % rows have no verdict yet but are not pending', bad;
  end if;

  if not exists (
    select 1 from pg_class where relname = 'location_pin_audit_mv_pk' and relkind = 'i'
  ) then
    raise exception '518: location_pin_audit_mv_pk missing (REFRESH CONCURRENTLY needs it)';
  end if;
  if not exists (
    select 1 from pg_class where relname = 'location_pin_audit_mv_state' and relkind = 'i'
  ) then
    raise exception '518: location_pin_audit_mv_state missing';
  end if;

  -- The summary function carries the new axis, and the refresh function is not
  -- a casualty of the CASCADE.
  if to_regprocedure('public.location_pin_audit_summary()') is null
     or to_regprocedure('public.refresh_location_pin_audit_mv()') is null then
    raise exception '518: the pin-audit functions are missing';
  end if;
  if not exists (
    select 1 from pg_proc
     where oid = 'public.location_pin_audit_summary()'::regprocedure
       and 'state' = any(proargnames)
  ) then
    raise exception '518: location_pin_audit_summary() does not return state';
  end if;

  if not exists (select 1 from public.derived_artifacts
                  where name = 'location_pin_audit_mv') then
    raise exception '518: location_pin_audit_mv not registered in derived_artifacts';
  end if;

  if exists (select 1 from pg_extension where extname = 'pg_cron') then
    if not exists (select 1 from cron.job where jobname = 'refresh-location-pin-audit') then
      raise exception '518: the hourly pin-audit refresh is not scheduled';
    end if;
  else
    raise notice '518: pg_cron unavailable, cron assertion skipped';
  end if;

  raise notice '518: pin-audit surface split into pending / unresolved';
end
$$;
