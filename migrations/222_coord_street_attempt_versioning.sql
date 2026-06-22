-- 222: version-gate the RÚIAN coordinate->street resolver so it stops re-scanning
-- its permanently-unresolvable backlog every run.
--
-- WHY: scripts.backfill_address_point_streets used to record a marker only on a
-- SUCCESSFUL match, so a candidate that produced no match (no RÚIAN point within
-- tolerance, ambiguous, or a town-centre pin) stayed street-NULL AND unmarked and
-- was re-probed against the 1.57M-point address_points table on EVERY weekly run.
-- Live: ~93k candidates carried no marker; 54% were `pozemek` (land parcels that
-- structurally have no building street) and a large slice sit in genuinely
-- streetless municipalities -- all permanent no-matches, re-scanned forever. That
-- long scan is also what kept the resolver's (now removed) single transaction open
-- long enough to deadlock against the */15 listings writers.
--
-- THE FIX (this migration's half): record the dataset version each candidate was
-- LAST attempted against, and only re-attempt when the address_points dataset
-- actually advances (the one event that can change a no-match outcome) -- the same
-- "invalidate when the evidence changes" discipline as the snapshot-keyed LLM
-- caches. A typed integer column (NOT a raw_json marker) so the candidate scan
-- stays index-cheap: migration 184 already proved that deref-ing a jsonb marker
-- per candidate is exactly what blows the statement timeout.

-- Provenance for the address_points mirror, mirroring rent_map_revisions /
-- city_index_revisions. Bumped at the end of each successful ingest
-- (scripts.ingest_address_points), guarded on source_date so re-running the same
-- published RÚIAN month does not trigger a pointless full re-attempt. Until now
-- the wholesale TRUNCATE+reload recorded no provenance at all -- this fixes that.
create table if not exists address_points_revisions (
  revision     bigserial primary key,
  source_date  date,                                  -- the RÚIAN YYYYMMDD published date (NULL = pre-provenance baseline)
  refreshed_at timestamptz not null default now(),
  row_count    integer,
  obec_count   integer
);

comment on table address_points_revisions is
  'One row per address_points wholesale refresh (scripts.ingest_address_points). '
  'revision is the version the resolver stamps onto attempted listings; a new row '
  'is appended only when source_date advances. Mirror of the rent_map_revisions pattern.';

-- Seed a baseline so the resolver has a current version before the first
-- provenance-aware ingest runs. Reflects whatever the last (pre-provenance)
-- ingest already loaded; source_date NULL so the next real ingest always bumps.
insert into address_points_revisions (source_date, row_count, obec_count)
select null::date, count(*), count(distinct obec_id) from address_points;

-- The per-listing attempt stamp. NULL = never attempted (or invalidated by a geom
-- change, see the trigger below) -> in the resolver's candidate pool. A value
-- equal to the current revision -> already attempted against today's dataset ->
-- skipped until the dataset advances. The candidate scan is already served by
-- migration 184's partial index (source, sreality_id) WHERE street IS NULL; this
-- column is an extra cheap filter on the fetched rows, so no new index is needed.
alter table listings
  add column if not exists coord_street_attempt_version integer;

comment on column listings.coord_street_attempt_version is
  'address_points_revisions.revision the RÚIAN coord->street resolver last attempted '
  'this row against (scripts.backfill_address_point_streets). NULL = pending. Cleared '
  'on a geom change by listings_set_admin_geo() so a refined coordinate is re-attempted.';


-- Re-emit the geo trigger with ONE addition: when a row's coordinate changes,
-- clear the resolver's attempt stamp if the row still has no street, because a
-- refined coordinate (e.g. a town-centre pin replaced by a real building point)
-- can turn a previous no-match into a clean match. The body is otherwise the
-- CURRENT live function VERBATIM — migrations 162 (obec_id) and 171
-- (okres_id/region_id/ku_id) evolved it past the 140 original, so it must be
-- reproduced as-is, not from 140, or those join keys would stop being derived.
create or replace function public.listings_set_admin_geo()
returns trigger
language plpgsql
as $function$
declare
  v_obec     text;
  v_okres    text;
  v_kraj     text;
  v_obec_id  bigint;
  v_okres_id bigint;
  v_kraj_id  bigint;
  v_ku_id    bigint;
begin
  if new.geom is null then
    return new;
  end if;

  if tg_op = 'UPDATE'
     and new.geom is not distinct from old.geom
     and new.okres is not null
     and new.obec_id is not null
     and new.okres_id is not null
     and new.ku_id is not null then
    return new;
  end if;

  select ob.id, ob.name, ok.id, ok.name, kr.id, kr.name
    into v_obec_id, v_obec, v_okres_id, v_okres, v_kraj_id, v_kraj
  from admin_boundaries ob
  left join admin_boundaries ok on ok.id = ob.parent_id and ok.level = 'okres'
  left join admin_boundaries kr on kr.id = ok.parent_id and kr.level = 'kraj'
  where ob.level = 'obec'
    and st_covers(ob.geom, new.geom)
  limit 1;

  select b.id
    into v_ku_id
  from admin_boundaries b
  where b.level = 'ku'
    and st_covers(b.geom, new.geom)
  limit 1;

  new.obec      := v_obec;
  new.okres     := v_okres;
  new.region    := v_kraj;
  new.obec_id   := v_obec_id;
  new.okres_id  := v_okres_id;
  new.region_id := v_kraj_id;
  new.ku_id     := v_ku_id;

  if new.district is null then
    if v_kraj = 'Hlavní město Praha' then
      new.district := v_obec;
    elsif v_okres is not null then
      new.district := 'okres ' || v_okres;
    end if;
  end if;

  -- A changed coordinate can turn a previously-unresolvable RÚIAN street match
  -- resolvable, so re-open the row for the resolver. Gated on a genuine geom
  -- change (or INSERT) so an unchanged-point refetch that only skipped the fast
  -- path on a NULL id does not needlessly clear the stamp. Never touches a row
  -- that already has a street.
  if new.street is null
     and (tg_op = 'INSERT' or new.geom is distinct from old.geom) then
    new.coord_street_attempt_version := null;
  end if;

  return new;
end;
$function$;

drop trigger if exists trg_listings_admin_geo on listings;
create trigger trg_listings_admin_geo
  before insert or update of geom on listings
  for each row
  when (new.geom is not null)
  execute function public.listings_set_admin_geo();
