import { supabase } from './supabase';
import { imageSrc } from './imageUrl';
import { type TaggedImageUrl } from './imageTags';
import { fetchListingBrokersByIds, fetchBrokersByIds } from './brokers';
import type { ListingDetailLite } from './dedupDiff';
import type { LlmCostDailyRow } from './llmCosts';
import {
  type CenterRadius,
  type DistrictChip,
  type ListingFilters,
  type MapBounds,
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
  CategoryTrend,
  HealthSummary,
  ImageFailureRow,
  ImagePublic,
  ImageStorageOverview,
  ListingFreshnessCheckPublic,
  ListingPublic,
  ListingSnapshotPublic,
  MfReferenceRent,
  PortalHealth,
  PropertySource,
  Ppm2Box,
  ScrapeRun,
  ScraperHealthChecks,
} from './types';

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

const MAP_COLS = 'sreality_id,lat,lng,price_czk,disposition,subtype,area_m2,district,last_seen_at,is_active,tom_days';
const TABLE_COLS =
  'sreality_id,district,locality,obec,okres,street,disposition,subtype,area_m2,price_czk,first_seen_at,last_seen_at,is_active,tom_days,' +
  'estate_area,usable_area,parking_lots,furnished,ownership,category_sub_cb,building_type';
const CARD_COLS =
  'property_id,sreality_id,district,locality,obec,okres,street,disposition,subtype,area_m2,price_czk,first_seen_at,last_seen_at,is_active,tom_days,' +
  'category_main,category_type,source,mf_gross_yield_pct';

export type SortField =
  | 'sreality_id' | 'district' | 'disposition'
  | 'area_m2' | 'price_czk' | 'price_per_m2'
  | 'first_seen_at' | 'last_seen_at' | 'is_active'
  | 'estate_area' | 'usable_area' | 'parking_lots'
  | 'mf_gross_yield_pct';

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

const SORTABLE_FIELDS: ReadonlyArray<SortField> = [
  'sreality_id', 'district', 'disposition',
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
  sreality_id: number;
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
  /* PostgREST applies a server-configured `db-max-rows` cap on every
   * response — Supabase's default is 1,000. With ~62k listings in
   * the table, a tag matched widely enough would silently truncate
   * the prefilter id list and bleed listings the operator asked to
   * exclude back into the cohort. `.range(0, 99999)` bypasses the
   * cap; the result is capped client-side instead, headroom for any
   * conceivable future cohort. */
  const { data, error } = await supabase
    .rpc('properties_with_tags', { tag_ids: f.tags })
    .range(0, 99999);
  if (error) throw error;
  return ((data ?? []) as Array<{ property_id: number }>).map(
    (r) => r.property_id,
  );
}

/* Phase QUAL — `listings_with_city_quality` RPC prefilter. Same
 * composition pattern as the tags prefilter above: when ANY city-quality
 * predicate is active, the RPC returns the sreality_id allowlist and the
 * main listings query AND's it via `.in('sreality_id', ids)`. Returns
 * null when no city-quality filter is set so the fast path stays
 * unchanged. */
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
  /* Filters carry the wire shape (snake_case) directly so no
   * translation layer is needed before calling the RPC. `.range`
   * bypasses PostgREST's default 1,000-row cap on the SETOF
   * response — same reason `resolveTagPrefilter` does it. */
  const { data, error } = await supabase
    .rpc('listings_with_city_quality', {
      p_index_rules: f.cityIndexRules.length === 0 ? null : f.cityIndexRules,
      /* pop bounds moved to the home_obec_pop column filter (migration 142);
       * never sent through this RPC anymore. */
      p_pop_min: null,
      p_pop_max: null,
      p_proximity: f.nearCityProximity,
    })
    .range(0, 99999);
  if (error) throw error;
  return ((data ?? []) as Array<{ sreality_id: number }>).map(
    (r) => r.sreality_id,
  );
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
  const { data, error } = await supabase
    .from('property_estimates_public')
    .select('property_id')
    .range(0, 99999);
  if (error) throw error;
  return ((data ?? []) as Array<{ property_id: number }>).map(
    (r) => r.property_id,
  );
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
interface BrowsePrefilters {
  srealityIds: number[] | null;   // city-quality (representative-listing grain)
  obecIds: number[] | null;       // market growth (price-stats datasets)
  propertyIds: number[] | null;   // tags ∩ with-estimates (property grain)
  empty: boolean;
}

async function resolveBrowsePrefilters(
  f: ListingFilters,
): Promise<BrowsePrefilters> {
  const [tagProps, cityIds, growthObec, estimateProps] = await Promise.all([
    resolveTagPrefilter(f),
    resolveCityQualityPrefilter(f),
    resolvePriceGrowthPrefilter(f),
    resolveEstimatesPrefilter(f),
  ]);
  // Tags are now property-grain (properties_with_tags) — intersect them with the
  // with-estimates property prefilter and apply via .in('property_id', …).
  // City-quality stays representative-listing grain (.in('sreality_id', …)).
  const propertyIds = intersectPrefilters(tagProps, estimateProps);
  const empty =
    (cityIds != null && cityIds.length === 0)
    || (growthObec != null && growthObec.length === 0)
    || (propertyIds != null && propertyIds.length === 0);
  return { srealityIds: cityIds, obecIds: growthObec, propertyIds, empty };
}

const applyPrefilters = <T>(q: T, p: BrowsePrefilters): T => {
  let r = q as unknown as {
    in: (c: string, v: readonly unknown[]) => typeof r;
  };
  if (p.srealityIds != null) r = r.in('sreality_id', p.srealityIds);
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
    .from('browse_list')
    .select('property_id', { count: 'exact', head: true });
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
  const base = supabase
    .from('properties_map_mv')
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
  sreality_id: number;
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
   * scrape cycle), rebuilt every 5 min from browse_projection. */
  const base = supabase
    .from('browse_list')
    .select(withKeysetColumns(TABLE_COLS, sort));
  const scoped = applyPrefilters(applyFilters(base, f), pre);
  const keyed = applyKeyset(
    scoped as unknown as KeysetBuilder,
    sort,
    cursor,
  ) as unknown as typeof scoped;
  const { data, error } = await keyed.limit(TABLE_PAGE_SIZE);
  if (error) throw error;
  const rows = (data ?? []) as unknown as TableRow[];
  return {
    rows,
    nextCursor: nextCursorFrom(rows as unknown as Record<string, unknown>[], sort),
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
          .from('browse_list')
          .select('property_id', { count: mode, head: true }),
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
   * properties_public). Used by the Browse merge-mode dedup action. */
  property_id: number;
  sreality_id: number;
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
  /* MF gross rental yield % (migration 133). Non-null only on sale
   * apartments that resolved to an MF territory. */
  mf_gross_yield_pct: number | null;
  /* Per-image render data (url + CLIP tag + confidence) in source-sequence
   * order. Empty when the listing has no photos yet. The card uses index 0 by
   * default and the carousel chevrons step through the remaining entries. */
  images: TaggedImageUrl[];
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
  const base = supabase
    .from('browse_list')
    .select(withKeysetColumns(CARD_COLS, sort));
  const scoped = applyPrefilters(applyFilters(base, f), pre);
  const keyed = applyKeyset(
    scoped as unknown as KeysetBuilder,
    sort,
    cursor,
  ) as unknown as typeof scoped;
  const { data, error } = await keyed.limit(CARD_PAGE_SIZE);
  if (error) throw error;
  const baseRows = (data ?? []) as unknown as Omit<CardRow, 'images'>[];
  const nextCursor = nextCursorFrom(
    baseRows as unknown as Record<string, unknown>[],
    sort,
  );
  if (baseRows.length === 0) return { rows: [], nextCursor };
  /* fetchImagesByListingIds already pulls every image row for the
   * visible ids over the wire; perId is a client-side retention cap.
   * 50 comfortably covers any sreality listing (typical max ~25) so
   * the card carousel never silently truncates. URLs only — actual
   * image bytes are lazy-loaded by the <img loading="lazy">. */
  const images = await fetchImagesByListingIds(
    baseRows.map((r) => r.sreality_id),
    50,
  );
  const rows: CardRow[] = baseRows.map((r) => {
    const imgs = images.get(r.sreality_id) ?? [];
    return {
      ...r,
      images: imgs.map((im) => ({
        url: imageSrc(im),
        tag: im.clip_fine_tag,
        confidence: im.clip_confidence,
        renderScore: im.clip_render_score,
      })),
    };
  });
  return { rows, nextCursor };
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
  });
  if (error) throw error;
  return data as BrowseStats;
};

const DETAIL_COLS =
  'sreality_id,first_seen_at,last_seen_at,is_active,source,tom_days,' +
  'category_main,category_type,price_czk,price_unit,' +
  'area_m2,disposition,subtype,locality,district,obec,okres,street,locality_district_id,locality_region_id,' +
  'lat,lng,floor,total_floors,has_balcony,has_parking,has_lift,' +
  'building_type,condition,energy_rating,' +
  'estate_area,usable_area,garden_area,category_sub_cb,' +
  'furnished,terrace,cellar,garage,parking_lots,ownership,' +
  'description,mf_reference_rent_czk,mf_gross_yield_pct,mf_reference_rent';

export const fetchListingById = async (
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

/* Resolve a property_id to its representative listing's sreality_id.
 * Lets /listing?property=ID (e.g. the dedup merge feed's link) land on the
 * survivor's detail page. properties_public exposes sreality_id = repr id. */
export const fetchPropertyReprId = async (
  property_id: number,
): Promise<number | null> => {
  const { data, error } = await supabase
    .from('properties_public')
    .select('sreality_id')
    .eq('property_id', property_id)
    .maybeSingle();
  if (error) throw error;
  const row = data as unknown as { sreality_id: number | null } | null;
  return row?.sreality_id ?? null;
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
 * child sreality_id via property_sources_public, not just the representative),
 * then return all of that property's per-portal observations. */
export const fetchPropertySources = async (
  sreality_id: number,
): Promise<{ property_id: number | null; sources: PropertySource[] }> => {
  const { data: row, error: e1 } = await supabase
    .from('property_sources_public')
    .select('property_id')
    .eq('sreality_id', sreality_id)
    .maybeSingle();
  if (e1) throw e1;
  const property_id = (row as { property_id: number } | null)?.property_id ?? null;
  if (property_id == null) return { property_id: null, sources: [] };
  const { data, error } = await supabase
    .from('property_sources_public')
    .select(
      'property_id,sreality_id,source,source_url,source_id_native,is_active,price_czk,first_seen_at,last_seen_at',
    )
    .eq('property_id', property_id)
    .order('first_seen_at', { ascending: true });
  if (error) throw error;
  return { property_id, sources: (data ?? []) as unknown as PropertySource[] };
};

/* Snapshots across several listings (a property's children) — the union that
 * makes the Listing Detail price chart cross-source. For a singleton property
 * this is identical to fetchSnapshotsByListing. */
export const fetchSnapshotsForListings = async (
  ids: number[],
): Promise<ListingSnapshotPublic[]> => {
  if (ids.length === 0) return [];
  const { data, error } = await supabase
    .from('listing_snapshots_public')
    .select('id,sreality_id,scraped_at,price_czk,description')
    .in('sreality_id', ids)
    .order('scraped_at', { ascending: true });
  if (error) throw error;
  return (data ?? []) as unknown as ListingSnapshotPublic[];
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
    out.set(row.sreality_id, row);
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
  'id,sreality_id,sequence,sreality_url,storage_path,clip_fine_tag,clip_logical_tag,clip_confidence,clip_render_score';

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

/* /dedup review card: per-side portal chips. Batched over the candidate
 * properties on screen (≤100), keyed on property_id. property_sources_public
 * is one row per (child listing) of a property — post-merge a property spans
 * several portals, which is exactly what the chips show. */
export const fetchPropertySourcesByPropertyIds = async (
  ids: ReadonlyArray<number>,
): Promise<Map<number, PropertySource[]>> => {
  if (ids.length === 0) return new Map();
  const { data, error } = await supabase
    .from('property_sources_public')
    .select(
      'property_id,sreality_id,source,source_url,source_id_native,is_active,price_czk,first_seen_at,last_seen_at',
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

/* /dedup review card: the street / house-number / floor the candidate payload
 * doesn't carry (migration 122 exposes street + house_number on
 * listings_public). Batched over the on-screen sides' sreality_ids. */
const DEDUP_DETAIL_COLS =
  'sreality_id,street,house_number,floor,disposition,district,price_czk,area_m2,category_type,category_main,category_sub_cb';

export const fetchListingDetailByIds = async (
  ids: ReadonlyArray<number>,
): Promise<Map<number, ListingDetailLite>> => {
  if (ids.length === 0) return new Map();
  const { data, error } = await supabase
    .from('listings_public')
    .select(DEDUP_DETAIL_COLS)
    .in('sreality_id', ids as number[]);
  if (error) throw error;
  const out = new Map<number, ListingDetailLite>();
  for (const row of (data ?? []) as unknown as ListingDetailLite[]) {
    out.set(row.sreality_id, row);
  }
  return out;
};

export const fetchImagesByListing = async (
  sreality_id: number,
): Promise<ImagePublic[]> => {
  const { data, error } = await supabase
    .from('images_public')
    .select(IMAGE_PUBLIC_COLS)
    .eq('sreality_id', sreality_id)
    .order('sequence', { ascending: true, nullsFirst: false })
    .order('id', { ascending: true });
  if (error) throw error;
  return (data ?? []) as unknown as ImagePublic[];
};

/* -------------------------------------------------------------------------- */
/* Health dashboard (Part E) — calls migration 013 health_summary RPC         */
/* -------------------------------------------------------------------------- */

export const fetchHealthSummary = async (): Promise<HealthSummary> => {
  const { data, error } = await supabase.rpc('health_summary');
  if (error) throw error;
  return data as HealthSummary;
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

/* Migration 273 — the dedup-aware publication gate. New properties are hidden
 * from Browse until dedup evaluates them; this single-row aggregate exposes the
 * backlog (`unpublished`), its age, and the active baseline. The gate has no
 * auto-publish timeout, so a rising `unpublished` is the dedup-stall signal. */
export interface PublicationGateRow {
  unpublished: number;
  oldest_unpublished_at: string | null;
  active_total: number;
}

export const fetchPublicationGateHealth = async (): Promise<PublicationGateRow> => {
  const { data, error } = await supabase
    .from('publication_gate_health_public')
    .select('unpublished,oldest_unpublished_at,active_total')
    .maybeSingle();
  if (error) throw error;
  return (
    (data as PublicationGateRow | null) ?? {
      unpublished: 0,
      oldest_unpublished_at: null,
      active_total: 0,
    }
  );
};

/* Migration 274 — dedup pipeline verification checks (latest row per check_key).
 * The DB stamps the ok/warn/fail status + a `value` whose unit is check-specific
 * (suspect-pair counts for street/geo debt, ratios/minutes elsewhere). */
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

export const fetchCuratedCities = async (): Promise<CuratedCity[]> => {
  /* `.range` bypasses PostgREST's default 1,000-row cap. 205 rows
   * today, headroom for future operator uploads that grow the set. */
  const { data, error } = await supabase
    .from('curated_cities_public')
    .select('*')
    .order('name')
    .range(0, 4999);
  if (error) throw error;
  return (data ?? []) as CuratedCity[];
};

export const fetchCityIndexDefinitions = async (): Promise<CityIndexDefinition[]> => {
  /* `.range` bypasses PostgREST's default 1,000-row cap. 33 rows
   * today, but defensive against future index additions. */
  const { data, error } = await supabase
    .from('city_index_definitions_public')
    .select('*')
    .range(0, 999);
  if (error) throw error;
  return (data ?? []) as CityIndexDefinition[];
};

export const fetchCityIndexValues = async (): Promise<CityIndexValue[]> => {
  /* The view has 205 cities × 33 indexes = 6,765 rows, but PostgREST
   * hard-caps every response at 1,000 rows on this project (db-max-rows)
   * — `.range(0, 49999)` does NOT lift it: it's a server-side ceiling,
   * not a page size. The old single-shot fetch therefore returned only
   * the first ~32 cities (1,000 ÷ 33), so every city past that point
   * (Dobříš included) showed em-dashes for every index in the popup and
   * a grey, value-less pin in the choropleth. Page through with a stable
   * order until a short page signals the end — the same fix
   * fetchRentMapChoropleth already uses. Cached (staleTime: Infinity),
   * so the ~7 round-trips only happen on first load. */
  const PAGE = 1000;
  const out: CityIndexValue[] = [];
  for (let from = 0; ; from += PAGE) {
    const { data, error } = await supabase
      .from('city_index_values_public')
      .select('city_id,index_name,value')
      .order('city_id', { ascending: true })
      .order('index_name', { ascending: true })
      .range(from, from + PAGE - 1);
    if (error) throw error;
    const rows = (data ?? []) as CityIndexValue[];
    out.push(...rows);
    if (rows.length < PAGE) break;
  }
  return out;
};

export interface CityPolygon {
  city_id: number;
  geojson: string;
}

export const fetchCuratedCityPolygons = async (): Promise<CityPolygon[]> => {
  /* One simplified municipality boundary per curated city (205 rows,
   * comfortably under the 1,000-row db-max-rows cap, so a single page
   * suffices). `geojson` is the raw ST_AsGeoJSON string the map
   * JSON.parses into a Feature geometry — the same contract as
   * rent_map_choropleth_public. Fetched once and cached
   * (staleTime: Infinity), and only when the map tab is active. */
  const { data, error } = await supabase
    .from('curated_city_polygons_public')
    .select('city_id,geojson')
    .order('city_id', { ascending: true })
    .range(0, 4999);
  if (error) throw error;
  return (data ?? []) as CityPolygon[];
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
  /* The view has ~7,630 rows (one per obec / katastrální území), but
   * PostgREST hard-caps every response at 1,000 rows on this project
   * (db-max-rows) — neither `.range(0, 9999)` nor `.limit()` lifts it.
   * So page through with a stable `order` until a short page signals
   * the end. Fetched once and cached (staleTime: Infinity), so the ~8
   * requests only happen when the operator first enables the layer. */
  const PAGE = 1000;
  const out: RentMapPolygon[] = [];
  for (let from = 0; ; from += PAGE) {
    const { data, error } = await supabase
      .from('rent_map_choropleth_public')
      .select('*')
      .order('ruian_code', { ascending: true })
      .range(from, from + PAGE - 1);
    if (error) throw error;
    const rows = (data ?? []) as RentMapPolygon[];
    out.push(...rows);
    if (rows.length < PAGE) break;
  }
  return out;
};

export const fetchRentMapKraje = async (): Promise<RentMapKraj[]> => {
  /* 14 kraje; `.range` kept for symmetry / headroom. */
  const { data, error } = await supabase
    .from('rent_map_kraje_public')
    .select('*')
    .range(0, 999);
  if (error) throw error;
  return (data ?? []) as RentMapKraj[];
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
  PipelineCard,
  PipelineStage,
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
  listEstimations({ sreality_ids: ids.join(','), limit: 100 });
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
  const { data, error } = await supabase
    .from('collection_properties_public')
    .select('property_id, collection_id')
    .range(0, 99999);
  if (error) throw error;
  const map = new Map<number, number[]>();
  for (const r of (data ?? []) as Array<{
    property_id: number;
    collection_id: number;
  }>) {
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

const sortedIds = (ids: ReadonlyArray<number>): number[] =>
  [...ids].sort((a, b) => a - b);

export const dedupKeys = {
  all: ['dedup'] as const,
  candidates: (params: Record<string, unknown>) =>
    ['dedup', 'candidates', params] as const,
  merges: (params: Record<string, unknown>) =>
    ['dedup', 'merges', params] as const,
  summary: (status: string) => ['dedup', 'summary', status] as const,
  sources: (propertyIds: ReadonlyArray<number>) =>
    ['dedup', 'sources', sortedIds(propertyIds)] as const,
  images: (srealityIds: ReadonlyArray<number>) =>
    ['dedup', 'images', sortedIds(srealityIds)] as const,
  detail: (srealityIds: ReadonlyArray<number>) =>
    ['dedup', 'detail', sortedIds(srealityIds)] as const,
  engineRuns: (limit: number) => ['dedup', 'engine-runs', limit] as const,
  scanState: ['dedup', 'scan-state'] as const,
};

export interface DedupEngineRun {
  id: number;
  started_at: string;
  ended_at: string | null;
  /* Market gauges — NULL on scoped runs (dirty/candidates; not measured, migration 265)
   * and geo-lane-scoped on run_kind='geo' rows. Read them from the latest FULL-scan row
   * (run_kind='full', or legacy null run_kind), not from runs[0]. */
  eligible: number | null;
  flagged_location: number | null;
  flagged_disposition: number | null;
  pairs_considered: number;
  rejected: number;
  auto_address: number;
  auto_phash: number;
  auto_visual: number;
  queued: number;
  vision_calls: number;
  auto_dismissed: number;
  floor_plan_deferred: number;
  clip_deferred: number;
  /* --dirty runs only (NULL on full scan / candidate / geo): the dedup-ready queue depth
   * at run start, how many this run claimed, how many it actually CLEARED (per-group
   * incremental clear), and whether it hit its budget. cleared==0 across truncated runs
   * is the livelock signature migration 258 exposes — assessDirtyQueue keys on it. */
  dirty_queue_depth: number | null;
  dirty_claimed: number | null;
  dirty_cleared: number | null;
  dirty_truncated: number | null;
  /* Every run (migration 262; null on pre-262 rows): the lane that wrote the row
   * ('full' | 'candidates' | 'dirty') + whether the run stopped on its wall-clock /
   * pair budget before finishing its scan. truncated on run_kind='full' is the
   * full-scan coverage-gap signal (the TTL backstop is only as good as scan coverage). */
  run_kind: string | null;
  truncated: number | null;
  /* Migration 271 observability — the stall tripwires that were previously invisible
   * (null on pre-271 rows / lanes that don't measure a given gauge):
   *  - skipped_oversized / oversized_groups: street groups over MAX_GROUP_SIZE the scan
   *    skipped whole (a coverage hole — those listings are never compared).
   *  - skipped_unresolved: pairs the free funnel reached but left undecided this run.
   *  - vision_errors: failed vision calls this run — the credit-outage tripwire (nonzero
   *    means the LLM lane is erroring, e.g. out-of-credit).
   *  - truncated_cause: WHY a truncated run stopped ('deadline' wall-clock | 'pair_cap').
   *  - scan_groups_total / scan_groups_scanned: full-scan cursor coverage this cycle.
   *  - dirty_age_p95_seconds: p95 wait of the dedup-ready queue — the real-time SLO gauge.
   *  - dirty_pruned: dedup-ready rows TTL-evicted (not merged) this run.
   *  - runner: which executor wrote the row ('actions' cron | 'worker' always-on). */
  skipped_unresolved: number | null;
  skipped_oversized: number | null;
  oversized_groups: number | null;
  vision_errors: number | null;
  truncated_cause: 'deadline' | 'pair_cap' | null;
  scan_groups_total: number | null;
  scan_groups_scanned: number | null;
  dirty_age_p95_seconds: number | null;
  dirty_pruned: number | null;
  runner: 'actions' | 'worker' | null;
}

/* Recent dedup-engine runs for the /dedup automation dashboard. Reads the anon
 * dedup_engine_runs_public view (migration 130) directly — operational counters,
 * no secrets, same posture as the rest of the page's public-view reads. */
export const fetchDedupEngineRuns = async (
  limit = 14,
): Promise<DedupEngineRun[]> => {
  const { data, error } = await supabase
    .from('dedup_engine_runs_public')
    .select(
      'id,started_at,ended_at,eligible,flagged_location,flagged_disposition,' +
        'pairs_considered,rejected,auto_address,auto_phash,auto_visual,queued,' +
        'vision_calls,auto_dismissed,floor_plan_deferred,clip_deferred,' +
        'dirty_queue_depth,dirty_claimed,dirty_cleared,dirty_truncated,run_kind,truncated,' +
        'skipped_unresolved,skipped_oversized,oversized_groups,vision_errors,truncated_cause,' +
        'scan_groups_total,scan_groups_scanned,dirty_age_p95_seconds,dirty_pruned,runner',
    )
    // Insert order (id), NOT started_at: started_at is now the REAL run start (migration
    // 262), so an 80-min full scan's row would sort below dirty runs that STARTED after it
    // and the completed scan would never headline. id preserves the pre-262 semantics.
    .order('id', { ascending: false })
    .limit(limit);
  if (error) throw error;
  return (data ?? []) as unknown as DedupEngineRun[];
};

/* Full-scan cursor + cycle state per dedup lane (dedup_scan_state_public, migration
 * 271). One row per lane; the 'street' lane is the apartment full scan whose cycle
 * completion is the dashboard's cursor-stall signal. */
export interface DedupScanState {
  lane: string;
  mid_cycle: boolean;
  cycle_started_at: string | null;
  last_cycle_started_at: string | null;
  last_cycle_completed_at: string | null;
  updated_at: string;
}

/* Latest scan-cycle state, preferring the 'street' (apartment) lane, else the most
 * recently updated lane. Small view — one row per lane — a cheap anon read well under
 * the 3 s statement timeout. */
export const fetchDedupScanState = async (): Promise<DedupScanState | null> => {
  const { data, error } = await supabase
    .from('dedup_scan_state_public')
    .select(
      'lane,mid_cycle,cycle_started_at,last_cycle_started_at,' +
        'last_cycle_completed_at,updated_at',
    )
    .order('updated_at', { ascending: false });
  if (error) throw error;
  const rows = (data ?? []) as unknown as DedupScanState[];
  return rows.find((r) => r.lane === 'street') ?? rows[0] ?? null;
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
/* through the FastAPI service. Single-valued — at most one card per property.   */
export const pipelineKeys = {
  card: (property_id: number) => ['pipeline', 'card', property_id] as const,
  board: ['pipeline', 'board'] as const,
  stages: ['pipeline', 'stages'] as const,
  members: ['pipeline', 'members'] as const,
};

/* The set of property_ids currently in the pipeline — one cheap read shared
 * (React Query dedupes the key) by every Browse-card bookmark toggle. */
export const fetchPipelineMemberSet = async (): Promise<Set<number>> => {
  const { data, error } = await supabase
    .from('property_pipeline_public')
    .select('property_id')
    .range(0, 99999);
  if (error) throw error;
  return new Set(
    ((data ?? []) as Array<{ property_id: number }>).map((r) => r.property_id),
  );
};

export const fetchPropertyPipeline = async (
  property_id: number,
): Promise<PipelineCard | null> => {
  const { data, error } = await supabase
    .from('property_pipeline_public')
    .select('property_id, stage_id, stage_key, stage_label, stage_color, is_terminal, stage_position')
    .eq('property_id', property_id)
    .maybeSingle();
  if (error) throw error;
  return (data as PipelineCard | null) ?? null;
};

export const fetchPipelineStages = async (): Promise<PipelineStage[]> => {
  const { data, error } = await supabase
    .from('pipeline_stages_public')
    .select('id, key, label, position, color, is_terminal, is_entry')
    .order('position');
  if (error) throw error;
  return (data ?? []) as PipelineStage[];
};

/* The kanban payload: every card joined to its property's display fields. Two
 * anon reads (property_pipeline_public + properties_public) joined client-side
 * by property_id — the same batched-hydration pattern Browse uses. */
export const fetchPipelineBoard = async (): Promise<PipelineBoardCard[]> => {
  const { data: cards, error: cErr } = await supabase
    .from('property_pipeline_public')
    .select('property_id, stage_id, board_position, entered_stage_at')
    .order('board_position');
  if (cErr) throw cErr;
  const rows = (cards ?? []) as Array<{
    property_id: number;
    stage_id: number;
    board_position: number;
    entered_stage_at: string;
  }>;
  if (rows.length === 0) return [];

  const ids = rows.map((r) => r.property_id);
  const { data: props, error: pErr } = await supabase
    .from('properties_public')
    .select(
      'property_id, sreality_id, category_main, street, district, disposition, subtype, area_m2, price_czk, mf_gross_yield_pct, obec_id, okres_id, region_id, place_search_text, okres, region',
    )
    .in('property_id', ids);
  if (pErr) throw pErr;
  const byId = new Map(
    ((props ?? []) as Array<Record<string, unknown>>).map((p) => [
      p.property_id as number,
      p,
    ]),
  );

  // One thumbnail per card — the same batched image hydration Browse cards use
  // (images are keyed on sreality_id, not on the property), resolved through the
  // shared imageSrc() helper so the board and Browse render identical URLs.
  const srealityIds = rows
    .map((r) => byId.get(r.property_id)?.sreality_id as number | null | undefined)
    .filter((x): x is number => x != null);
  const imagesById = await fetchImagesByListingIds(srealityIds, 1);

  // Canonical broker per card (name + firm + contact for the hover box), two
  // batched reads (listing→broker, then broker→contact) — no N+1.
  const listingBrokers = await fetchListingBrokersByIds(srealityIds);
  const brokerContacts = await fetchBrokersByIds([
    ...new Set([...listingBrokers.values()].map((b) => b.broker_id)),
  ]);

  return rows.map((r) => {
    const p = byId.get(r.property_id);
    const sid = (p?.sreality_id as number | null) ?? null;
    const firstImage = sid != null ? imagesById.get(sid)?.[0] : undefined;
    const lb = sid != null ? listingBrokers.get(sid) : undefined;
    const contact = lb ? brokerContacts.get(lb.broker_id) : undefined;
    return {
      property_id: r.property_id,
      stage_id: r.stage_id,
      board_position: r.board_position,
      entered_stage_at: r.entered_stage_at,
      sreality_id: sid,
      category_main: (p?.category_main as string | null) ?? null,
      street: (p?.street as string | null) ?? null,
      district: (p?.district as string | null) ?? null,
      disposition: (p?.disposition as string | null) ?? null,
      subtype: (p?.subtype as string | null) ?? null,
      area_m2: (p?.area_m2 as number | null) ?? null,
      price_czk: (p?.price_czk as number | null) ?? null,
      mf_gross_yield_pct: (p?.mf_gross_yield_pct as number | null) ?? null,
      obec_id: (p?.obec_id as number | null) ?? null,
      okres_id: (p?.okres_id as number | null) ?? null,
      region_id: (p?.region_id as number | null) ?? null,
      place_search_text: (p?.place_search_text as string | null) ?? null,
      okres: (p?.okres as string | null) ?? null,
      region: (p?.region as string | null) ?? null,
      image_url: firstImage ? imageSrc(firstImage) : null,
      broker: lb
        ? {
            broker_id: lb.broker_id,
            display_name: lb.broker_display_name,
            firm_label: lb.broker_firm_label,
            email: contact?.primary_email ?? null,
            phone: contact?.primary_phone ?? null,
          }
        : null,
    };
  });
};

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
