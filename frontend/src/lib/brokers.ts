import { ApiError, apiGet, apiPost } from './api';
import type { DistrictChip } from './filters';

// 420731404040 -> +420 731 404 040 (display only; storage stays digit-normalized).
export function prettyPhone(p: string): string {
  if (p.startsWith('420') && p.length === 12) {
    const n = p.slice(3);
    return `+420 ${n.slice(0, 3)} ${n.slice(3, 6)} ${n.slice(6)}`;
  }
  return p;
}

/* Broker intelligence read layer — every read goes through the FastAPI service
 * (`/brokers/*`) on the caller's real Supabase session JWT, never PostgREST.
 *
 * WHY not supabase-js, as this module did until 2026-08-12: migration 299's
 * Amendment A6 revoked every broker view/RPC from `authenticated`, so the direct
 * reads had been silently dark since 2026-07-12 — each one degraded to "no
 * broker" instead of failing. The API's service-role path is now the only one
 * that answers, and it rejects the static VITE_API_TOKEN outright, so `jwt: true`
 * is mandatory on every call below.
 *
 * Contact PII is masked server-side per caller (toolkit.brokers.apply_pii_policy):
 * a non-admin's rows carry `has_email` / `has_phone` flags INSTEAD of the value
 * columns — the keys themselves differ — and the dossier's raw `contacts` array is
 * dropped whole. Render that through `contactState` so a hidden contact never
 * looks like an absent one.
 */

export type LeaderMetric =
  | 'active_property_count'
  | 'property_count'
  | 'listing_count'
  | 'active_listing_count';

/* The masked pair. `primary_*` is present only for an admin session; a non-admin
 * gets `has_*` in its place. Both are optional because which one arrives is a
 * property of the CALLER, not of the row. */
export interface BrokerContactFields {
  primary_email?: string | null;
  primary_phone?: string | null;
  has_email?: boolean;
  has_phone?: boolean;
}

export interface BrokerLeaderRow extends BrokerContactFields {
  broker_id: number;
  display_name: string | null;
  firm_name: string | null;
  firm_domain: string | null;
  listing_count: number;
  property_count: number;
  active_listing_count: number;
  active_property_count: number;
}

export interface BrokerPublic extends BrokerContactFields {
  broker_id: number;
  display_name: string | null;
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
  /* The same counts restricted to listings that resolved to a Czech obec
   * (migration 396). The ranking columns — broker search orders on
   * cz_active_property_count, so a surface that renders a broker row must show
   * this number, not the unscoped twin, or the order looks arbitrary. */
  cz_listing_count: number;
  cz_property_count: number;
  cz_active_listing_count: number;
  cz_active_property_count: number;
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
  name: string | null;
  property_count: number;
  active_property_count: number;
  listing_count: number;
}

/* One distinct (kind, value) contact across a broker's identities. Admin-only —
 * the dossier omits the whole array for everyone else. */
export interface BrokerContact {
  kind: string;
  value: string;
  sources: string[];
  last_seen_at: string | null;
}

/* GET /brokers/{id} returns identity + memberships + regional footprint (+ the
 * full contact set for an admin) in ONE call, so the detail page doesn't fan out
 * four queries and doesn't need the region-name lookup the old client-side
 * aggregation depended on — `region_shares[].name` is joined server-side. */
export interface BrokerDossier {
  broker: BrokerPublic;
  memberships: BrokerMembership[];
  region_shares: BrokerRegionShare[];
  contacts?: BrokerContact[];
  pii_masked: boolean;
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

/* Three distinguishable states for one contact field. Collapsing `masked` into
 * `none` would render "this broker has no phone" for a viewer who simply isn't
 * allowed to see it. */
export type ContactState =
  | { state: 'value'; value: string }
  | { state: 'masked' }
  | { state: 'none' };

export function contactState(
  value: string | null | undefined,
  has: boolean | undefined,
): ContactState {
  if (value) return { state: 'value', value };
  return has ? { state: 'masked' } : { state: 'none' };
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

/* The standard toolkit envelope. `pii_masked` is stamped on every /brokers
 * response, masked or not. */
interface Envelope<T> {
  data: T;
  metadata?: { pii_masked?: boolean };
}

const JWT = true;

/* The two `detail` strings the /brokers routes send when 404 is an ANSWER
 * ("nothing is attributed here"), from api/routes/brokers.py. Status alone is
 * not enough: Railway's edge answers an unrouted domain with 404, a stale
 * VITE_API_BASE_URL 404s every path, and FastAPI 404s a renamed route with a
 * generic "Not Found" — swallowing any of those would put the whole corpus back
 * in the silent "Makléř nenalezen." dark state this module was repointed to end.
 * An unrecognised 404 therefore propagates and the page shows a real error. */
const ANSWERED_404 = /broker not found|listing has no attributed broker/;

const isAnsweredNotFound = (err: unknown): boolean =>
  err instanceof ApiError && err.status === 404 && ANSWERED_404.test(err.message);

/* Both batch routes bound their input at toolkit.brokers.MAX_BATCH (1000) and
 * answer a 422 rather than a truncated 200 beyond it, so one oversized call
 * would lose EVERY row instead of the overflow. Chunk below the cap; the GET
 * slice is smaller because 1000 repeated `ids=` params is an ~8 KB URL. */
const POST_ID_BATCH = 1000;
const GET_ID_BATCH = 200;

function chunk<T>(xs: ReadonlyArray<T>, size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < xs.length; i += size) out.push(xs.slice(i, i + size));
  return out;
}

/* No wrapper for GET /brokers/geo-options: this module's caller (BrokerDetail's
 * region-name map) went away when the dossier started joining region_shares[].name
 * server-side, and the page's location control is the shared LocationTypeahead +
 * chipsToGeoArrays, which matches on stable admin ids like Browse and Datasets do.
 * A broker-specific geo vocabulary would fork that contract for one page. The
 * route stays — it is the only path to broker_geo_options, which is dark to
 * `authenticated` — so re-adding a wrapper is a few lines if a consumer appears. */

export async function fetchBrokerLeaderboard(
  p: LeaderboardParams,
): Promise<BrokerLeaderRow[]> {
  const r = await apiGet<Envelope<BrokerLeaderRow[]>>(
    '/brokers/leaderboard',
    {
      region_ids: p.regionIds,
      okres_ids: p.okresIds,
      obec_ids: p.obecIds,
      category_main: p.categoryMain,
      category_type: p.categoryType,
      metric: p.metric,
      limit: p.limit ?? 100,
    },
    undefined,
    JWT,
  );
  return r.data ?? [];
}

export async function searchBrokersByName(
  q: string,
  limit = 12,
): Promise<BrokerPublic[]> {
  const term = q.trim();
  // The route requires a non-empty q; below 2 chars the result is noise anyway.
  if (term.length < 2) return [];
  const r = await apiGet<Envelope<BrokerPublic[]>>(
    '/brokers/search',
    { q: term, limit },
    undefined,
    JWT,
  );
  return r.data ?? [];
}

// Keyed on the surrogate `listing_id` (migration 343), NOT sreality_id — a
// post-Gate-2 non-sreality listing has a NULL sreality_id, so a sreality-keyed
// lookup would silently find nothing. An unattributed listing is a 404, which is
// an answer ("no broker resolved"), not a failure.
export async function fetchListingBroker(
  listingId: number,
): Promise<ListingBroker | null> {
  try {
    const r = await apiGet<Envelope<ListingBroker>>(
      '/brokers/by-listing',
      { listing_id: listingId },
      undefined,
      JWT,
    );
    return r.data ?? null;
  } catch (err) {
    if (isAnsweredNotFound(err)) return null;
    throw err;
  }
}

// Batched canonical-broker lookup for many listings at once (the pipeline board
// hydrates N cards in one round-trip — no N+1). Keyed on the surrogate
// `listing_id`, same NULL-safety reason as fetchListingBroker above.
export async function fetchListingBrokersByIds(
  listingIds: ReadonlyArray<number>,
): Promise<Map<number, ListingBroker>> {
  const out = new Map<number, ListingBroker>();
  for (const slice of chunk(listingIds, POST_ID_BATCH)) {
    const r = await apiPost<Envelope<ListingBroker[]>>(
      '/brokers/by-listings',
      { listing_ids: slice },
      undefined,
      JWT,
    );
    for (const row of r.data ?? []) out.set(row.listing_id, row);
  }
  return out;
}

// Batched canonical-broker lookup by broker_id (primary contact + firm) — pairs
// with fetchListingBrokersByIds to fill a card's hover contact box.
export async function fetchBrokersByIds(
  brokerIds: ReadonlyArray<number>,
): Promise<Map<number, BrokerPublic>> {
  const out = new Map<number, BrokerPublic>();
  for (const slice of chunk(brokerIds, GET_ID_BATCH)) {
    const r = await apiGet<Envelope<BrokerPublic[]>>(
      '/brokers',
      { ids: slice },
      undefined,
      JWT,
    );
    for (const row of r.data ?? []) out.set(row.broker_id, row);
  }
  return out;
}

export async function fetchBrokerDossier(
  brokerId: number,
): Promise<BrokerDossier | null> {
  try {
    const r = await apiGet<Envelope<Omit<BrokerDossier, 'pii_masked'>>>(
      `/brokers/${brokerId}`,
      undefined,
      undefined,
      JWT,
    );
    // A 200 that carries no envelope is not an empty dossier — it's an HTML
    // SPA-fallback or a proxy page. Spreading it would yield a broker-less object
    // that renders as "Makléř nenalezen." for every id.
    if (!r?.data) throw new ApiError('malformed /brokers response', 0, r);
    // Absent metadata means we can't prove the caller is an admin — assume masked
    // so a missing flag under-promises rather than showing a blank as "no contact".
    return { ...r.data, pii_masked: r.metadata?.pii_masked ?? true };
  } catch (err) {
    if (isAnsweredNotFound(err)) return null;
    throw err;
  }
}

export async function fetchBrokerListings(
  brokerId: number,
  limit = 500,
): Promise<BrokerListing[]> {
  const r = await apiGet<Envelope<BrokerListing[]>>(
    `/brokers/${brokerId}/listings`,
    { limit },
    undefined,
    JWT,
  );
  return r.data ?? [];
}
