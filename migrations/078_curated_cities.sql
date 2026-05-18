-- 078_curated_cities.sql
--
-- Phase QUAL — qualitative city indexes + population overlay (Browse +
-- Watchdog only; deliberately not exposed to the estimation agent or
-- comparables tooling per the registry-driven agenda gating in
-- toolkit/filter_registry.py).
--
-- Schema only. Seed data lands via the operator-triggered
-- "Seed curated cities" GitHub Action (see
-- .github/workflows/seed-curated-cities.yml), which geocodes the 206
-- cities in data/obce_v_datech_2025.csv via Mapy.cz and inserts.
--
-- Storage model:
--   curated_cities          — the 206 operator-curated city rows
--                             (name + kraj + centroid + per-city radius)
--   city_index_revisions    — append-only audit of each CSV upload
--   city_index_values       — long-form (city_id, revision, index, value)
--                             so a new index column doesn't need a migration
--   city_index_definitions  — slug → Czech/English label + scale domain;
--                             drives the Browse filter dropdown + the
--                             map color-coding legend
--   city_population         — one row per (city, year); latest exposed
--                             via the *_public view's lateral subquery
--
-- All five tables RLS-on; anon reads only via the three _public views.

set local lock_timeout = '5s';

create table curated_cities (
  id                bigserial primary key,
  name              text not null,
  kraj_name         text not null,
  centroid          geography(point, 4326) not null,
  default_radius_m  integer not null default 5000,
  source            text not null default 'mapy_cz',
  source_confidence text,
  created_at        timestamptz not null default now(),
  unique (name, kraj_name)
);

create index curated_cities_centroid_idx on curated_cities using gist (centroid);
create index curated_cities_kraj_idx on curated_cities (kraj_name);


create table city_index_revisions (
  source_revision  bigserial primary key,
  uploaded_at      timestamptz not null default now(),
  uploaded_by      text,
  source_filename  text not null,
  row_count        integer not null,
  raw_rows         jsonb not null
);


create table city_index_values (
  city_id          bigint not null references curated_cities(id) on delete cascade,
  source_revision  bigint not null references city_index_revisions(source_revision) on delete cascade,
  index_name       text not null,
  value            numeric not null,
  primary key (city_id, source_revision, index_name)
);

create index city_index_values_lookup_idx
  on city_index_values (index_name, value)
  include (city_id, source_revision);


create table city_index_definitions (
  index_name       text primary key,
  label_cs         text not null,
  label_en         text,
  category         text not null
    check (category in ('overall', 'health_env', 'material_edu',
                        'services_relations', 'sub_index')),
  scale_min        numeric not null default 0,
  scale_max        numeric not null default 10,
  higher_is_better boolean not null default true,
  sort_order       integer not null default 0,
  description      text
);


create table city_population (
  city_id          bigint not null references curated_cities(id) on delete cascade,
  as_of_year       integer not null,
  population       integer not null,
  source           text not null default 'csu',
  loaded_at        timestamptz not null default now(),
  primary key (city_id, as_of_year)
);

create index city_population_year_idx on city_population (as_of_year desc);


-- --- public read views (anon) ----------------------------------------------
-- Same _public pattern as migration 008: anon reads through views, base
-- tables stay RLS-on with no anon policy. The matcher and Browse RPC
-- read from the public views so they share the same latest-revision
-- semantics as the frontend.

create view curated_cities_public as
  select
    c.id                              as city_id,
    c.name,
    c.kraj_name,
    st_y(c.centroid::geometry)        as lat,
    st_x(c.centroid::geometry)        as lng,
    c.default_radius_m,
    p.population,
    p.as_of_year                      as population_as_of_year
  from curated_cities c
  left join lateral (
    select population, as_of_year
      from city_population
     where city_id = c.id
     order by as_of_year desc
     limit 1
  ) p on true;


create view city_index_values_public as
  select v.city_id, v.index_name, v.value, v.source_revision
    from city_index_values v
   where v.source_revision = (select max(source_revision) from city_index_values);


create view city_index_definitions_public as
  select index_name, label_cs, label_en, category,
         scale_min, scale_max, higher_is_better, sort_order, description
    from city_index_definitions
   order by sort_order, index_name;


alter table curated_cities         enable row level security;
alter table city_index_revisions   enable row level security;
alter table city_index_values      enable row level security;
alter table city_index_definitions enable row level security;
alter table city_population        enable row level security;

grant select on curated_cities_public         to anon;
grant select on city_index_values_public      to anon;
grant select on city_index_definitions_public to anon;
