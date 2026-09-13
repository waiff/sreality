/* Reads for the pin-loss audit page (migration 510).
 *
 * TEMPORARY, like the page it feeds: the whole module goes when the operator
 * has ruled on migration 503's pin-collapse guard.
 *
 * Two reads, deliberately shaped so the page never counts anything itself:
 *   - `fetchPinAuditSummary` — one RPC returning (portal, type, bucket, n).
 *     ~120 rows, so the overview matrix AND every "how many match the current
 *     filters" number are sums over ONE payload. The three group-by columns are
 *     exactly the page's three filter axes (active/delisted is encoded in the
 *     bucket), so a filtered total can always be derived and can never disagree
 *     with the list.
 *   - `fetchPinAuditPage` — a keyset page of 100 rows. Keyset, not offset:
 *     `.range()` is banned outside fetchAllRows (it does not lift PostgREST's
 *     row cap), and the matview carries a (last_seen_at, listing_id) btree that
 *     the shared applyKeyset emits against directly.
 */

import { supabase } from '@/lib/supabase';
import {
  applyKeyset,
  nextCursorFrom,
  withKeysetColumns,
  type KeysetBuilder,
  type KeysetCursor,
} from '@/lib/keyset';
import type { SortSpec } from '@/lib/queries';

export const PIN_AUDIT_RELATION = 'location_pin_audit_mv';
export const PIN_AUDIT_PAGE_SIZE = 100;
/* The map draws points client-side; beyond this the page says so rather than
 * silently drawing a subset (the operator must never read a capped map as the
 * whole set). */
export const PIN_AUDIT_MAP_CAP = 5000;

/* The keyset tiebreaker. `property_id` — applyKeyset's default — is NOT legal
 * here: the relation is listing-grain, and `listing_id` is its primary key. */
const TIEBREAK = 'listing_id';

export type PinAuditQuality =
  | 'active_no_claims'
  | 'active_unresolved'
  | 'delisted_no_claims'
  | 'delisted_unresolved';

export const PIN_AUDIT_QUALITIES: ReadonlyArray<PinAuditQuality> = [
  'active_no_claims',
  'active_unresolved',
  'delisted_no_claims',
  'delisted_unresolved',
];

export interface PinAuditRow {
  listing_id: number;
  property_id: number;
  sreality_id: number | null;
  source: string;
  source_id_native: string | null;
  source_url: string | null;
  category_main: string | null;
  category_type: string | null;
  disposition: string | null;
  area_m2: number | null;
  street: string | null;
  locality: string | null;
  district: string | null;
  price_czk: number | null;
  is_active: boolean;
  first_seen_at: string;
  last_seen_at: string;
  legacy_lat: number;
  legacy_lng: number;
  country_status: string | null;
  granularity: string | null;
  match_confidence: string | null;
  resolver_version: string | null;
  resolved_at: string | null;
  has_row: boolean;
  has_claims: boolean;
  claims_now: boolean;
  quality: PinAuditQuality;
  refreshed_at: string;
}

export interface PinAuditSummaryRow {
  source: string;
  category_main: string | null;
  quality: PinAuditQuality;
  n: number;
  /* One value across the whole relation — the last refresh. */
  refreshed_at: string | null;
}

export interface PinAuditFilters {
  /* Empty array = no constraint on that axis (the fail-open filter idiom). */
  sources: ReadonlyArray<string>;
  categories: ReadonlyArray<string>;
  qualities: ReadonlyArray<PinAuditQuality>;
  status: 'all' | 'active' | 'delisted';
}

export const EMPTY_PIN_AUDIT_FILTERS: PinAuditFilters = {
  sources: [],
  categories: [],
  qualities: [],
  status: 'all',
};

const ROW_COLS = [
  'listing_id', 'property_id', 'sreality_id', 'source', 'source_id_native',
  'source_url', 'category_main', 'category_type', 'disposition', 'area_m2',
  'street', 'locality', 'district', 'price_czk', 'is_active', 'first_seen_at',
  'last_seen_at', 'legacy_lat', 'legacy_lng', 'country_status', 'granularity',
  'match_confidence', 'resolver_version', 'resolved_at', 'has_row',
  'has_claims', 'claims_now', 'quality', 'refreshed_at',
].join(',');

const POINT_COLS = 'listing_id,legacy_lat,legacy_lng,quality,is_active';

/* A PostgREST builder narrowed to the filter methods this module uses, so the
 * helper below is shared by the list read and the map read without either
 * leaking supabase-js's generics. */
interface FilterBuilder {
  in: (column: string, values: readonly string[]) => FilterBuilder;
  eq: (column: string, value: boolean) => FilterBuilder;
}

function applyPinAuditFilters<T extends FilterBuilder>(
  query: T,
  f: PinAuditFilters,
): T {
  let q = query;
  if (f.sources.length > 0) q = q.in('source', f.sources) as T;
  if (f.categories.length > 0) q = q.in('category_main', f.categories) as T;
  if (f.qualities.length > 0) q = q.in('quality', f.qualities) as T;
  if (f.status !== 'all') q = q.eq('is_active', f.status === 'active') as T;
  return q;
}

/* Does a summary row fall inside the filters? The same predicate the SQL
 * applies, so the matrix and the list can never disagree. */
export function summaryRowMatches(
  row: PinAuditSummaryRow,
  f: PinAuditFilters,
): boolean {
  if (f.sources.length > 0 && !f.sources.includes(row.source)) return false;
  if (f.categories.length > 0 && !f.categories.includes(row.category_main ?? '')) {
    return false;
  }
  if (f.qualities.length > 0 && !f.qualities.includes(row.quality)) return false;
  if (f.status === 'active' && !row.quality.startsWith('active_')) return false;
  if (f.status === 'delisted' && !row.quality.startsWith('delisted_')) return false;
  return true;
}

export const fetchPinAuditSummary = async (): Promise<PinAuditSummaryRow[]> => {
  const { data, error } = await supabase.rpc('location_pin_audit_summary');
  if (error) throw error;
  return (data ?? []) as PinAuditSummaryRow[];
};

export interface PinAuditPage {
  rows: PinAuditRow[];
  nextCursor: KeysetCursor | null;
}

export const fetchPinAuditPage = async (
  f: PinAuditFilters,
  sort: SortSpec,
  cursor: KeysetCursor | null,
): Promise<PinAuditPage> => {
  const base = supabase
    .from(PIN_AUDIT_RELATION)
    .select(withKeysetColumns(ROW_COLS, sort, TIEBREAK));
  const scoped = applyPinAuditFilters(base as unknown as FilterBuilder, f);
  const keyed = applyKeyset(
    scoped as unknown as KeysetBuilder,
    sort,
    cursor,
    TIEBREAK,
  ) as unknown as typeof base;
  const { data, error } = await keyed.limit(PIN_AUDIT_PAGE_SIZE);
  if (error) throw error;
  const rows = (data ?? []) as unknown as PinAuditRow[];
  return {
    rows,
    nextCursor: nextCursorFrom(
      rows as unknown as Record<string, unknown>[],
      sort,
      TIEBREAK,
    ),
  };
};

export interface PinAuditPoint {
  listing_id: number;
  legacy_lat: number;
  legacy_lng: number;
  quality: PinAuditQuality;
  is_active: boolean;
}

export interface PinAuditPoints {
  points: PinAuditPoint[];
  /* True when the cohort is larger than the cap, so the map is showing a
   * prefix. The page must SAY so. */
  capped: boolean;
}

/* One bounded read for the map. Ordered by listing_id so the prefix is stable
 * between refetches (an unordered LIMIT may return a different subset each
 * time, which would make pins flicker in and out for no reason). The cap+1
 * probe is how "there are more than we drew" is detected without a count. */
export const fetchPinAuditPoints = async (
  f: PinAuditFilters,
): Promise<PinAuditPoints> => {
  const base = supabase.from(PIN_AUDIT_RELATION).select(POINT_COLS);
  const scoped = applyPinAuditFilters(
    base as unknown as FilterBuilder,
    f,
  ) as unknown as typeof base;
  const { data, error } = await scoped
    .order('listing_id', { ascending: true })
    .limit(PIN_AUDIT_MAP_CAP + 1);
  if (error) throw error;
  const all = (data ?? []) as unknown as PinAuditPoint[];
  return {
    points: all.slice(0, PIN_AUDIT_MAP_CAP),
    capped: all.length > PIN_AUDIT_MAP_CAP,
  };
};
