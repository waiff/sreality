-- 396: scope the broker ranking surfaces to Czech (domestic) inventory.
--
-- WHY. Two idnes syndication feeds advertise foreign stock through a single Czech
-- broker account: ibero-casa.com (Spain, 20,152 listings / 15,028 active) and
-- nemovitosti-chorvatsko.eu (Croatia, 5,007). They rank #1 and #2 on every surface
-- that sorts by `brokers.active_property_count` — 8x the busiest genuinely Czech
-- broker (1,862) — which makes the broker leaderboard, the broker search and the
-- outreach target list read as if a Marbella agent were the biggest player in the
-- Czech market. 38,197 of the 489,590 broker-attributed listings (7.8%) are
-- foreign, concentrated in 317 brokers that are >=90% foreign.
--
-- WHAT THIS IS NOT. Nothing is deleted, un-attributed or hidden (rule #3). Every
-- foreign listing keeps its broker_identity_id / broker_firm_id, still appears on
-- the broker's dossier and in broker_listings_public, and the existing
-- listing_count / property_count / active_* columns keep counting it. This adds a
-- PARALLEL set of cz_* counts and points the ranking surfaces at those, so the
-- foreign inventory stays visible as "of which N abroad" instead of vanishing.
--
-- THE SIGNAL. `listings.obec_id IS NULL`. The Czech admin hierarchy is derived
-- from `geom` by a BEFORE trigger (migration 140, nearest-within-250m fallback
-- since 289), so a foreign pin matches no CZ boundary and region/okres/obec all
-- stay NULL. Verified on the attributed corpus: of the 38,197 obec-less rows, 0
-- carry a region_id, and all 22,340 rows whose geom falls outside the CZ bbox have
-- all three NULL. It is the same predicate
-- docs/design/media-integrity-architecture.md §Q4 already committed the platform
-- to for the media/quality metric split — one definition of "foreign", not two.
-- It over-captures a few thousand domestic-but-ungeocoded rows (idnes ~2.2k,
-- ceskereality ~1.9k, realitymix ~1.6k); that is the conservative direction for a
-- ranking (an unplaceable listing does not prove market presence) and it self-heals
-- the moment the row gets coordinates.
--
-- Additive + narrowing. scripts/resolve_brokers.py writes the new columns from the
-- same predicate (`_DOMESTIC`); this migration backfills them once so the surfaces
-- are correct before the next daily sweep.

begin;

set local lock_timeout = '5s';

-- 1. Parallel CZ-scoped rollup columns. Same types/defaults as the originals.
alter table brokers
  add column if not exists cz_listing_count integer not null default 0,
  add column if not exists cz_property_count integer not null default 0,
  add column if not exists cz_active_listing_count integer not null default 0,
  add column if not exists cz_active_property_count integer not null default 0;

comment on column brokers.cz_active_property_count is
  'active_property_count restricted to listings that resolved to a Czech obec. '
  'The ranking column: foreign syndication feeds stay attributed but do not lead '
  'the leaderboard. Written by scripts.resolve_brokers._BROKER_ROLLUP.';

-- 2. Backfill from the live corpus (one pass; the resolver keeps them fresh after).
with lst as (
  select bi.broker_id,
    count(*) filter (where l.obec_id is not null) as cz_lc,
    count(distinct coalesce(l.property_id, -l.id))
      filter (where l.obec_id is not null) as cz_pc,
    count(*) filter (where l.obec_id is not null
      and l.is_active and l.last_seen_at > now() - interval '7 days') as cz_alc,
    count(distinct coalesce(l.property_id, -l.id)) filter (where l.obec_id is not null
      and l.is_active and l.last_seen_at > now() - interval '7 days') as cz_apc
  from listings l
  join broker_identities bi on bi.id = l.broker_identity_id
  where bi.broker_id is not null
  group by bi.broker_id
)
update brokers b set
  cz_listing_count = lst.cz_lc,
  cz_property_count = lst.cz_pc,
  cz_active_listing_count = lst.cz_alc,
  cz_active_property_count = lst.cz_apc
from lst where lst.broker_id = b.id;

-- 3. Widen brokers_public. CREATE OR REPLACE VIEW may only APPEND columns, so the
--    cz_* block sits after first_seen_at/last_seen_at rather than beside its
--    unscoped twins. Plain view, service-role only since migration 299 — no grants
--    to restore. Every consumer does `select *` into a dict, so position is inert.
create or replace view brokers_public as
 select b.id as broker_id,
    b.display_name,
    b.primary_email,
    b.primary_phone,
    b.primary_firm_id as firm_id,
    f.canonical_domain as firm_domain,
    f.display_name as firm_name,
    f.is_franchise as firm_is_franchise,
    b.source_count,
    b.distinct_source_count,
    b.listing_count,
    b.property_count,
    b.active_listing_count,
    b.active_property_count,
    b.first_seen_at,
    b.last_seen_at,
    b.cz_listing_count,
    b.cz_property_count,
    b.cz_active_listing_count,
    b.cz_active_property_count
   from brokers b
     left join firms f on f.id = b.primary_firm_id
  where b.status = 'active'::text;

-- 4. broker_region_type_stats: make the exclusion EXPLICIT rather than incidental.
--    The three per_level arms already drop a row whose region/okres/obec id is
--    NULL, so no foreign listing reaches the matview today (verified: this rebuild
--    changes 0 rows). That is an accident of the geo join, not a stated rule — a
--    later national / geo_level='all' arm would silently readmit every foreign
--    listing into the leaderboard. Stating the predicate once in `attributed`
--    makes the guarantee independent of the arms, and matches the cz_* columns
--    above so the two halves of the ranking can never diverge.
--    DROP + CREATE (a matview cannot be REPLACEd), mirroring migration 361.
drop view broker_geo_options;
drop materialized view broker_region_type_stats;

create materialized view broker_region_type_stats as
 with attributed as (
         select b.id as broker_id,
            l.region_id,
            l.okres_id,
            l.obec_id,
            coalesce(l.category_main, ''::text) as category_main,
            coalesce(l.category_type, ''::text) as category_type,
            coalesce(l.property_id, (- l.id)) as property_key,
            (l.is_active and (l.last_seen_at > (now() - '7 days'::interval))) as is_live
           from ((listings l
             join broker_identities bi on ((bi.id = l.broker_identity_id)))
             join brokers b on (((b.id = bi.broker_id) and (b.status = 'active'::text))))
          where (l.obec_id is not null)
        ), per_level as (
         select 'region'::text as geo_level,
            attributed.region_id as geo_id,
            attributed.broker_id,
            attributed.category_main,
            attributed.category_type,
            attributed.property_key,
            attributed.is_live
           from attributed
          where (attributed.region_id is not null)
        union all
         select 'okres'::text,
            attributed.okres_id,
            attributed.broker_id,
            attributed.category_main,
            attributed.category_type,
            attributed.property_key,
            attributed.is_live
           from attributed
          where (attributed.okres_id is not null)
        union all
         select 'obec'::text,
            attributed.obec_id,
            attributed.broker_id,
            attributed.category_main,
            attributed.category_type,
            attributed.property_key,
            attributed.is_live
           from attributed
          where (attributed.obec_id is not null)
        )
 select broker_id,
    geo_level,
    geo_id,
    category_main,
    category_type,
    count(*) as listing_count,
    count(distinct property_key) as property_count,
    count(*) filter (where is_live) as active_listing_count,
    count(distinct property_key) filter (where is_live) as active_property_count
   from per_level
  group by broker_id, geo_level, geo_id, category_main, category_type;

create unique index broker_region_type_stats_pk on broker_region_type_stats
  using btree (broker_id, geo_level, geo_id, category_main, category_type);
create index broker_region_type_stats_rank_idx on broker_region_type_stats
  using btree (geo_level, geo_id, category_main, category_type, active_property_count desc);
revoke all on broker_region_type_stats from anon, authenticated;

create view broker_geo_options as
select s.geo_level, s.geo_id, ab.name, ab.parent_id,
       count(distinct s.broker_id) as broker_count
from broker_region_type_stats s
join admin_boundaries ab on ab.id = s.geo_id
where s.geo_level in ('region', 'okres')
group by s.geo_level, s.geo_id, ab.name, ab.parent_id;
revoke all on broker_geo_options from anon, authenticated;

commit;
