-- 302: denormalize `place_search_text` onto `properties` for direct-table
-- (non-view) location filtering.
--
-- WHY: the dedup Decision history + manual review Queue (api/property_dedup.py)
-- query `properties` directly via psycopg (they need the raw `geom`/id-keyed
-- columns `_PROP_SIDE_SQL` reads, and a pair-grain join `properties_public`
-- can't express), not through `properties_public`. The shared location-chip
-- builder (`api/location_filter.district_where`, extracted from Watchdog's
-- matcher) needs a `place_search_text` column on every alias it's pointed at,
-- for the 'locality' (street-pick) and legacy (unresolved chip) branches --
-- exactly the column `properties_public` already computes (migration 183:
-- `concat_ws(', ', coalesce(p.street, l.street), p.locality)`), but that
-- computation doesn't exist on the bare table.
--
-- FIX: `properties.street` is already the group-best street, denormalized by
-- `recompute_property_stats` (migration 183) -- the `coalesce(p.street,
-- l.street)` in the view is a defensive fallback to the representative
-- listing for rows the recompute hasn't touched yet, not a second source of
-- truth. So a STORED GENERATED column computed straight from `properties`'
-- own `street` + `locality` reproduces the view's value for every row that's
-- been through a recompute (i.e. everything but a brand-new, not-yet-computed
-- property) -- self-maintaining, no recompute_property_stats change needed.
--
-- Scope: `district_where` still only reads `place_search_text` on the two
-- dedup surfaces (aliases against `properties` directly); Browse/Watchdog
-- keep reading `properties_public`'s own computation unchanged.

-- concat_ws() is STABLE (not IMMUTABLE — it relies on type-output functions),
-- so Postgres rejects it in a generated column. This CASE is the immutable
-- equivalent of concat_ws(', ', street, locality): join both with ', ' when
-- present, fall back to whichever one is non-null, else '' (concat_ws itself
-- returns '', not NULL, when every argument is NULL — matched here for exact
-- parity with properties_public.place_search_text).
alter table properties
  add column if not exists place_search_text text
  generated always as (
    case
      when street is null and locality is null then ''
      when street is null then locality
      when locality is null then street
      else street || ', ' || locality
    end
  ) stored;

comment on column public.properties.place_search_text is
  'Free-text place words for location-chip matching (group-best street + locality, '', ''-joined) -- the properties_public view''s recipe (migration 183), denormalized onto the base table for direct queries (dedup Decision history + Queue). Matching-only -- not a display field.';
