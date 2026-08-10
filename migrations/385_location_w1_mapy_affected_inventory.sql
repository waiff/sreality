-- 385_location_w1_mapy_affected_inventory.sql
--
-- Location-data W1, remediation step R2: the Mapy.cz five-arm affected-set
-- inventory (design 04-connectors-ops.md C7.2, MASTER.md 7.2).
--
-- WHY NOW. R2 is a W1 *input*, not a W1 output: 06-migration-backfill.md 6.1.2
-- admits a `carry_forward` coordinate as class B only if the row is ABSENT from
-- this inventory, so the first location claim must not be written before these
-- tables are populated. R4 (W4) then uses the same rows as the before/after
-- purge ledger, and the W1 licence gate runs
--     claims JOIN mapy_affected WHERE claim_type = 'coordinate'  -- must be 0.
--
-- WHAT IT MAY NOT HOLD (the 6.1.5 class-E carve-out, non-negotiable). Mapy's
-- terms prohibit storing "API function results" with no forensic carve-out, so a
-- quarantine table holding Mapy lat/lng would recreate the exposure under a new
-- name. These tables therefore record IDENTITY and REASON only: no lat, no lng,
-- no matched_type, no confidence, no rounded/derived coordinate key. Arm 3
-- ("geom matches a geocode_cache coordinate") survives as a BOOLEAN; the
-- comparison itself happens in memory in scripts/location_mapy_inventory.py and
-- nothing about the coordinate is written back. `coords.source` IS storable: it
-- is a scraper-authored provenance token, not portal or vendor data (6.1.2).
--
-- THE FIVE ARMS (C7.2's set A, verbatim):
--   1. raw_json->'coords'->>'source' IN ('geocode','carry_forward')  [+ bazos
--      'street' / 'locality', which 6.1.2 row 5 establishes as Mapy output from
--      bazos' in-parser geocoder — a deliberate SUPERSET of C7.2's literal list]
--   2. geocode_attempted_at IS NOT NULL
--   3. geom matches a geocode_cache coordinate
--   4. all geocode_cache rows                     -> mapy_affected_cache
--   5. every property whose children intersect A  -> mapy_affected_props
-- Arms 1-3 are per listing and live on one mapy_affected row (a listing hit by
-- several arms is ONE row carrying several true flags).
--
-- IMMUTABLE. Evidence of a live licensing exposure must not be editable after
-- the fact, so UPDATE / DELETE / TRUNCATE raise on all three evidence tables.
-- INSERT stays open precisely so the batched job can re-run with
-- `ON CONFLICT DO NOTHING` (that path fires INSERT triggers only, never UPDATE).
-- mapy_inventory_runs is deliberately NOT immutable: it is run bookkeeping
-- (progress high-water mark, terminal counts), not evidence.
--
-- Backend/service-role only: every table, the sequence and the trigger function
-- are revoked from anon/authenticated (this project's default privileges
-- auto-GRANT on new tables AND new functions).

set lock_timeout = '5s';

------------------------------------------------------------------
-- Run bookkeeping. Mutable by design: the job stamps the keyset
-- high-water mark after every batch so a re-run resumes instead of
-- re-detoasting 650k raw_json values, and writes terminal counts at
-- the end.
------------------------------------------------------------------
create table mapy_inventory_runs (
  id                          bigserial primary key,
  started_at                  timestamptz not null default now(),
  finished_at                 timestamptz,
  -- 'stopped' = hit its --limit / --max-seconds budget; the next run resumes
  -- from scanned_through_listing_id.
  status                      text not null default 'running'
                                check (status in ('running', 'stopped',
                                                  'completed', 'failed')),
  -- Arm 3's match tolerance, in degrees, recorded so a later re-run is
  -- comparable: the job rounds to this grid and additionally accepts the 3x3
  -- neighbourhood (the h3 extension is not available on this instance, so the
  -- design's rounded-cell-key fallback is what runs).
  match_epsilon_deg           double precision not null,
  cache_rows_total            integer,
  cache_rows_with_coordinate  integer,
  listings_scanned            bigint not null default 0,
  scanned_through_listing_id  bigint,
  -- False when the run was started at an operator-chosen listing_id: its
  -- high-water mark does NOT mean "everything below is scanned", so the resume
  -- query must ignore it or a whole id range would be silently skipped in a
  -- ledger whose only job is completeness.
  resumable                   boolean not null default true,
  arm1_rows                   bigint not null default 0,
  arm2_rows                   bigint not null default 0,
  arm3_rows                   bigint not null default 0,
  arm4_rows                   bigint not null default 0,
  arm5_rows                   bigint not null default 0,
  listings_inserted           bigint not null default 0,
  note                        text
);

comment on table mapy_inventory_runs is
  'Run bookkeeping for the C7.2 R2 Mapy affected-set inventory. Mutable (progress '
  'high-water mark + terminal counts); the three evidence tables it stamps are not.';

------------------------------------------------------------------
-- Arm 1-3: listing grain.
------------------------------------------------------------------
create table mapy_affected (
  listing_id              bigint primary key,
  source                  text not null,
  arm1_coords_source      boolean not null,
  coords_source           text,
  arm2_geocode_attempted  boolean not null,
  geocode_attempted_at    timestamptz,
  arm3_geom_matches_cache boolean not null,
  reason_code             text not null
                            check (reason_code in ('mapy_derived_coordinate',
                                                   'coordinate_provenance_unknown')),
  captured_at             timestamptz not null default now(),
  inventory_run_id        bigint not null references mapy_inventory_runs(id),
  constraint mapy_affected_some_arm
    check (arm1_coords_source or arm2_geocode_attempted or arm3_geom_matches_cache),
  constraint mapy_affected_arm1_evidence
    check ((coords_source is not null) = arm1_coords_source),
  constraint mapy_affected_arm2_evidence
    check ((geocode_attempted_at is not null) = arm2_geocode_attempted)
);

-- No FK to listings(id): the evidence must outlive its subject (a ledger of a
-- licensing violation cannot be erasable by deleting the listing), and adding a
-- child FK would take a lock on the always-on `listings` writer for nothing.
comment on table mapy_affected is
  'C7.2 R2 arms 1-3, listing grain. IDENTITY + REASON ONLY: never a coordinate, '
  'matched_type or confidence (06 6.1.5 class-E carve-out). Arm 3 is a boolean; '
  'the coordinate comparison happens in memory and is not persisted. Immutable.';
comment on column mapy_affected.coords_source is
  'The scraper-authored raw_json.coords.source token that triggered arm 1 '
  '(geocode / carry_forward / street / locality). Provenance, not vendor output.';
comment on column mapy_affected.arm3_geom_matches_cache is
  'listings.geom fell on (or adjacent to) a rounded geocode_cache coordinate cell. '
  'Boolean only — storing the cell key would be storing a Mapy coordinate.';

create index mapy_affected_source_idx on mapy_affected (source);
create index mapy_affected_run_idx on mapy_affected (inventory_run_id);

------------------------------------------------------------------
-- Arm 4: geocode_cache identity. Per 06 6.1.4 only
-- (query_key_hash, resolved_at, reason_code) survives the R4 drop of
-- geocode_cache; query_key itself is the request string WE authored,
-- not an output of the service, and is kept so the ledger stays
-- joinable to the live cache until R4 drops it.
------------------------------------------------------------------
create table mapy_affected_cache (
  query_key         text primary key,
  query_key_sha256  text not null,
  resolved_at       timestamptz not null,
  reason_code       text not null
                      check (reason_code in ('mapy_derived_coordinate',
                                             'coordinate_provenance_unknown')),
  captured_at       timestamptz not null default now(),
  inventory_run_id  bigint not null references mapy_inventory_runs(id)
);

comment on table mapy_affected_cache is
  'C7.2 R2 arm 4: the identity of every geocode_cache row, so R4 can drop that '
  'table and still prove what it contained. No lat/lng, no matched_type, no '
  'confidence — 06 6.1.4/6.1.5. Immutable.';

------------------------------------------------------------------
-- Arm 5: the property closure.
------------------------------------------------------------------
create table mapy_affected_props (
  property_id       bigint primary key,
  reason_code       text not null
                      check (reason_code in ('child_listing_in_affected_set')),
  captured_at       timestamptz not null default now(),
  inventory_run_id  bigint not null references mapy_inventory_runs(id)
);

comment on table mapy_affected_props is
  'C7.2 R2 arm 5: every property with a child listing in mapy_affected. '
  'properties carries its own geom/lat/lng copy, so nulling listings.geom alone '
  'would leave the Mapy coordinate live in properties_public. Immutable.';

------------------------------------------------------------------
-- Immutability.
------------------------------------------------------------------
create or replace function mapy_inventory_immutable() returns trigger
language plpgsql as $$
begin
  raise exception
    'relation % is immutable licensing evidence (04-connectors-ops.md C7.2 R2): % is forbidden; the only sanctioned write is INSERT ... ON CONFLICT DO NOTHING',
    tg_table_name, tg_op
    using errcode = '42501';
end;
$$;

comment on function mapy_inventory_immutable() is
  'Blocks UPDATE/DELETE/TRUNCATE on the C7.2 R2 evidence tables. Statement-level, '
  'so a zero-row UPDATE is refused too.';

create trigger mapy_affected_immutable
  before update or delete or truncate on mapy_affected
  for each statement execute function mapy_inventory_immutable();

create trigger mapy_affected_cache_immutable
  before update or delete or truncate on mapy_affected_cache
  for each statement execute function mapy_inventory_immutable();

create trigger mapy_affected_props_immutable
  before update or delete or truncate on mapy_affected_props
  for each statement execute function mapy_inventory_immutable();

------------------------------------------------------------------
-- Posture: RLS on, browser roles dark, service-role only.
------------------------------------------------------------------
alter table mapy_inventory_runs  enable row level security;
alter table mapy_affected        enable row level security;
alter table mapy_affected_cache  enable row level security;
alter table mapy_affected_props  enable row level security;

revoke all on table mapy_inventory_runs, mapy_affected, mapy_affected_cache,
                    mapy_affected_props
  from anon, authenticated, public;
revoke all on sequence mapy_inventory_runs_id_seq from anon, authenticated, public;
revoke all on function mapy_inventory_immutable() from anon, authenticated, public;
