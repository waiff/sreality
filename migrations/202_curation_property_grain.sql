-- 202: re-key curation (collections membership, tags, notes) from listing grain
-- (sreality_id) to PROPERTY grain (property_id).
--
-- WHY: a tag like "nice", a collection membership, or a note is a fact about the
-- real-world PROPERTY, not about one portal's advert of it. Keyed on a listing
-- (the old grain) curation was dedup-unstable: the Browse tag filter matched only
-- a property's REPRESENTATIVE listing (see browse_stats_properties / the
-- properties_public.sreality_id prefilter), so a property tagged on a
-- non-representative sibling was silently missed. Re-keyed on property_id,
-- curation describes the property and follows it across merge/unmerge/split via
-- the property_identity reconciler (the merge re-points it onto the survivor),
-- so it can never orphan onto a merged_away property or leak onto an unrelated one.
--
-- The curation tables are EMPTY in production (verified 0 rows on every table),
-- so this is a clean re-key with NO backfill. The collections / tags PARENT
-- tables are grain-agnostic and unchanged; only the membership/annotation
-- children move to property_id. Notes keep an origin_listing_id for display
-- provenance ("written while viewing this advert") — it is not a grouping key.
--
-- This migration is purely ADDITIVE (new tables/views/RPC). Migration 203 retires
-- the listing-grain tables once browse_stats_properties is repointed, so at no
-- point is a live reader left referencing a dropped relation.

-- new property-grain children -------------------------------------------------

create table collection_properties (
  collection_id bigint      not null references collections(id) on delete cascade,
  property_id   bigint      not null references properties(id)  on delete cascade,
  added_at      timestamptz not null default now(),
  primary key (collection_id, property_id)
);
create index collection_properties_by_property on collection_properties (property_id);

create table property_tags (
  property_id bigint      not null references properties(id) on delete cascade,
  tag_id      bigint      not null references tags(id)       on delete cascade,
  attached_at timestamptz not null default now(),
  primary key (property_id, tag_id)
);
create index property_tags_by_tag on property_tags (tag_id);

create table property_notes (
  id                bigserial   primary key,
  property_id       bigint      not null references properties(id) on delete cascade,
  body              text        not null check (length(body) between 1 and 4000),
  origin_listing_id bigint      references listings(sreality_id) on delete set null,
  created_at        timestamptz not null default now()
);
create index property_notes_by_property on property_notes (property_id, created_at desc);

alter table collection_properties enable row level security;
alter table property_tags         enable row level security;
alter table property_notes        enable row level security;

-- property-grain public views (anon read) -------------------------------------

create view collection_properties_public as
  select collection_id, property_id, added_at
  from collection_properties;

create view property_tags_public as
  select property_id, tag_id, attached_at
  from property_tags;

create view property_notes_public as
  select id, property_id, body, origin_listing_id, created_at
  from property_notes;

-- The parent-view count subqueries now count properties (the curation grain).
-- The column keeps the name `listing_count` so the existing API/UI binding is
-- unchanged; semantically it is now a property count.
create or replace view collections_public as
  select c.id, c.name, c.description, c.created_at, c.updated_at,
         (select count(*) from collection_properties cp
          where cp.collection_id = c.id) as listing_count
  from collections c;

create or replace view tags_public as
  select t.id, t.name, t.color, t.created_at,
         (select count(*) from property_tags pt where pt.tag_id = t.id)
           as listing_count
  from tags t;

-- property-grain tag filter RPC (replaces listings_with_tags) -----------------
-- Properties carrying ALL supplied tags (AND semantics), directly at property
-- grain — no representative-listing indirection, so it is dedup-stable.
create or replace function properties_with_tags(tag_ids bigint[])
returns table (property_id bigint)
language sql
stable
security invoker
as $$
  select pt.property_id
  from property_tags pt
  where pt.tag_id = any(tag_ids)
  group by pt.property_id
  having count(distinct pt.tag_id) = coalesce(array_length(tag_ids, 1), 0)
  limit 5000;
$$;

grant select on collection_properties_public to anon;
grant select on property_tags_public          to anon;
grant select on property_notes_public          to anon;
grant execute on function properties_with_tags(bigint[]) to anon;
