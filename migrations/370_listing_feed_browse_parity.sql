-- 370_listing_feed_browse_parity.sql
--
-- Phase 2+3 follow-up (docs/design/portal-order-fidelity.md): make
-- `listing_feed_public` (migration 369) actually SERVICEABLE by Browse.
--
-- 369 shipped the read contract as a bare projection of `listings`. Wiring the
-- frontend onto it surfaced three gaps that make a naive
-- `.from('browse_list')` -> `.from('listing_feed_public')` swap impossible:
--
--   1. MISSING FILTER SURFACE. Browse's filter engine dispatches ~46 registry
--      columns plus a dozen hand-coded ones (queries.ts:applyFilters,
--      registryQueryBuilder.ts). Seven of them do not exist on `listings` at
--      all: place_search_text, tom_days, last_change_at, home_obec_pop +
--      the eight near_* proximity precomputes, the four price_change_count*
--      columns and total_price_change_pct. Against 369's view every one of
--      those filters is a PostgREST 42703 (`column does not exist`) — not a
--      silent no-op, a hard 400. The operator's directive for this follow-up
--      was explicit: the filtering engine does not change, only what it reads.
--      So the view has to carry the whole Browse column contract.
--
--   2. NO PUBLICATION GATE. `browse_list` is built from `browse_projection`,
--      which filters `properties.status = 'active'` AND the dedup-aware
--      publication gate (`publication_gate_enabled()` is TRUE in production;
--      12,784 active properties sit unpublished behind it as of 2026-08-04).
--      369's view has neither, so single-portal mode would have surfaced
--      listings Browse deliberately hides — the counts would jump for reasons
--      no operator could explain. The gate is reproduced here VERBATIM,
--      including the `(select ...)` wrapper that keeps it a once-per-query
--      InitPlan rather than a per-row call.
--
--   3. UNUSABLE SORT KEY. See the portal_sort_key note below.
--
-- `create or replace view` can only APPEND columns, so 369's 51 columns keep
-- their exact names/types/order and everything new lands at the end.
--
-- ---------------------------------------------------------------------------
-- portal_sort_key — why one text column instead of 369's three-column ORDER BY
-- ---------------------------------------------------------------------------
-- 369 specified `order by portal_date desc nulls last, discovery_seq desc
-- nulls last, id desc`. That order is CORRECT, and this migration does not
-- change it — it just makes it expressible as a single comparison.
--
-- The reason is the reader, not the database. Browse pages by KEYSET cursor
-- (frontend/src/lib/keyset.ts): each page is anchored to the concrete sort
-- value of the previous page's last row, and PostgREST can only express that
-- anchor as an `or=()` disjunction. For one nullable sort column plus a
-- tiebreak that is already three disjuncts and a two-phase NULL cursor. For
-- TWO nullable sort columns plus a tiebreak it becomes a nested six-disjunct
-- tree with four cursor phases — the exact "needs live pagination testing"
-- risk 369's own frontend spec flagged, and the class of bug (a page boundary
-- that silently skips or repeats rows) that is nearly invisible in review.
--
-- `portal_sort_key` collapses the pair into one fixed-width, NOT NULL,
-- digits-only string whose byte order is IDENTICAL to the three-column order:
--
--     <12 digits: portal_date as UTC epoch seconds><19 digits: discovery_seq>
--
--   * a NULL portal_date renders '000000000000', which sorts LAST under DESC
--     — exactly `nulls last`. For the seven sources where portal_date is NULL
--     for the entire result set that prefix is constant, so discovery_seq
--     becomes the effective key with no per-portal branching, precisely as 369
--     intended.
--   * a NULL discovery_seq (every pre-368 row) renders 19 zeros and likewise
--     sorts last within its date.
--   * equal portal_date falls through to discovery_seq, equal both falls
--     through to the `id` tiebreak in the ORDER BY — same as the 3-column form.
--
-- Keyset then needs only its existing, proven single-column-plus-tiebreak
-- machinery, and the column being NOT NULL means it skips the `OR col IS NULL`
-- disjunct that would otherwise defeat the index.
--
-- IMMUTABILITY (this is the whole reason for the epoch + `at time zone 'UTC'`
-- dance rather than the obvious `to_char(portal_date, 'YYYYMMDD')`): an
-- expression index requires an IMMUTABLE expression, and BOTH to_char
-- timestamp overloads are only STABLE — verified against pg_proc.provolatile
-- on this database, they are 's' for `to_char(timestamptz, text)` AND for
-- `to_char(timestamp, text)` (the latter reads DateStyle/lc_time). What IS
-- immutable: `timezone('UTC', timestamptz) -> timestamp` ('i'), and
-- `extract(epoch from timestamp)` — the plain-timestamp overload, unlike the
-- timestamptz one which is STABLE because it depends on the session TimeZone.
-- So the date half is rendered as UTC epoch seconds. Encoding the full instant
-- rather than truncating to a day also means the UTC normalisation can never
-- reorder two rows across a local day boundary — the key tracks the real
-- instant, and equal instants fall through to discovery_seq exactly as the
-- three-column form does.
--
-- `greatest(0, ...)` does double duty: GREATEST ignores NULLs, so a NULL
-- portal_date collapses to 0 (the sorts-last prefix) with no separate
-- coalesce, and a nonsensical pre-1970 published_at — which would otherwise
-- render a negative number and break lexicographic order outright — is
-- clamped to the same sorts-last bucket instead of corrupting the ordering.
-- 12 digits holds epoch seconds through the year 33658.
--
-- COLLATE "C" is explicit, not incidental: it pins byte ordering (so the
-- digit string compares numerically regardless of the database's default
-- collation) and makes every comparison a memcmp. The index below repeats the
-- expression verbatim, collation included, so the planner matches it.

create or replace view listing_feed_public as
select
  l.id,
  l.source,
  l.source_id_native,
  l.sreality_id,                          -- legacy compat only; NULL post-Gate-2 for non-sreality
  l.category_main,
  l.category_type,
  l.category_sub_cb,
  l.subtype,
  l.disposition,
  l.price_czk,
  l.price_unit,
  l.area_m2,
  l.locality,
  l.district,
  l.obec,
  l.okres,
  l.region,
  l.obec_id,
  l.okres_id,
  l.region_id,
  st_y(l.geom::geometry) as lat,
  st_x(l.geom::geometry) as lng,
  l.floor,
  l.total_floors,
  l.has_balcony,
  l.has_parking,
  l.has_lift,
  l.building_type,
  l.condition,
  l.energy_rating,
  l.estate_area,
  l.usable_area,
  l.garden_area,
  l.furnished,
  l.terrace,
  l.cellar,
  l.garage,
  l.parking_lots,
  l.ownership,
  l.building_condition_level,
  l.apartment_condition_level,
  l.description,
  l.street,
  l.house_number,
  l.is_active,
  l.first_seen_at,
  l.last_seen_at,
  l.published_at,
  l.discovery_seq,
  case when l.source in ('bazos', 'ceskereality') then l.published_at end as portal_date,
  -- Qualified `l.` (369 left these bare): the `properties` join added below
  -- brings its own area_m2/price_czk into scope, so the unqualified form is now
  -- ambiguous. Same expression, same output column — only the resolution is pinned.
  case when l.area_m2 is not null and l.area_m2 > 0::numeric and l.price_czk is not null
       then l.price_czk::numeric / l.area_m2::numeric
       else null::numeric end as price_per_m2,
  -- ---- appended by migration 370 ----
  -- `listing_id` is a deliberate alias of `id`, not redundancy: every Browse
  -- SELECT list, the React key, the maplibre feature-state id, the hover-sync
  -- set and the card-image batch all key on `listing_id` because that is what
  -- browse_list / properties_map_mv call the same surrogate (migration 343).
  -- Aliasing it here means the frontend's column contract is IDENTICAL across
  -- both read models and the single-portal swap touches only the relation name.
  l.id as listing_id,
  -- Carried so operator curation (tags, collections, pipeline, merge) keeps
  -- working from a mirrored card, and as the detail-link fallback. NULLABLE by
  -- rule #19 (a freshly drained listing is attached out-of-band), which is why
  -- it must NOT be the keyset tiebreak in this mode — `listing_id` is.
  l.property_id,
  (
    lpad(
      greatest(0, floor(extract(epoch from
        (case when l.source in ('bazos', 'ceskereality') then l.published_at end)
          at time zone 'UTC'
      )))::bigint::text,
      12, '0'
    )
    || lpad(coalesce(l.discovery_seq, 0)::text, 19, '0')
  ) collate "C" as portal_sort_key,
  -- Listing-grain equivalents of two browse_projection expressions. Both are
  -- pure functions of columns already on the row, so they are exact at this
  -- grain rather than the property-grain approximation browse_list carries.
  concat_ws(', '::text, l.street, l.locality) as place_search_text,
  case when l.is_active
       then greatest(0, floor(extract(epoch from now() - l.first_seen_at) / 86400::numeric)::integer)
       else greatest(0, floor(extract(epoch from l.last_seen_at - l.first_seen_at) / 86400::numeric)::integer)
  end as tom_days,
  l.mf_gross_yield_pct,
  -- Property-grain precomputes. These exist ONLY so the corresponding Browse
  -- filters keep working unchanged in single-portal mode; none of them is
  -- displayed on a card, so root cause 3's golden-record leakage (a DISPLAYED
  -- field lifted from a different portal's sibling listing) cannot occur
  -- through them. They are genuinely property-grain facts by construction:
  -- city proximity/population is a function of location, and the price-change
  -- and last-change series are maintained per property.
  p.last_change_at,
  p.home_obec_pop,
  p.near_pop_5km, p.near_pop_15km,
  p.near_jobs_5km, p.near_jobs_15km,
  p.near_youth_5km, p.near_youth_15km,
  p.near_overall_5km, p.near_overall_15km,
  p.price_change_count,
  p.price_change_count_30d,
  p.price_change_count_90d,
  p.price_change_count_365d,
  p.total_price_change_pct
from listings l
join properties p on p.id = l.property_id
where p.status = 'active'
  and (not (select publication_gate_enabled()) or p.published_at is not null);

comment on view listing_feed_public is
  'Listing-grain, single-portal Browse read surface (migrations 369 + 370). '
  'Every displayed column is the filtered listing''s OWN row — never a '
  'trust-blended golden record (rule #15 / root cause 3). Carries the full '
  'Browse filter contract and the same publication gate as browse_projection, '
  'so single-portal mode changes only WHICH rows are read, never how they are '
  'filtered. Order by portal_sort_key desc, listing_id desc.';

grant select on listing_feed_public to authenticated;

-- The serving index for the intended shape: filter `source` (single-portal
-- mode always pins exactly one), sort by portal_sort_key, tiebreak `id`.
--
-- `is_active` is deliberately NOT a leading column even though most reads want
-- active-only: Browse's default status filter is 'any' (filters.ts:282), and a
-- leading column the query does not constrain breaks the index's usefulness for
-- the trailing sort columns. As a plain filter on an index scan it costs almost
-- nothing — inactive rows are a minority within any one portal's slice.
create index listings_portal_feed_idx on listings (
  source,
  (
    (
      lpad(
        greatest(0, floor(extract(epoch from
          (case when source in ('bazos', 'ceskereality') then published_at end)
            at time zone 'UTC'
        )))::bigint::text,
        12, '0'
      )
      || lpad(coalesce(discovery_seq, 0)::text, 19, '0')
    ) collate "C"
  ) desc,
  id desc
);

-- The publication-gate join's serving index. Without it the gate probe reads
-- the `properties` HEAP once per candidate row, which is fine for a 24-row card
-- page and ruinous for the map's 50k-point fetch: measured on the largest
-- portal (idnes, ~110k active listings, no other filter) the map query ran
-- 6.86s — over the anon 3s statement timeout — with ~206k buffers spent almost
-- entirely on 52k random heap probes. Making the probe an INDEX-ONLY scan by
-- covering the two gate columns took the same query to 1.52s. Every mirror-mode
-- surface pays this join, so the index earns its ~15 MB on the card and count
-- paths too.
--
-- Applied live with CREATE INDEX CONCURRENTLY (a 450k-row table under constant
-- write load); written non-concurrently here because migrations run inside a
-- transaction, with IF NOT EXISTS so this replays as a no-op against the
-- database that already has it.
create index if not exists properties_gate_cover_idx
  on properties (id) include (status, published_at);

-- 369's `listings_feed_sort_idx` served the three-column ORDER BY that
-- portal_sort_key replaces; no shipped reader ever issued that query shape (the
-- frontend wiring was explicitly deferred to this follow-up), so this drops a
-- never-used index rather than changing any live plan. Pruning it in a forward
-- migration is the sanctioned way to retire schema (rule #1) — 369 stays intact
-- on disk and still replays cleanly.
drop index if exists listings_feed_sort_idx;
