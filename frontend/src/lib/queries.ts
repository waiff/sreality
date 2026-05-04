import { supabase } from './supabase';
import {
  type ListingFilters,
  seenWithinToIso,
} from './filters';

export const MAP_CAP = 5000;
export const TABLE_PAGE_SIZE = 50;

const MAP_COLS = 'sreality_id,lat,lng,price_czk,disposition,area_m2,district,last_seen_at,is_active';

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
