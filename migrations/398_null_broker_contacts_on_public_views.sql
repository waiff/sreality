-- 398_null_broker_contacts_on_public_views.sql
--
-- PRIVACY. Stop serving advertised agent email + phone to every logged-in session.
--
-- WHAT IS EXPOSED TODAY. `listings_public` and `properties_public` are relkind='v',
-- owned by postgres, carry NO `security_invoker`, and hold a live SELECT grant to
-- `authenticated`. Owner rights + postgres's rolbypassrls means they read straight
-- through the deny-all RLS on base `listings`. Measured live as the real role
-- (`begin; set local role authenticated; ... rollback;`) on 2026-08-12:
--
--   listings_public    664,240 rows   48,489 broker_email / 48,457 broker_phone
--                                      8,023 distinct emails / 7,968 distinct phones
--   properties_public  543,314 rows   43,914 broker_email / 43,889 broker_phone
--                                      7,829 distinct emails / 7,778 distinct phones
--
-- Joined back to the broker directory, 19,921 of the 28,209 brokers that have a
-- `primary_email` (70.6%) have that exact address readable this way — i.e. most of
-- the contact set migration 395 / PR #1030's `apply_pii_policy` collapses into
-- has_email/has_phone booleans is one PostgREST query away for anyone holding any
-- account. `anon` is correctly refused ('permission denied for view listings_public').
--
-- THIS CONTRADICTS AN OPERATOR-CONFIRMED DEFERRAL — read before applying.
-- Migration 299 PART F names these two views EXPLICITLY and excludes them on
-- purpose, under Amendment A6 (operator-confirmed 2026-07-12):
--
--   "(brokers_public.primary_email/primary_phone is the aggregated broker contact
--    DB. The per-listing advertised agent contact on listings_public/
--    properties_public is a distinct, lesser exposure handled holistically by the
--    Wave-4 DPIA — NOT here.)"
--
-- docs/design/waves-1-4-public-features.md § Wave 4 keeps that DPIA "blocked on
-- legal". This migration CLOSES THAT DEFERRAL EARLY, before the DPIA rules on it,
-- because the aggregation exposure is live, bulk, and reachable by self-signup
-- (frontend/src/pages/Login.tsx ships a working supabase.auth.signUp). It is
-- therefore an operator decision, not an autonomous one.
--
-- 299 PART G's invariant is PRESERVED, not violated: PART G aborts unless
-- `authenticated` still holds SELECT on listings_public ("the SPA would break").
-- CREATE OR REPLACE VIEW preserves every grant, so the grant survives untouched —
-- re-asserted at the bottom of this file.
--
-- MECHANISM — why NULL rather than DROP, and why not column grants.
--   * The columns are KEPT and projected `null::text`. CREATE OR REPLACE VIEW
--     cannot remove a column, so dropping them would need DROP VIEW ... CASCADE.
--     Five materialized views depend on listings_public (verified via pg_depend on
--     2026-08-12): category_trends_mv, health_summary_mv, image_storage_overview_mv,
--     portal_health_mv, scraper_health_checks_mv. CASCADE would drop all five plus
--     their indexes and break their pg_cron refresh. (properties_public has zero
--     dependents, but is kept symmetric.) A recreated view would also come back
--     WITHOUT the authenticated grant, since 299 PART A revoked the postgres
--     default ACL in schema public — i.e. the exact SPA breakage PART G guards.
--   * Column-level grants are NOT used: frontend/src/lib/queries.ts `ping()` issues
--     `.select('*', { count: 'exact', head: true })` on listings_public, which 403s
--     under column privileges, and such a grant fails closed on every future column.
--   * Nulling keeps PostgREST answering `?select=broker_email` with nulls instead of
--     a 400, so an unknown consumer degrades instead of breaking.
--
-- NO CONSUMER LOSES DATA. `git grep broker_email/broker_phone` over frontend/src,
-- chrome-extension/, api/, toolkit/, scripts/, scraper/ finds only prose comments;
-- neither column appears in queries.ts's DETAIL_COLS/CARD_COLS/TABLE_COLS/MAP_COLS.
-- The broker resolver reads BASE `listings` as postgres, which is untouched here.
-- `broker_name` is kept: a label, not a contact.
--
-- PROD/GIT DRIFT THIS FILE MUST STEP AROUND (flag to the operator, pre-existing).
-- Migration 375's `properties_public` body ends with `p.home_city_id`, but the LIVE
-- view has 81 columns and stops at `l.source_id_native` — prod applied that file
-- under the ledger name `374_home_city_precompute` without the view's last column,
-- while `properties.home_city_id` and `recompute_home_city()` both exist live. A
-- from-zero replay therefore has 82 columns and prod has 81.
--
-- The reference really is unresolvable against the live view — verified read-only:
--   select 1 from properties_public l where l.home_city_id is not null limit 0;
--   ERROR: 42703: column l.home_city_id does not exist
--
-- But the drift is LATENT, not breaking anything today. Measured 2026-08-12, not
-- assumed: `toolkit/comparables._city_quality_clauses` names `home_city_id` from
-- exactly ONE branch, `city_index_rules`. The near-city-proximity branch builds its
-- point from `l.lng`/`l.lat` and never names the column; the population branch reads
-- `home_obec_pop`, which the 81-column view DOES carry. That helper's only
-- properties_public-grain caller is the watchdog matcher in api/notifications.py,
-- which selects `WHERE is_active = true`. Production's `notification_subscriptions`
-- holds exactly 2 rows, BOTH `is_active = false`, neither carrying city_index_rules
-- (one omits the key, the other stores JSON null) nor near_city_proximity. So the
-- affected population is EMPTY: nothing in production emits the column today. It
-- becomes load-bearing the moment a city-quality subscription is created, or one of
-- those two rows is reactivated carrying such a rule.
--
-- The restatement cannot be omitted regardless of that: CREATE OR REPLACE cannot
-- drop a column, so restating prod's 81-column shape would FAIL the CI schema
-- replay, which builds the 82-column shape from migration 375. This file therefore
-- restates 375's canonical 82-column body; applying it to production additionally
-- re-adds the missing trailing `home_city_id` column, which is purely additive and
-- converges prod back onto git.
--
-- Permission/definition-only. No data is modified and nothing is dropped.

begin;

-- CREATE OR REPLACE VIEW takes ACCESS EXCLUSIVE; listings_public is read by the
-- */10 pg_cron health-matview refresh. Fail fast rather than queue against it.
set local lock_timeout = '5s';

-- listings_public: migration 334's body verbatim, with the two contact columns
-- replaced by null::text. Every other column keeps its name, type and ordinal.
create or replace view public.listings_public as
 select sreality_id,
    first_seen_at,
    last_seen_at,
    is_active,
    category_main,
    category_type,
    price_czk,
    price_unit,
    area_m2,
    disposition,
    locality,
    district,
    locality_district_id,
    locality_region_id,
    st_y(geom::geometry) as lat,
    st_x(geom::geometry) as lng,
    floor,
    total_floors,
    has_balcony,
    has_parking,
    has_lift,
    building_type,
    condition,
    energy_rating,
    estate_area,
    usable_area,
    garden_area,
    category_sub_cb,
    furnished,
    terrace,
    cellar,
    garage,
    parking_lots,
    ownership,
    broker_name,
    null::text as broker_email,
    null::text as broker_phone,
        case
            when is_active then greatest(0, floor(extract(epoch from now() - first_seen_at) / 86400::numeric)::integer)
            else greatest(0, floor(extract(epoch from last_seen_at - first_seen_at) / 86400::numeric)::integer)
        end as tom_days,
        case
            when area_m2 is not null and area_m2 > 0::numeric and price_czk is not null then price_czk::numeric / area_m2::numeric
            else null::numeric
        end as price_per_m2,
    building_condition_level,
    apartment_condition_level,
    description,
    source,
    street,
    house_number,
    mf_reference_rent_czk,
    mf_gross_yield_pct,
    mf_reference_rent,
    obec,
    okres,
    region,
    subtype,
    obec_id,
    okres_id,
    region_id,
    id
   from listings;

-- properties_public: migration 375's body verbatim, same two columns nulled.
create or replace view public.properties_public as
select
    p.id as property_id,
    p.repr_listing_id as sreality_id,
    p.first_seen_at,
    p.last_seen_at,
    p.is_active,
    p.category_main,
    p.category_type,
    p.current_price_czk as price_czk,
    l.price_unit,
    p.area_m2,
    p.disposition,
    p.locality,
    p.district,
    p.locality_district_id,
    p.locality_region_id,
    p.lat,
    p.lng,
    l.floor,
    l.total_floors,
    p.has_balcony,
    p.has_parking,
    p.has_lift,
    p.building_type,
    p.condition,
    p.energy_rating,
    p.estate_area,
    p.usable_area,
    p.garden_area,
    p.category_sub_cb,
    p.furnished,
    p.terrace,
    p.cellar,
    p.garage,
    p.parking_lots,
    p.ownership,
    l.broker_name,
    null::text as broker_email,
    null::text as broker_phone,
    case
        when p.is_active then greatest(0, floor(extract(epoch from now() - p.first_seen_at) / 86400::numeric)::integer)
        else greatest(0, floor(extract(epoch from p.last_seen_at - p.first_seen_at) / 86400::numeric)::integer)
    end as tom_days,
    case
        when p.area_m2 is not null and p.area_m2 > 0::numeric and p.current_price_czk is not null then round(p.current_price_czk::numeric / p.area_m2, 2)
        else null::numeric
    end as price_per_m2,
    p.building_condition_level,
    p.apartment_condition_level,
    l.description,
    p.source_count,
    p.distinct_site_count,
    p.price_drop_count,
    p.price_rise_count,
    p.max_price_drop_pct,
    p.stats_computed_at,
    p.source,
    coalesce(p.street, l.street) as street,
    p.mf_reference_rent_czk,
    p.mf_gross_yield_pct,
    p.obec,
    p.okres,
    p.region,
    p.home_obec_pop,
    p.near_pop_5km,
    p.near_pop_15km,
    p.near_jobs_5km,
    p.near_jobs_15km,
    p.near_youth_5km,
    p.near_youth_15km,
    p.near_overall_5km,
    p.near_overall_15km,
    p.subtype,
    p.last_change_at,
    p.obec_id,
    p.okres_id,
    p.region_id,
    p.price_change_count,
    p.price_change_count_30d,
    p.price_change_count_90d,
    p.price_change_count_365d,
    p.total_price_change_pct,
    concat_ws(', '::text, p.street, p.locality) as place_search_text,
    p.asset_id,
    p.mf_reference_rent,
    p.published_at,
    p.repr_listing_ref_id as listing_id,
    l.source_id_native,
    p.home_city_id
from properties p
     left join listings l on l.id = p.repr_listing_ref_id
where p.status = 'active'::text
  and (not (select publication_gate_enabled()) or p.published_at is not null);

comment on view public.listings_public is
  'Shared-market listing read surface for the login-gated SPA. broker_email/'
  'broker_phone are retained as NULL-projected columns (migration 398) so '
  'PostgREST keeps answering ?select= for them; raw agent contacts are admin-only '
  'via the /brokers API. Closes migration 299 PART F''s Wave-4 DPIA deferral.';

comment on view public.properties_public is
  'Property-grain twin of listings_public. broker_email/broker_phone are '
  'NULL-projected (migration 398) for the same reason.';

-- Post-conditions: fail (and roll back) if either half did not take.
do $$
begin
  assert pg_get_viewdef('public.listings_public'::regclass, true) ~ 'NULL::text\s+AS\s+broker_email',
         'listings_public still projects a real broker_email';
  assert pg_get_viewdef('public.listings_public'::regclass, true) ~ 'NULL::text\s+AS\s+broker_phone',
         'listings_public still projects a real broker_phone';
  assert pg_get_viewdef('public.properties_public'::regclass, true) ~ 'NULL::text\s+AS\s+broker_email',
         'properties_public still projects a real broker_email';
  assert pg_get_viewdef('public.properties_public'::regclass, true) ~ 'NULL::text\s+AS\s+broker_phone',
         'properties_public still projects a real broker_phone';

  -- 299 PART G's invariant. Only meaningful where the Supabase default-ACL
  -- baseline exists; the CI schema-replay container has no such baseline (roles
  -- hold no grants), so skip it there exactly as 299 PART G does.
  if not exists (select 1 from pg_roles where rolname = 'supabase_admin') then
    raise notice '398: non-Supabase env — skipping the 299 PART G grant re-assertion';
    return;
  end if;
  assert has_table_privilege('authenticated','public.listings_public','SELECT'),
         'authenticated LOST listings_public SELECT — 299 PART G invariant broken, aborting';
  assert has_table_privilege('authenticated','public.properties_public','SELECT'),
         'authenticated LOST properties_public SELECT — the SPA would break, aborting';
  assert not has_table_privilege('anon','public.listings_public','SELECT'),
         'anon regained listings_public SELECT — 299 PART B regression';
end $$;

commit;
