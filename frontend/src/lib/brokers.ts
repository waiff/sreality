import { apiGet, ApiError } from './api';
import type { DistrictChip } from './filters';

// 420731404040 -> +420 731 404 040 (display only; storage stays digit-normalized).
export function prettyPhone(p: string): string {
  if (p.startsWith('420') && p.length === 12) {
    const n = p.slice(3);
    return `+420 ${n.slice(0, 3)} ${n.slice(3, 6)} ${n.slice(6)}`;
  }
  return p;
}

// Broker intelligence read layer. Every broker-* view/function is revoked from
// `anon` AND `authenticated` at the DB layer (Phase 0 Amendment A6, migration
// 299 — broker PII stays dark to non-admin sessions until Wave 4 ships masked
// columns), so this is a thin client over the admin-gated `/brokers/*` FastAPI
// routes (api/routes/brokers.py), not a second implementation reading the
// public views/RPC directly. No writes from the browser.

export type LeaderMetric =
  | 'active_property_count'
  | 'property_count'
  | 'listing_count'
  | 'active_listing_count';

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
  // The LEFT JOIN to broker_geo_options in toolkit.brokers.get_broker can miss
  // (a geo_id with no matching admin_boundaries row) — nullable, not '—'; the
  // fallback display string is a render concern, not a data-layer one.
  name: string | null;
  property_count: number;
  active_property_count: number;
  listing_count: number;
}

// One distinct (kind, value) contact across a broker's identities — the full
// reachable set for outreach, richer than BrokerPublic's primary_email/phone.
export interface BrokerContact {
  kind: string;
  value: string;
  sources: string[];
  last_seen_at: string | null;
}

// The broker detail "dossier" — one round trip for the whole /brokers/:id page
// (GET /brokers/{id}), mirroring toolkit.brokers.get_broker exactly.
export interface BrokerDossier {
  broker: BrokerPublic;
  memberships: BrokerMembership[];
  region_shares: BrokerRegionShare[];
  contacts: BrokerContact[];
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
// route. Only resolved, non-excluded chips contribute; a 'locality' chip's id is
// its containing obec.
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

// FastAPI `list[int] = Query(...)` params need a repeated key
// (?region_ids=1&region_ids=2), not the comma-joined form `apiGet`'s plain
// `params` object produces — build the query string by hand for these.
function appendIds(q: URLSearchParams, key: string, ids: ReadonlyArray<number>): void {
  for (const id of ids) q.append(key, String(id));
}

export async function fetchBrokerLeaderboard(
  p: LeaderboardParams,
): Promise<BrokerLeaderRow[]> {
  const q = new URLSearchParams();
  appendIds(q, 'region_ids', p.regionIds);
  appendIds(q, 'okres_ids', p.okresIds);
  appendIds(q, 'obec_ids', p.obecIds);
  if (p.categoryMain != null) q.set('category_main', p.categoryMain);
  if (p.categoryType != null) q.set('category_type', p.categoryType);
  q.set('metric', p.metric);
  q.set('limit', String(p.limit ?? 100));
  const res = await apiGet<{ data: BrokerLeaderRow[] }>(`/brokers/leaderboard?${q.toString()}`);
  return res.data ?? [];
}

export async function searchBrokersByName(q: string): Promise<BrokerPublic[]> {
  const term = q.trim();
  if (term.length < 2) return [];
  const res = await apiGet<{ data: BrokerPublic[] }>('/brokers/search', {
    q: term,
    limit: 12,
  });
  return res.data ?? [];
}

// Keyed on the surrogate `listing_id` (migration 343), NOT sreality_id — a
// post-Gate-2 non-sreality listing has a NULL sreality_id, so a sreality-keyed
// lookup would silently find nothing. Returns null for an unattributed listing.
export async function fetchListingBroker(listingId: number): Promise<ListingBroker | null> {
  try {
    const res = await apiGet<{ data: ListingBroker }>(`/brokers/by-listing/${listingId}`);
    return res.data;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

// Batched canonical-broker lookup for many listings at once (the pipeline board
// hydrates N cards in one round-trip — no N+1). Keyed on the surrogate
// `listing_id`, same NULL-safety reason as fetchListingBroker above.
export async function fetchListingBrokersByIds(
  listingIds: ReadonlyArray<number>,
): Promise<Map<number, ListingBroker>> {
  if (listingIds.length === 0) return new Map();
  const q = new URLSearchParams();
  appendIds(q, 'listing_ids', listingIds);
  const res = await apiGet<{ data: ListingBroker[] }>(`/brokers/by-listing?${q.toString()}`);
  const out = new Map<number, ListingBroker>();
  for (const r of res.data ?? []) out.set(r.listing_id, r);
  return out;
}

// Batched canonical-broker contact lookup by broker_id (primary email/phone +
// firm) — pairs with fetchListingBrokersByIds to fill a card's hover contact box.
export async function fetchBrokersByIds(
  brokerIds: ReadonlyArray<number>,
): Promise<Map<number, BrokerPublic>> {
  if (brokerIds.length === 0) return new Map();
  const q = new URLSearchParams();
  appendIds(q, 'broker_ids', brokerIds);
  const res = await apiGet<{ data: BrokerPublic[] }>(`/brokers/by-ids?${q.toString()}`);
  const out = new Map<number, BrokerPublic>();
  for (const r of res.data ?? []) out.set(r.broker_id, r);
  return out;
}

// The full broker-detail dossier (identity + firm memberships + regional
// footprint + every distinct contact) in one round trip. Returns null for an
// unknown / merged-away broker id.
export async function fetchBrokerDossier(brokerId: number): Promise<BrokerDossier | null> {
  try {
    const res = await apiGet<{ data: BrokerDossier }>(`/brokers/${brokerId}`);
    return res.data;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

export async function fetchBrokerListings(brokerId: number): Promise<BrokerListing[]> {
  const res = await apiGet<{ data: BrokerListing[] }>(`/brokers/${brokerId}/listings`, {
    limit: 500,
  });
  return res.data ?? [];
}
