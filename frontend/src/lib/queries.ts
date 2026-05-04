import { supabase } from './supabase';
import {
  type ListingFilters,
  seenWithinToIso,
} from './filters';

/* Maplibre-gl renders a GeoJSON source via WebGL with clustering, so
 * the bottleneck is wire-bytes, not DOM. 50k features ≈ 0.3 MB gzipped. */
export const MAP_CAP = 50_000;
export const TABLE_PAGE_SIZE = 50;

const MAP_COLS = 'sreality_id,lat,lng,price_czk,disposition,area_m2,district,last_seen_at,is_active';
const TABLE_COLS = 'sreality_id,district,disposition,area_m2,price_czk,last_seen_at,is_active';

export type SortField =
  | 'sreality_id' | 'district' | 'disposition'
  | 'area_m2' | 'price_czk'
  | 'last_seen_at' | 'is_active';

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
  if (f.activeOnly) r = r.eq('is_active', true);
  const since = seenWithinToIso(f.seenWithin);
  if (since) r = r.gte('last_seen_at', since);
  if (f.districts.length) r = r.in('district', f.districts);
  if (f.dispositions.length) r = r.in('disposition', f.dispositions);
  if (f.priceMin != null) r = r.gte('price_czk', f.priceMin);
  if (f.priceMax != null) r = r.lte('price_czk', f.priceMax);
  if (f.areaMin  != null) r = r.gte('area_m2',  f.areaMin);
  if (f.areaMax  != null) r = r.lte('area_m2',  f.areaMax);
  if (f.hasBalcony !== 'any') r = r.eq('has_balcony', f.hasBalcony === 'yes');
  if (f.hasLift    !== 'any') r = r.eq('has_lift',    f.hasLift    === 'yes');
  if (f.hasParking !== 'any') r = r.eq('has_parking', f.hasParking === 'yes');
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
}

export interface MapResult {
  rows: MapRow[];
  total: number | null;
  capped: boolean;
}

export const fetchListingsForMap = async (
  f: ListingFilters,
): Promise<MapResult> => {
  const base = supabase
    .from('listings_public')
    .select(MAP_COLS, { count: 'exact' })
    .not('lat', 'is', null)
    .not('lng', 'is', null);
  const filtered = applyFilters(base, f);
  const { data, count, error } = await filtered.limit(MAP_CAP);
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
  last_seen_at: string;
  is_active: boolean;
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
  const from = (page - 1) * TABLE_PAGE_SIZE;
  const to = from + TABLE_PAGE_SIZE - 1;
  const base = supabase
    .from('listings_public')
    .select(TABLE_COLS, { count: 'exact' });
  const filtered = applyFilters(base, f);
  const sorted = filtered.order(sort.field, {
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

export interface DistrictFacet {
  district: string;
  count: number;
}

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

export const ping = async (): Promise<{ ok: boolean; count: number | null }> => {
  const { count, error } = await supabase
    .from('listings_public')
    .select('*', { count: 'exact', head: true });
  return { ok: !error, count: count ?? null };
};
