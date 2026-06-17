import { supabase } from './supabase';

// 420731404040 -> +420 731 404 040 (display only; storage stays digit-normalized).
export function prettyPhone(p: string): string {
  if (p.startsWith('420') && p.length === 12) {
    const n = p.slice(3);
    return `+420 ${n.slice(0, 3)} ${n.slice(3, 6)} ${n.slice(6)}`;
  }
  return p;
}

// Broker intelligence read layer. All reads go through the anon-public views +
// the broker_leaderboard RPC (migrations 187 / 189). No writes from the browser.

export type GeoLevel = 'region' | 'okres';
export type LeaderMetric =
  | 'active_property_count'
  | 'property_count'
  | 'listing_count'
  | 'active_listing_count';

export interface BrokerGeoOption {
  geo_level: GeoLevel;
  geo_id: number;
  name: string;
  parent_id: number | null;
  broker_count: number;
}

export interface BrokerLeaderRow {
  broker_id: number;
  display_name: string | null;
  primary_email: string | null;
  primary_phone: string | null;
  firm_name: string | null;
  firm_domain: string | null;
  listing_count: number;
  property_count: number;
  active_listing_count: number;
  active_property_count: number;
}

export interface BrokerPublic {
  broker_id: number;
  display_name: string | null;
  primary_email: string | null;
  primary_phone: string | null;
  firm_id: number | null;
  firm_domain: string | null;
  firm_name: string | null;
  firm_is_franchise: boolean | null;
  source_count: number;
  distinct_source_count: number;
  listing_count: number;
  property_count: number;
  active_listing_count: number;
  active_property_count: number;
  first_seen_at: string | null;
  last_seen_at: string | null;
}

export interface BrokerMembership {
  broker_id: number;
  firm_id: number;
  firm_domain: string | null;
  firm_name: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  listing_count: number;
  is_current: boolean;
}

export interface BrokerListing {
  broker_id: number;
  sreality_id: number;
  source: string;
  source_url: string | null;
  locality: string | null;
  district: string | null;
  category_main: string | null;
  category_type: string | null;
  disposition: string | null;
  area_m2: number | null;
  price_czk: number | null;
  is_active: boolean;
  last_seen_at: string | null;
  property_id: number | null;
}

export interface BrokerRegionShare {
  geo_id: number;
  name: string;
  property_count: number;
  active_property_count: number;
  listing_count: number;
}

export interface LeaderboardParams {
  geoLevel: GeoLevel;
  geoId: number;
  categoryMain: string | null;
  categoryType: string | null;
  metric: LeaderMetric;
  limit?: number;
}

export async function fetchBrokerGeoOptions(): Promise<BrokerGeoOption[]> {
  const { data, error } = await supabase
    .from('broker_geo_options')
    .select('geo_level, geo_id, name, parent_id, broker_count');
  if (error) throw error;
  return (data ?? []) as BrokerGeoOption[];
}

export async function fetchBrokerLeaderboard(
  p: LeaderboardParams,
): Promise<BrokerLeaderRow[]> {
  const { data, error } = await supabase.rpc('broker_leaderboard', {
    p_geo_level: p.geoLevel,
    p_geo_id: p.geoId,
    p_category_main: p.categoryMain,
    p_category_type: p.categoryType,
    p_metric: p.metric,
    p_limit: p.limit ?? 200,
  });
  if (error) throw error;
  return (data ?? []) as BrokerLeaderRow[];
}

export async function fetchBroker(brokerId: number): Promise<BrokerPublic | null> {
  const { data, error } = await supabase
    .from('brokers_public')
    .select('*')
    .eq('broker_id', brokerId)
    .maybeSingle();
  if (error) throw error;
  return (data as BrokerPublic) ?? null;
}

export async function fetchBrokerMemberships(
  brokerId: number,
): Promise<BrokerMembership[]> {
  const { data, error } = await supabase
    .from('broker_firm_memberships_public')
    .select('*')
    .eq('broker_id', brokerId)
    .order('last_seen_at', { ascending: false });
  if (error) throw error;
  return (data ?? []) as BrokerMembership[];
}

export async function fetchBrokerListings(brokerId: number): Promise<BrokerListing[]> {
  const { data, error } = await supabase
    .from('broker_listings_public')
    .select('*')
    .eq('broker_id', brokerId)
    .order('is_active', { ascending: false })
    .order('last_seen_at', { ascending: false })
    .limit(500);
  if (error) throw error;
  return (data ?? []) as BrokerListing[];
}

// The broker's regional footprint: the leaderboard matview at region grain, summed
// across categories (disjoint per property), with region names from the geo options.
export async function fetchBrokerRegionShares(
  brokerId: number,
  regionNames: Map<number, string>,
): Promise<BrokerRegionShare[]> {
  const { data, error } = await supabase
    .from('broker_region_type_stats')
    .select('geo_id, property_count, active_property_count, listing_count')
    .eq('broker_id', brokerId)
    .eq('geo_level', 'region');
  if (error) throw error;
  const byRegion = new Map<number, BrokerRegionShare>();
  for (const r of (data ?? []) as Array<{
    geo_id: number;
    property_count: number;
    active_property_count: number;
    listing_count: number;
  }>) {
    const cur = byRegion.get(r.geo_id) ?? {
      geo_id: r.geo_id,
      name: regionNames.get(r.geo_id) ?? '—',
      property_count: 0,
      active_property_count: 0,
      listing_count: 0,
    };
    cur.property_count += r.property_count;
    cur.active_property_count += r.active_property_count;
    cur.listing_count += r.listing_count;
    byRegion.set(r.geo_id, cur);
  }
  return [...byRegion.values()].sort(
    (a, b) => b.active_property_count - a.active_property_count,
  );
}
