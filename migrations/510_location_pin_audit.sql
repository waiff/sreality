-- 510_location_pin_audit.sql
--
-- TEMPORARY operator review surface for the migration-503 pin-collapse ruling.
-- Additive: one materialized view, one summary function, one hourly pg_cron
-- refresh. Nothing existing is altered or dropped. Delete the whole set (and
-- the SPA page it feeds) once the operator has ruled.
--
-- WHAT IT AUDITS. The Browse map still draws a pin for every active property
-- that carries legacy coordinates. The new location store (`listing_location`,
-- migration 501) has no Czech point for a large slice of them. If 503's guard
-- collapses the map onto the new store, those pins disappear. This view is
-- exactly that set, one row per property (at its representative listing), so
-- the operator can look at them before ruling. Measured 2026-09-13 06:25Z:
-- 36,970 rows — 2,413 still-live ads (almost all bazos dead ads, which #1451
-- delists over the coming days) and 34,557 delisted display listings.
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
-- set plus per-row evidence lookups. As a live view that is far over
-- PostgREST's 3s browser budget; materialized and refreshed hourly it is a
-- ~37k-row indexed relation the page pages through in milliseconds. The cohort
-- CTE is MATERIALIZED so the three lateral lookups run ~37k times and not once
-- per active property.
--
-- WHAT THE EVIDENCE COLUMNS MEAN — and the reading that was WRONG. The first
-- cut of this migration carried a `claims_now` that counted ANY row in
-- `location_claims`, and concluded from it that ~35.6k verdicts predate their
-- own evidence, i.e. the claims were mined afterwards. That is not what the
-- data says. Measured on production 2026-09-13 06:05Z, of the newest 366 rows
-- in the set 361 have claims and ZERO of them sit under an active contract:
-- they are bazos@1/@3/@4 `surface=legacy_column, extraction_method=
-- legacy_column` copies of the legacy `listings` columns (the Mapy-era pin, the
-- legacy PSČ/locality fields the doctrine dropped in W1-b), plus some
-- `archived_html` / `url_slug_parse` claims under superseded contract versions.
-- None was observed after the verdict; none is queued. So:
--   * has_claims  — the SAVED verdict consumed at least one claim.
--     `claim_set_hash` is NOT NULL on every row and the resolver hashes the
--     CONSUMED claim list, so an empty consumption is the digest of the empty
--     list, not a NULL (location_data/resolver/serialize.py::claim_set_hash ->
--     digest([]) = sha256('[]')).
--   * claims_now  — evidence exists TODAY under an ACTIVE contract
--     (`portal_contract_entries` -> `portal_contracts.is_active`). Measured:
--     1,351 rows, which is exactly the `has_claims` set. The lane has consumed
--     everything a live contract offers; there is no backlog to wait for.
--   * old_evidence — what the SUPERSEDED evidence is, so "no live evidence"
--     never reads as "nothing was ever there": 'legacy' (every superseded claim
--     is a `legacy_column` copy — Mapy-era pin / legacy columns, 4.6k rows),
--     'archived' (some came off an older page version, 32.2k), 'none' (172).
--   * sibling_has_pin — ANOTHER listing of the same property DOES have a
--     `listing_location.geom`. 1,163 rows: the property-level fallback would
--     recover ~3 % of the set without any resolver change, which is a decision
--     the operator can take on its own.
--
-- IDEMPOTENCE. The matview is DROPPED and rebuilt rather than guarded with
-- `if not exists`, and the summary function likewise, because this migration
-- was revised after its first version was pushed and an `if not exists` would
-- silently keep a stale definition on any database that got the first one. It
-- is a rebuildable cache created by this same file — dropping it destroys no
-- history and no other object depends on it.
--
-- GRANTS. `authenticated` only, never `anon` — the same posture migration 376
-- enforces on `properties_map_mv`. PostgREST reads matviews directly, so there
-- is no `_public` wrapper view (the wrapper convention exists for views over
-- base tables that need a narrowed column set; this relation IS the narrowed
-- set). `listing_location` and `location_claims` stay revoked from both browser
-- roles — the audit columns reach the SPA only through this relation.

-- The first population scans the 672k-row map matview with ~37k evidence laterals;
-- the role's default budget (120 s) cancelled it on 2026-09-13 (apply run 34743551009).
set statement_timeout = '900s';
set lock_timeout = '30s';

drop materialized view if exists location_pin_audit_mv;

create materialized view location_pin_audit_mv as
with cohort as materialized (
  select
    p.repr_listing_ref_id                          as listing_id,
    p.id                                           as property_id,
    p.repr_listing_id                              as sreality_id,
    p.source,
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
    -- The saved verdict consumed evidence: a non-empty claim set (see header).
    (ll.claim_set_hash is not null
       and ll.claim_set_hash <> sha256('[]'::bytea)) as has_claims
  from properties p
       left join listing_location ll on ll.listing_id = p.repr_listing_ref_id
  where p.status = 'active'
    and p.lat is not null
    and p.lng is not null
    -- The unique index below is what REFRESH ... CONCURRENTLY diffs on, and a
    -- unique index tolerates repeated NULLs — two representative-less
    -- properties would fail the refresh with "contains duplicate rows".
    and p.repr_listing_ref_id is not null
    and ll.geom is null
    and (ll.country_status is null or ll.country_status <> 'foreign')
)
select
  c.*,
  l.source_id_native,
  l.source_url,
  (ev.n_active > 0)                                as claims_now,
  case
    when coalesce(ev.n_superseded, 0) = 0          then 'none'
    when ev.n_superseded_nonlegacy = 0             then 'legacy'
    else                                                'archived'
  end                                              as old_evidence,
  sib.found                                        as sibling_has_pin,
  -- The bucket the page filters on. Active/delisted x "did the saved verdict
  -- consume anything". `has_claims` is a plain boolean by here (a missing
  -- listing_location row yields false, never NULL), so the CASE is total.
  case
    when c.is_active and not c.has_claims          then 'active_no_claims'
    when c.is_active                               then 'active_unresolved'
    when not c.has_claims                          then 'delisted_no_claims'
    else                                                'delisted_unresolved'
  end                                              as quality,
  now()                                            as refreshed_at
from cohort c
     left join listings l on l.id = c.listing_id
     left join lateral (
       select
         count(*) filter (where pc.is_active)                   as n_active,
         count(*) filter (where not coalesce(pc.is_active, false)) as n_superseded,
         count(*) filter (where not coalesce(pc.is_active, false)
                            and cl.extraction_method <> 'legacy_column')
                                                                as n_superseded_nonlegacy
       from location_claims cl
            left join portal_contract_entries pce on pce.id = cl.contract_entry_id
            left join portal_contracts pc         on pc.id  = pce.contract_id
       where cl.listing_id = c.listing_id
     ) ev on true
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

-- REFRESH ... CONCURRENTLY requires a unique index.
create unique index if not exists location_pin_audit_mv_pk
  on location_pin_audit_mv (listing_id);

-- The page's filter axes. Small relation, but these keep the summary
-- function's group-by and the filtered pages off a full scan.
create index if not exists location_pin_audit_mv_source
  on location_pin_audit_mv (source);
create index if not exists location_pin_audit_mv_category
  on location_pin_audit_mv (category_main);
create index if not exists location_pin_audit_mv_quality
  on location_pin_audit_mv (quality);
-- Partial: `sibling_has_pin` is true on ~3 % of the set, so the interesting
-- half of that filter is a small index and the other half a seq scan.
create index if not exists location_pin_audit_mv_sibling
  on location_pin_audit_mv (listing_id) where sibling_has_pin;

-- The list's keyset lanes (sort column, tiebroken on `listing_id` — the same
-- (col, id) btree shape frontend/src/lib/keyset.ts pages against).
create index if not exists location_pin_audit_mv_last_seen
  on location_pin_audit_mv (last_seen_at, listing_id);
create index if not exists location_pin_audit_mv_first_seen
  on location_pin_audit_mv (first_seen_at, listing_id);

revoke all on location_pin_audit_mv from anon;
grant select on location_pin_audit_mv to authenticated;

-- Corollary E (migrations 437/440): a precomputed artifact declares its
-- producer, cadence and staleness budget, or the freshness surface cannot tell
-- "fresh" from "the job died three days ago". 130 min = two hourly ticks plus
-- slack, so one skipped run is not an alarm and two consecutive ones are. The
-- :25 slot collides with none of the live jobs (*/10 health, */15 browse-list,
-- 7,37 browse-map, 4,19,34,49 llm-cost, 11,41 location-compare, :10, 30 */6).
insert into public.derived_artifacts
  (name, producer, host, cadence, staleness_budget, is_serving)
values ('location_pin_audit_mv', 'refresh_location_pin_audit_mv', 'pg_cron',
        '25 * * * *', interval '130 minutes', true)
on conflict (name) do nothing;

-- The overview matrix: portal rows x property-type columns, split by bucket.
-- At most (portals x category_main x 4 buckets) rows — ~120 — so the page reads
-- the whole thing once and sums it for whatever the filters select, instead of
-- issuing a count per cell.
-- `sibling_has_pin` is a group-by column and not a separate count, so the
-- page's "how many would the property-level fallback recover" number is a sum
-- over the SAME payload as every other number on the page. `refreshed_at`
-- rides along (it is one value across the whole relation) so the page can print
-- "stav k HH:MM" without a second read.
--
-- DROP first: the signature gained a column after the first version of this
-- file was pushed, and `create or replace function` cannot change a return
-- type.
drop function if exists location_pin_audit_summary();

create function location_pin_audit_summary()
returns table (
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
  select a.source, a.category_main, a.quality, a.sibling_has_pin,
         count(*)::bigint, max(a.refreshed_at)
  from location_pin_audit_mv a
  group by a.source, a.category_main, a.quality, a.sibling_has_pin
$$;

-- Revoke the default ACL from all three, THEN grant back deliberately. `public`
-- matters most: the default is EXECUTE TO PUBLIC, which anon and authenticated
-- inherit, so naming only the two roles would leave the function callable
-- (tests/location_data/test_location_schema_contracts.py).
revoke execute on function location_pin_audit_summary()
  from public, anon, authenticated;
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
declare
  t0 timestamptz := clock_timestamp();
  n  bigint;
begin
  refresh materialized view concurrently location_pin_audit_mv;
  select count(*) into n from location_pin_audit_mv;
  perform stamp_derived_artifact(
    'location_pin_audit_mv', n,
    (extract(epoch from clock_timestamp() - t0) * 1000)::integer);
end;
$$;

revoke execute on function refresh_location_pin_audit_mv()
  from public, anon, authenticated;

-- Hourly, off the top of the hour so it does not land with the */15 map rebuild.
-- Guarded exactly like migrations 136 and 274: the CI schema-replay container
-- has no pg_cron, and the migration must still apply there (it logs a notice and
-- skips the schedule). Re-applying upserts the named job.
--
-- THE TIMEOUT IS ARMED IN THE CRON COMMAND, not in the function's proconfig —
-- migration 371's lesson, which shipped twice before it was understood: Postgres
-- arms statement_timeout once, when the top-level statement begins, so a
-- function can never raise its own budget. Without this the refresh runs on the
-- 120s database default; the cohort scan plus its evidence laterals can exceed
-- that, and it would fail at exactly 120.0s every hour with nothing else to see.
-- pg_cron runs the command over the simple query protocol, so the `set` is its
-- own top-level statement in the same session — the one place it takes effect.
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
  if not exists (
    select 1 from public.derived_artifacts where name = 'location_pin_audit_mv'
  ) then
    raise exception '510: location_pin_audit_mv not registered in derived_artifacts';
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
