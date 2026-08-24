import { supabase } from './supabase';
import { fetchAllRows } from './fetchAllRows';
import {
  composePipelineCards,
  PIPELINE_BOARD_COLS,
  type PipelineBoardRow,
} from './pipelineBoardModel';
import type { ListingBroker } from './brokers';
import type { LlmCostDailyRow, LlmCostHourlyRow } from './llmCosts';
import {
  type CenterRadius,
  type DistrictChip,
  type ListingFilters,
  type MapBounds,
  type PipelineScope,
  buildingMaterialToValues,
  isoNDaysAgo,
  priceChangeCountColumn,
  UNKNOWN_FILTER_VALUE,
  FURNISHED_CANONICAL,
  OWNERSHIP_CANONICAL,
} from './filters';
import { applyRegistryFilters } from './registryQueryBuilder';
import {
  applyKeyset,
  nextCursorFrom,
  withKeysetColumns,
  type KeysetBuilder,
  type KeysetCursor,
} from './keyset';
import { fetchGrowth } from './priceStats';
import type {
  BrowseReadModelState,
  CategoryTrend,
  HealthSummary,
  ImageFailureRow,
  ImagePublic,
  ImageStorageOverview,
  ListingFreshnessCheckPublic,
  ListingPublic,
  ListingSnapshotPublic,
  MfReferenceRent,
  PipelineCardBroker,
  PortalHealth,
  PropertySource,
  PropertyStatusEventPublic,
  Ppm2Box,
  ScrapeRun,
  ScraperHealthChecks,
} from './types';
import type { BorderCase, ImageAnnotation, TrainingExample } from './api';

/* Circle → bounding box approximation. Used when the operator picks
 * the centre+radius mode on the map: PostgREST has no native
 * ST_DWithin filter, so we send the bounding box of the radius
 * circle as the spatial predicate. The bbox is slightly oversized
 * versus the true circle (a square circumscribes a circle), which
 * means a few extra listings near the corners can slip into the
 * cohort — acceptable for the headline use case; true distance
 * filtering belongs in a follow-up RPC if it ever matters. */
const EARTH_RADIUS_M = 6_371_000;

const centerRadiusBbox = (cr: CenterRadius): MapBounds => {
  const dLat = (cr.radius_m / EARTH_RADIUS_M) * (180 / Math.PI);
  const dLng =
    (cr.radius_m / (EARTH_RADIUS_M * Math.cos((cr.lat * Math.PI) / 180))) *
    (180 / Math.PI);
  return {
    south: cr.lat - dLat,
    north: cr.lat + dLat,
    west: cr.lng - dLng,
    east: cr.lng + dLng,
  };
};

/** Returns the bbox the cohort filter should apply for a given
 *  filters object. Honours `locationMode`: viewport → use bounds
 *  (or null); center_radius → derive bbox from centerRadius (or null
 *  if no centre is set). The caller doesn't have to branch. */
export const effectiveBbox = (f: ListingFilters): MapBounds | null => {
  if (f.locationMode === 'center_radius') {
    return f.centerRadius ? centerRadiusBbox(f.centerRadius) : null;
  }
  return f.bounds;
};

/* Maplibre-gl renders a GeoJSON source via WebGL with clustering, so
 * the bottleneck is wire-bytes, not DOM. 50k features ≈ 0.3 MB gzipped. */
export const MAP_CAP = 50_000;
export const TABLE_PAGE_SIZE = 50;
export const CARD_PAGE_SIZE = 24;

/* Every Browse row carries `listing_id` (the surrogate = the repr child's
 * listings.id, migration 343 on browse_list / properties_map_mv) — the stable,
 * NEVER-NULL identity used for React keys, the maplibre feature-state id, and
 * the hover-sync set. The detail link is CANONICAL-first: `source` +
 * `source_id_native` (migration 362 exposed the latter on the Browse read path)
 * build `/listing/{source}/{native}`, so the URL bar never flashes the negative
 * synthetic id. `sreality_id` stays selected for the legacy fallback (and as a
 * sort field) but is NULLABLE post-Gate-2, so it must never be a key/feature id.
 * The map has no keyset tiebreaker column, so it carries `property_id` explicitly
 * (Table/Cards get it from withKeysetColumns / CARD_COLS) for the final null-safe
 * `?property=` detail-link fallback. */
const MAP_COLS = 'listing_id,property_id,sreality_id,source,source_id_native,lat,lng,price_czk,disposition,subtype,area_m2,district,last_seen_at,is_active,tom_days';
/* `property_id` is listed explicitly rather than arriving via withKeysetColumns:
 * it used to come free because the tiebreak was ALWAYS property_id, but the
 * portal-mirror lane tiebreaks on listing_id, which would have left
 * TableRow.property_id undefined at runtime while still typed `number`. */
const TABLE_COLS =
  'listing_id,property_id,sreality_id,source,source_id_native,district,locality,obec,okres,street,disposition,subtype,area_m2,price_czk,first_seen_at,last_seen_at,is_active,tom_days,' +
  'estate_area,usable_area,parking_lots,furnished,ownership,category_sub_cb,building_type,total_price_change_pct,price_change_count';
const CARD_COLS =
  'listing_id,property_id,sreality_id,source,source_id_native,district,locality,obec,okres,street,disposition,subtype,area_m2,price_czk,first_seen_at,last_seen_at,is_active,tom_days,' +
  /* The two price-history columns back <PriceDelta>. Both were already on
   * browse_list (migrations 276/343/363) and simply never selected — they
   * existed only as filter inputs, never as anything displayed. */
  'category_main,category_type,mf_gross_yield_pct,total_price_change_pct,price_change_count';

export type SortField =
  | 'sreality_id' | 'district' | 'disposition'
  | 'area_m2' | 'price_czk' | 'price_per_m2'
  | 'first_seen_at' | 'last_seen_at' | 'is_active'
  | 'estate_area' | 'usable_area' | 'parking_lots'
  | 'mf_gross_yield_pct'
  /* Portal-mirror only, never user-selectable and never in a URL — derived by
   * portalMirrorSort() below. See the PORTAL MIRROR block. */
  | 'portal_sort_key';

export type SortDirection = 'asc' | 'desc';

export interface SortSpec {
  field: SortField;
  direction: SortDirection;
}

/* first_seen_at DESC = "newest listings first" (operator decision 2026-07-07,
 * with the browse_list read model): meaningful (genuinely new listings, not
 * "whichever portal the scraper touched last" — touch_listings bumps
 * last_seen_at market-wide every cycle) and IMMUTABLE, so keyset cursors stay
 * valid across snapshot rebuilds. last_seen_at remains a selectable option. */
export const DEFAULT_SORT: SortSpec = { field: 'first_seen_at', direction: 'desc' };

/* sreality_id stays in SortField (it's still a selected/displayed column) but
 * is intentionally absent here: it mixes real positive ids with synthetic
 * negative ones, so sorting by it is meaningless. Saved presets / URLs that
 * still carry sort=sreality_id fall back to DEFAULT_SORT via parseSort. */
const SORTABLE_FIELDS: ReadonlyArray<SortField> = [
  'district', 'disposition',
  'area_m2', 'price_czk', 'price_per_m2',
  'first_seen_at', 'last_seen_at', 'is_active',
  'estate_area', 'usable_area', 'parking_lots',
  'mf_gross_yield_pct',
];

export const parseSort = (raw: string | null): SortSpec => {
  if (!raw) return DEFAULT_SORT;
  const direction: SortDirection = raw.startsWith('-') ? 'desc' : 'asc';
  const field = (raw.startsWith('-') ? raw.slice(1) : raw) as SortField;
  if (!(SORTABLE_FIELDS as ReadonlyArray<string>).includes(field)) return DEFAULT_SORT;
  return { field, direction };
};

export const sortToParam = (s: SortSpec): string =>
  `${s.direction === 'desc' ? '-' : ''}${s.field}`;

/* -------------------------------------------------------------------------- */
/* PORTAL MIRROR — filter to exactly one portal and Browse mirrors that        */
/* portal's own page (docs/design/portal-order-fidelity.md, migrations 368-370)*/
/* -------------------------------------------------------------------------- */
/* The default Browse read models are PROPERTY-grain (rule #15): one row per
 * real-world property, its displayed fields assembled from whichever child
 * listing wins a trust rank. That is the right model for the market-wide view
 * and the wrong one for "show me portal X's page", in two ways that are both
 * measured, not theoretical (live, 2026-08-04):
 *
 *   - MISSING ROWS. The portal filter constrains `properties.source`, i.e. the
 *     REPRESENTATIVE child's portal — so a property whose repr is sreality is
 *     invisible under `portal = idnes` even when it has a perfectly good active
 *     idnes listing. That hides 23,429 of the 109,034 properties with an active
 *     idnes listing (21%); 19% for ceskereality and realitymix, 10% for bazos.
 *   - WRONG FIELDS. Even for a row that IS shown, `area_m2` / `district` /
 *     `street` / `condition` / `ownership` come from golden-record CTEs that
 *     rank source trust ABOVE activity, so a card under "portal = bazos" can
 *     display area and location lifted from a DELISTED sreality sibling
 *     (root cause 3 in the design doc).
 *
 * With exactly one portal selected, every cohort surface switches to the
 * listing-grain `listing_feed_public` (migrations 369 + 370) instead. That view
 * carries the SAME filter columns and the SAME publication gate as
 * browse_projection, so nothing about the filter engine changes — only which
 * relation it reads. Two portals or none keeps today's deduped property view,
 * which is exactly what dedup exists for.
 *
 * Deliberately NOT changed: the Stats tab. It is a property-grain RPC
 * (browse_stats_properties); mirroring it needs a listing-grain twin, tracked
 * as a follow-up in the design doc rather than half-done here. */
export const portalMirrorSource = (f: ListingFilters): string | null =>
  f.portals.length === 1 ? f.portals[0] : null;

export const isPortalMirror = (f: ListingFilters): boolean =>
  portalMirrorSource(f) != null;

/* Relation names, in one place so a fetcher cannot read one grain and count
 * another. */
const PORTAL_FEED_RELATION = 'listing_feed_public';
const BROWSE_LIST_RELATION = 'browse_list';
const MAP_RELATION = 'properties_map_mv';

const listRelation = (f: ListingFilters): string =>
  isPortalMirror(f) ? PORTAL_FEED_RELATION : BROWSE_LIST_RELATION;

const mapRelation = (f: ListingFilters): string =>
  isPortalMirror(f) ? PORTAL_FEED_RELATION : MAP_RELATION;

/* Keyset tiebreak — REQUIRED to differ per grain, not a stylistic choice.
 * `property_id` is not unique on the listing-grain feed: 7,951 properties hold
 * more than one active listing on a single portal (18,521 rows, live
 * 2026-08-04). A keyset cursor anchored on a non-unique column skips or
 * repeats rows at page boundaries, and the same value used as a React row key
 * would collapse those rows out of the list entirely. `listing_id` (the
 * surrogate `listings.id`) is unique and never null on both read models. */
export const keysetTiebreak = (f: ListingFilters): string =>
  isPortalMirror(f) ? 'listing_id' : 'property_id';

/* "Newest first" means something different once we are mirroring a portal: not
 * "newest in OUR archive" (first_seen_at, stamped at batched detail-drain write
 * time and therefore scrambled — root cause 2) but "newest on THAT portal".
 * `portal_sort_key` (migration 370) is the single fixed-width column encoding
 * `portal_date desc nulls last, discovery_seq desc nulls last`, verified
 * order-identical to that pair. Every other sort field the UI offers exists on
 * the feed with listing-grain semantics and is passed through untouched. */
export const effectiveSort = (f: ListingFilters, sort: SortSpec): SortSpec =>
  isPortalMirror(f) && sort.field === 'first_seen_at'
    ? { field: 'portal_sort_key', direction: sort.direction }
    : sort;

/* Escape a literal user-supplied substring for embedding in a
 * PostgREST `or=(...)` clause as the right-hand side of `ilike`.
 * Reserved chars: `*` (wildcard), `,` (clause separator), `(` `)`
 * (grouping), `"` (quote), `\` (escape). Wrap in quotes and escape
 * the breakouts. Mapy.cz suggestion names are usually clean Czech
 * place names, but some POI names include parentheses. */
const escapeIlikePattern = (raw: string): string => {
  const escaped = raw
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"')
    .replace(/\*/g, '\\*')
    .replace(/,/g, '\\,')
    .replace(/\(/g, '\\(')
    .replace(/\)/g, '\\)');
  return `"*${escaped}*"`;
};

/* PostgREST `or=(...)` predicate for the location chips, or null when no
 * chips are set. Each chip resolves to a STABLE ADMIN ID at the level the
 * user picked (migration 171/172): an obec pick matches `obec_id`, an okres
 * pick `okres_id`, a kraj pick `region_id` — so picking obec "Jihlava" can't
 * collide with its same-named okres. A `locality` pick (street / POI /
 * address) matches its containing `obec_id` AND an ILIKE on
 * `place_search_text` (street + locality, migration 182 — bazos stores the
 * street outside `locality`, so bare `locality` would miss it), narrowing a
 * street to its municipality without dragging in same-named streets
 * elsewhere. A legacy / unresolved chip (no level/id — a pre-resolution
 * saved filter, or a point that matched no admin unit) falls back to the
 * name ILIKE across district/place_search_text/okres/region with an
 * optional parent-municipality context narrow.
 *
 * Chips split by `excluded`: INCLUDE chips are OR'd (match any), then
 * AND'd with NOT-(OR of the EXCLUDE chips) so an excluded locality is
 * subtracted from the cohort. Combined into a single `and(...)` tree so
 * PostgREST AND's the two groups. Kept in lockstep with the watchdog
 * matcher (`_build_match_clauses`) and browse_stats (migration 182),
 * which apply the same per-chip predicate + include/exclude split. */
export const districtsFilterClause = (districts: DistrictChip[]): string | null => {
  if (!districts.length) return null;
  const ID_COL: Record<string, string> = {
    obec: 'obec_id', okres: 'okres_id', kraj: 'region_id',
  };
  const chipClause = (d: DistrictChip): string => {
    if (d.id != null && d.level != null && d.level in ID_COL) {
      return `${ID_COL[d.level]}.eq.${d.id}`;
    }
    const namePat = escapeIlikePattern(d.name);
    if (d.level === 'locality') {
      const loc = `place_search_text.ilike.${namePat}`;
      return d.id != null ? `and(obec_id.eq.${d.id},${loc})` : loc;
    }
    const cols = (pat: string): string =>
      `district.ilike.${pat},place_search_text.ilike.${pat},okres.ilike.${pat},region.ilike.${pat}`;
    const nameHalf = `or(${cols(namePat)})`;
    if (!d.context) return nameHalf;
    const ctxPat = escapeIlikePattern(d.context);
    return `and(${nameHalf},or(${cols(ctxPat)}))`;
  };
  const inc = districts.filter((d) => !d.excluded).map(chipClause);
  const exc = districts.filter((d) => d.excluded).map(chipClause);
  const groups: string[] = [];
  if (inc.length) groups.push(`or(${inc.join(',')})`);
  if (exc.length) groups.push(`not.or(${exc.join(',')})`);
  return groups.length ? `and(${groups.join(',')})` : null;
};

/* Client-side counterpart to `districtsFilterClause` — the SAME include/exclude
 * + admin-id + name-fallback semantics, but as a row predicate for in-memory
 * filtering. The SQL builder above can't be reused directly (it emits a
 * PostgREST string, not a predicate); the pipeline board loads its small card
 * set fully and filters locally (rule #22), so it needs this. Keep the two in
 * LOCKSTEP — they share the column contract (obec_id/okres_id/region_id +
 * district/place_search_text/okres/region on properties_public) pinned by
 * queries.test.ts. A resolved chip matches by exact admin id; an unresolved one
 * by case-insensitive substring across the place columns (mirroring ILIKE
 * "*…*"), AND its context when present. */
export interface DistrictMatchRow {
  obec_id: number | null;
  okres_id: number | null;
  region_id: number | null;
  district: string | null;
  place_search_text: string | null;
  okres: string | null;
  region: string | null;
}

const ilikeContains = (text: string | null, needle: string): boolean =>
  text != null && text.toLowerCase().includes(needle.toLowerCase());

const matchesDistrictChip = (row: DistrictMatchRow, d: DistrictChip): boolean => {
  if (
    d.id != null
    && (d.level === 'obec' || d.level === 'okres' || d.level === 'kraj')
  ) {
    const col = { obec: 'obec_id', okres: 'okres_id', kraj: 'region_id' }[d.level] as
      'obec_id' | 'okres_id' | 'region_id';
    return row[col] === d.id;
  }
  if (d.level === 'locality') {
    const loc = ilikeContains(row.place_search_text, d.name);
    return d.id != null ? row.obec_id === d.id && loc : loc;
  }
  const nameHalf =
    ilikeContains(row.district, d.name)
    || ilikeContains(row.place_search_text, d.name)
    || ilikeContains(row.okres, d.name)
    || ilikeContains(row.region, d.name);
  if (!d.context) return nameHalf;
  const ctxHalf =
    ilikeContains(row.district, d.context)
    || ilikeContains(row.place_search_text, d.context)
    || ilikeContains(row.okres, d.context)
    || ilikeContains(row.region, d.context);
  return nameHalf && ctxHalf;
};

export const matchesDistricts = (
  row: DistrictMatchRow,
  districts: DistrictChip[],
): boolean => {
  if (!districts.length) return true;
  const inc = districts.filter((d) => !d.excluded);
  const exc = districts.filter((d) => d.excluded);
  const included = inc.length === 0 || inc.some((d) => matchesDistrictChip(row, d));
  const notExcluded = !exc.some((d) => matchesDistrictChip(row, d));
  return included && notExcluded;
};

/* Generic identity-typed helper. Postgrest's filter methods all return the
 * same builder, so passing the chain through any subset of them preserves
 * the input type at runtime.
 *
 * The straight-forward registry filters (min/max numeric ranges,
 * tristates, single-value enums, multi-value IN lists) are dispatched
 * automatically by `applyRegistryFilters` from registryQueryBuilder.ts.
 * What stays hand-coded here is the small set of irregular shapes:
 * the `status` multi-enum → boolean column predicate, the
 * days-ago → ISO timestamp translation, the 1-enum → IN-over-many
 * `building_material` expansion, the multi-chip district predicate
 * (districtsFilterClause), and the bbox spatial predicates that aren't
 * registry filters at all. The drift test in registryQueryBuilder.test.ts
 * fails CI if a new registry filter is added that fits no path. */

/* PostgREST `.or()` argument for a NULL-tolerant absolute-price bound: keeps
 * no-price listings (price_czk IS NULL) alongside the [min,max] range. Only
 * used when `includeNoPrice` is on AND at least one bound is set. Mirrors the
 * SQL `(price >= lo and price <= hi) or price is null` that
 * browse_stats_properties + the watchdog matcher apply. Pure + exported so the
 * shape is unit-tested (like districtsFilterClause). */
export const priceNullTolerantOr = (
  min: number | null,
  max: number | null,
): string => {
  const bounds: string[] = [];
  if (min != null) bounds.push(`price_czk.gte.${min}`);
  if (max != null) bounds.push(`price_czk.lte.${max}`);
  const range = bounds.length > 1 ? `and(${bounds.join(',')})` : bounds[0];
  return `${range},price_czk.is.null`;
};

const applyFilters = <T>(q: T, f: ListingFilters): T => {
  let r = applyRegistryFilters(q, f) as unknown as {
    eq:  (c: string, v: unknown) => typeof r;
    gte: (c: string, v: unknown) => typeof r;
    lte: (c: string, v: unknown) => typeof r;
    in:  (c: string, v: readonly unknown[]) => typeof r;
    or:  (q: string) => typeof r;
  };
  if (f.status === 'active') r = r.eq('is_active', true);
  else if (f.status === 'inactive') r = r.eq('is_active', false);
  /* Days-ago ranges. min = most recent allowed (so last_seen >= now()
   * minus min); max = oldest allowed (so last_seen <= now() minus max).
   * Wait — that's inverted. min_days = 3 means "seen at least 3 days
   * ago", which is `last_seen <= now() - 3d`. max_days = 10 means
   * "seen at most 10 days ago", which is `last_seen >= now() - 10d`. */
  if (f.lastSeenMaxDays != null) r = r.gte('last_seen_at', isoNDaysAgo(f.lastSeenMaxDays));
  if (f.lastSeenMinDays != null) r = r.lte('last_seen_at', isoNDaysAgo(f.lastSeenMinDays));
  if (f.firstSeenMaxDays != null) r = r.gte('first_seen_at', isoNDaysAgo(f.firstSeenMaxDays));
  if (f.firstSeenMinDays != null) r = r.lte('first_seen_at', isoNDaysAgo(f.firstSeenMinDays));
  /* Status-section recency presets. "added" = first seen within N days;
   * "changed" = newest content snapshot (last_change_at) within N days. */
  if (f.recentlyAddedDays != null) r = r.gte('first_seen_at', isoNDaysAgo(f.recentlyAddedDays));
  if (f.recentlyChangedDays != null) r = r.gte('last_change_at', isoNDaysAgo(f.recentlyChangedDays));
  const districtsClause = districtsFilterClause(f.districts);
  if (districtsClause) r = r.or(districtsClause);
  if (f.buildingMaterial.length) {
    r = r.in('building_type', buildingMaterialToValues(f.buildingMaterial));
  }
  /* Multi-select enums with the '__unknown__' sentinel. The sentinel matches a
   * NULL or non-canonical value, which the plain `.in()` auto-dispatch can't
   * express — so they're hand-coded here as an `.or(in.(…),is.null,not.in.(…))`
   * clause. Mirrors browse_stats_properties + the watchdog matcher (the
   * shared toolkit.comparables._enum_or_unknown_clause). */
  const enumOrUnknown = (
    col: string, values: string[], canonical: readonly string[],
  ): string | null => {
    if (!values.length) return null;
    const reals = values.filter((v) => v !== UNKNOWN_FILTER_VALUE);
    const parts: string[] = [];
    if (reals.length) parts.push(`${col}.in.(${reals.join(',')})`);
    if (values.includes(UNKNOWN_FILTER_VALUE)) {
      parts.push(`${col}.is.null`);
      parts.push(`${col}.not.in.(${canonical.join(',')})`);
    }
    return parts.length ? parts.join(',') : null;
  };
  const furnishedOr = enumOrUnknown('furnished', f.furnished, FURNISHED_CANONICAL);
  if (furnishedOr) r = r.or(furnishedOr);
  const ownershipOr = enumOrUnknown('ownership', f.ownership, OWNERSHIP_CANONICAL);
  if (ownershipOr) r = r.or(ownershipOr);
  /* Absolute price bound (price_czk). Hand-coded — not the registry auto
   * `.gte`/`.lte` — so `includeNoPrice` can widen it to keep no-price listings:
   * a plain `.gte` already drops NULLs and a later `.or` can't add them back, so
   * the whole bound is re-expressed as one disjunction. Scope is price_czk only;
   * the price/m² + yield bounds deliberately keep dropping NULL-price rows. */
  if (f.priceMin != null || f.priceMax != null) {
    if (f.includeNoPrice) {
      r = r.or(priceNullTolerantOr(f.priceMin, f.priceMax));
    } else {
      if (f.priceMin != null) r = r.gte('price_czk', f.priceMin);
      if (f.priceMax != null) r = r.lte('price_czk', f.priceMax);
    }
  }
  /* Merged price-history filters (migration 173). The window picks which
   * precomputed count column the threshold reads; the signed total-change
   * threshold flips direction on sign (negative = "dropped at least",
   * positive = "rose at least"). Mirrors browse_stats_properties and the
   * watchdog matcher. */
  if (f.priceChangeCountMin != null) {
    r = r.gte(priceChangeCountColumn(f.priceChangeWindowDays), f.priceChangeCountMin);
  }
  if (f.totalPriceChangePct != null && f.totalPriceChangePct !== 0) {
    r = f.totalPriceChangePct < 0
      ? r.lte('total_price_change_pct', f.totalPriceChangePct)
      : r.gte('total_price_change_pct', f.totalPriceChangePct);
  }
  const bbox = effectiveBbox(f);
  if (bbox) {
    r = r.gte('lng', bbox.west)
         .lte('lng', bbox.east)
         .gte('lat', bbox.south)
         .lte('lat', bbox.north);
  }
  return r as unknown as T;
};

export interface MapRow {
  /* Surrogate identity (never null) — the maplibre feature-state id + hover-sync
   * key. `sreality_id` is nullable post-Gate-2 and is only for the detail link. */
  listing_id: number;
  property_id: number;
  sreality_id: number | null;
  source: string | null;
  source_id_native: string | null;
  lat: number;
  lng: number;
  price_czk: number | null;
  disposition: string | null;
  subtype: string | null;
  area_m2: number | null;
  district: string | null;
  last_seen_at: string;
  is_active: boolean;
  tom_days: number | null;
}

export interface MapResult {
  rows: MapRow[];
  total: number | null;
  capped: boolean;
}

/* Tags facet is composed of two server queries: (1) properties_with_tags RPC
 * resolves the PROPERTY ids matching ALL selected tag ids (property grain, so
 * a property matches if any of its listings carries the tags), (2) the Browse
 * query gets .in('property_id', ids) appended. Returns null if no tags are
 * selected (skip the prefilter entirely), an empty array if none match (caller
 * short-circuits to empty results), or the id list. Declared as a hoistable
 * function so the Map/Table fetchers below can call it without forward-ref issues. */
async function resolveTagPrefilter(
  f: ListingFilters,
): Promise<number[] | null> {
  if (f.tags.length === 0) return null;
  /* Exhaustive by contract: a truncated allowlist would silently bleed
   * listings the operator asked to exclude back into the cohort — so the read
   * pages via fetchAllRows (complete-or-throw, correct under any db-max-rows;
   * see its header for the cap-drift history). */
  const rows = await fetchAllRows<{ property_id: number }>({
    relation: 'properties_with_tags',
    build: () =>
      supabase.rpc('properties_with_tags', { tag_ids: f.tags }, { count: 'exact' }),
    orderBy: [{ column: 'property_id' }],
    key: ['property_id'],
    expectMax: 100_000,
  });
  return rows.map((r) => r.property_id);
}

/* Phase QUAL — `listings_with_city_quality` RPC prefilter. Same
 * composition pattern as the tags prefilter above: when ANY city-quality
 * predicate is active, the RPC returns the listing_id allowlist and the
 * main listings query AND's it via `.in('listing_id', ids)`. Returns
 * null when no city-quality filter is set so the fast path stays
 * unchanged. Keyed on the surrogate `listing_id` (migration 351), NOT
 * sreality_id — a post-Gate-2 non-sreality repr has a NULL sreality_id, and
 * `IN` never matches NULL, so the old sreality-keyed filter silently dropped
 * those listings from Map/Table/Cards/Count while browse_stats still counted
 * them (count-vs-list divergence). */
/* min/max city population and the near_* proximity filters are NOT here:
 * since migration 142 they're precomputed columns on properties_public, so
 * they dispatch directly via applyRegistryFilters (no prefilter RPC, no anon
 * 3s timeout). Only the flexible any-index rule list + the legacy centroid
 * near_city_proximity still need the spatial RPC. */
const hasCityQualityFilter = (f: ListingFilters): boolean =>
  f.cityIndexRules.length > 0
  || f.nearCityProximity != null;

async function resolveCityQualityPrefilter(
  f: ListingFilters,
): Promise<number[] | null> {
  if (!hasCityQualityFilter(f)) return null;
  /* Filters carry the wire shape (snake_case) directly so no translation layer
   * is needed before calling the RPC. Exhaustive by contract, same reason as
   * `resolveTagPrefilter`. */
  const rows = await fetchAllRows<{ listing_id: number }>({
    relation: 'listings_with_city_quality',
    build: () =>
      supabase.rpc(
        'listings_with_city_quality',
        {
          p_index_rules: f.cityIndexRules.length === 0 ? null : f.cityIndexRules,
          /* pop bounds moved to the home_obec_pop column filter (migration 142);
           * never sent through this RPC anymore. */
          p_pop_min: null,
          p_pop_max: null,
          p_proximity: f.nearCityProximity,
        },
        { count: 'exact' },
      ),
    /* The RPC returns the surrogate `listing_id` (migration 351); the row type
     * pins that so a stray `r.sreality_id` can't silently reintroduce the
     * id-space half-swap. Applied downstream via `.in('listing_id', ids)`. */
    orderBy: [{ column: 'listing_id' }],
    key: ['listing_id'],
    expectMax: 100_000,
  });
  return rows.map((r) => r.listing_id);
}

/* Market-growth (price-stats datasets) prefilter. For each active rule the
 * price_stat_growth RPC computes per-obec CAGR over [fromYm, toYm]; we keep
 * obce meeting/exceeding the entered rent + sale growth thresholds (≥), then
 * INTERSECT across rules (AND across datasets). Returns null when no rule has a
 * threshold set, [] when no obec qualifies (caller short-circuits), else the
 * obec_id allowlist — applied via .in('obec_id', ids) on the cohort queries and
 * obec_ids_filter on browse_stats_properties. BROWSE-only (window-dependent). */
async function resolvePriceGrowthPrefilter(
  f: ListingFilters,
): Promise<number[] | null> {
  const rules = f.priceGrowthRules.filter(
    (r) => r.rentMinPct != null || r.saleMinPct != null,
  );
  if (rules.length === 0) return null;
  const perRule = await Promise.all(
    rules.map(async (r) => {
      const rentMin = r.rentMinPct;
      const saleMin = r.saleMinPct;
      const rows = await fetchGrowth(r.datasetId, r.fromYm, r.toYm);
      return rows
        .filter(
          (g) =>
            (rentMin == null || (g.rent_cagr_pct != null && g.rent_cagr_pct >= rentMin))
            && (saleMin == null || (g.sale_cagr_pct != null && g.sale_cagr_pct >= saleMin)),
        )
        .map((g) => g.obec_id);
    }),
  );
  let acc = perRule[0] ?? [];
  for (let i = 1; i < perRule.length; i++) {
    const set = new Set(perRule[i]);
    acc = acc.filter((id) => set.has(id));
  }
  return acc;
}

/* With-estimates prefilter (migration 173). property_estimates_public is the
 * anon-readable property-grain projection of successful estimation runs —
 * tiny (one row per estimated property), so fetching the full id list and
 * AND'ing it via `.in('property_id', ids)` is the same composition pattern
 * as the tags prefilter. Returns null when the filter is off. */
async function resolveEstimatesPrefilter(
  f: ListingFilters,
): Promise<number[] | null> {
  if (!f.withEstimates) return null;
  const rows = await fetchAllRows<{ property_id: number }>({
    relation: 'property_estimates_public',
    build: () =>
      supabase.from('property_estimates_public').select('property_id', { count: 'exact' }),
    orderBy: [{ column: 'property_id' }],
    key: ['property_id'],
    expectMax: 100_000,
  });
  return rows.map((r) => r.property_id);
}

/* Deal-pipeline scope prefilter (rule #22, migration 205). Same composition
 * pattern as tags / with-estimates: resolve the property-id allowlist, AND it
 * onto the cohort via `.in('property_id', …)`. Reads through the SAME
 * `fetchPipelineMembers` every funnel uses, so "what is in my pipeline" has one
 * definition — a second query here could disagree with the badges on screen.
 *
 * An empty `stage_ids` means "any stage"; a non-empty one narrows to those
 * stages (ids are account-scoped, and the map only ever holds this account's
 * cards, so a foreign id simply matches nothing). Returns null when off. */
export const pipelineIdsForScope = (
  members: PipelineMembers,
  scope: PipelineScope | null,
): number[] | null => {
  if (scope == null) return null;
  const wanted = new Set(scope.stage_ids);
  const ids: number[] = [];
  for (const m of members.values()) {
    if (wanted.size === 0 || wanted.has(m.stage_id)) ids.push(m.property_id);
  }
  return ids;
};

async function resolvePipelinePrefilter(
  f: ListingFilters,
): Promise<number[] | null> {
  if (f.pipeline == null) return null;
  return pipelineIdsForScope(await fetchPipelineMembers(), f.pipeline);
}

/* Intersect two prefilter id sets (null = "no constraint"). Used so a
 * filter that combines tags + city-quality applies both prefilters
 * before paging the main query. */
const intersectPrefilters = (
  a: number[] | null,
  b: number[] | null,
): number[] | null => {
  if (a == null) return b;
  if (b == null) return a;
  const set = new Set(b);
  return a.filter((id) => set.has(id));
};

/* All Browse prefilters resolved together. Each is an id allowlist at its
 * own grain (null = inactive); `empty` is true when any ACTIVE prefilter
 * matched nothing, so the caller can short-circuit to zero results without
 * issuing the main query. Shared by the Map / Table / Cards fetchers. */
export interface BrowsePrefilters {
  listingIds: number[] | null;    // city-quality (surrogate listing_id, migration 351)
  obecIds: number[] | null;       // market growth (price-stats datasets)
  propertyIds: number[] | null;   // tags ∩ with-estimates (property grain)
  empty: boolean;
}

async function resolveBrowsePrefilters(
  f: ListingFilters,
): Promise<BrowsePrefilters> {
  const [tagProps, cityIds, growthObec, estimateProps, pipelineProps] =
    await Promise.all([
      resolveTagPrefilter(f),
      resolveCityQualityPrefilter(f),
      resolvePriceGrowthPrefilter(f),
      resolveEstimatesPrefilter(f),
      resolvePipelinePrefilter(f),
    ]);
  // Tags are now property-grain (properties_with_tags) — intersect them with the
  // with-estimates and pipeline property prefilters and apply via
  // .in('property_id', …). City-quality is representative-listing grain, keyed
  // on the surrogate listing_id (.in('listing_id', …)) — null-safe past Gate-2.
  const propertyIds = intersectPrefilters(
    intersectPrefilters(tagProps, estimateProps),
    pipelineProps,
  );
  const empty =
    (cityIds != null && cityIds.length === 0)
    || (growthObec != null && growthObec.length === 0)
    || (propertyIds != null && propertyIds.length === 0);
  return { listingIds: cityIds, obecIds: growthObec, propertyIds, empty };
}

/* Exported for queries.test.ts — pins that the city-quality allowlist filters on
 * the surrogate `listing_id` (migration 351), not the nullable `sreality_id`
 * (passing a sreality_id into an `IN listing_id` predicate would silently read a
 * DIFFERENT listing, the id-spaces overlap by ~435). */
export const applyPrefilters = <T>(q: T, p: BrowsePrefilters): T => {
  let r = q as unknown as {
    in: (c: string, v: readonly unknown[]) => typeof r;
  };
  if (p.listingIds != null) r = r.in('listing_id', p.listingIds);
  if (p.obecIds != null) r = r.in('obec_id', p.obecIds);
  if (p.propertyIds != null) r = r.in('property_id', p.propertyIds);
  return r as unknown as T;
};

/* Count of properties matching the CURRENT filters EXCEPT the price bound that
 * have no listed price (price_czk IS NULL) — i.e. how many a min/max price
 * bound is hiding (or, with the toggle on, including). Powers the discoverable
 * "N listings have no listed price" hint next to the Price section's toggle.
 * Reuses the exact cohort filter path (resolveBrowsePrefilters + applyFilters)
 * so it can never drift from the Map/Table semantics. A `head:true` count, same
 * risk class as the result-badge count Browse already issues. Callers gate on a
 * price bound being set; on error the UI just omits the number (graceful). */
export const fetchNoPriceCount = async (f: ListingFilters): Promise<number> => {
  const pre = await resolveBrowsePrefilters(f);
  if (pre.empty) return 0;
  const base = supabase
    .from(listRelation(f))
    .select(keysetTiebreak(f), { count: 'exact', head: true });
  // Strip the price bound (and the toggle) so the count is purely "no-price
  // rows in the rest of the cohort", then restrict to NULL price.
  const noPriceFilters: ListingFilters = {
    ...f, priceMin: null, priceMax: null, includeNoPrice: false,
  };
  const scoped = applyPrefilters(applyFilters(base, noPriceFilters), pre)
    .is('price_czk', null);
  const { count, error } = await scoped;
  if (error) throw error;
  return count ?? 0;
};

/* Browse cohort fetchers (Map / Table / Cards) AND fetchBrowseStats read the
 * property grain (properties_public / browse_stats_properties), so Browse is
 * one-dot-per-property. `sreality_id` on properties_public is the
 * representative child, so detail links, image / snapshot / tag lookups, and
 * the sreality_id-keyed prefilters all carry over unchanged. Today every
 * property is a singleton, so the surface is visually identical to
 * listings_public; multi-source collapsing arrives with the portal scrapers.
 *
 * The Stats RPC was repointed in Slice 2a once migration 095 denormalised the
 * filter columns onto `properties` — that drops the listings join from the
 * function's plan, making browse_stats_properties perf-equivalent to the
 * listing-grain browse_stats. Migration 173 carries the merged price-change
 * predicates, the condition-level bounds, and the with-estimates flag. */
export const fetchListingsForMap = async (
  f: ListingFilters,
): Promise<MapResult> => {
  const pre = await resolveBrowsePrefilters(f);
  if (pre.empty) return { rows: [], total: 0, capped: false };
  /* The map reads `properties_map_mv` (migration 254), NOT `properties_public`.
   * Shipping up to MAP_CAP points off the live, churned `properties` table was
   * cold-fragile (>3s, the anon statement_timeout) — the matview is a clean,
   * all-visible, cached copy of the same columns, so the identical scan stays
   * robust cold (~200ms). It carries properties_public's full FILTERABLE surface,
   * so applyFilters / applyPrefilters are a drop-in (only the source differs).
   * Rebuilt from browse_projection by rebuild_properties_map_mv() (pg_cron,
   * every 30 min — migration 277); freshness readable off
   * browse_read_model_state_public. */
  /* Single-portal mode reads the listing-grain feed here too (see the PORTAL
   * MIRROR block). It has no matview twin, so this is a live indexed read of
   * `listings` rather than the cached copy — acceptable because the mirror
   * cohort is bounded by one portal (largest is idnes at ~110k active rows,
   * measured 1.2s for a full uncapped 50k-point fetch, inside the anon 3s
   * budget) and because plotting one portal's own listings is the entire point:
   * property-grain pins would silently relocate a listing to a sibling
   * portal's coordinates. */
  const base = supabase
    .from(mapRelation(f))
    .select(MAP_COLS)
    .not('lat', 'is', null)
    .not('lng', 'is', null);
  const scoped = applyPrefilters(applyFilters(base, f), pre);
  const { data, error } = await scoped.limit(MAP_CAP);
  if (error) throw error;
  const rows = (data ?? []) as unknown as MapRow[];
  /* The cohort total (which also counts coordinate-less listings) comes from
   * fetchBrowseCount; the map only needs how many points it actually plotted
   * and whether it hit the cap. Counting the whole cohort here too was a
   * redundant O(cohort) exact count — the heaviest part of the map fetch,
   * left over from before fetchBrowseCount existed. `total` is now the
   * plotted-point count; `capped` is whether more points exist than shown. */
  return {
    rows,
    total: rows.length,
    capped: rows.length >= MAP_CAP,
  };
};

export interface TableRow {
  /* Property-grain tiebreaker for keyset paging + row de-dup. */
  property_id: number;
  /* Surrogate identity (never null) — the React key + hover-sync key. */
  listing_id: number;
  sreality_id: number | null;
  source: string | null;
  source_id_native: string | null;
  district: string | null;
  locality: string | null;
  obec: string | null;
  okres: string | null;
  street: string | null;
  disposition: string | null;
  subtype: string | null;
  area_m2: number | null;
  price_czk: number | null;
  first_seen_at: string;
  last_seen_at: string;
  is_active: boolean;
  tom_days: number | null;
  estate_area: number | null;
  usable_area: number | null;
  parking_lots: number | null;
  furnished: string | null;
  ownership: string | null;
  category_sub_cb: number | null;
  building_type: string | null;
  /* Same price-movement pair the cards read — the table's Price column shows
   * the delta beside the figure so the two lanes agree. */
  total_price_change_pct: number | null;
  price_change_count: number | null;
}

/* A page of the keyset-paginated infinite list (see lib/keyset.ts).
 * `nextCursor` anchors the page that follows; the cohort total is fetched
 * separately, once, by fetchBrowseCount (it doesn't change per page). */
export interface TableResult {
  rows: TableRow[];
  nextCursor: KeysetCursor | null;
}

export const fetchListingsForTable = async (
  f: ListingFilters,
  sort: SortSpec,
  cursor: KeysetCursor | null,
): Promise<TableResult> => {
  const pre = await resolveBrowsePrefilters(f);
  if (pre.empty) return { rows: [], nextCursor: null };
  /* browse_list (migration 276): the compact snapshot read model — a STABLE
   * relation under the scroll (the live table mutates last_seen_at every
   * scrape cycle), rebuilt every 5 min from browse_projection. Single-portal
   * mode swaps in listing_feed_public; that one IS the live table, so a row
   * whose last_seen_at is bumped mid-scroll can shift — harmless here because
   * the mirror's sort key (portal_sort_key) is immutable after first write. */
  const s = effectiveSort(f, sort);
  const tiebreak = keysetTiebreak(f);
  const base = supabase
    .from(listRelation(f))
    .select(withKeysetColumns(TABLE_COLS, s, tiebreak));
  const scoped = applyPrefilters(applyFilters(base, f), pre);
  const keyed = applyKeyset(
    scoped as unknown as KeysetBuilder,
    s,
    cursor,
    tiebreak,
  ) as unknown as typeof scoped;
  const { data, error } = await keyed.limit(TABLE_PAGE_SIZE);
  if (error) throw error;
  const rows = (data ?? []) as unknown as TableRow[];
  return {
    rows,
    nextCursor: nextCursorFrom(
      rows as unknown as Record<string, unknown>[],
      s,
      tiebreak,
    ),
  };
};

export interface CohortCount {
  /* The cohort size — exact when an exact count fit the budget, otherwise the
   * query planner's estimate. */
  value: number;
  /* False when `value` is the planner estimate (the exact count would exceed
   * the anon statement_timeout). The UI renders an approximate value as "~N". */
  precise: boolean;
}

/* The ONE cohort total — header, tab badge, and the infinite-scroll progress
 * labels. EXACT FIRST on the compact browse_list read model: on the snapshot,
 * a market-wide exact count is an index-only scan (measured 201 ms fully cold
 * for the broadest single cohort — 68k rows, zero heap fetches), so precision
 * is the norm, not the exception. The pre-read-model planner-estimate-first
 * hybrid existed because an exact count on the churned live table could not
 * finish under the anon 3s budget; that constraint is gone. `count=planned`
 * stays as the graceful FALLBACK when the exact count exceeds the abort budget
 * (a pathological filter combination or a saturated instance) — rendered as
 * "~N". The planned estimate depends on the rebuild's ANALYZE-before-swap
 * (pinned by tests/test_browse_read_path_guardrail.py). Shares the exact
 * filter chain (resolveBrowsePrefilters + applyFilters) with the Table/Cards
 * fetchers, so the total can never disagree with the listed rows. */
const EXACT_COUNT_BUDGET_MS = 2500;
export const fetchBrowseCount = async (
  f: ListingFilters,
): Promise<CohortCount> => {
  const pre = await resolveBrowsePrefilters(f);
  if (pre.empty) return { value: 0, precise: true };
  type CountResp = { count: number | null; error: { message: string } | null };
  type CountQuery = PromiseLike<CountResp> & {
    abortSignal: (s: AbortSignal) => PromiseLike<CountResp>;
  };
  const build = (mode: 'exact' | 'planned') =>
    applyPrefilters(
      applyFilters(
        supabase
          .from(listRelation(f))
          .select(keysetTiebreak(f), { count: mode, head: true }),
        f,
      ),
      pre,
    ) as unknown as CountQuery;
  try {
    const { count, error } = await build('exact').abortSignal(
      AbortSignal.timeout(EXACT_COUNT_BUDGET_MS),
    );
    if (error) throw error;
    if (count != null) return { value: count, precise: true };
  } catch {
    // Exact didn't finish under budget — fall through to the estimate.
  }
  const planned = await build('planned');
  if (planned.error) throw planned.error;
  const estimate = planned.count ?? 0;
  return { value: estimate, precise: estimate === 0 };
};

/* -------------------------------------------------------------------------- */
/* Cards (sreality-style image-first list). Same filter chain as table, plus  */
/* a batched image lookup for the first photo per visible listing. Sorted by  */
/* first_seen_at desc — the cards lane is for "what's new", not for arbitrary */
/* re-sorting (that's the Table tab's job).                                   */
/* -------------------------------------------------------------------------- */

export interface CardRow {
  /* The canonical property this card represents (Browse is property-grain via
   * properties_public). Used by the Browse merge-mode action. */
  property_id: number;
  /* Surrogate identity (never null) — the React key, the hover-sync key, and the
   * key the card image hydration batches on. `sreality_id` is nullable post-
   * Gate-2 and is only the fast detail link + the (sreality-backed) estimate. */
  listing_id: number;
  sreality_id: number | null;
  district: string | null;
  locality: string | null;
  obec: string | null;
  okres: string | null;
  street: string | null;
  disposition: string | null;
  /* Portal-agnostic property sub-type (migration 152) — the meaningful "kind"
   * for commercial/houses, where disposition is NULL. NULL for apartments. */
  subtype: string | null;
  area_m2: number | null;
  price_czk: number | null;
  first_seen_at: string;
  last_seen_at: string;
  is_active: boolean;
  tom_days: number | null;
  category_main: string | null;
  category_type: string | null;
  source: string | null;
  /* Portal-native id (migration 091). With `source`, builds the canonical
   * `/listing/{source}/{native}` link; null on pre-091 rows → legacy fallback. */
  source_id_native: string | null;
  /* MF gross rental yield % (migration 133). Non-null only on sale
   * apartments that resolved to an MF territory. */
  mf_gross_yield_pct: number | null;
  /* Signed percent across the representative listing's own price series, and
   * the per-child change count. NULL pct = fewer than two observed prices,
   * which <PriceDelta> renders as nothing rather than as "unchanged". */
  total_price_change_pct: number | null;
  price_change_count: number | null;
}

export interface CardsResult {
  rows: CardRow[];
  nextCursor: KeysetCursor | null;
}

export const fetchListingsForCards = async (
  f: ListingFilters,
  sort: SortSpec,
  cursor: KeysetCursor | null,
): Promise<CardsResult> => {
  const pre = await resolveBrowsePrefilters(f);
  if (pre.empty) return { rows: [], nextCursor: null };
  const s = effectiveSort(f, sort);
  const tiebreak = keysetTiebreak(f);
  const base = supabase
    .from(listRelation(f))
    .select(withKeysetColumns(CARD_COLS, s, tiebreak));
  const scoped = applyPrefilters(applyFilters(base, f), pre);
  const keyed = applyKeyset(
    scoped as unknown as KeysetBuilder,
    s,
    cursor,
    tiebreak,
  ) as unknown as typeof scoped;
  const { data, error } = await keyed.limit(CARD_PAGE_SIZE);
  if (error) throw error;
  const baseRows = (data ?? []) as unknown as CardRow[];
  const nextCursor = nextCursorFrom(
    baseRows as unknown as Record<string, unknown>[],
    s,
    tiebreak,
  );
  /* W7a: the card photos USED to be awaited right here, before this function
   * would return a single row — so no card painted until every card's carousel
   * had landed. Measured live on 24 real ids, that await is 178 image rows, 178
   * correlated CLIP-tag lookups, 750 buffers and ~131 ms of server work, all of
   * it on the paint path. It is not wasteful work: the carousel renders those
   * rows, which is precisely why the fix is to move it off the paint path rather
   * than to shrink it to one cover. Photos now arrive through the shared
   * hydration layer (lib/hydration/useListingPhotos), keyed on the same
   * surrogate listing_id, non-blocking, in its own cache namespace — and this
   * read is one relation again. */
  return { rows: baseRows, nextCursor };
};


export interface BrowseStatsDispositionRow {
  disposition: string;
  n: number;
  ppm2_box: Ppm2Box | null;
}

export interface TomBox {
  n: number;
  min: number;
  p25: number;
  median: number;
  mean: number;
  p75: number;
  max: number;
}

export interface PriceBandVelocityRow {
  bucket: 1 | 2 | 3 | 4 | 5 | 6 | 7;
  p_lo: number;
  p_hi: number;
  n: number;
  pct_share: number | null;
  price_min: number | null;
  price_max: number | null;
  tom_box: TomBox | null;
}

export interface BrowseStats {
  total: number;
  new_7d: number;
  new_30d: number;
  price: { p25: number; p50: number; p75: number } | null;
  ppm2:  { p25: number; p50: number; p75: number } | null;
  dispositions: ReadonlyArray<BrowseStatsDispositionRow>;
  price_band_velocity: ReadonlyArray<PriceBandVelocityRow>;
}

export const fetchBrowseStats = async (
  f: ListingFilters,
): Promise<BrowseStats> => {
  const triToBool = (t: typeof f.hasBalcony): boolean | null =>
    t === 'any' ? null : t === 'yes';

  const buildingTypeArray = f.buildingMaterial.length
    ? [...buildingMaterialToValues(f.buildingMaterial)]
    : null;

  const effBbox = effectiveBbox(f);
  /* Market-growth allowlist (obec_ids); null = no active rule, [] = no obec
   * qualifies (the RPC's `= any('{}')` then yields total 0). Keeps Stats
   * aligned with Map/Table. */
  const growthObec = await resolvePriceGrowthPrefilter(f);
  /* Deal-pipeline allowlist (property_ids); same contract as growthObec above —
   * null = scope off, [] = an empty pipeline (the RPC's `= any('{}')` then
   * yields total 0). Without this the Stats tab would keep counting the whole
   * market while Cards/Table/Map/Count show only the pipeline: the exact
   * count-vs-list divergence migration 351 was written to close. */
  const pipelineProps = await resolvePipelinePrefilter(f);

  const { data, error } = await supabase.rpc('browse_stats_properties', {
    category_main_filter:    f.categoryMain.length ? f.categoryMain : null,
    category_type_filter:    f.categoryType,
    districts_filter:        f.districts.length ? f.districts.map((d) => d.name) : null,
    districts_context_filter: f.districts.length
      ? f.districts.map((d) => d.context ?? '')
      : null,
    /* Parallel exclude flags (migration 146) — full-length array so the RPC's
     * unnest stays aligned with names; absent excluded => include. */
    districts_excluded_filter: f.districts.length
      ? f.districts.map((d) => d.excluded === true)
      : null,
    /* Migration 172 — resolved admin level + id parallel to the names, so the
     * Stats cohort matches by stable id (obec_id/okres_id/region_id) exactly
     * like Map/Table. NULL entries = legacy/unresolved chips → name fallback. */
    districts_levels: f.districts.length
      ? f.districts.map((d) => d.level ?? null)
      : null,
    districts_ids: f.districts.length
      ? f.districts.map((d) => (d.id == null ? null : d.id))
      : null,
    dispositions_filter:     f.dispositions.length ? f.dispositions : null,
    price_min_filter:        f.priceMin,
    price_max_filter:        f.priceMax,
    include_no_price:        f.includeNoPrice,
    area_min_filter:         f.areaMin,
    area_max_filter:         f.areaMax,
    active_only_filter:      f.status === 'active',
    inactive_only_filter:    f.status === 'inactive',
    last_seen_min_days:      f.lastSeenMinDays,
    last_seen_max_days:      f.lastSeenMaxDays,
    first_seen_min_days:     f.firstSeenMinDays,
    first_seen_max_days:     f.firstSeenMaxDays,
    /* Migration 159 — Status-section recency presets (first_seen_at /
     * last_change_at within N days). */
    recently_added_days:     f.recentlyAddedDays,
    recently_changed_days:   f.recentlyChangedDays,
    tom_days_min:            f.tomDaysMin,
    tom_days_max:            f.tomDaysMax,
    has_balcony_filter:      triToBool(f.hasBalcony),
    has_lift_filter:         triToBool(f.hasLift),
    has_parking_filter:      triToBool(f.hasParking),
    furnished_filter:        f.furnished.length ? f.furnished : null,
    ownership_filter:        f.ownership.length ? f.ownership : null,
    terrace_filter:          triToBool(f.terrace),
    cellar_filter:           triToBool(f.cellar),
    garage_filter:           triToBool(f.garage),
    category_sub_cb_filter:  f.categorySubCb,
    subtype_filter:          f.subtype.length ? f.subtype : null,
    building_type_filter:    buildingTypeArray,
    condition_match_filter:  f.conditionMatch.length ? f.conditionMatch : null,
    tag_ids:                 f.tags.length ? f.tags : null,
    bbox_west:               effBbox?.west  ?? null,
    bbox_south:              effBbox?.south ?? null,
    bbox_east:               effBbox?.east  ?? null,
    bbox_north:              effBbox?.north ?? null,
    /* Phase QUAL — same shape the `listings_with_city_quality` RPC
     * accepts. Migration 080 added these four params to browse_stats
     * so Stats counts stay aligned with Map / Table when a city-
     * quality filter is active. */
    city_index_rules:        f.cityIndexRules.length === 0 ? null : f.cityIndexRules,
    city_pop_min:            f.minCityPopulation,
    city_pop_max:            f.maxCityPopulation,
    city_proximity:          f.nearCityProximity,
    /* Migration 142/143 — fast polygon-edge proximity precomputed columns. */
    near_pop_5km_min:        f.nearPop5kmMin,
    near_pop_15km_min:       f.nearPop15kmMin,
    near_jobs_5km_min:       f.nearJobs5kmMin,
    near_jobs_15km_min:      f.nearJobs15kmMin,
    near_youth_5km_min:      f.nearYouth5kmMin,
    near_youth_15km_min:     f.nearYouth15kmMin,
    near_overall_5km_min:    f.nearOverall5kmMin,
    near_overall_15km_min:   f.nearOverall15kmMin,
    /* Migration 083 — price-per-m² bounds. NULL area_m2 listings fall
     * out when either bound is set. */
    price_per_m2_min:        f.pricePerM2Min,
    price_per_m2_max:        f.pricePerM2Max,
    /* Migration 133 — MF gross rental yield % bounds (sale apartments). */
    mf_gross_yield_pct_min:  f.mfGrossYieldPctMin,
    mf_gross_yield_pct_max:  f.mfGrossYieldPctMax,
    /* Migration 173 — merged price-history predicates + condition-level
     * bounds + with-estimates. Property grain; columns maintained by the
     * recompute job, estimates read via property_estimates_public. */
    price_change_count_min:        f.priceChangeCountMin,
    price_change_window_days:      f.priceChangeWindowDays,
    total_price_change_pct_filter: f.totalPriceChangePct,
    with_estimates:                f.withEstimates,
    building_condition_level_min:  f.buildingConditionLevelMin,
    building_condition_level_max:  f.buildingConditionLevelMax,
    apartment_condition_level_min: f.apartmentConditionLevelMin,
    apartment_condition_level_max: f.apartmentConditionLevelMax,
    /* Migration 118 — filter the Stats cohort by source portal. */
    portal_filter:           f.portals.length ? f.portals : null,
    /* Migration 162 — market-growth obec allowlist (price-stats datasets). */
    obec_ids_filter:         growthObec,
    /* Migration 378 — generic property-id allowlist; carries the deal-pipeline
     * scope (rule #22) today, and is the seam any future property-grain
     * prefilter should reuse instead of growing another bespoke param. */
    property_ids_filter:     pipelineProps,
  });
  if (error) throw error;
  return data as BrowseStats;
};

/* `source_id_native` + `property_id` are migration 420 (W9b): the listing's own
 * identity, which used to be reachable only through property_sources_public — a
 * thin view over `listings` itself, so that read re-fetched THIS row's heap tuple
 * one round trip later to learn two of its own columns. They cost nothing here
 * (measured: the detail read is 7 buffers with them, and was 7 without). */
const DETAIL_COLS =
  'id,sreality_id,first_seen_at,last_seen_at,is_active,source,source_id_native,property_id,tom_days,' +
  'category_main,category_type,price_czk,price_unit,' +
  'area_m2,disposition,subtype,locality,district,obec,okres,street,locality_district_id,locality_region_id,' +
  'lat,lng,floor,total_floors,has_balcony,has_parking,has_lift,' +
  'building_type,condition,energy_rating,' +
  'estate_area,usable_area,garden_area,category_sub_cb,' +
  'furnished,terrace,cellar,garage,parking_lots,ownership,' +
  'description,mf_reference_rent_czk,mf_gross_yield_pct,mf_reference_rent';

/* Legacy /listing/{id} route: URL literally IS the sreality_id, one round trip,
 * unchanged forever — a listing only ever gets a legacy numeric URL when it HAS a
 * sreality_id to put in it, so there's no forward-compat concern here. */
export const fetchListingBySreality = async (
  sreality_id: number,
): Promise<ListingPublic | null> => {
  const { data, error } = await supabase
    .from('listings_public')
    .select(DETAIL_COLS)
    .eq('sreality_id', sreality_id)
    .maybeSingle();
  if (error) throw error;
  return (data as unknown as ListingPublic | null) ?? null;
};

/* Canonical /listing/{source}/{native} route, second half: fetch by the surrogate
 * id fetchListingIdByNaturalKey resolved. Keyed on id, not sreality_id (R2 Phase C
 * resolver-chain cutover) — a listing reachable only by natural key (a future
 * non-sreality row created after Gate 2 stops drawing the synthetic sreality_id
 * sequence) may have no sreality_id to filter on at all. */
export const fetchListingById = async (
  id: number,
): Promise<ListingPublic | null> => {
  const { data, error } = await supabase
    .from('listings_public')
    .select(DETAIL_COLS)
    .eq('id', id)
    .maybeSingle();
  if (error) throw error;
  return (data as unknown as ListingPublic | null) ?? null;
};

/* Resolve a listing's natural key (source, source_id_native) to its surrogate id
 * (migration 334 exposes it — was sreality_id before the R2 Phase C cutover), so
 * the canonical /listing/{source}/{native} route can reuse fetchListingById. Uses
 * listing_natural_key_public (migration 315) — an UNFILTERED view over every
 * listing — NOT property_sources_public, which filters `property_id is not null`
 * and so cannot resolve a freshly-scraped listing during its ~5-min pre-attach
 * window (the canonical URL would 404 while the legacy one loaded). The key
 * (source, source_id_native) is unique (migration 091), so maybeSingle is safe. */
export const fetchListingIdByNaturalKey = async (
  source: string,
  sourceIdNative: string,
): Promise<number | null> => {
  const { data, error } = await supabase
    .from('listing_natural_key_public')
    .select('id')
    .eq('source', source)
    .eq('source_id_native', sourceIdNative)
    .maybeSingle();
  if (error) throw error;
  const row = data as unknown as { id: number | null } | null;
  return row?.id ?? null;
};

/* Resolve a property_id to its representative listing's NATURAL KEY
 * (source, source_id_native). Lets /listing?property=ID (a property-grain
 * link) land on the survivor's detail page via the canonical natural-key
 * route. NOT the surrogate id: listingPath() builds the LEGACY sreality route,
 * and the id-spaces overlap (~435 collisions), so routing the surrogate through
 * it would load the WRONG listing. NOT sreality_id either: a post-Gate-2 repr may
 * have none, which is exactly why the old sreality-id form dead-ended (returned
 * null → NoListingState). properties_public exposes source + source_id_native for
 * the repr child (migration 343). */
export const fetchPropertyReprNaturalKey = async (
  property_id: number,
): Promise<{ source: string; source_id_native: string; listing_id: number | null } | null> => {
  const { data, error } = await supabase
    .from('properties_public')
    .select('source, source_id_native, listing_id')
    .eq('property_id', property_id)
    .maybeSingle();
  if (error) throw error;
  const row = data as unknown as {
    source: string | null;
    source_id_native: string | null;
    listing_id: number | null;
  } | null;
  return row?.source != null && row.source_id_native != null
    ? { source: row.source, source_id_native: row.source_id_native, listing_id: row.listing_id }
    : null;
};

/* The PROPERTY-grain MF reference rent/yield (the golden record, migration 257):
 * one figure per real-world property, so every portal's advert of the same flat
 * shows the same MF. The listing-detail header reads THIS, not the subject
 * advert's per-listing listings.mf_* (which could be one portal's under-stated
 * parse). */
export interface PropertyMf {
  mf_reference_rent: MfReferenceRent | null;
  mf_gross_yield_pct: number | null;
  /* The property's canonical asking price (current_price_czk = most-recently-seen
   * active ask) — the price the golden-record estimate/yield is built on, so the
   * UI can flag any active sibling advertised at a different number. */
  price_czk: number | null;
}

export const fetchPropertyMf = async (
  property_id: number,
): Promise<PropertyMf | null> => {
  const { data, error } = await supabase
    .from('properties_public')
    .select('mf_reference_rent, mf_gross_yield_pct, price_czk')
    .eq('property_id', property_id)
    .maybeSingle();
  if (error) throw error;
  return (data as unknown as PropertyMf | null) ?? null;
};

export const fetchSnapshotsByListing = async (
  sreality_id: number,
): Promise<ListingSnapshotPublic[]> => {
  const { data, error } = await supabase
    .from('listing_snapshots_public')
    .select('id,sreality_id,scraped_at,price_czk,description')
    .eq('sreality_id', sreality_id)
    .order('scraped_at', { ascending: true });
  if (error) throw error;
  return (data ?? []) as unknown as ListingSnapshotPublic[];
};

/* Multi-portal: resolve the property a listing belongs to (works from ANY
 * child listing's surrogate id via property_sources_public, not just the
 * representative), then return all of that property's per-portal observations.
 * Keyed on id, not sreality_id (R2 Phase C resolver-chain cutover) — property_
 * sources_public.id is the same listings.id migration 334 exposed.
 *
 * `knownPropertyId` (W9b) skips the resolve hop. That hop asks
 * property_sources_public — a thin view over `listings` filtered to
 * `property_id is not null` — for one column of the SAME row listings_public has
 * already returned, so a caller holding the listing row already knows the answer.
 * Verified live: property_id and source_id_native agree between the two paths on
 * every row the view exposes (0 mismatches), and no listing points at a
 * non-active property, so they cannot disagree about which side of a merge won.
 *
 * Only a NUMBER takes the fast path. NULL/undefined means "ask" — a NULL
 * property_id is the ~5-min pre-attach window after a scrape, rare enough that
 * paying the hop beats reasoning about how stale the caller's row might be. Note
 * the caller must NOT gate the whole query on having a property_id: on the
 * canonical route this read fires in PARALLEL with the listing read (W9a), and
 * making it wait would trade one hop back for a whole waterfall level. */
export const fetchPropertySources = async (
  id: number,
  knownPropertyId?: number | null,
): Promise<{ property_id: number | null; sources: PropertySource[] }> => {
  let property_id: number | null = null;
  if (typeof knownPropertyId === 'number') {
    property_id = knownPropertyId;
  } else {
    const { data: row, error: e1 } = await supabase
      .from('property_sources_public')
      .select('property_id')
      .eq('id', id)
      .maybeSingle();
    if (e1) throw e1;
    property_id = (row as { property_id: number } | null)?.property_id ?? null;
  }
  if (property_id == null) return { property_id: null, sources: [] };
  const { data, error } = await supabase
    .from('property_sources_public')
    .select(
      'id,property_id,sreality_id,source,source_url,source_id_native,is_active,price_czk,first_seen_at,last_seen_at',
    )
    .eq('property_id', property_id)
    .order('first_seen_at', { ascending: true });
  if (error) throw error;
  return { property_id, sources: (data ?? []) as unknown as PropertySource[] };
};

/* Snapshots across several listings (a property's children) — the union that
 * makes the Listing Detail price chart cross-source. Keyed on the surrogate
 * `listing_id` (listing_snapshots_public.listing_id, migration 343), NOT
 * sreality_id — the caller passes the children's surrogate ids, and a post-
 * Gate-2 non-sreality child has a NULL sreality_id (the old sreality filter
 * would then get `[null]` and the chart would silently go empty). */
export const fetchSnapshotsForListings = async (
  ids: number[],
): Promise<ListingSnapshotPublic[]> => {
  if (ids.length === 0) return [];
  const { data, error } = await supabase
    .from('listing_snapshots_public')
    .select('id,sreality_id,listing_id,scraped_at,price_czk,description')
    .in('listing_id', ids)
    .order('scraped_at', { ascending: true });
  if (error) throw error;
  return (data ?? []) as unknown as ListingSnapshotPublic[];
};

/* Property-grain activity log driving the price-history chart's inactive-
 * period gaps (migration 392's property_status_events, trigger-populated from
 * the SAME is_active aggregate Browse/badges already trust — see
 * priceHistory.buildActiveWindows). */
export const fetchPropertyStatusEvents = async (
  propertyId: number,
): Promise<PropertyStatusEventPublic[]> => {
  const { data, error } = await supabase
    .from('property_status_events_public')
    .select('property_id,is_active,event_at')
    .eq('property_id', propertyId)
    .order('event_at', { ascending: true });
  if (error) throw error;
  return (data ?? []) as unknown as PropertyStatusEventPublic[];
};

export const fetchFreshnessChecksByListing = async (
  sreality_id: number,
): Promise<ListingFreshnessCheckPublic[]> => {
  const { data, error } = await supabase
    .from('listing_freshness_checks_public')
    .select('id,sreality_id,checked_at,outcome')
    .eq('sreality_id', sreality_id)
    .order('checked_at', { ascending: true });
  if (error) throw error;
  return (data ?? []) as unknown as ListingFreshnessCheckPublic[];
};

/* Batch fetch of the listings_public rows behind a set of comparables.
 * Pulls the same field set as the detail page so the Estimate page's
 * comparable modal can render rich info without an extra round-trip
 * per listing. Returns a map keyed on sreality_id for O(1) lookup in
 * the renderer. */
export const fetchListingsByIds = async (
  ids: ReadonlyArray<number>,
): Promise<Map<number, ListingPublic>> => {
  if (ids.length === 0) return new Map();
  const { data, error } = await supabase
    .from('listings_public')
    .select(DETAIL_COLS)
    .in('sreality_id', ids as number[]);
  if (error) throw error;
  const out = new Map<number, ListingPublic>();
  for (const row of (data ?? []) as unknown as ListingPublic[]) {
    // Non-null by construction: the select above filters on sreality_id, so a
    // NULL-sreality row cannot be in `data`.
    out.set(row.sreality_id!, row);
  }
  return out;
};

/* Sibling of fetchListingsByIds keyed on the surrogate `id` (listings_public.id).
 * The estimation comparables track (RunPanel's ComparablesSection) carries
 * `listing_id` on every comparable emitted after R2 (#879/#892) — this is what
 * a comparable with a NULL sreality_id (post-Gate-2-flip non-sreality listing)
 * must be fetched through instead, mirroring fetchImagesForListingIds. */
export const fetchListingsForListingIds = async (
  ids: ReadonlyArray<number>,
): Promise<Map<number, ListingPublic>> => {
  if (ids.length === 0) return new Map();
  const { data, error } = await supabase
    .from('listings_public')
    .select(DETAIL_COLS)
    .in('id', ids as number[]);
  if (error) throw error;
  const out = new Map<number, ListingPublic>();
  for (const row of (data ?? []) as unknown as ListingPublic[]) {
    out.set(row.id, row);
  }
  return out;
};

/* Batch image fetch for the comparables modal — first three per id is
 * enough for the modal's thumbnail strip; the Listing Detail page
 * still pulls the full set independently. */
/* Columns every image fetch pulls from images_public — incl. the CLIP tag
 * (clip_fine_tag / clip_logical_tag / clip_confidence, migration 236) so every
 * photo surface can render its bottom-left tag badge from the same read. */
const IMAGE_PUBLIC_COLS =
  'id,sreality_id,sequence,sreality_url,storage_path,clip_fine_tag,clip_logical_tag,clip_confidence,clip_render_score,phash';

/* Batch image fetch keyed on `sreality_id` — kept for the callers whose upstream
 * read model is still sreality-keyed and carries no surrogate id. These
 * CANNOT cut to listing_id without a backend change to those payloads, and
 * flipping this loader in place would be a catastrophic half-swap (a
 * sreality_id fed into an `IN listing_id` matches a DIFFERENT listing,
 * id-spaces overlap). The Browse card path uses fetchImagesForListingIds
 * below instead — as does RunPanel's ComparablesSection now, for the subset
 * of comparables (ComparableUsed) that carry a `listing_id`; this loader
 * only covers the sreality_id-only fallback for pre-#879 frozen runs
 * (see RunPanel.fetchComparableImages). */
export const fetchImagesByListingIds = async (
  ids: ReadonlyArray<number>,
  perId = 3,
): Promise<Map<number, ImagePublic[]>> => {
  if (ids.length === 0) return new Map();
  const { data, error } = await supabase
    .from('images_public')
    .select(IMAGE_PUBLIC_COLS)
    .in('sreality_id', ids as number[])
    .order('sequence', { ascending: true, nullsFirst: false })
    .order('id', { ascending: true });
  if (error) throw error;
  const out = new Map<number, ImagePublic[]>();
  for (const row of (data ?? []) as unknown as ImagePublic[]) {
    const arr = out.get(row.sreality_id);
    if (arr) {
      if (arr.length < perId) arr.push(row);
    } else {
      out.set(row.sreality_id, [row]);
    }
  }
  return out;
};

/* Sibling of fetchImagesByListingIds keyed on the surrogate `listing_id`
 * (images_public.listing_id, migration 335). The Browse card hydration uses this
 * so a post-Gate-2 non-sreality card (sreality_id NULL) still gets its photos.
 * `listing_id` is appended to the select just for keying the result map. */
export const fetchImagesForListingIds = async (
  ids: ReadonlyArray<number>,
  perId = 3,
): Promise<Map<number, ImagePublic[]>> => {
  if (ids.length === 0) return new Map();
  const { data, error } = await supabase
    .from('images_public')
    .select(`${IMAGE_PUBLIC_COLS},listing_id`)
    .in('listing_id', ids as number[])
    .order('sequence', { ascending: true, nullsFirst: false })
    .order('id', { ascending: true });
  if (error) throw error;
  const out = new Map<number, ImagePublic[]>();
  for (const row of (data ?? []) as unknown as Array<ImagePublic & { listing_id: number }>) {
    const arr = out.get(row.listing_id);
    if (arr) {
      if (arr.length < perId) arr.push(row);
    } else {
      out.set(row.listing_id, [row]);
    }
  }
  return out;
};

/* W4: the ONE-cover-per-listing read (the card grid's 48px thumbnail),
 * against listing_cover_public — a server-side DISTINCT ON, not
 * fetchImagesForListingIds(ids, 1)'s fetch-every-image-then-discard.
 * Measured live: 44 real listing ids went from 901 image rows / 901
 * correlated CLIP-tag lookups / 3,995 buffers to 44 rows / 44 lookups / 788
 * buffers — the row count now equals what the card grid actually renders. */
/* NOTE for the W7a loader collapse: `listing_cover_public` is fast only when
 * the predicate is on `listing_id`. Its DISTINCT ON key is listing_id, but it
 * also projects `id` and `sreality_id` — a qual on either of those is applied
 * ABOVE the Unique node, so the full 10.4M-row images scan runs first and the
 * request dies on the 8s statement_timeout. Repointing a sreality-keyed loader
 * at this view is therefore not a drop-in. */
export const fetchListingCovers = async (
  ids: ReadonlyArray<number>,
): Promise<Map<number, ImagePublic>> => {
  if (ids.length === 0) return new Map();
  const { data, error } = await supabase
    .from('listing_cover_public')
    .select(`${IMAGE_PUBLIC_COLS},listing_id`)
    .in('listing_id', ids as number[]);
  if (error) throw error;
  const out = new Map<number, ImagePublic>();
  for (const row of (data ?? []) as unknown as Array<ImagePublic & { listing_id: number }>) {
    out.set(row.listing_id, row);
  }
  return out;
};

/* Per-side portal chips. Batched over the properties on screen (≤100), keyed
 * on property_id. property_sources_public
 * is one row per (child listing) of a property — post-merge a property spans
 * several portals, which is exactly what the chips show. */
export const fetchPropertySourcesByPropertyIds = async (
  ids: ReadonlyArray<number>,
): Promise<Map<number, PropertySource[]>> => {
  if (ids.length === 0) return new Map();
  const { data, error } = await supabase
    .from('property_sources_public')
    .select(
      'id,property_id,sreality_id,source,source_url,source_id_native,is_active,price_czk,first_seen_at,last_seen_at',
    )
    .in('property_id', ids as number[])
    .order('is_active', { ascending: false })
    .order('first_seen_at', { ascending: true });
  if (error) throw error;
  const out = new Map<number, PropertySource[]>();
  for (const row of (data ?? []) as unknown as PropertySource[]) {
    const arr = out.get(row.property_id);
    if (arr) arr.push(row);
    else out.set(row.property_id, [row]);
  }
  return out;
};

/* /clip-audit: the property feed — newest-first within ONE property type. Reads
 * browse_list, same read model as Browse. `category_main` is REQUIRED (no "all
 * types" option): browse_list's only covering index is `(category_main,
 * category_type, first_seen_at desc, property_id desc)` — measured live, a plain
 * `ORDER BY first_seen_at DESC` with no category filter falls back to a parallel
 * seq scan + sort (~3.5s cold on the full active cohort, over the anon 3s budget);
 * filtered by category_main it's a sub-ms index scan. Same keyset machinery as the
 * Browse table (lib/keyset) so paging behaves identically and stays index-matched. */
export interface ClipAuditPropertyRow {
  property_id: number;
  sreality_id: number;
  category_main: string;
  category_type: string | null;
  first_seen_at: string;
}

const CLIP_AUDIT_COLS = 'property_id,sreality_id,category_main,category_type,first_seen_at';
const CLIP_AUDIT_SORT: SortSpec = { field: 'first_seen_at', direction: 'desc' };
export const CLIP_AUDIT_PAGE_SIZE = 24;

export const fetchClipAuditProperties = async (
  categoryMain: string,
  cursor: KeysetCursor | null,
): Promise<{ rows: ClipAuditPropertyRow[]; nextCursor: KeysetCursor | null }> => {
  const base = supabase
    .from('browse_list')
    .select(withKeysetColumns(CLIP_AUDIT_COLS, CLIP_AUDIT_SORT))
    .eq('category_main', categoryMain);
  const keyed = applyKeyset(
    base as unknown as KeysetBuilder, CLIP_AUDIT_SORT, cursor,
  ) as unknown as typeof base;
  const { data, error } = await keyed.limit(CLIP_AUDIT_PAGE_SIZE);
  if (error) throw error;
  const rows = (data ?? []) as unknown as ClipAuditPropertyRow[];
  return {
    rows,
    nextCursor: nextCursorFrom(rows as unknown as Record<string, unknown>[], CLIP_AUDIT_SORT),
  };
};

/* /clip-audit: the operator's per-image correction/note (migration
 * 308), batched by the on-screen image ids. Keyed on image_id for O(1) lookup while
 * rendering the photo grid. */
export const fetchImageAnnotationsByImageIds = async (
  ids: ReadonlyArray<number>,
): Promise<Map<number, ImageAnnotation>> => {
  if (ids.length === 0) return new Map();
  const { data, error } = await supabase
    .from('image_tag_annotations_public')
    .select('image_id,tag_flagged,render_flagged,note,updated_at')
    .in('image_id', ids as number[]);
  if (error) throw error;
  const out = new Map<number, ImageAnnotation>();
  for (const row of (data ?? []) as unknown as ImageAnnotation[]) {
    out.set(row.image_id, row);
  }
  return out;
};

/* /clip-audit "Train": the operator's linear-probe training-set label per image
 * (migration 309), batched by the on-screen image ids. Also used to seed the label
 * combobox's "labels currently in use" suggestion list, alongside the CLIP taxonomy. */
export const fetchTrainingExamplesForImageIds = async (
  ids: ReadonlyArray<number>,
): Promise<Map<number, TrainingExample>> => {
  if (ids.length === 0) return new Map();
  const { data, error } = await supabase
    .from('image_training_examples_public')
    .select('image_id,label,updated_at')
    .in('image_id', ids as number[]);
  if (error) throw error;
  const out = new Map<number, TrainingExample>();
  for (const row of (data ?? []) as unknown as TrainingExample[]) {
    out.set(row.image_id, row);
  }
  return out;
};

/* The "Border case" button's flagged/unflagged state, batched per page-group —
 * same shape/read path as fetchTrainingExamplesForImageIds, just image_id-keyed
 * with no other payload (a border case carries no value of its own). */
export const fetchBorderCasesByImageIds = async (
  ids: ReadonlyArray<number>,
): Promise<Set<number>> => {
  if (ids.length === 0) return new Set();
  const { data, error } = await supabase
    .from('image_border_cases_public')
    .select('image_id')
    .in('image_id', ids as number[]);
  if (error) throw error;
  return new Set((data ?? []).map((r) => (r as BorderCase).image_id));
};

/* /clip-audit training-label browser: every image filed under ONE training label,
 * newest edit first. The property feed can't express this — a label's images are
 * scattered across the whole corpus (any category, any property, most of them far
 * past the loaded page), so auditing a label as a CLASS needs its own flat read.
 *
 * The cap is deliberately the SAME 500 as the bulk-relabel endpoint's batch cap, so
 * "select all" on a fully-loaded class can never be rejected as too large. The page
 * says so when a label actually hits it (biggest class today: 86). */
export const TRAINING_LABEL_PAGE_MAX = 500;

export const fetchTrainingExamplesByLabel = async (
  label: string,
): Promise<TrainingExample[]> => {
  const { data, error } = await supabase
    .from('image_training_examples_public')
    .select('image_id,label,updated_at')
    .eq('label', label)
    .order('updated_at', { ascending: false })
    .limit(TRAINING_LABEL_PAGE_MAX);
  if (error) throw error;
  return (data ?? []) as unknown as TrainingExample[];
};

/* The images behind those examples. Sibling of fetchImagesByListingIds — same view
 * and columns, keyed on the image's own id instead of its listing's. */
export const fetchImagesByImageIds = async (
  ids: ReadonlyArray<number>,
): Promise<Map<number, ImagePublic>> => {
  if (ids.length === 0) return new Map();
  const { data, error } = await supabase
    .from('images_public')
    .select(IMAGE_PUBLIC_COLS)
    .in('id', ids as number[]);
  if (error) throw error;
  const out = new Map<number, ImagePublic>();
  for (const row of (data ?? []) as unknown as ImagePublic[]) out.set(row.id, row);
  return out;
};

export interface TrainingLabelCount {
  label: string;
  count: number;
}

/* /clip-audit's "Jen v trénovací sadě" Tag row: how many examples exist per label —
 * lets the operator judge class coverage ("is this one big enough yet?") while
 * building the set. A GLOBAL count (the whole training set), not scoped to the
 * current Hamming-range/outcome/category filters — coverage is a property of the
 * training set itself, not of whichever window is currently being browsed. Same
 * underlying read as fetchDistinctTrainingLabels (see its comment re: bound), just
 * aggregated client-side instead of deduped, since the table is still small. */
export const fetchTrainingLabelCounts = async (): Promise<TrainingLabelCount[]> => {
  /* Exhaustive — the labeling program grows this daily (1,198 rows when the
   * old silent `.limit(2000)` was replaced; it was the nearest-term casualty). */
  const rows = await fetchAllRows<{ image_id: number; label: string }>({
    relation: 'image_training_examples_public',
    build: () =>
      supabase
        .from('image_training_examples_public')
        .select('image_id,label', { count: 'exact' }),
    orderBy: [{ column: 'image_id' }],
    key: ['image_id'],
    expectMax: 250_000,
  });
  const counts = new Map<string, number>();
  for (const row of rows) {
    counts.set(row.label, (counts.get(row.label) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([label, count]) => ({ label, count }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label, 'cs'));
};

/* Keyed on listing_id, not sreality_id (R2 Phase C resolver-chain cutover;
 * migration 335 exposes it on images_public).
 *
 * W7a: this is now a one-id call into fetchImagesForListingIds rather than a
 * fourth hand-rolled copy of the same select + double order-by. The two differed
 * only in `.eq` vs `.in` and in the uncapped result — so `perId: Infinity`,
 * which the retention loop reads as "keep them all". The listing gallery must
 * stay uncapped: it renders every photo, and a cap here would silently truncate
 * the lightbox. */
export const fetchImagesByListing = async (
  listing_id: number,
): Promise<ImagePublic[]> => {
  const byListing = await fetchImagesForListingIds([listing_id], Infinity);
  return byListing.get(listing_id) ?? [];
};

/* -------------------------------------------------------------------------- */
/* Health dashboard (Part E) — calls migration 013 health_summary RPC         */
/* -------------------------------------------------------------------------- */

export const fetchHealthSummary = async (): Promise<HealthSummary> => {
  const { data, error } = await supabase.rpc('health_summary');
  if (error) throw error;
  return data as HealthSummary;
};

/* browse_read_model_state_public (migration 276) — the blue-green rebuild's
 * only refresh evidence, since the DROP+CREATE cycle destroys pg_stat
 * history on every swap. Written by rebuild_browse_list()/
 * rebuild_properties_map_mv() themselves; a RAISE inside either (migration
 * 374's anon-grant self-check) rolls back the whole tick, so a stalled
 * list_rebuilt_at/map_rebuilt_at here is exactly the signal that a rebuild
 * has started failing — surfaced on Health via StaleHealthDataBanner. */
export const fetchBrowseReadModelState = async (): Promise<BrowseReadModelState> => {
  const { data, error } = await supabase
    .from('browse_read_model_state_public')
    .select('list_rebuilt_at, list_duration_ms, list_rows, map_rebuilt_at, map_duration_ms, map_rows')
    .single();
  if (error) throw error;
  return data as BrowseReadModelState;
};

export const fetchRecentScrapeRuns = async (
  days: number = 14,
): Promise<ScrapeRun[]> => {
  const { data, error } = await supabase.rpc('recent_scrape_runs', { p_days: days });
  if (error) throw error;
  return (data ?? []) as ScrapeRun[];
};

export const fetchCategoryTrends = async (
  source: string = 'sreality',
): Promise<CategoryTrend[]> => {
  const { data, error } = await supabase.rpc('category_trends', { p_source: source });
  if (error) throw error;
  return (data ?? []) as CategoryTrend[];
};

export const fetchImageStorageOverview = async (): Promise<ImageStorageOverview> => {
  const { data, error } = await supabase.rpc('image_storage_overview');
  if (error) throw error;
  return data as ImageStorageOverview;
};

export const fetchImagesFailureOverview = async (): Promise<ImageFailureRow[]> => {
  const { data, error } = await supabase.rpc('images_failure_overview');
  if (error) throw error;
  return (data ?? []) as ImageFailureRow[];
};

export const fetchPortalHealth = async (): Promise<PortalHealth[]> => {
  const { data, error } = await supabase.rpc('portal_health_summary');
  if (error) throw error;
  return (data ?? []) as PortalHealth[];
};

export const fetchScraperHealthChecks = async (
  source: string = 'sreality',
): Promise<ScraperHealthChecks> => {
  const { data, error } = await supabase.rpc('scraper_health_checks', { p_source: source });
  if (error) throw error;
  return data as ScraperHealthChecks;
};

/* Migration 274 — pipeline verification checks (latest row per check_key).
 * The DB stamps the ok/warn/fail status + a `value` whose unit is check-specific
 * (ratios, minutes, counts — see scripts/verify_pipeline.py). */
export interface PipelineCheckRow {
  check_key: string;
  status: string;
  value: number | null;
  details: Record<string, unknown> | null;
  run_at: string | null;
}

export const fetchPipelineChecks = async (): Promise<PipelineCheckRow[]> => {
  const { data, error } = await supabase
    .from('pipeline_checks_public')
    .select('check_key,status,value,details,run_at')
    .order('check_key', { ascending: true });
  if (error) throw error;
  return (data ?? []) as PipelineCheckRow[];
};

/* Migration 178 — failed GitHub Actions runs recorded by the 30-min poller
 * (monitor_workflow_failures.yml). */
export interface WorkflowFailureRow {
  workflow_name: string;
  conclusion: string;
  run_started_at: string | null;
  html_url: string | null;
}

export const fetchRecentWorkflowFailures = async (
  hours: number = 48,
): Promise<WorkflowFailureRow[]> => {
  const { data, error } = await supabase.rpc('recent_workflow_failures', { p_hours: hours });
  if (error) throw error;
  return (data ?? []) as WorkflowFailureRow[];
};

/* Migration 220 — streak-aware per-workflow failure summary. One row per
 * workflow with the consecutive-failure streak + is_chronic flag, so the Health
 * card can separate a chronic break (failing every run for days) from a 1%
 * self-healing transient. Supersedes recent_workflow_failures for the card. */
export interface WorkflowFailureSummaryRow {
  workflow_path: string;
  workflow_name: string;
  failure_count: number;
  first_failure_at: string | null;
  last_failure_at: string | null;
  last_conclusion: string;
  last_html_url: string | null;
  last_success_at: string | null;
  consecutive_failures: number;
  is_chronic: boolean;
}

export const fetchWorkflowFailureSummary = async (
  hours: number = 168,
): Promise<WorkflowFailureSummaryRow[]> => {
  const { data, error } = await supabase.rpc('workflow_failure_summary', { p_hours: hours });
  if (error) throw error;
  return (data ?? []) as WorkflowFailureSummaryRow[];
};

export const ping = async (): Promise<{ ok: boolean; count: number | null }> => {
  const { count, error } = await supabase
    .from('listings_public')
    .select('*', { count: 'exact', head: true });
  return { ok: !error, count: count ?? null };
};

/* -------------------------------------------------------------------------- */
/* Phase QUAL — curated cities (operator-curated qualitative indexes +        */
/* population). Browse map renders matching cities as a separate pin layer;   */
/* the filter UI picks rules + an optional color-coding index.                */
/* -------------------------------------------------------------------------- */

export interface CuratedCity {
  city_id: number;
  name: string;
  kraj_name: string;
  lat: number;
  lng: number;
  default_radius_m: number;
  population: number | null;
  population_as_of_year: number | null;
  /* The RÚIAN obec this curated city was matched to (migration 081), or null
   * when the name match failed. The view has always exposed it and
   * fetchCuratedCities has always `select('*')`-ed it — it was simply absent
   * from this type. It is the join key that lets a property carry its city's
   * indexes without a PostGIS round-trip: `properties.obec_id =
   * curated_cities.admin_boundary_id`. All 206 curated cities have one, so the
   * centroid+radius fallback arm in the SQL predicates is currently dead. */
  admin_boundary_id: number | null;
}

export interface CityIndexDefinition {
  index_name: string;
  label_cs: string;
  label_en: string | null;
  category: 'overall' | 'health_env' | 'material_edu' | 'services_relations' | 'sub_index';
  scale_min: number;
  scale_max: number;
  higher_is_better: boolean;
  sort_order: number;
  description: string | null;
}

export interface CityIndexValue {
  city_id: number;
  index_name: string;
  value: number;
}

/* Query keys for the three operator-static city-quality datasets. They were
 * inline string literals in BrowseExperience; anything else wanting the same
 * cached rows (the pipeline board's index strip) has to spell them identically
 * or it silently refetches into a parallel cache entry. */
export const cityQualityKeys = {
  cities: ['curated_cities'] as const,
  definitions: ['city_index_definitions'] as const,
  values: ['city_index_values'] as const,
};

export const fetchCuratedCities = async (): Promise<CuratedCity[]> => {
  /* 205 rows today; operator uploads grow the set. Two obce can share a name
   * (see the same-name-obce price-stats fix), hence the city_id tiebreak. */
  return await fetchAllRows<CuratedCity>({
    relation: 'curated_cities_public',
    build: () => supabase.from('curated_cities_public').select('*', { count: 'exact' }),
    orderBy: [{ column: 'name' }, { column: 'city_id' }],
    key: ['city_id'],
    expectMax: 25_000,
  });
};

export const fetchCityIndexDefinitions = async (): Promise<CityIndexDefinition[]> => {
  /* ~33 rows today, operator additions grow it. Display order is sort_order,
   * applied by the consumers — the fetch orders by the unique name for paging. */
  return await fetchAllRows<CityIndexDefinition>({
    relation: 'city_index_definitions_public',
    build: () =>
      supabase.from('city_index_definitions_public').select('*', { count: 'exact' }),
    orderBy: [{ column: 'index_name' }],
    key: ['index_name'],
    expectMax: 25_000,
  });
};

export const fetchCityIndexValues = async (): Promise<CityIndexValue[]> => {
  /* 205 cities × 33 indexes = 6,798 rows. This read shipped THE truncation bug
   * fetchAllRows exists to kill (only the first ~32 cities came back under the
   * then-1,000-row db-max-rows; Dobříš showed em-dashes for every index).
   * Cached (staleTime: Infinity), so the handful of pages is first-load only. */
  return await fetchAllRows<CityIndexValue>({
    relation: 'city_index_values_public',
    build: () =>
      supabase
        .from('city_index_values_public')
        .select('city_id,index_name,value', { count: 'exact' }),
    orderBy: [{ column: 'city_id' }, { column: 'index_name' }],
    key: ['city_id', 'index_name'],
    expectMax: 100_000,
  });
};

export interface CityPolygon {
  city_id: number;
  geojson: string;
}

export const fetchCuratedCityPolygons = async (): Promise<CityPolygon[]> => {
  /* One simplified municipality boundary per curated city (205 rows).
   * `geojson` is the raw ST_AsGeoJSON string the map JSON.parses into a
   * Feature geometry — the same contract as rent_map_choropleth_public.
   * Fetched once and cached (staleTime: Infinity), and only when the map tab
   * is active. */
  return await fetchAllRows<CityPolygon>({
    relation: 'curated_city_polygons_public',
    build: () =>
      supabase
        .from('curated_city_polygons_public')
        .select('city_id,geojson', { count: 'exact' }),
    orderBy: [{ column: 'city_id' }],
    key: ['city_id'],
    expectMax: 25_000,
  });
};

/* -------------------------------------------------------------------------- */
/* MF rent-price choropleth ("Cenová mapa nájemného"). One polygon per Czech  */
/* obec / katastrální území, coloured by reference rent (Kč/m²) per size      */
/* category VK1..VK4. The optional kraj overlay draws the 14 region borders.  */
/* Both are operator-static reference datasets — fetch once, cache forever    */
/* (staleTime: Infinity in the Browse useQuery). `geojson` is the raw         */
/* ST_AsGeoJSON string; the map layer JSON.parses it into a Feature geometry. */
/* -------------------------------------------------------------------------- */

export interface RentMapPolygon {
  ruian_code: number;
  level: 'ku' | 'obec';
  name: string;
  kraj: string | null;
  geojson: string;
  vk1_per_m2: number | null;
  vk2_per_m2: number | null;
  vk3_per_m2: number | null;
  vk4_per_m2: number | null;
}

export interface RentMapKraj {
  ruian_code: number;
  name: string;
  geojson: string;
}

export const fetchRentMapChoropleth = async (): Promise<RentMapPolygon[]> => {
  /* ~7,630 rows (one per obec / katastrální území) — the OTHER read that
   * shipped the db-max-rows truncation bug (see fetchAllRows' header).
   * Fetched once and cached (staleTime: Infinity), so the handful of pages
   * only happens when the operator first enables the layer. */
  return await fetchAllRows<RentMapPolygon>({
    relation: 'rent_map_choropleth_public',
    build: () =>
      supabase.from('rent_map_choropleth_public').select('*', { count: 'exact' }),
    orderBy: [{ column: 'ruian_code' }],
    key: ['ruian_code'],
    expectMax: 100_000,
  });
};

export const fetchRentMapKraje = async (): Promise<RentMapKraj[]> => {
  /* 14 kraje — fixed by geography. */
  return await fetchAllRows<RentMapKraj>({
    relation: 'rent_map_kraje_public',
    build: () => supabase.from('rent_map_kraje_public').select('*', { count: 'exact' }),
    orderBy: [{ column: 'ruian_code' }],
    key: ['ruian_code'],
    expectMax: 1_000,
  });
};

/* -------------------------------------------------------------------------- */
/* Estimations (U2). Hits the Railway FastAPI service via lib/api.ts; pages   */
/* combine these helpers with useQuery / useMutation directly, matching the   */
/* convention used by Supabase fetchers above.                                */
/* -------------------------------------------------------------------------- */

import {
  useMutation,
  useQuery,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import {
  ApiError,
  createEstimation,
  getEstimation,
  getTracePayload,
  listEstimations,
  previewListingUrl,
  type TracePayload,
} from './api';
import type {
  CreateEstimationIn,
  EstimationListParams,
  ParseResult,
  PipelineBoardCard,
  PipelineStage,
  TagColor,
} from './types';

export const estimationKeys = {
  all: ['estimations'] as const,
  list: (params: EstimationListParams) =>
    ['estimations', 'list', params] as const,
  byListing: (ids: ReadonlyArray<number>) =>
    ['estimations', 'by-listing', ids] as const,
  detail: (id: number) =>
    ['estimations', 'detail', id] as const,
  preview: (url: string) =>
    ['estimations', 'preview', url] as const,
  tracePayload: (runId: number, stepN: number) =>
    ['estimations', 'detail', runId, 'trace', stepN, 'payload'] as const,
};

export const fetchEstimation = (id: number) => getEstimation(id);

export const useTracePayload = (
  runId: number,
  stepN: number,
  enabled: boolean,
): UseQueryResult<TracePayload, ApiError> =>
  useQuery<TracePayload, ApiError>({
    queryKey: estimationKeys.tracePayload(runId, stepN),
    queryFn: () => getTracePayload(runId, stepN),
    enabled,
    staleTime: Infinity,
  });
export const fetchEstimationsList = (params: EstimationListParams) =>
  listEstimations(params);

/* Property-grain run fetch for the Listing Detail estimations section:
 * every run on any of the property's child listings, newest first. List
 * rows carry the full run projection (minus source_html), so the section
 * renders the selected run without a second per-run request. */
export const fetchEstimationsForListings = (ids: ReadonlyArray<number>) =>
  listEstimations({ listing_ids: ids.join(','), limit: 100 });
export const submitEstimation = (input: CreateEstimationIn) =>
  createEstimation(input);

export interface UrlPreviewVars {
  url: string;
  force_refresh?: boolean;
}

/* Mutation wrapper around POST /estimations/preview. Pages call
 * `mutate({ url })` for a normal preview and `mutate({ url, force_refresh: true })`
 * for the bypass-cache path. The mutation isn't keyed (TanStack
 * Query mutations aren't), so re-running the same URL never reads
 * a stale React-Query cache — the cache decision lives entirely on
 * the backend's parsed_url_cache table. */
export const useUrlPreview = (): UseMutationResult<
  ParseResult, ApiError, UrlPreviewVars
> =>
  useMutation<ParseResult, ApiError, UrlPreviewVars>({
    mutationFn: ({ url, force_refresh }) =>
      previewListingUrl(url, { force_refresh }),
  });

/* -------------------------------------------------------------------------- */
/* Curation (U2.6) — read paths.                                              */
/*                                                                            */
/* The "list collections / tags / notes" indices go through the bearer-gated  */
/* FastAPI service (lib/api.ts) so listing_count + ordering live in one      */
/* place. The reverse-index queries below — "which tags / collections does    */
/* property X belong to" — read directly from the property-grain *_public      */
/* views via the anon key, matching the read-only pattern Browse / Region use. */
/* The `properties_with_tags(tag_ids)` RPC powers the Browse "tags" facet:     */
/* AND-semantics across the supplied ids, capped at 5000 rows on the server.   */
/* -------------------------------------------------------------------------- */

export const fetchPropertyTagIds = async (
  property_id: number,
): Promise<number[]> => {
  const { data, error } = await supabase
    .from('property_tags_public')
    .select('tag_id')
    .eq('property_id', property_id);
  if (error) throw error;
  return ((data ?? []) as Array<{ tag_id: number }>).map((r) => r.tag_id);
};

export const fetchPropertyCollectionIds = async (
  property_id: number,
): Promise<number[]> => {
  const { data, error } = await supabase
    .from('collection_properties_public')
    .select('collection_id')
    .eq('property_id', property_id);
  if (error) throw error;
  return ((data ?? []) as Array<{ collection_id: number }>).map(
    (r) => r.collection_id,
  );
};

/* All (property_id → collection_ids) memberships in ONE read, shared (React
 * Query dedupes the key) by every Browse-card collection control — the
 * collection analogue of fetchPipelineMemberSet, so Browse fires one query
 * instead of one-per-card. */
export const fetchPropertyCollectionMemberSet = async (): Promise<
  Map<number, number[]>
> => {
  const rows = await fetchAllRows<{ property_id: number; collection_id: number }>({
    relation: 'collection_properties_public',
    build: () =>
      supabase
        .from('collection_properties_public')
        .select('property_id, collection_id', { count: 'exact' }),
    orderBy: [{ column: 'property_id' }, { column: 'collection_id' }],
    key: ['property_id', 'collection_id'],
    expectMax: 100_000,
  });
  const map = new Map<number, number[]>();
  for (const r of rows) {
    const arr = map.get(r.property_id);
    if (arr) arr.push(r.collection_id);
    else map.set(r.property_id, [r.collection_id]);
  }
  return map;
};

export const watchdogKeys = {
  all: ['watchdog'] as const,
  subscriptions: ['watchdog', 'subscriptions'] as const,
  subscription: (id: string) => ['watchdog', 'subscriptions', id] as const,
  dispatches: (params: Record<string, unknown>) =>
    ['watchdog', 'dispatches', params] as const,
};

/* Unified notifications feed (watchdog matches + collection-monitor events). */
export const notificationKeys = {
  all: ['notifications'] as const,
  feed: (params: Record<string, unknown>) =>
    ['notifications', 'feed', params] as const,
  unreadCount: ['notifications', 'unread-count'] as const,
};

export const filterPresetKeys = {
  all: ['filter-presets'] as const,
};

export const curationKeys = {
  collections: ['curation', 'collections'] as const,
  collection: (id: number) => ['curation', 'collection', id] as const,
  tags: ['curation', 'tags'] as const,
  propertyTags: (property_id: number) =>
    ['curation', 'property-tags', property_id] as const,
  propertyCollections: (property_id: number) =>
    ['curation', 'property-collections', property_id] as const,
  propertyCollectionMembers: ['curation', 'property-collection-members'] as const,
  propertyNotes: (property_id: number) =>
    ['curation', 'property-notes', property_id] as const,
  manualEstimates: (sreality_id: number) =>
    ['curation', 'manual-estimates', sreality_id] as const,
};

/* Deal pipeline (migration 205). The "is this property bookmarked + at which   */
/* stage" read pulls from property_pipeline_public via the anon key; writes go   */
/* through the FastAPI service. Single-valued — at most one card per property.
 *
 * W3 collapsed the per-property `card(id)` key into `members` — a single
 * property's card is just `members.get(id)`. The two used to be separate
 * reads (fetchPropertyPipeline + fetchPipelineMembers) that once drifted
 * out of sync on which columns they selected (a property badged "9" on a
 * card and "5" in its own header) — one shared query now backs every
 * surface that needs a property's pipeline state, so that class of bug
 * can't recur. */
export const pipelineKeys = {
  board: ['pipeline', 'board'] as const,
  stages: ['pipeline', 'stages'] as const,
  members: ['pipeline', 'members'] as const,
};

/* Every pipeline card the caller's account holds, keyed by property_id — one
 * cheap read shared (React Query dedupes the key) by every funnel on every
 * surface: Browse cards, the Table rows, and the pipeline scope's prefilter.
 *
 * It carries the stage, not just membership, because the funnel renders the
 * stage badge (migration 377) — the earlier "set of ids" shape forced each
 * surface to either show a colourless funnel or issue its own per-property
 * read. `property_pipeline_public` is RLS-scoped + security_invoker (migration
 * 316), so this returns the caller's own board and nothing else.
 *
 * Exhaustive via fetchAllRows for the same reason the tag prefilter is: a
 * silently truncated membership map would both blank funnels and, once the
 * pipeline scope is on, drop properties the operator explicitly asked to see. */
export interface PipelineMembership {
  property_id: number;
  stage_id: number;
  stage_label: string;
  stage_color: TagColor | null;
  stage_code: string | null;
  stage_position: number;
  is_terminal: boolean;
}

export type PipelineMembers = Map<number, PipelineMembership>;

export const fetchPipelineMembers = async (): Promise<PipelineMembers> => {
  const rows = await fetchAllRows<PipelineMembership>({
    relation: 'property_pipeline_public',
    build: () =>
      supabase
        .from('property_pipeline_public')
        .select(
          'property_id, stage_id, stage_label, stage_color, stage_code, stage_position, is_terminal',
          { count: 'exact' },
        ),
    orderBy: [{ column: 'property_id' }],
    key: ['property_id'],
    expectMax: 100_000,
  });
  return new Map(rows.map((r) => [r.property_id, r]));
};

export const fetchPipelineStages = async (): Promise<PipelineStage[]> => {
  const { data, error } = await supabase
    .from('pipeline_stages_public')
    /* `code` too (migration 377): the stage menu badges every row from this
     * list, and the stage editor pre-fills its code box from it — omitting the
     * column rendered every existing code as blank, inviting the operator to
     * overwrite intentional badges (three stages deliberately share "9"). */
    .select('id, key, label, position, color, is_terminal, is_entry, code')
    .order('position');
  if (error) throw error;
  return (data ?? []) as PipelineStage[];
};

/* The kanban's STRUCTURAL read: which property sits in which stage, plus the
 * display fields the board can filter and sort on. ONE PostgREST read
 * (pipeline_board_public, migration 417, W5) — the pipeline/property join
 * that used to be two sequential round trips composed client-side now
 * happens server-side, so the second request never has to wait on the
 * first's ids.
 *
 * Decorations are deliberately NOT here. This function used to await, in one
 * promise, the cover images and two broker calls as well: six serialized
 * cross-origin round trips before the board could paint a single column, the
 * last of which existed only to fill a hover tooltip. They now load through
 * lib/hydration as independent non-blocking queries keyed on listing_id, so
 * the board paints as soon as this resolves and the thumbnails and broker
 * lines arrive behind it. Anything added to a card from here on is a
 * decoration until proven structural: if the board cannot filter, sort or
 * place a card without it, it does not belong in this queryFn. */
export const fetchPipelineBoard = async (): Promise<PipelineBoardCard[]> => {
  /* board_position is the MANUAL order and stays the default sort, but it is
   * not unique — it is assigned max+1 within the entry stage at bookmark time
   * and never renumbered on a stage move, so live data has collisions WITHIN
   * a stage. property_id is the deterministic tiebreak; without it equal
   * positions reshuffle between refetches. Any explicit sort re-sorts
   * client-side (lib/pipelineSort) and tiebreaks the same way. */
  const rows = await fetchAllRows<PipelineBoardRow>({
    relation: 'pipeline_board_public',
    build: () =>
      supabase.from('pipeline_board_public').select(PIPELINE_BOARD_COLS, { count: 'exact' }),
    orderBy: [{ column: 'board_position' }, { column: 'property_id' }],
    key: ['property_id'],
    expectMax: 100_000,
  });
  return composePipelineCards(rows);
};

/* Project ONE batched broker read onto a card's broker block (W6).
 *
 * There used to be a second argument — the contact row from a chained
 * /brokers?ids= call. Migration 419 put primary_email / primary_phone on
 * listing_broker_public, so identity and contact arrive together and the second
 * round trip is gone; a card can no longer be in the split state where it knows
 * the broker's name but not whether he is reachable.
 *
 * `has_email`/`has_phone` arrive INSTEAD of the values for a non-admin caller and
 * are absent for an admin — so derive the flag from whichever the API sent, and
 * the card can then say "contact exists, admin only" rather than showing nothing.
 * A card with no resolved broker (private bazos seller, or a failed enrichment
 * read) stays null. */
export const pipelineCardBroker = (
  lb: ListingBroker | undefined,
): PipelineCardBroker | null =>
  lb
    ? {
        broker_id: lb.broker_id,
        display_name: lb.broker_display_name,
        firm_label: lb.broker_firm_label,
        email: lb.primary_email ?? null,
        phone: lb.primary_phone ?? null,
        has_email: lb.has_email ?? Boolean(lb.primary_email),
        has_phone: lb.has_phone ?? Boolean(lb.primary_phone),
      }
    : null;

/* ---- LLM cost dashboard (/costs) -------------------------------------- */

/* Daily × feature × model spend aggregates from `llm_cost_daily_public`
 * (migration 280). numeric/bigint arrive as strings from PostgREST in
 * some paths — coerce every measure to a number once, here. */
export const fetchLlmCostDaily = async (days: number): Promise<LlmCostDailyRow[]> => {
  const from = new Date();
  from.setUTCDate(from.getUTCDate() - days);
  const { data, error } = await supabase
    .from('llm_cost_daily_public')
    .select('*')
    .gte('day', from.toISOString().slice(0, 10))
    .order('day', { ascending: true });
  if (error) throw error;
  return (data ?? []).map((r: Record<string, unknown>) => ({
    day: String(r.day),
    called_for: String(r.called_for),
    provider: String(r.provider),
    model: String(r.model),
    calls: Number(r.calls ?? 0),
    error_calls: Number(r.error_calls ?? 0),
    cost_usd: Number(r.cost_usd ?? 0),
    input_tokens: Number(r.input_tokens ?? 0),
    output_tokens: Number(r.output_tokens ?? 0),
    cache_read_tokens: Number(r.cache_read_tokens ?? 0),
    cache_write_tokens: Number(r.cache_write_tokens ?? 0),
  }));
};

/* Hour-grain twin from `llm_cost_hourly_public` (migration 281); the
 * bucket timestamptz is normalized to a canonical ISO so client-side
 * zero-filling can key on exact string equality. */
export const fetchLlmCostHourly = async (hours: number): Promise<LlmCostHourlyRow[]> => {
  const from = new Date();
  from.setUTCMinutes(0, 0, 0);
  from.setUTCHours(from.getUTCHours() - hours);
  const { data, error } = await supabase
    .from('llm_cost_hourly_public')
    .select('*')
    .gte('bucket', from.toISOString())
    .order('bucket', { ascending: true });
  if (error) throw error;
  return (data ?? []).map((r: Record<string, unknown>) => ({
    bucket: new Date(String(r.bucket)).toISOString(),
    called_for: String(r.called_for),
    provider: String(r.provider),
    model: String(r.model),
    calls: Number(r.calls ?? 0),
    error_calls: Number(r.error_calls ?? 0),
    cost_usd: Number(r.cost_usd ?? 0),
    input_tokens: Number(r.input_tokens ?? 0),
    output_tokens: Number(r.output_tokens ?? 0),
    cache_read_tokens: Number(r.cache_read_tokens ?? 0),
    cache_write_tokens: Number(r.cache_write_tokens ?? 0),
  }));
};
