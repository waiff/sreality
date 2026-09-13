-- 510_location_pin_audit.sql
--
-- TEMPORARY operator review surface for the migration-503 pin-collapse ruling.
-- Additive: one materialized view, one summary function, one hourly pg_cron
-- refresh. Nothing existing is altered or dropped. Delete the whole set (and
-- the SPA page it feeds) once the operator has ruled.
--
-- WHAT IT AUDITS. The Browse map still draws a pin for every active property
-- that carries legacy coordinates. The new location store (`listing_location`,
-- migration 501) has no Czech point for a large slice of them — it either never
-- saw any evidence, or saw some and could not turn it into a position. If 503's
-- guard collapses the map onto the new store, those pins disappear. This view
-- is exactly that set, one row per property (at its representative listing), so
-- the operator can look at them before ruling.
--
-- The cohort predicate mirrors the map feed's own (`properties_map_mv` =
-- `browse_projection` where lat/lng are not null = `properties` where
-- status='active' and lat/lng are not null), but is written against
-- `properties` directly rather than against the map matview. That is
-- deliberate: `properties_map_mv` is REBUILT at runtime by
-- rebuild_properties_map_mv() (migration 376), so its live column set is newer
-- than the one migration 254 statically creates — and the CI schema replay only
-- ever sees 254's. Reading `properties` keeps this migration replayable.
-- Foreign listings are excluded: a determined `country_status='foreign'` is a
-- correct answer, not a loss.
--
-- WHY A MATVIEW. The cohort is a full scan of the ~672k-row active-property
-- set plus a per-row EXISTS on `location_claims`. As a live view that is far
-- over PostgREST's 3s browser budget; materialized and refreshed hourly it is a
-- ~37k-row indexed relation the page pages through in milliseconds.
--
-- THE TWO EVIDENCE SIGNALS ARE NOT THE SAME QUESTION, and the page shows both:
--   * has_claims  — the SAVED verdict consumed at least one claim.
--     `claim_set_hash` is NOT NULL on every row, and the resolver hashes the
--     CONSUMED claim list, so an empty consumption is the digest of the empty
--     list, not a NULL (location_data/resolver/serialize.py::claim_set_hash ->
--     digest([]) = sha256('[]')). Measured 2026-09-13: 35,608 of 36,959 rows.
--   * claims_now  — the claim store holds location evidence for this listing
--     TODAY. Measured on the same set, nearly all of them do. The gap between
--     the two is the finding: most saved verdicts predate their own evidence
--     (the claims were mined afterwards), so the pin loss is mostly a stale
--     verdict rather than a genuinely unlocatable ad.
--
-- GRANTS. `authenticated` only, never `anon` — the same posture migration 376
-- enforces on `properties_map_mv`. PostgREST reads matviews directly, so there
-- is no `_public` wrapper view (the wrapper convention exists for views over
-- base tables that need a narrowed column set; this relation IS the narrowed
-- set). `listing_location` and `location_claims` stay revoked from both browser
-- roles — the audit columns reach the SPA only through this relation.

create materialized view if not exists location_pin_audit_mv as
select
  p.repr_listing_ref_id                          as listing_id,
  p.id                                           as property_id,
  p.repr_listing_id                              as sreality_id,
  p.source,
  l.source_id_native,
  l.source_url,
  p.category_main,
  p.category_type,
  p.disposition,
  p.area_m2,
  p.street,
  p.locality,
  p.district,
  p.current_price_czk                            as price_czk,
  p.is_active,
  p.first_seen_at,
  p.last_seen_at,
  p.lat                                          as legacy_lat,
  p.lng                                          as legacy_lng,
  ll.country_status::text                        as country_status,
  ll.granularity::text                           as granularity,
  ll.match_confidence::text                      as match_confidence,
  ll.resolver_version,
  ll.resolved_at,
  (ll.listing_id is not null)                    as has_row,
  -- The saved verdict consumed evidence (see the header): a non-empty claim set.
  (ll.claim_set_hash is not null
     and ll.claim_set_hash <> sha256('[]'::bytea))  as has_claims,
  -- Evidence exists in the claim store right now, consumed or not.
  exists (
    select 1 from location_claims c
     where c.listing_id = p.repr_listing_ref_id
  )                                              as claims_now,
  case
    when p.is_active and not (ll.claim_set_hash is not null
                              and ll.claim_set_hash <> sha256('[]'::bytea))
      then 'active_no_claims'
    when p.is_active
      then 'active_unresolved'
    when not (ll.claim_set_hash is not null
              and ll.claim_set_hash <> sha256('[]'::bytea))
      then 'delisted_no_claims'
    else 'delisted_unresolved'
  end                                            as quality,
  now()                                          as refreshed_at
from properties p
     left join listing_location ll on ll.listing_id = p.repr_listing_ref_id
     left join listings l          on l.id          = p.repr_listing_ref_id
where p.status = 'active'
  and p.lat is not null
  and p.lng is not null
  -- The unique index below is what REFRESH ... CONCURRENTLY diffs on, and a
  -- unique index tolerates repeated NULLs — two representative-less properties
  -- would make the refresh fail with "contains duplicate rows". Guard the key.
  and p.repr_listing_ref_id is not null
  and ll.geom is null
  and (ll.country_status is null or ll.country_status <> 'foreign');

-- REFRESH ... CONCURRENTLY requires a unique index.
create unique index if not exists location_pin_audit_mv_pk
  on location_pin_audit_mv (listing_id);

-- The page's three filter axes. Small relation, but these keep the summary
-- function's group-by and the filtered pages off a full scan.
create index if not exists location_pin_audit_mv_source
  on location_pin_audit_mv (source);
create index if not exists location_pin_audit_mv_category
  on location_pin_audit_mv (category_main);
create index if not exists location_pin_audit_mv_quality
  on location_pin_audit_mv (quality);

-- The list's keyset lane (`last_seen_at`, tiebroken on `listing_id` — the same
-- (col, id) btree shape frontend/src/lib/keyset.ts pages against).
create index if not exists location_pin_audit_mv_last_seen
  on location_pin_audit_mv (last_seen_at, listing_id);
create index if not exists location_pin_audit_mv_first_seen
  on location_pin_audit_mv (first_seen_at, listing_id);

revoke all on location_pin_audit_mv from anon;
grant select on location_pin_audit_mv to authenticated;

-- The overview matrix: portal rows x property-type columns, split by bucket.
-- At most (portals x category_main x 4 buckets) rows — ~120 — so the page reads
-- the whole thing once and sums it for whatever the filters select, instead of
-- issuing a count per cell.
-- `refreshed_at` rides along (it is one value across the whole relation) so the
-- page can print "stav k HH:MM" without a second read, and without inferring
-- freshness from whichever list page happens to be loaded.
create or replace function location_pin_audit_summary()
returns table (
  source text,
  category_main text,
  quality text,
  n bigint,
  refreshed_at timestamptz
)
language sql
stable
security invoker
set search_path = public
as $$
  select a.source, a.category_main, a.quality, count(*)::bigint, max(a.refreshed_at)
  from location_pin_audit_mv a
  group by a.source, a.category_main, a.quality
$$;

revoke all on function location_pin_audit_summary() from public, anon;
grant execute on function location_pin_audit_summary() to authenticated;

-- The refresh entry point (the refresh_health_matviews shape, migration 136).
-- CONCURRENTLY so the page never reads a half-built relation mid-refresh, and so
-- the refresh never blocks a reader. Nobody but the owner may call it — pg_cron
-- runs the job as the user that scheduled it.
create or replace function refresh_location_pin_audit_mv()
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  refresh materialized view concurrently location_pin_audit_mv;
end;
$$;

revoke all on function refresh_location_pin_audit_mv() from public, anon, authenticated;

-- Hourly, off the top of the hour so it does not land with the */15 map rebuild.
-- Guarded exactly like migrations 136 and 274: the CI schema-replay container
-- has no pg_cron, and the migration must still apply there (it logs a notice and
-- skips the schedule). Re-applying upserts the named job.
do $cron$
begin
  create extension if not exists pg_cron;
  perform cron.schedule(
    'refresh-location-pin-audit',
    '25 * * * *',
    $$select public.refresh_location_pin_audit_mv();$$
  );
exception when others then
  raise notice 'pg_cron unavailable; location pin audit refresh not scheduled (%). Refresh via refresh_location_pin_audit_mv() on another scheduler.', sqlerrm;
end
$cron$;

-- Assert what this migration promised. The cron arm is conditional on the
-- extension actually being installed, so the assertion is real on Supabase and
-- skipped (with a notice) in the replay container — a guard that cannot fire is
-- worse than no guard (tests/test_migration_catalog_guards.py).
do $assert$
declare
  has_cron boolean := exists (select 1 from pg_extension where extname = 'pg_cron');
begin
  if to_regclass('public.location_pin_audit_mv') is null then
    raise exception '510: location_pin_audit_mv missing';
  end if;
  if not exists (
    select 1 from pg_class where relname = 'location_pin_audit_mv_pk' and relkind = 'i'
  ) then
    raise exception '510: location_pin_audit_mv_pk missing (REFRESH CONCURRENTLY needs it)';
  end if;
  if to_regprocedure('public.location_pin_audit_summary()') is null then
    raise exception '510: location_pin_audit_summary() missing';
  end if;
  if to_regprocedure('public.refresh_location_pin_audit_mv()') is null then
    raise exception '510: refresh_location_pin_audit_mv() missing';
  end if;
  if has_cron then
    if not exists (select 1 from cron.job where jobname = 'refresh-location-pin-audit') then
      raise exception '510: cron job refresh-location-pin-audit not scheduled';
    end if;
  else
    raise notice '510: pg_cron absent, refresh schedule not asserted (CI replay container)';
  end if;
end
$assert$;
