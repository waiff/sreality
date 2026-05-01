-- 001_initial.sql
-- Initial schema for the Sreality rental tracker.
--
-- Three tables:
--   listings           - current state of every listing we have ever seen
--   listing_snapshots  - append-only history, one row per content change
--   images             - image URLs only (we do not download files in v1)
--
-- Row Level Security is enabled on all tables. The scraper authenticates
-- with the service_role key, which bypasses RLS, so writes work without
-- any policies. The public anon key cannot read or write until policies
-- are added (which we will do when there is a frontend).
--
-- Run this once in the Supabase SQL editor against an empty database.

create extension if not exists postgis;

create table listings (
  sreality_id    bigint primary key,
  first_seen_at  timestamptz not null default now(),
  last_seen_at   timestamptz not null default now(),
  is_active      boolean     not null default true,
  category_main  text,
  category_type  text,
  price_czk      integer,
  price_unit     text,
  area_m2        numeric(7,1),
  disposition    text,
  locality       text,
  district       text,
  geom           geography(point, 4326),
  floor          integer,
  has_balcony    boolean,
  has_parking    boolean,
  has_lift       boolean,
  building_type  text,
  condition      text,
  energy_rating  text,
  raw_json       jsonb not null
);

create index on listings using gist (geom);
create index on listings (disposition, area_m2);
create index on listings (is_active, last_seen_at);
create index on listings (category_main, category_type);
create index on listings using gin (raw_json);

create table listing_snapshots (
  id            bigserial primary key,
  sreality_id   bigint references listings(sreality_id) on delete cascade,
  scraped_at    timestamptz not null default now(),
  price_czk     integer,
  content_hash  text not null,
  raw_json      jsonb not null
);

create index on listing_snapshots (sreality_id, scraped_at desc);
create index on listing_snapshots (content_hash);

create table images (
  id           bigserial primary key,
  sreality_id  bigint references listings(sreality_id) on delete cascade,
  sreality_url text not null,
  sequence     integer,
  unique (sreality_id, sequence)
);

alter table listings          enable row level security;
alter table listing_snapshots enable row level security;
alter table images            enable row level security;
