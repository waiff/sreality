-- 289_admin_geo_sliver_fallback.sql
-- Boundary-sliver fallback for the admin-geo PIP: a CZ point that no obec polygon
-- COVERS falls back to the NEAREST obec within 250 m (same for ku).
--
-- WHY (2026-07 dedup-blind-spots audit, re-verified): a small but real population of
-- genuinely-Czech listings carries geom but obec_id NULL because st_covers misses —
-- geocode jitter or polygon simplification puts the point just outside every obec
-- (confirmed live cases: Chýně, Vejprty ×2, Hostivice/Palouky, Karviná Staré Město,
-- Březník/Budeč/Hřebeč on pozemek; ~11 komerční rows sat within 250 m of an obec
-- polygon, ~100-140 dům candidates in-bbox). obec_id NULL starves the dedup geo cell
-- key, the geo-eligibility pass, Browse district filters, and the RÚIAN street
-- resolver (all require obec_id).
--
-- 250 m is deliberately tight: the confirmed slivers sit within it, while a genuinely
-- foreign point near the border (Žitava DE, Groß-Siegharts AT — both seen in the
-- audit) stays >250 m from any CZ obec polygon and keeps NULL. A real cross-border
-- property within 250 m of CZ territory would be mis-assigned — accepted: every
-- confirmed in-band case was a Czech property with an imprecise pin, and "no admin
-- hierarchy at all" costs more than a rare border-sliver assignment.
--
-- The fallback runs ONLY on the containment-miss path (v_obec_id NULL after the
-- st_covers probe), so the common case pays nothing; foreign points pay one extra
-- indexed ST_DWithin probe per geom-writing update (they already pay the full PIP —
-- their obec_id never satisfies the early-return).
--
-- Everything else is copied VERBATIM from migration 263's definition (early-return,
-- resolver-street drop-on-geom-change guard, district display fill, resolver
-- re-open tail).
--
-- BACKFILL (out-of-band, run right after applying — small, ~2-4k rows): re-fire the
-- trigger on the standing obec-less CZ-bbox rows; rows whose nearest obec is >250 m
-- stay NULL (genuinely foreign). No snapshots (no raw_json change); newly-located
-- properties are marked dirty for the maintenance mirror:
--
--   WITH upd AS (
--     UPDATE listings SET geom = geom
--     WHERE geom IS NOT NULL AND obec_id IS NULL
--       AND ST_Y(geom::geometry) BETWEEN 48.0 AND 51.5
--       AND ST_X(geom::geometry) BETWEEN 12.0 AND 19.0
--     RETURNING property_id, obec_id
--   )
--   INSERT INTO dirty_properties (property_id)
--   SELECT DISTINCT property_id FROM upd
--   WHERE property_id IS NOT NULL AND obec_id IS NOT NULL
--   ON CONFLICT DO NOTHING;

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

  -- Boundary-sliver fallback (migration 289): containment missed — geocode jitter /
  -- polygon simplification can put a genuinely-Czech point just outside every obec.
  -- Take the NEAREST obec within 250 m; farther than that = genuinely foreign, stay NULL.
  if v_obec_id is null then
    select ob.id, ob.name, ok.id, ok.name, kr.id, kr.name
      into v_obec_id, v_obec, v_okres_id, v_okres, v_kraj_id, v_kraj
    from admin_boundaries ob
    left join admin_boundaries ok on ok.id = ob.parent_id and ok.level = 'okres'
    left join admin_boundaries kr on kr.id = ok.parent_id and kr.level = 'kraj'
    where ob.level = 'obec'
      and st_dwithin(ob.geom, new.geom, 250)
    order by st_distance(ob.geom, new.geom)
    limit 1;
  end if;

  select b.id
    into v_ku_id
  from admin_boundaries b
  where b.level = 'ku'
    and st_covers(b.geom, new.geom)
  limit 1;

  -- Same sliver fallback for the cadastral territory (a point outside its obec
  -- polygon is outside its ku polygon for the same reason).
  if v_ku_id is null then
    select b.id
      into v_ku_id
    from admin_boundaries b
    where b.level = 'ku'
      and st_dwithin(b.geom, new.geom, 250)
    order by st_distance(b.geom, new.geom)
    limit 1;
  end if;

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
