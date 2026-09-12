-- 501_location_w2a_listing_location.sql
--
-- Location-data programme, wave W2-a: the ONE answer table the four-step
-- resolver writes (CLAUDE.md rule 25). ADDITIVE ONLY - it creates
-- `listing_location` and touches nothing else. `listing_location_current` and
-- `property_location_current` stay exactly as they are, frozen and readable,
-- until W2-b cuts their five readers and drops them.
--
-- WHY A NEW TABLE RATHER THAN AN ALTER. `listing_location_current` is 81
-- columns over ~756k rows, continuously rewritten by the drain and read by a
-- pg_cron job every 30 minutes. Dropping 54 columns in place means a long
-- ACCESS EXCLUSIVE window on a hot table, which is a shape this repo has been
-- bitten by before (hot-table DDL, `lock_timeout` never queues). Only the
-- resolver writes the projection and only five modules read it, so building
-- beside it and swapping once is strictly cheaper and reversible until the
-- drop.
--
-- THE 26 COLUMNS, and why each is here:
--   1  identity     listing_id
--   1  position     geom (NULL when the listing has no usable coordinate)
--   9  names        country_code, kraj/okres/obec/cast_obce name, street_name,
--                   house_number_cp, house_number_co, psc
--   6  registry ids kraj/okres/obec/cast_obce/ulice kod + ruian_adm_kod
--   3  grade        match_confidence, granularity, uncertainty_radius_m
--   2  status       country_status, disputed
--   4  housekeeping resolver_version, resolved_at, claim_set_hash,
--                   registry_version
--
-- What is deliberately NOT here:
--   * `source` - join `listings`. It was a verbatim copy of a column that
--     already exists one primary-key join away.
--   * a display label, `place_search_text`, `admin_path`, the blocking keys -
--     derived at READ in W3. A stored derivation is a second definition.
--   * `position_source` / `blur_evidence` / `radius_semantics` /
--     `position_licence_class` / the pin-collision block / the four
--     `*_unit_id` surrogates / momc/ku/pou/orp - producers deleted with the
--     policy tables, the collision epoch and the contradiction ledger, or
--     provably NULL on every row.
--   * `pin_shared_by_n`. It was in the 27 the wave opened with, and it came out
--     for the reason rule 25 exists: its producer WAS the pin-collision epoch,
--     which this wave deletes, so the column would have shipped writing 0 on
--     every row forever. The shared-pin count is a read-time aggregate
--     (`count(*) over (partition by geom)`), and W3 computes it in the
--     `browse_list` rebuild, where the map is the thing that needs it.
--   * the licence rail as a COLUMN. It moves to the claim read: the resolver
--     selects only claims with `licence_class IN ('portal','operator')`, so a
--     Mapy-class coordinate can never reach a position at all. Pinned by
--     `tests/location_data/test_resolver_jobs.py`.
--
-- `disputed` is ONE nullable text column whose VALUE is the reason (NULL =
-- clean), never a boolean plus a reason pair - the pair can disagree with
-- itself. The CHECK keeps it one lower_snake word so it stays groupable.
--
-- Backend/service-role only: RLS on, Supabase's default anon/authenticated ACL
-- revoked at the foot of the file (this project auto-GRANTs both on every new
-- table).

begin;

create table listing_location (
  -- identity
  listing_id            bigint primary key,

  -- position: one point, NULL when the listing has none. The radius below is
  -- what says how much to trust it; there is no second position column.
  geom                  geometry(Point, 4326),

  -- names. NULL means "unknown", never "none" - the admin names are ALWAYS the
  -- RUIAN chain's own spelling, never a portal's.
  country_code          text,
  kraj_name             text,
  okres_name            text,
  obec_name             text,
  cast_obce_name        text,
  street_name           text,
  house_number_cp       text,
  house_number_co       text,
  psc                   text,

  -- registry identity. NULL means "not bound to that level".
  kraj_kod              bigint,
  okres_kod             bigint,
  obec_kod              bigint,
  cast_obce_kod         bigint,
  ulice_kod             bigint,
  ruian_adm_kod         bigint,

  -- grade: all three NOT NULL, because a NULL reads as "no gate" and fails
  -- open - a NULL radius makes both branches of the three-valued containment
  -- test evaluate NULL, so the row silently drops out of `certain` AND
  -- `possible` (01 section 7 / 05 P5).
  match_confidence      match_confidence not null,
  granularity           location_granularity not null,
  uncertainty_radius_m  numeric not null,

  -- status. `country_status` NOT NULL because foreign is a DETERMINATION the
  -- resolver makes and `undetermined` is a value - never a default for "no
  -- town found" (rule 25).
  country_status        country_status not null,
  disputed              text constraint listing_location_disputed_word
                          check (disputed is null or disputed ~ '^[a-z][a-z0-9_]*$'),

  -- housekeeping. The three version inputs are what the sweep compares to
  -- decide a row is stale, and `claim_set_hash` is what says the inputs
  -- themselves moved.
  resolver_version      text not null,
  resolved_at           timestamptz not null default now(),
  claim_set_hash        bytea not null,
  registry_version      text not null
);
alter table listing_location enable row level security;

-- The one cohort read every consumer makes: "the listings in this town, at or
-- above this precision". Granularity is the second column, never an ordinal
-- comparison inside a predicate (01 section 0.4) - callers compare
-- `location_granularity_rank.rank`.
create index listing_location_obec_granularity on listing_location (obec_kod, granularity);

-- Map/bbox reads.
create index listing_location_geom_gist on listing_location using gist (geom);

revoke all on listing_location from anon, authenticated;

commit;
