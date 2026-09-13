/* Reads for the audit page (`location_pin_audit_mv`, migration 514).
 *
 * The relation is exactly the set W5 hides from consumers: a SERVED listing
 * (live, or the display listing of a live property) whose location the store
 * cannot answer for. It is a WORK QUEUE, not a one-off review — a row leaves it
 * the moment the resolver lane places the listing.
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

/* The keyset tiebreaker. `property_id` — applyKeyset's default — is NOT legal
 * here: the relation is listing-grain, and `listing_id` is its primary key. */
const TIEBREAK = 'listing_id';

export type PinAuditQuality =
  | 'active_no_claims'
  | 'active_unresolved'
  | 'delisted_no_claims'
  | 'delisted_unresolved';

/* What SUPERSEDED evidence the listing carries, so "no live evidence" is never
 * read as "nothing was ever there". `legacy` = every superseded claim is a
 * `legacy_column` copy (the Mapy-era pin, the legacy PSČ/locality columns the
 * doctrine dropped in W1-b); `archived` = some came off an older page version;
 * `none` = there is none. */
export type PinAuditOldEvidence = 'none' | 'legacy' | 'archived';

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
  /* The ONE place string (rule 25). NULL on most of this set by construction —
   * that is the finding, not a defect. */
  display_label: string | null;
  price_czk: number | null;
  is_active: boolean;
  first_seen_at: string;
  last_seen_at: string;
  country_status: string | null;
  granularity: string | null;
  match_confidence: string | null;
  resolver_version: string | null;
  resolved_at: string | null;
  has_row: boolean;
  has_claims: boolean;
  /* Evidence under an ACTIVE contract. Measured 2026-09-13 this is exactly the
   * `has_claims` set — the lane has consumed everything a live contract
   * offers, so there is no backlog to wait for. */
  claims_now: boolean;
  old_evidence: PinAuditOldEvidence;
  sibling_has_pin: boolean;
  quality: PinAuditQuality;
  refreshed_at: string;
}

export interface PinAuditSummaryRow {
  source: string;
  category_main: string | null;
  quality: PinAuditQuality;
  sibling_has_pin: boolean;
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
  /* Does another listing of the same property already have a pin? That is the
   * property-level fallback the operator can take without a resolver change. */
  sibling: 'all' | 'yes' | 'no';
}

export const EMPTY_PIN_AUDIT_FILTERS: PinAuditFilters = {
  sources: [],
  categories: [],
  qualities: [],
  status: 'all',
  sibling: 'all',
};

const ROW_COLS = [
  'listing_id', 'property_id', 'sreality_id', 'source', 'source_id_native',
  'source_url', 'category_main', 'category_type', 'disposition', 'area_m2',
  'display_label', 'price_czk', 'is_active', 'first_seen_at',
  'last_seen_at', 'country_status', 'granularity',
  'match_confidence', 'resolver_version', 'resolved_at', 'has_row',
  'has_claims', 'claims_now', 'old_evidence', 'sibling_has_pin', 'quality',
  'refreshed_at',
].join(',');

/* A PostgREST builder narrowed to the filter methods this module uses, so the
 * helper below is shared without leaking supabase-js's generics. */
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
  if (f.sibling !== 'all') {
    q = q.eq('sibling_has_pin', f.sibling === 'yes') as T;
  }
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
  if (f.sibling === 'yes' && !row.sibling_has_pin) return false;
  if (f.sibling === 'no' && row.sibling_has_pin) return false;
  return true;
}

/* The nav badge's number: every row in the audit set, no filters — the same
 * total the page header prints. A `head` request with an exact count, so
 * PostgREST answers with the number in the Content-Range header and zero rows:
 * one cheap round trip, cheap enough to sit in the app shell. The nav must
 * never depend on it — a caller renders the label alone when this throws. */
export const PIN_AUDIT_TOTAL_KEY = ['pin-audit', 'total'] as const;

export const fetchPinAuditTotal = async (): Promise<number> => {
  const { count, error } = await supabase
    .from(PIN_AUDIT_RELATION)
    .select(TIEBREAK, { count: 'exact', head: true });
  if (error) throw error;
  return count ?? 0;
};

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
