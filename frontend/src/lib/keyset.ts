/* Keyset (cursor) pagination for the property-grain Browse surfaces.
 *
 * WHY keyset, not offset: the default Browse lane is sorted by
 * `last_seen_at DESC`, the very column the scraper mutates every cycle
 * (`touch_listings` bumps it on each index sighting — architectural rule
 * #4). Under OFFSET paging, a row whose `last_seen_at` is bumped jumps to
 * the top of the order, shifting every later window down by one — so the
 * next page silently re-shows a row already seen (duplicate) and skips the
 * one that slid into the boundary. Measured on the live 317k-active-row
 * `properties_public` view, `OFFSET 50000` also takes ~3.7s, exceeding the
 * anon 3s statement timeout — exactly where infinite scroll drives the
 * user. Keyset anchors each page to the concrete `(sort value, property_id)`
 * of the last row, so it is correct under mutation and FLAT in latency
 * regardless of depth (measured ~120ms at any depth).
 *
 * The tiebreaker is `property_id` (= `properties.id`): immutable, unique,
 * never null — the only safe choice (`sreality_id` is nullable and can be
 * re-pointed by the dedup engine). It is appended to every ORDER BY and
 * compared DESC, so a whole scrape batch that lands on one identical
 * `last_seen_at` second still has a total order.
 *
 * NULLS LAST two-phase: nullable sort columns (district, area_m2, …) place
 * NULLs last in both directions (`nullsFirst: false`). A cursor therefore
 * has two phases — while the boundary value is non-null we page the
 * non-null block; once the boundary value is null we have crossed into the
 * tail and page the null block by `property_id` alone.
 */

import type { SortSpec } from './queries';

/* The (sort value, tiebreaker) of the last row of the previous page.
 * `value === null` means the boundary row's sort column was SQL NULL — we
 * are in the NULLS-LAST tail. */
export interface KeysetCursor {
  value: string | number | boolean | null;
  id: number;
}

/* A PostgREST filter builder, narrowed to the methods keyset needs. The
 * supabase-js query builder satisfies this at runtime; typing it
 * structurally keeps this module decoupled from the concrete builder. */
export interface KeysetBuilder {
  order: (
    column: string,
    opts: { ascending: boolean; nullsFirst?: boolean },
  ) => KeysetBuilder;
  or: (filters: string) => KeysetBuilder;
  is: (column: string, value: null) => KeysetBuilder;
  lt: (column: string, value: number) => KeysetBuilder;
}

/* Format a non-null cursor value for embedding in a PostgREST `or=()`
 * logical string. Strings (incl. ISO timestamps and text columns like
 * `district`) are double-quoted so reserved chars (`, . : ( )`) are
 * protected; inner `"`/`\` are escaped. Numbers/booleans go bare. */
export function formatKeysetValue(value: string | number | boolean): string {
  if (typeof value === 'number') return String(value);
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  const escaped = value.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  return `"${escaped}"`;
}

/* Apply the keyset ORDER BY (sort field, then property_id DESC) and, when a
 * cursor is supplied, the "everything strictly after this row" predicate.
 * Direction-aware: DESC pages with `<`, ASC with `>`; the property_id
 * tiebreaker always pages with `<` (it is ordered DESC in both cases). */
export function applyKeyset<T extends KeysetBuilder>(
  query: T,
  sort: SortSpec,
  cursor: KeysetCursor | null,
): T {
  const ordered = query
    .order(sort.field, { ascending: sort.direction === 'asc', nullsFirst: false })
    .order('property_id', { ascending: false }) as T;

  if (!cursor) return ordered;

  /* Phase 2 — NULLS-LAST tail: every non-null-sort-value row has already
   * been emitted, so the remainder is exactly the null rows with a smaller
   * property_id. A plain AND of two predicates, no OR needed. */
  if (cursor.value === null) {
    return ordered.is(sort.field, null).lt('property_id', cursor.id) as T;
  }

  /* Phase 1 — non-null block. `field <op> v` excludes NULLs for free (NULL
   * comparisons are never true), so the tail is correctly withheld until
   * the boundary itself goes null. */
  const op = sort.direction === 'asc' ? 'gt' : 'lt';
  const v = formatKeysetValue(cursor.value);
  return ordered.or(
    `${sort.field}.${op}.${v},and(${sort.field}.eq.${v},property_id.lt.${cursor.id})`,
  ) as T;
}

/* Derive the cursor for the NEXT page from the last row of THIS page.
 * Reads the active sort column and `property_id` off the row (both are
 * guaranteed selected by the fetcher). Returns null for an empty page. */
export function nextCursorFrom(
  rows: ReadonlyArray<Record<string, unknown>>,
  sort: SortSpec,
): KeysetCursor | null {
  if (rows.length === 0) return null;
  const last = rows[rows.length - 1];
  const raw = last[sort.field];
  const value =
    raw == null
      ? null
      : (raw as string | number | boolean);
  return { value, id: Number(last.property_id) };
}

/* Ensure the SELECT carries the columns keyset needs: the tiebreaker
 * (`property_id`) and the active sort column (e.g. `price_per_m2`, a
 * computed view column not otherwise selected). Deduped, order-stable. */
export function withKeysetColumns(baseCols: string, sort: SortSpec): string {
  const cols = baseCols.split(',').map((c) => c.trim());
  const set = new Set(cols);
  set.add('property_id');
  set.add(sort.field);
  return Array.from(set).join(',');
}
