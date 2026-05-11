-- 025_curation_public_views.sql
-- Anon-readable views over the curation tables (migrations 022-024).
-- Pattern matches 008_ui_read_policies.sql: never expose raw tables to
-- the SPA; project only the columns the UI needs through dedicated
-- views and grant select on the views.
--
-- collections_public and tags_public include a computed listing_count
-- subquery so the SPA's index pages can show "N listings" without an
-- extra round-trip.
--
-- The listings_with_tags(tag_ids) RPC supports the Browse Filters
-- "tags" facet: AND-semantics across the supplied tag_ids (a listing
-- must carry every selected tag to qualify). The SPA composes the
-- result with the existing listings_public query via .in('sreality_id',
-- result). security invoker so anon's existing SELECT grants apply.
-- Capped at 5000 rows to bound the wire payload.

create view collections_public as
  select c.id, c.name, c.description, c.created_at, c.updated_at,
         (select count(*) from collection_listings cl
          where cl.collection_id = c.id) as listing_count
  from collections c;

create view collection_listings_public as
  select collection_id, sreality_id, added_at
  from collection_listings;

create view listing_notes_public as
  select id, sreality_id, body, created_at
  from listing_notes;

create view tags_public as
  select t.id, t.name, t.color, t.created_at,
         (select count(*) from listing_tags lt where lt.tag_id = t.id)
           as listing_count
  from tags t;

create view listing_tags_public as
  select sreality_id, tag_id, attached_at
  from listing_tags;

grant select on collections_public         to anon;
grant select on collection_listings_public to anon;
grant select on listing_notes_public       to anon;
grant select on tags_public                to anon;
grant select on listing_tags_public        to anon;

create or replace function listings_with_tags(tag_ids bigint[])
returns table (sreality_id bigint)
language sql
stable
security invoker
as $$
  select lt.sreality_id
  from listing_tags lt
  where lt.tag_id = any(tag_ids)
  group by lt.sreality_id
  having count(distinct lt.tag_id) = coalesce(array_length(tag_ids, 1), 0)
  limit 5000;
$$;

grant execute on function listings_with_tags(bigint[]) to anon;
