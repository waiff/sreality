-- 438 — add the missing 'plot' token to listings_area_basis_check.
--
-- PRODUCTION DRIFT, found by a backfill crashing on it:
--
--   psycopg.errors.CheckViolation: new row for relation "listings"
--   violates check constraint "listings_area_basis_check"
--
-- The live constraint is
--   CHECK (area_basis IS NULL OR area_basis = ANY (ARRAY['usable','floor','total','unknown']))
-- while migration 423 in the repo says it should be
--   check (area_basis is null or area_basis in ('usable','floor','total','plot','unknown'))
-- and `properties_area_basis_check` on the very same migration DOES carry all
-- five. So `listings` has been missing exactly one token: 'plot'.
--
-- HOW THE DRIFT SURVIVED. Migration 423 wraps both ALTERs in
-- `if not exists (select 1 from pg_constraint where conname = ...)`. A
-- constraint of that name already existed on `listings` from W1 development --
-- built against the four-token vocabulary, before the land branch was inverted
-- to return 'plot' -- so 423's guard skipped it and left the stale one in place.
-- `properties_area_basis_check` did not exist, so it was created correctly. An
-- idempotent guard is the right default, and this is its one failure mode:
-- it cannot tell "already done" from "done differently".
--
-- WHY THIS IS NOT COSMETIC. `scraper.area.derive_headline_area` returns
-- (value, 'plot') for every `category_main = 'pozemek'` row that carries any
-- measure, `area_basis` is in db.LISTING_COLUMNS, and it is NOT in
-- _PRESERVE_IF_NULL_COLUMNS -- so it is written as EXCLUDED.area_basis on every
-- detail write. Any land listing with an area therefore FAILS this constraint
-- the moment the detail drain reaches it, and because the drain writes through
-- a BATCHED upsert, one such row takes its whole batch with it.
--
-- It has not fired yet only because the land rows re-drained since W1 shipped
-- happen to be the ones with no area at all (sreality land carries its parcel in
-- estate_area and leaves area_m2 NULL; bezrealitky the same). The 442 pozemek
-- snapshots in the last 24h are index-walk writes, which do not touch the area
-- columns. 30,632 rows sit in listing_detail_queue. This is latent, not benign.
--
-- It also explains the roadmap's "zero rows anywhere carry `plot`" -- that was
-- read as "migration 423 shipped no backfill", and a backfill was written on
-- that premise. The backfill was necessary but not sufficient: the token was
-- not merely absent, it was FORBIDDEN.
--
-- SAFETY. The new constraint is a strict SUPERSET of the old one, so every
-- existing row already satisfies it and no data can be invalidated. Widening a
-- CHECK grants; it revokes nothing.
--
-- LOCK DISCIPLINE (listings is 11 GB and hot). `add constraint ... check` would
-- normally hold ACCESS EXCLUSIVE for a full-table validation scan, which on this
-- table is a multi-minute outage. So it is added NOT VALID -- a catalog-only
-- change, instant -- and validated in a separate statement, which takes only
-- SHARE UPDATE EXCLUSIVE and does not block readers or writers. The drop+add
-- pair still needs a brief ACCESS EXCLUSIVE, so it waits for a gap in the
-- `rebuild_%` jobs and then queues with lock_timeout = '6s' rather than
-- retrying at a shorter timeout, which never queues and therefore never wins
-- under continuous drain traffic.

do $$
declare
  attempt int := 0;
  busy    int;
begin
  loop
    attempt := attempt + 1;

    select count(*) into busy
      from pg_stat_activity
     where state = 'active'
       and query like '%rebuild\_%'
       and pid <> pg_backend_pid();

    if busy = 0 then
      begin
        set local lock_timeout = '6s';

        alter table listings drop constraint if exists listings_area_basis_check;
        alter table listings add constraint listings_area_basis_check
          check (area_basis is null
                 or area_basis in ('usable','floor','total','plot','unknown'))
          not valid;

        raise notice '438: constraint replaced on attempt %', attempt;
        exit;
      exception when lock_not_available then
        raise notice '438: attempt % lost the lock race, retrying', attempt;
      end;
    else
      raise notice '438: attempt % skipped, % rebuild_%% statement(s) active',
                   attempt, busy;
    end if;

    if attempt >= 10 then
      raise exception '438: could not acquire the lock in % attempts', attempt;
    end if;
    perform pg_sleep(3);
  end loop;
end
$$;

-- Separate statement, separate lock: SHARE UPDATE EXCLUSIVE, concurrent-safe.
alter table listings validate constraint listings_area_basis_check;

-- Verification (run after applying):
--   select pg_get_constraintdef(oid) from pg_constraint
--    where conname = 'listings_area_basis_check';
--   -> expected: CHECK (((area_basis IS NULL) OR (area_basis = ANY
--      (ARRAY['usable'::text, 'floor'::text, 'total'::text, 'plot'::text,
--      'unknown'::text]))))
--
--   select convalidated from pg_constraint
--    where conname = 'listings_area_basis_check';
--   -> expected: true
