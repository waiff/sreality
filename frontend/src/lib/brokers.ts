import { supabase } from './supabase';
import type { DistrictChip } from './filters';

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
  // NULL for a post-Gate-2 (non-sreality) listing. listing_id is the
  // surrogate that's always present — use it for a stable React key.
  sreality_id: number | null;
  listing_id: number;
  source: string;
  source_url: string | null;
  locality: string | null;
  district: string | null;
  category_main: string | null;
  category_type: string | null;
  disposition: string | null;
  subtype: string | null;
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
  regionIds: number[];
  okresIds: number[];
  obecIds: number[];
  categoryMain: string | null;
  categoryType: string | null;
  metric: LeaderMetric;
  limit?: number;
}

export interface ListingBroker {
  // NULL for a post-Gate-2 (non-sreality) listing. listing_id (migration 343)
  // is the surrogate that's always present — key lookups on it, not this.
  sreality_id: number | null;
  listing_id: number;
  broker_id: number;
  broker_display_name: string | null;
  broker_firm_label: string | null;
}

// Split Browse location chips into per-level admin-id arrays for the leaderboard
// RPC. Only resolved, non-excluded chips contribute; a 'locality' chip's id is its
// containing obec.
export function chipsToGeoArrays(chips: DistrictChip[]): {
  regionIds: number[];
  okresIds: number[];
  obecIds: number[];
} {
  const regionIds: number[] = [];
  const okresIds: number[] = [];
  const obecIds: number[] = [];
  for (const c of chips) {
    if (c.excluded || c.id == null) continue;
    if (c.level === 'kraj') regionIds.push(c.id);
    else if (c.level === 'okres') okresIds.push(c.id);
    else if (c.level === 'obec' || c.level === 'locality') obecIds.push(c.id);
  }
  return { regionIds, okresIds, obecIds };
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
    p_region_ids: p.regionIds.length ? p.regionIds : null,
    p_okres_ids: p.okresIds.length ? p.okresIds : null,
    p_obec_ids: p.obecIds.length ? p.obecIds : null,
    p_category_main: p.categoryMain,
    p_category_type: p.categoryType,
    p_metric: p.metric,
    p_limit: p.limit ?? 100,
  });
  if (error) throw error;
  return (data ?? []) as BrokerLeaderRow[];
}

export async function searchBrokersByName(q: string): Promise<BrokerPublic[]> {
  const term = q.trim();
  if (term.length < 2) return [];
  const { data, error } = await supabase
    .from('brokers_public')
    .select('*')
    .ilike('display_name', `%${term}%`)
    .order('active_property_count', { ascending: false })
    .limit(12);
  if (error) throw error;
  return (data ?? []) as BrokerPublic[];
}

// Keyed on the surrogate `listing_id` (listing_broker_public.listing_id,
// migration 343), NOT sreality_id — a post-Gate-2 non-sreality listing has a
// NULL sreality_id, so a sreality-keyed lookup would silently find nothing.
export async function fetchListingBroker(listingId: number): Promise<ListingBroker | null> {
  const { data, error } = await supabase
    .from('listing_broker_public')
    .select('*')
    .eq('listing_id', listingId)
    .maybeSingle();
  if (error) throw error;
  return (data as ListingBroker) ?? null;
}

// Batched canonical-broker lookup for many listings at once (the pipeline board
// hydrates N cards in one round-trip — no N+1). Keyed on the surrogate
// `listing_id`, same NULL-safety reason as fetchListingBroker above.
export async function fetchListingBrokersByIds(
  listingIds: ReadonlyArray<number>,
): Promise<Map<number, ListingBroker>> {
  if (listingIds.length === 0) return new Map();
  const { data, error } = await supabase
    .from('listing_broker_public')
    .select('sreality_id, listing_id, broker_id, broker_display_name, broker_firm_label')
    .in('listing_id', listingIds as number[]);
  if (error) throw error;
  const out = new Map<number, ListingBroker>();
  for (const r of (data ?? []) as ListingBroker[]) out.set(r.listing_id, r);
  return out;
}

// Batched canonical-broker contact lookup by broker_id (primary email/phone +
// firm) — pairs with fetchListingBrokersByIds to fill a card's hover contact box.
export async function fetchBrokersByIds(
  brokerIds: ReadonlyArray<number>,
): Promise<Map<number, BrokerPublic>> {
  if (brokerIds.length === 0) return new Map();
  const { data, error } = await supabase
    .from('brokers_public')
    .select('*')
    .in('broker_id', brokerIds as number[]);
  if (error) throw error;
  const out = new Map<number, BrokerPublic>();
  for (const r of (data ?? []) as BrokerPublic[]) out.set(r.broker_id, r);
  return out;
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
