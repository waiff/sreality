-- 262_listings_street_source.sql
-- Street provenance + the resolver-street lifecycle fix. The RÚIAN coord→street resolver
-- (resolve_coord_streets.yml) fills street/street_name_key/house_number on rows whose portal
-- page carries no street — and every detail refetch then CLOBBERED that fill back to NULL
-- (`street = EXCLUDED.street`; the page still has no street). Measured: 40% of a resolver
-- cohort lost in 2.5 days; only ~455 of ~4.9k ever-filled still held streets; ~4.6k active
-- rows uniquely resolvable but streetless. The resolver's old provenance was a raw_json
-- marker — destroyed by the same refetch that clobbered the street.
--
-- Three pieces (code side in scraper/db.py, same PR):
--   1. `street_source` ('parser' | 'resolver') — REAL provenance that survives refetches.
--      Ingest stamps 'parser' when the page yields a street, else preserves the stored value;
--      the resolver stamps 'resolver'.
--   2. Ingest upserts become preserve-if-null for the street trio (COALESCE(EXCLUDED.c, l.c)),
--      so an incoming NULL never erases a resolver fill; a page-parsed street still wins.
--   3. The admin-geo trigger gains the "wrong street is worse than NULL" guard: when a
--      listing's COORDINATES change, a 'resolver'-sourced street was derived from the OLD
--      point and may be wrong -> NULL the trio + provenance. The trigger's existing tail
--      block then re-opens the resolver (street NULL + geom changed -> attempt version NULL),
--      so the row re-resolves at the new coordinates. Parser streets are untouched (the page
--      re-derives them every fetch).

alter table listings add column if not exists street_source text
  check (street_source is null or street_source in ('parser', 'resolver'));

comment on column listings.street_source is
  'Provenance of listings.street: ''resolver'' = filled by the RÚIAN coord->street resolver; '
  '''parser'' = extracted from the portal page at ingest; NULL = parser-or-legacy (pre-262 '
  'rows; semantically identical to ''parser'' everywhere — the geom-change guard fires ONLY '
  'on ''resolver'', and each refetch that yields a page street stamps ''parser'' organically). '
  'Drives the preserve-if-null ingest carry-forward + the geom-change guard. Out of the '
  'content hash.';

-- Backfill ONLY the 'resolver' provenance (rows whose resolver raw_json marker survived,
-- narrowed by coord_street_attempt_version so the jsonb probe detoasts a tiny set). The
-- correctness-critical distinction is 'resolver' vs everything-else: the geom-change guard
-- and the preserve semantics key exclusively off 'resolver'. Legacy parser rows are
-- deliberately LEFT NULL (defined above as parser-equivalent) — a bulk 210k-row 'parser'
-- UPDATE deadlocked against the live detail drains for zero semantic gain; organic refetches
-- stamp 'parser' as pages re-yield streets. (Rows whose resolver marker was already destroyed
-- by a refetch re-fill + re-stamp via the post-deploy --force-rescan resolver pass.)
update listings
set street_source = 'resolver'
where coord_street_attempt_version is not null
  and street is not null and street <> ''
  and raw_json->>'coord_street_resolved' = 'true'
  and street_source is null;

-- Reproduced from the LIVE definition (pg_get_functiondef verified 2026-07-02 — includes the
-- streetless-coord-change attempt-version reset that migration 222's copy predates), with ONE
-- addition: the resolver-street geom-change guard (see header). Everything else byte-identical.
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

  -- Resolver-street guard (migration 262): the listing's coordinates CHANGED, so a street
  -- derived from the OLD point ('resolver' provenance) may now be wrong — and a wrong street
  -- is worse than NULL (it poisons the dedup street-key + Browse). Drop the trio; the tail
  -- block below re-opens the resolver for the new coordinates. Parser streets are kept —
  -- the page re-derives them on every fetch.
  if tg_op = 'UPDATE'
     and new.geom is distinct from old.geom
     and old.street_source = 'resolver' then
    new.street := null;
    new.street_name_key := null;
    new.house_number := null;
    new.street_source := null;
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

  if new.street is null
     and (tg_op = 'INSERT' or new.geom is distinct from old.geom) then
    new.coord_street_attempt_version := null;
  end if;

  return new;
end;
$function$;
