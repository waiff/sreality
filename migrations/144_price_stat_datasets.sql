-- 144_price_stat_datasets.sql
--
-- Price-stats datasets: the aggregate market statistics sreality publishes at
-- /ceny-nemovitosti (NOT individual listings). A "dataset" is one named filter
-- set (e.g. byty / velmi dobrý / panel / osobní / 30–80 m²); each ingestion run
-- fetches the per-municipality monthly series for BOTH prodej (1) and pronájem
-- (2). Source + design: docs/design/price-stats-datasets.md.
--
-- Tables:
--   price_stat_datasets    — one row per filter set. Columns mirror the
--                            estate_prices API params; category_type is NOT
--                            stored (a dataset always covers both 1 and 2).
--   price_stat_localities  — resolved-entity cache (localities/suggest output).
--                            obec_id is the RÚIAN municipality (admin_boundaries
--                            .id) found by PIP on the entity coordinate — the
--                            geo join for the map + city-quality features.
--   price_stat_runs        — one row per ingestion run (bookkeeping).
--   price_stat_observations — latest-wins per-month fact, snapshot-light (the
--                            monthly aggregates sreality publishes effectively
--                            never revise once a month closes).
--
-- Base tables RLS-on; anon reads only through the _public views.

set local lock_timeout = '5s';

create table price_stat_datasets (
  id                 bigserial primary key,
  slug               text not null unique,
  name               text not null,
  description        text,
  category_main_cb   integer not null default 1,
  building_condition text,
  building_type      text,
  ownership          text,
  usable_area_from   integer,
  usable_area_to     integer,
  distance           integer not null default 0,
  is_active          boolean not null default true,
  created_by         text,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);


create table price_stat_localities (
  entity_type           text not null,
  entity_id             integer not null,
  name                  text not null,
  municipality_id       integer,
  municipality_seo_name text,
  district              text,
  district_id           integer,
  district_seo_name     text,
  region                text,
  region_id             integer,
  region_seo_name       text,
  lat                   double precision,
  lon                   double precision,
  geom                  geography(point, 4326),
  obec_id               bigint references admin_boundaries(id),
  resolved_at           timestamptz not null default now(),
  primary key (entity_type, entity_id)
);

create index price_stat_localities_obec_idx on price_stat_localities (obec_id);


create table price_stat_runs (
  id            bigserial primary key,
  dataset_id    bigint references price_stat_datasets(id) on delete cascade,
  status        text not null default 'running'
                  check (status in ('running', 'success', 'failed')),
  localities    integer not null default 0,
  observations  integer not null default 0,
  error         text,
  started_at    timestamptz not null default now(),
  finished_at   timestamptz
);


create table price_stat_observations (
  dataset_id        bigint not null
                      references price_stat_datasets(id) on delete cascade,
  entity_type       text not null,
  entity_id         integer not null,
  category_type_cb  smallint not null check (category_type_cb in (1, 2)),
  year              smallint not null,
  month             smallint not null check (month between 1 and 12),
  price             integer,
  active_count      integer,
  new_count         integer,
  deleted_count     integer,
  run_id            bigint references price_stat_runs(id) on delete set null,
  fetched_at        timestamptz not null default now(),
  primary key (dataset_id, entity_type, entity_id, category_type_cb, year, month),
  foreign key (entity_type, entity_id)
    references price_stat_localities (entity_type, entity_id)
);

create index price_stat_obs_dataset_cat_idx
  on price_stat_observations (dataset_id, category_type_cb);


-- --- public read views (anon) ----------------------------------------------

create view price_stat_datasets_public as
  select id, slug, name, description, category_main_cb, building_condition,
         building_type, ownership, usable_area_from, usable_area_to, distance,
         is_active, created_at, updated_at
    from price_stat_datasets
   where is_active;

-- Per-city monthly series for the drill-down chart (filtered by dataset+entity
-- on read, so the PK index keeps it well under the anon statement timeout).
create view price_stat_observations_public as
  select o.dataset_id, o.entity_type, o.entity_id, l.name as locality_name,
         l.obec_id, o.category_type_cb, o.year, o.month, o.price,
         o.active_count, o.new_count, o.deleted_count
    from price_stat_observations o
    join price_stat_localities l
      on l.entity_type = o.entity_type and l.entity_id = o.entity_id;


alter table price_stat_datasets     enable row level security;
alter table price_stat_localities   enable row level security;
alter table price_stat_runs         enable row level security;
alter table price_stat_observations enable row level security;

grant select on price_stat_datasets_public      to anon;
grant select on price_stat_observations_public  to anon;
