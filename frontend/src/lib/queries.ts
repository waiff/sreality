import { supabase } from './supabase';
import { imageSrc } from './imageUrl';
import type { ListingDetailLite } from './dedupDiff';
import {
  type CenterRadius,
  type ListingFilters,
  type MapBounds,
  buildingMaterialToValues,
  isoNDaysAgo,
} from './filters';
import { applyRegistryFilters } from './registryQueryBuilder';
import type {
  CategoryTrend,
  HealthSummary,
  ImagePublic,
  ImageStorageOverview,
  ListingFreshnessCheckPublic,
  ListingPublic,
  ListingSnapshotPublic,
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

const MAP_COLS = 'sreality_id,lat,lng,price_czk,disposition,area_m2,district,last_seen_at,is_active,tom_days';
const TABLE_COLS =
  'sreality_id,district,disposition,area_m2,price_czk,first_seen_at,last_seen_at,is_active,tom_days,' +
  'estate_area,usable_area,parking_lots,furnished,ownership,category_sub_cb,building_type';
const CARD_COLS =
  'property_id,sreality_id,district,locality,disposition,area_m2,price_czk,first_seen_at,last_seen_at,is_active,tom_days,' +
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

export const DEFAULT_SORT: SortSpec = { field: 'last_seen_at', direction: 'desc' };

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
 * `building_material` expansion, the multi-chip district ILIKE OR
 * predicate, and the bbox spatial predicates that aren't registry
 * filters at all. The drift test in registryQueryBuilder.test.ts
 * fails CI if a new registry filter is added that fits no path. */
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
  if (f.districts.length) {
    /* Each chip becomes:
     *   (district ilike *name* OR locality ilike *name*)
     *   AND (no context, OR district/locality ilike *context*)
     * Mapy.cz suggests at every granularity (kraj, okres, obec, část obce,
     * street, POI); listings carry the geo-derived `okres` / `region`
     * (kraj) + the canonical `district`, and the part-of-municipality /
     * street / POI name appears in the `locality` free-text — so matching
     * all four lets a pick at any level resolve (migration 141). The
     * context half (parent municipality from `regionalStructure`) is what
     * stops a "Edvarda Beneše" pick in Plzeň from also matching the streets
     * of the same name in Olomouc / Hradec Králové.
     *
     * Chips split by `excluded`: INCLUDE chips are OR'd (match any), then
     * AND'd with NOT-(OR of the EXCLUDE chips) so an excluded locality is
     * subtracted from the cohort. Combined into a single `and(...)` tree so
     * PostgREST AND's the two groups. Kept in lockstep with the watchdog
     * matcher (`_build_match_clauses`) and browse_stats (migration 146),
     * which apply the same include/exclude split. */
    const chipClause = (d: { name: string; context: string | null }): string => {
      const namePat = escapeIlikePattern(d.name);
      const cols = (pat: string): string =>
        `district.ilike.${pat},locality.ilike.${pat},okres.ilike.${pat},region.ilike.${pat}`;
      const nameHalf = `or(${cols(namePat)})`;
      if (!d.context) return nameHalf;
      const ctxPat = escapeIlikePattern(d.context);
      return `and(${nameHalf},or(${cols(ctxPat)}))`;
    };
    const inc = f.districts.filter((d) => !d.excluded).map(chipClause);
    const exc = f.districts.filter((d) => d.excluded).map(chipClause);
    const groups: string[] = [];
    if (inc.length) groups.push(`or(${inc.join(',')})`);
    if (exc.length) groups.push(`not.or(${exc.join(',')})`);
    if (groups.length) r = r.or(`and(${groups.join(',')})`);
  }
  if (f.buildingMaterial.length) {
    r = r.in('building_type', buildingMaterialToValues(f.buildingMaterial));
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

/* Tags facet is composed of two server queries: (1) listings_with_tags RPC
 * resolves the ids matching ALL selected tag ids, (2) the regular listings
 * query gets .in('sreality_id', ids) appended. Returns null if no tags
 * are selected (skip the prefilter entirely), an empty array if none
 * match (caller should short-circuit to empty results), or the id list.
 * Declared as a hoistable function so the Map/Table fetchers below can
 * call it without forward-reference issues. */
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
    .rpc('listings_with_tags', { tag_ids: f.tags })
    .range(0, 99999);
  if (error) throw error;
  return ((data ?? []) as Array<{ sreality_id: number }>).map(
    (r) => r.sreality_id,
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
 * listing-grain browse_stats. It also gained the four derived predicates
 * (distinct_site_count_min / price_{drop,rise}_count_min / max_price_drop_pct_min). */
export const fetchListingsForMap = async (
  f: ListingFilters,
): Promise<MapResult> => {
  const [tagIds, cityIds] = await Promise.all([
    resolveTagPrefilter(f),
    resolveCityQualityPrefilter(f),
  ]);
  const prefilter = intersectPrefilters(tagIds, cityIds);
  if (prefilter != null && prefilter.length === 0) {
    return { rows: [], total: 0, capped: false };
  }
  const base = supabase
    .from('properties_public')
    .select(MAP_COLS, { count: 'exact' })
    .not('lat', 'is', null)
    .not('lng', 'is', null);
  const filtered = applyFilters(base, f);
  const scoped = prefilter != null
    ? filtered.in('sreality_id', prefilter)
    : filtered;
  const { data, count, error } = await scoped.limit(MAP_CAP);
  if (error) throw error;
  const rows = (data ?? []) as unknown as MapRow[];
  return {
    rows,
    total: count ?? null,
    capped: count != null && count > MAP_CAP,
  };
};

export interface TableRow {
  sreality_id: number;
  district: string | null;
  disposition: string | null;
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

export interface TableResult {
  rows: TableRow[];
  total: number | null;
}

export const fetchListingsForTable = async (
  f: ListingFilters,
  sort: SortSpec,
  page: number,
): Promise<TableResult> => {
  const [tagIds, cityIds] = await Promise.all([
    resolveTagPrefilter(f),
    resolveCityQualityPrefilter(f),
  ]);
  const prefilter = intersectPrefilters(tagIds, cityIds);
  if (prefilter != null && prefilter.length === 0) {
    return { rows: [], total: 0 };
  }
  const from = (page - 1) * TABLE_PAGE_SIZE;
  const to = from + TABLE_PAGE_SIZE - 1;
  const base = supabase
    .from('properties_public')
    .select(TABLE_COLS, { count: 'exact' });
  const filtered = applyFilters(base, f);
  const scoped = prefilter != null
    ? filtered.in('sreality_id', prefilter)
    : filtered;
  const sorted = scoped.order(sort.field, {
    ascending: sort.direction === 'asc',
    nullsFirst: false,
  });
  const { data, count, error } = await sorted.range(from, to);
  if (error) throw error;
  return {
    rows: (data ?? []) as unknown as TableRow[],
    total: count ?? null,
  };
};

/* -------------------------------------------------------------------------- */
/* Cards (sreality-style image-first list). Same filter chain as table, plus  */
/* a batched image lookup for the first photo per visible listing. Sorted by  */
/* last_seen_at desc — the cards lane is for "what's new", not for arbitrary  */
/* re-sorting (that's the Table tab's job).                                   */
/* -------------------------------------------------------------------------- */

export interface CardRow {
  /* The canonical property this card represents (Browse is property-grain via
   * properties_public). Used by the Browse merge-mode dedup action. */
  property_id: number;
  sreality_id: number;
  district: string | null;
  locality: string | null;
  disposition: string | null;
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
  /* Up to 5 image URLs in source-sequence order. Empty when the
   * listing has no photos yet. The card uses index 0 by default and
   * the carousel chevrons step through the remaining entries. */
  image_urls: string[];
}

export interface CardsResult {
  rows: CardRow[];
  total: number | null;
}

export const fetchListingsForCards = async (
  f: ListingFilters,
  sort: SortSpec,
  page: number,
): Promise<CardsResult> => {
  const [tagIds, cityIds] = await Promise.all([
    resolveTagPrefilter(f),
    resolveCityQualityPrefilter(f),
  ]);
  const prefilter = intersectPrefilters(tagIds, cityIds);
  if (prefilter != null && prefilter.length === 0) {
    return { rows: [], total: 0 };
  }
  const from = (page - 1) * CARD_PAGE_SIZE;
  const to = from + CARD_PAGE_SIZE - 1;
  const base = supabase
    .from('properties_public')
    .select(CARD_COLS, { count: 'exact' });
  const filtered = applyFilters(base, f);
  const scoped = prefilter != null
    ? filtered.in('sreality_id', prefilter)
    : filtered;
  const sorted = scoped.order(sort.field, {
    ascending: sort.direction === 'asc',
    nullsFirst: false,
  });
  const { data, count, error } = await sorted.range(from, to);
  if (error) throw error;
  const baseRows = (data ?? []) as unknown as Omit<CardRow, 'image_urls'>[];
  if (baseRows.length === 0) return { rows: [], total: count ?? 0 };
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
      image_urls: imgs.map(imageSrc),
    };
  });
  return { rows, total: count ?? null };
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

  const { data, error } = await supabase.rpc('browse_stats_properties', {
    category_main_filter:    f.categoryMain,
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
    dispositions_filter:     f.dispositions.length ? f.dispositions : null,
    price_min_filter:        f.priceMin,
    price_max_filter:        f.priceMax,
    area_min_filter:         f.areaMin,
    area_max_filter:         f.areaMax,
    active_only_filter:      f.status === 'active',
    inactive_only_filter:    f.status === 'inactive',
    last_seen_min_days:      f.lastSeenMinDays,
    last_seen_max_days:      f.lastSeenMaxDays,
    first_seen_min_days:     f.firstSeenMinDays,
    first_seen_max_days:     f.firstSeenMaxDays,
    tom_days_min:            f.tomDaysMin,
    tom_days_max:            f.tomDaysMax,
    has_balcony_filter:      triToBool(f.hasBalcony),
    has_lift_filter:         triToBool(f.hasLift),
    has_parking_filter:      triToBool(f.hasParking),
    furnished_filter:        f.furnished,
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
    /* Migration 095 — multi-portal / price-history derived predicates.
     * Property grain only; columns maintained by the recompute job. */
    distinct_site_count_min: f.distinctSiteCountMin,
    price_drop_count_min:    f.priceDropCountMin,
    price_rise_count_min:    f.priceRiseCountMin,
    max_price_drop_pct_min:  f.maxPriceDropPctMin,
    /* Migration 118 — filter the Stats cohort by source portal. */
    portal_filter:           f.portals.length ? f.portals : null,
  });
  if (error) throw error;
  return data as BrowseStats;
};

const DETAIL_COLS =
  'sreality_id,first_seen_at,last_seen_at,is_active,source,tom_days,' +
  'category_main,category_type,price_czk,price_unit,' +
  'area_m2,disposition,locality,district,locality_district_id,locality_region_id,' +
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
export const fetchImagesByListingIds = async (
  ids: ReadonlyArray<number>,
  perId = 3,
): Promise<Map<number, ImagePublic[]>> => {
  if (ids.length === 0) return new Map();
  const { data, error } = await supabase
    .from('images_public')
    .select('id,sreality_id,sequence,sreality_url,storage_path')
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
  'sreality_id,street,house_number,floor,disposition,district,price_czk,area_m2';

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
    .select('id,sreality_id,sequence,sreality_url,storage_path')
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
} from './types';

export const estimationKeys = {
  all: ['estimations'] as const,
  list: (params: EstimationListParams) =>
    ['estimations', 'list', params] as const,
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
/* listing X belong to" — read directly from the *_public views via the anon  */
/* key, matching the same read-only pattern Browse / Region already use. The  */
/* `listings_with_tags(tag_ids)` RPC powers the Browse "tags" facet:          */
/* AND-semantics across the supplied ids, capped at 5000 rows on the server.  */
/* -------------------------------------------------------------------------- */

export const fetchListingTagIds = async (
  sreality_id: number,
): Promise<number[]> => {
  const { data, error } = await supabase
    .from('listing_tags_public')
    .select('tag_id')
    .eq('sreality_id', sreality_id);
  if (error) throw error;
  return ((data ?? []) as Array<{ tag_id: number }>).map((r) => r.tag_id);
};

export const fetchListingCollectionIds = async (
  sreality_id: number,
): Promise<number[]> => {
  const { data, error } = await supabase
    .from('collection_listings_public')
    .select('collection_id')
    .eq('sreality_id', sreality_id);
  if (error) throw error;
  return ((data ?? []) as Array<{ collection_id: number }>).map(
    (r) => r.collection_id,
  );
};

export const fetchListingIdsWithAllTags = async (
  tag_ids: number[],
): Promise<number[]> => {
  if (tag_ids.length === 0) return [];
  /* `.range` bypasses PostgREST's default 1,000-row cap so a widely-
   * matched tag set doesn't silently truncate. Mirrors the same
   * fix in `resolveTagPrefilter` / `resolveCityQualityPrefilter`. */
  const { data, error } = await supabase
    .rpc('listings_with_tags', { tag_ids })
    .range(0, 99999);
  if (error) throw error;
  return ((data ?? []) as Array<{ sreality_id: number }>).map(
    (r) => r.sreality_id,
  );
};

export const watchdogKeys = {
  all: ['watchdog'] as const,
  subscriptions: ['watchdog', 'subscriptions'] as const,
  subscription: (id: string) => ['watchdog', 'subscriptions', id] as const,
  dispatches: (params: Record<string, unknown>) =>
    ['watchdog', 'dispatches', params] as const,
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
  sources: (propertyIds: ReadonlyArray<number>) =>
    ['dedup', 'sources', sortedIds(propertyIds)] as const,
  images: (srealityIds: ReadonlyArray<number>) =>
    ['dedup', 'images', sortedIds(srealityIds)] as const,
  detail: (srealityIds: ReadonlyArray<number>) =>
    ['dedup', 'detail', sortedIds(srealityIds)] as const,
  engineRuns: (limit: number) => ['dedup', 'engine-runs', limit] as const,
};

export interface DedupEngineRun {
  id: number;
  started_at: string;
  ended_at: string | null;
  eligible: number;
  flagged_location: number;
  flagged_disposition: number;
  pairs_considered: number;
  rejected: number;
  auto_address: number;
  auto_phash: number;
  auto_visual: number;
  queued: number;
  vision_calls: number;
  cost_usd: number;
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
        'vision_calls,cost_usd',
    )
    .order('started_at', { ascending: false })
    .limit(limit);
  if (error) throw error;
  return (data ?? []) as unknown as DedupEngineRun[];
};

export const curationKeys = {
  collections: ['curation', 'collections'] as const,
  collection: (id: number) => ['curation', 'collection', id] as const,
  tags: ['curation', 'tags'] as const,
  listingTags: (sreality_id: number) =>
    ['curation', 'listing-tags', sreality_id] as const,
  listingCollections: (sreality_id: number) =>
    ['curation', 'listing-collections', sreality_id] as const,
  listingNotes: (sreality_id: number) =>
    ['curation', 'listing-notes', sreality_id] as const,
  manualEstimates: (sreality_id: number) =>
    ['curation', 'manual-estimates', sreality_id] as const,
};
