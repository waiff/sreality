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

/* Sort columns that are NOT NULL on `properties`. For these the NULLS-LAST
 * tail doesn't exist, so the `field IS NULL` disjunct (which nullable columns
 * REQUIRE — see applyKeyset) is omitted: an `OR col IS NULL` defeats the
 * composite (col, id) btree, forcing a seq-scan + sort, and these are exactly
 * the hot, indexed lanes (the default last_seen_at and the cards' first_seen
 * presets). Verified against information_schema; keep in sync if a sort
 * column's nullability changes. Everything else is treated as nullable. */
const NON_NULL_SORT_FIELDS = new Set<string>([
  'last_seen_at',
  'first_seen_at',
  'is_active',
]);

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
  gt: (column: string, value: number) => KeysetBuilder;
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

/* Apply the keyset ORDER BY (sort field, then property_id — BOTH in the sort
 * direction) and, when a cursor is supplied, the "everything strictly after
 * this row" predicate. Tying the tiebreaker to the sort direction (rather
 * than fixing it DESC) is what lets a single plain `(col, id)` btree serve
 * BOTH scroll directions via a forward / backward scan. Direction-aware: DESC
 * pages with `<`, ASC with `>`, for the field AND the tiebreaker. */
export function applyKeyset<T extends KeysetBuilder>(
  query: T,
  sort: SortSpec,
  cursor: KeysetCursor | null,
): T {
  const asc = sort.direction === 'asc';
  const ordered = query
    .order(sort.field, { ascending: asc, nullsFirst: false })
    .order('property_id', { ascending: asc }) as T;

  if (!cursor) return ordered;

  /* Phase 2 — NULLS-LAST tail: every non-null-sort-value row has already
   * been emitted, so the remainder is exactly the null rows beyond the
   * tiebreaker. A plain AND of two predicates, no OR needed. */
  if (cursor.value === null) {
    const tail = ordered.is(sort.field, null);
    return (asc
      ? tail.gt('property_id', cursor.id)
      : tail.lt('property_id', cursor.id)) as T;
  }

  /* Phase 1 — non-null block. Disjuncts: the strictly-beyond rows, the
   * equal-value-beyond-tiebreaker rows, and (NULLABLE columns only) every
   * NULL row. That last term is essential and easy to miss: with NULLS LAST,
   * all nulls sort AFTER every non-null, so "everything after a non-null
   * boundary" includes them — but `field <op> v` can never match a null
   * (null comparisons aren't true), so without `field IS NULL` the page at
   * the non-null→null boundary returns empty and the whole tail becomes
   * unreachable. The ORDER BY keeps nulls last, so they only surface once the
   * non-nulls within the LIMIT are exhausted. NOT NULL columns omit the term
   * (it would defeat the index — see NON_NULL_SORT_FIELDS). */
  const op = asc ? 'gt' : 'lt';
  const v = formatKeysetValue(cursor.value);
  const terms = [
    `${sort.field}.${op}.${v}`,
    `and(${sort.field}.eq.${v},property_id.${op}.${cursor.id})`,
  ];
  if (!NON_NULL_SORT_FIELDS.has(sort.field)) {
    terms.push(`${sort.field}.is.null`);
  }
  return ordered.or(terms.join(',')) as T;
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
