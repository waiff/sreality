-- 511_w4_backup_before_drops.sql
--
-- rule 1 backup; the operator drops schema backup_w4 once W4 has been live for a week.
--
-- WHAT THIS IS. Migrations 506 (W3 S4) and 508 (W4-c) are the sprint's two
-- DESTRUCTIVE applies: between them they drop three `properties` columns, 16
-- more off `properties`, 24 off `listings`, and seven whole tables. CLAUDE.md
-- rule 1 gates a destructive migration on a backup. This file IS that backup,
-- taken in the database rather than as a pg_dump file so it is restorable with
-- an INSERT ... SELECT and cannot be misplaced: every dropped column is copied
-- into a table keyed by `id` under `backup_w4`, and every dropped TABLE is
-- copied there whole.
--
-- COPIED, NOT MOVED. `alter table ... set schema` would be the cheap way to
-- preserve the seven tables, and it is the wrong one HERE: this file is applied
-- BEFORE 506, while the Mapy veto and the address-point reader are still live on
-- `main` (W4-b and W4-c remove them), and a table renamed out from under a
-- running reader is an outage, not a backup. The seven are 325 MB in total, so
-- copying costs a minute and nothing else. 508 then drops the originals exactly
-- as it is written, and the copies remain.
--
-- IT IS NUMBERED AFTER THE MIGRATIONS IT PROTECTS, which is a hazard this file
-- has to handle rather than a mistake: on production it runs first (the applies
-- are ordered by hand), but a fresh schema replay runs 506 and 508 BEFORE it,
-- and by then every column named here is gone. So each copy is built from the
-- catalog at run time — the column list is the declared list INTERSECTED with
-- what the table still has, inside `execute`, and a source that has nothing
-- left logs a notice instead of failing. The same shape makes the file
-- idempotent: an existing backup table is never rebuilt or appended to.
--
-- THE BUDGET. `listings` is ~830k rows and its 24 columns include `geom`; the
-- role default of 120 s is not enough, so the session raises it to 900 s.
-- Postgres arms statement_timeout when the top-level statement begins, which is
-- why it is set here and not inside the DO block.
--
-- ci-allow-dynamic: backup_w4.* — every CREATE TABLE here is built with
-- `execute format(...)` because the column list and even the source table may be
-- gone by the time the file runs (see the numbering note above). The tables it
-- creates live in `backup_w4`, which is revoked from anon and authenticated at
-- the end of this file, so the statement scanner's blind spot cannot leak a row:
-- nothing it builds is in `public` and nothing it builds is granted.
--
-- NOT GRANTED TO ANYONE. The backup holds the same rows the public tables hold,
-- so anon and authenticated are revoked on the schema and on everything in it;
-- only the owner (the service role) reads it.

set statement_timeout = '900s';

create schema if not exists backup_w4;

revoke all on schema backup_w4 from anon, authenticated;

-- ---------------------------------------------------------------------------
-- 1. The dropped COLUMNS: 506's three off `properties`, then 508's 24 off
--    `listings` and 16 off `properties`. `place_search_text` is in both
--    property backups on purpose — 506 drops it and 508 re-declares the drop,
--    and a backup that assumes an apply order is not a backup.
-- ---------------------------------------------------------------------------

do $$
declare
  spec record;
  cols text;
begin
  for spec in
    select * from (values
      ('properties_cols_506', 'properties',
       array['place_search_text', 'home_city_id', 'home_city_computed_at']),
      ('listings_cols_508', 'listings',
       array['geom', 'obec_id', 'okres_id', 'region_id', 'ku_id',
             'locality', 'district', 'obec', 'okres', 'region',
             'street', 'house_number', 'zip', 'street_id',
             'locality_municipality_id', 'locality_quarter_id', 'locality_ward_id',
             'locality_district_id', 'locality_region_id',
             'street_name_key', 'street_source', 'geo_cell_key',
             'coord_street_attempt_version', 'geocode_attempted_at']),
      ('properties_cols_508', 'properties',
       array['place_search_text', 'geom', 'lat', 'lng',
             'district', 'locality', 'street', 'obec', 'okres', 'region',
             'obec_id', 'okres_id', 'region_id',
             'locality_district_id', 'locality_region_id', 'ku_id'])
    ) as v(backup_name, source, columns)
  loop
    if to_regclass('backup_w4.' || spec.backup_name) is not null then
      raise notice 'backup_w4.% already present; skipped', spec.backup_name;
      continue;
    end if;

    select string_agg(quote_ident(a.attname), ', ' order by w.ord)
      into cols
      from unnest(spec.columns) with ordinality as w(name, ord)
      join pg_attribute a
        on a.attrelid = ('public.' || spec.source)::regclass
       and a.attname = w.name
       and not a.attisdropped;

    if cols is null then
      raise notice 'public.% has none of the % legacy columns left; nothing to back up',
        spec.source, array_length(spec.columns, 1);
      continue;
    end if;

    execute format('create table backup_w4.%I as select id, %s from public.%I',
                   spec.backup_name, cols, spec.source);
    raise notice 'backup_w4.% created from public.%', spec.backup_name, spec.source;
  end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- 2. The seven TABLES migration 508 drops, copied whole.
-- ---------------------------------------------------------------------------

do $$
declare
  t text;
begin
  foreach t in array array['geocode_cache', 'mapy_affected_cache', 'mapy_affected_props',
                           'mapy_affected', 'mapy_inventory_runs',
                           'address_points_revisions', 'address_points']
  loop
    if to_regclass('backup_w4.' || t) is not null then
      raise notice 'backup_w4.% already present; skipped', t;
    elsif to_regclass('public.' || t) is null then
      raise notice 'public.% is already gone; nothing to back up', t;
    else
      execute format('create table backup_w4.%I as select * from public.%I', t, t);
      raise notice 'backup_w4.% copied', t;
    end if;
  end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- 3. Nobody but the owner reads the backup, and the backup is not empty while
--    the source still has rows (the arm that makes this a GATE and not a
--    gesture -- on an empty replay database both sides are zero and it passes).
-- ---------------------------------------------------------------------------

revoke all on all tables in schema backup_w4 from anon, authenticated;

do $$
declare
  pair record;
  n_src bigint;
  n_bak bigint;
  n_tab int;
begin
  select count(*) into n_tab from pg_class c
    join pg_namespace ns on ns.oid = c.relnamespace
   where ns.nspname = 'backup_w4' and c.relkind = 'r';
  raise notice 'backup_w4 holds % tables', n_tab;

  for pair in
    select * from (values ('listings_cols_508', 'listings'),
                          ('properties_cols_508', 'properties'),
                          ('properties_cols_506', 'properties')) as v(backup_name, source)
  loop
    execute format('select count(*) from public.%I', pair.source) into n_src;
    if to_regclass('backup_w4.' || pair.backup_name) is null then
      n_bak := 0;
    else
      execute format('select count(*) from backup_w4.%I', pair.backup_name) into n_bak;
    end if;
    raise notice 'backup_w4.%: % rows backed up of % live in public.%',
      pair.backup_name, n_bak, n_src, pair.source;
    -- A replay database is empty on both sides and passes; a production apply
    -- with rows on the left and nothing on the right is the gate firing.
    if n_src > 0 and n_bak = 0 then
      raise exception 'backup_w4.% is empty while public.% has % rows -- do NOT apply 506 or 508',
        pair.backup_name, pair.source, n_src;
    end if;
  end loop;
end
$$;
