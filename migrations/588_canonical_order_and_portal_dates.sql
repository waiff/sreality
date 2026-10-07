-- 588_canonical_order_and_portal_dates.sql -- MERGE SPRINT W2a (docs/design/merge-sprint/PROGRAM.md
-- MS5, MS19). ADDITIVE: nothing is dropped, renamed or rewritten.
--
-- 1. Nine `properties.newest_ad_at_<portal>` dates, one per `toolkit.filter_registry.PORTAL_OPTIONS`
--    code (MS19): when the property's newest ad on that portal was first seen, active or not; NULL
--    with no ad there. The rollup (scripts/recompute_property_stats.py) writes them; W5 copies them
--    into `browse_list`. Nullable, no default, ONE ALTER: catalog-only, no table rewrite, no index.
-- 2. `property_canonical_listings(property_id)`, THE canonical order (MS5), with 561's signature
--    and ACL: active first; then a map point (`listing_location.geom` present); then, among active
--    ads, the earliest first sighting, among inactive ads the latest last sighting; then portal
--    trust; then id. 561's "most recently seen" key moved with every index sighting, so two live ads
--    swapped places on each recompute and restamped `repr_since`. first_seen_at never changes, and
--    an inactive ad's last_seen_at moves only when it is seen again, which revives it and dirties
--    its property. SQL, stable, invoker, no SET, not strict: still inlined into its one caller, the
--    rollup. `listing_location` holds one row per ad (its primary key), so the join adds no row.
-- 3. Comments: the rollup writes the two portal lists from now on (MS21); `source_trust_rank`
--    loses two false claims (a Python mirror, deleted with this wave, and the area fallback 561
--    removed).
--
-- APPLY BEFORE THE CODE MERGES (the rollup writes the nine columns with no fallback), and only
-- while no `rebuild_%` job runs: the ALTER needs a brief ACCESS EXCLUSIVE lock on `properties`,
-- which the */15 Browse rebuild reads for minutes. The 6 s lock limit gets that lock behind short
-- readers or fails, never stalling the table behind a long one; on a timeout re-run the file,
-- every statement is idempotent. From the apply on, the running rollup ranks by the new order.
-- Verify:
--   select count(*) from pg_attribute where attrelid = 'public.properties'::regclass
--      and attname like 'newest\_ad\_at\_%' and not attisdropped;                -- 9
--   select pg_get_functiondef('public.property_canonical_listings(bigint)'::regprocedure);

set lock_timeout = '6s';

alter table public.properties
  add column if not exists newest_ad_at_sreality     timestamptz,
  add column if not exists newest_ad_at_bazos        timestamptz,
  add column if not exists newest_ad_at_idnes        timestamptz,
  add column if not exists newest_ad_at_maxima       timestamptz,
  add column if not exists newest_ad_at_ceskereality timestamptz,
  add column if not exists newest_ad_at_bezrealitky  timestamptz,
  add column if not exists newest_ad_at_mmreality    timestamptz,
  add column if not exists newest_ad_at_remax        timestamptz,
  add column if not exists newest_ad_at_realitymix   timestamptz;

comment on column public.properties.newest_ad_at_sreality is 'MS19 (migration 588): first sighting of this property''s newest sreality ad, active or not; NULL with none. Written by the rollup.';
comment on column public.properties.newest_ad_at_bazos is 'MS19 (migration 588): first sighting of this property''s newest bazos ad, active or not; NULL with none. Written by the rollup.';
comment on column public.properties.newest_ad_at_idnes is 'MS19 (migration 588): first sighting of this property''s newest idnes ad, active or not; NULL with none. Written by the rollup.';
comment on column public.properties.newest_ad_at_maxima is 'MS19 (migration 588): first sighting of this property''s newest maxima ad, active or not; NULL with none. Written by the rollup.';
comment on column public.properties.newest_ad_at_ceskereality is 'MS19 (migration 588): first sighting of this property''s newest ceskereality ad, active or not; NULL with none. Written by the rollup.';
comment on column public.properties.newest_ad_at_bezrealitky is 'MS19 (migration 588): first sighting of this property''s newest bezrealitky ad, active or not; NULL with none. Written by the rollup.';
comment on column public.properties.newest_ad_at_mmreality is 'MS19 (migration 588): first sighting of this property''s newest mmreality ad, active or not; NULL with none. Written by the rollup.';
comment on column public.properties.newest_ad_at_remax is 'MS19 (migration 588): first sighting of this property''s newest remax ad, active or not; NULL with none. Written by the rollup.';
comment on column public.properties.newest_ad_at_realitymix is 'MS19 (migration 588): first sighting of this property''s newest realitymix ad, active or not; NULL with none. Written by the rollup.';

comment on column public.properties.all_sources is
  'Every portal this property has an ad on, sorted (MS19). Written by the rollup since migration '
  '588; a row it has not reached since (a merged-away property) may hold an older writer''s value.';
comment on column public.properties.active_sources is
  'The portals this property has an ACTIVE ad on, sorted; empty when none is active (MS19). '
  'Written by the rollup since migration 588.';

create or replace function public.property_canonical_listings(p_property_id bigint)
returns table (listing_id bigint, canonical_rank integer)
language sql
stable
as $$
  select l.id,
         (row_number() over (
            order by l.is_active desc,
                     (ll.geom is not null) desc,
                     case when l.is_active then l.first_seen_at end,
                     case when not l.is_active then l.last_seen_at end desc,
                     public.source_trust_rank(l.source),
                     l.id))::integer
    from public.listings l
    left join public.listing_location ll on ll.listing_id = l.id
   where l.property_id = p_property_id
$$;

revoke execute on function public.property_canonical_listings(bigint) from public, anon, authenticated;
grant execute on function public.property_canonical_listings(bigint) to service_role;

comment on function public.property_canonical_listings(bigint) is
  'MS5 (migration 588): THE canonical order of a property''s ads; rank 1 is the canonical ad. '
  'Active first; a map point; the earliest first sighting among active ads, the latest last '
  'sighting among inactive ones; portal trust; id. Its one caller is the property rollup.';

comment on function public.source_trust_rank(text) is
  'Per-portal trust order (lower = more trusted): a tie-break key of property_canonical_listings '
  '(migration 588) and of the paused condition-level propagation (toolkit/condition_scoring.py). '
  'Non-sensitive static logic, callable by all roles.';

reset lock_timeout;
