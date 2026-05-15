import { supabase } from './supabase';
import {
  type CenterRadius,
  type ListingFilters,
  type MapBounds,
  buildingMaterialToValues,
  isoNDaysAgo,
} from './filters';
import type {
  HealthSummary,
  ImagePublic,
  ListingFreshnessCheckPublic,
  ListingPublic,
  ListingSnapshotPublic,
  Ppm2Box,
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
  'sreality_id,district,locality,disposition,area_m2,price_czk,first_seen_at,last_seen_at,is_active,tom_days,' +
  'category_main,category_type';

export type SortField =
  | 'sreality_id' | 'district' | 'disposition'
  | 'area_m2' | 'price_czk'
  | 'last_seen_at' | 'is_active'
  | 'estate_area' | 'usable_area' | 'parking_lots';

export type SortDirection = 'asc' | 'desc';

export interface SortSpec {
  field: SortField;
  direction: SortDirection;
}

export const DEFAULT_SORT: SortSpec = { field: 'last_seen_at', direction: 'desc' };

const SORTABLE_FIELDS: ReadonlyArray<SortField> = [
  'sreality_id', 'district', 'disposition',
  'area_m2', 'price_czk',
  'last_seen_at', 'is_active',
  'estate_area', 'usable_area', 'parking_lots',
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

/* Generic identity-typed helper. Postgrest's filter methods all return the
 * same builder, so passing the chain through any subset of them preserves
 * the input type at runtime. */
const applyFilters = <T>(q: T, f: ListingFilters): T => {
  let r = q as unknown as {
    eq:  (c: string, v: unknown) => typeof r;
    gte: (c: string, v: unknown) => typeof r;
    lte: (c: string, v: unknown) => typeof r;
    in:  (c: string, v: readonly unknown[]) => typeof r;
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
  if (f.tomDaysMin != null) r = r.gte('tom_days', f.tomDaysMin);
  if (f.tomDaysMax != null) r = r.lte('tom_days', f.tomDaysMax);
  r = r.eq('category_main', f.categoryMain);
  r = r.eq('category_type', f.categoryType);
  if (f.districts.length) r = r.in('district', f.districts);
  if (f.dispositions.length) r = r.in('disposition', f.dispositions);
  if (f.priceMin != null) r = r.gte('price_czk', f.priceMin);
  if (f.priceMax != null) r = r.lte('price_czk', f.priceMax);
  if (f.areaMin  != null) r = r.gte('area_m2',  f.areaMin);
  if (f.areaMax  != null) r = r.lte('area_m2',  f.areaMax);
  if (f.hasBalcony !== 'any') r = r.eq('has_balcony', f.hasBalcony === 'yes');
  if (f.hasLift    !== 'any') r = r.eq('has_lift',    f.hasLift    === 'yes');
  if (f.hasParking !== 'any') r = r.eq('has_parking', f.hasParking === 'yes');
  if (f.terrace    !== 'any') r = r.eq('terrace',     f.terrace    === 'yes');
  if (f.cellar     !== 'any') r = r.eq('cellar',      f.cellar     === 'yes');
  if (f.garage     !== 'any') r = r.eq('garage',      f.garage     === 'yes');
  if (f.furnished       != null) r = r.eq('furnished',      f.furnished);
  if (f.ownership       != null) r = r.eq('ownership',      f.ownership);
  if (f.categorySubCb   != null) r = r.eq('category_sub_cb', f.categorySubCb);
  if (f.buildingMaterial != null) {
    r = r.in('building_type', buildingMaterialToValues(f.buildingMaterial));
  }
  if (f.estateAreaMin   != null) r = r.gte('estate_area',   f.estateAreaMin);
  if (f.estateAreaMax   != null) r = r.lte('estate_area',   f.estateAreaMax);
  if (f.usableAreaMin   != null) r = r.gte('usable_area',   f.usableAreaMin);
  if (f.usableAreaMax   != null) r = r.lte('usable_area',   f.usableAreaMax);
  if (f.parkingLotsMin  != null) r = r.gte('parking_lots',  f.parkingLotsMin);
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
  const { data, error } = await supabase.rpc('listings_with_tags', {
    tag_ids: f.tags,
  });
  if (error) throw error;
  return ((data ?? []) as Array<{ sreality_id: number }>).map(
    (r) => r.sreality_id,
  );
}

export const fetchListingsForMap = async (
  f: ListingFilters,
): Promise<MapResult> => {
  const tagIds = await resolveTagPrefilter(f);
  if (tagIds != null && tagIds.length === 0) {
    return { rows: [], total: 0, capped: false };
  }
  const base = supabase
    .from('listings_public')
    .select(MAP_COLS, { count: 'exact' })
    .not('lat', 'is', null)
    .not('lng', 'is', null);
  const filtered = applyFilters(base, f);
  const scoped = tagIds != null ? filtered.in('sreality_id', tagIds) : filtered;
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
  const tagIds = await resolveTagPrefilter(f);
  if (tagIds != null && tagIds.length === 0) {
    return { rows: [], total: 0 };
  }
  const from = (page - 1) * TABLE_PAGE_SIZE;
  const to = from + TABLE_PAGE_SIZE - 1;
  const base = supabase
    .from('listings_public')
    .select(TABLE_COLS, { count: 'exact' });
  const filtered = applyFilters(base, f);
  const scoped = tagIds != null ? filtered.in('sreality_id', tagIds) : filtered;
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
  /* Up to 5 image URLs in source-sequence order. Empty when the
   * listing has no photos yet. The card uses index 0 by default and
   * the carousel chevrons step through the remaining entries. */
  image_urls: string[];
}

export interface CardsResult {
  rows: CardRow[];
  total: number | null;
}

const R2_BASE = (import.meta.env.VITE_R2_PUBLIC_BASE as string | undefined) ?? undefined;

const pickImageUrl = (img: {
  sreality_url: string;
  storage_path: string | null;
}): string => {
  if (R2_BASE && img.storage_path) {
    return `${R2_BASE.replace(/\/$/, '')}/${img.storage_path}`;
  }
  return img.sreality_url;
};

export const fetchListingsForCards = async (
  f: ListingFilters,
  page: number,
): Promise<CardsResult> => {
  const tagIds = await resolveTagPrefilter(f);
  if (tagIds != null && tagIds.length === 0) {
    return { rows: [], total: 0 };
  }
  const from = (page - 1) * CARD_PAGE_SIZE;
  const to = from + CARD_PAGE_SIZE - 1;
  const base = supabase
    .from('listings_public')
    .select(CARD_COLS, { count: 'exact' });
  const filtered = applyFilters(base, f);
  const scoped = tagIds != null ? filtered.in('sreality_id', tagIds) : filtered;
  const sorted = scoped.order('last_seen_at', { ascending: false, nullsFirst: false });
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
      image_urls: imgs.map(pickImageUrl),
    };
  });
  return { rows, total: count ?? null };
};


export interface DistrictFacet {
  district: string;
  count: number;
}

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

  const buildingTypeArray = f.buildingMaterial
    ? [...buildingMaterialToValues(f.buildingMaterial)]
    : null;

  const effBbox = effectiveBbox(f);

  const { data, error } = await supabase.rpc('browse_stats', {
    category_main_filter:    f.categoryMain,
    category_type_filter:    f.categoryType,
    districts_filter:        f.districts.length ? f.districts : null,
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
    building_type_filter:    buildingTypeArray,
    tag_ids:                 f.tags.length ? f.tags : null,
    bbox_west:               effBbox?.west  ?? null,
    bbox_south:              effBbox?.south ?? null,
    bbox_east:               effBbox?.east  ?? null,
    bbox_north:              effBbox?.north ?? null,
  });
  if (error) throw error;
  return data as BrowseStats;
};

let districtCache: DistrictFacet[] | null = null;

export const fetchDistrictFacets = async (): Promise<DistrictFacet[]> => {
  if (districtCache) return districtCache;
  const { data, error } = await supabase
    .from('listings_public')
    .select('district')
    .not('district', 'is', null);
  if (error) throw error;
  const counts = new Map<string, number>();
  for (const row of (data ?? []) as Array<{ district: string | null }>) {
    if (!row.district) continue;
    counts.set(row.district, (counts.get(row.district) ?? 0) + 1);
  }
  districtCache = [...counts.entries()]
    .map(([district, count]) => ({ district, count }))
    .sort((a, b) => b.count - a.count || a.district.localeCompare(b.district));
  return districtCache;
};

const DETAIL_COLS =
  'sreality_id,first_seen_at,last_seen_at,is_active,tom_days,' +
  'category_main,category_type,price_czk,price_unit,' +
  'area_m2,disposition,locality,district,locality_district_id,locality_region_id,' +
  'lat,lng,floor,total_floors,has_balcony,has_parking,has_lift,' +
  'building_type,condition,energy_rating,' +
  'estate_area,usable_area,garden_area,category_sub_cb,' +
  'furnished,terrace,cellar,garage,parking_lots,ownership';

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

export const fetchSnapshotsByListing = async (
  sreality_id: number,
): Promise<ListingSnapshotPublic[]> => {
  const { data, error } = await supabase
    .from('listing_snapshots_public')
    .select('id,sreality_id,scraped_at,price_czk')
    .eq('sreality_id', sreality_id)
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

export const ping = async (): Promise<{ ok: boolean; count: number | null }> => {
  const { count, error } = await supabase
    .from('listings_public')
    .select('*', { count: 'exact', head: true });
  return { ok: !error, count: count ?? null };
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
  const { data, error } = await supabase.rpc('listings_with_tags', {
    tag_ids,
  });
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
