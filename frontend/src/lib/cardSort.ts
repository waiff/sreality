/* Client-side card sorting — the shared half of an app-wide sort story.
 *
 * WHAT IS AND IS NOT SHARED. Browse sorts SERVER-side (keyset pagination
 * against `browse_list`), so its sort fields must be real SQL columns and its
 * comparator lives in Postgres. Small boards — the pipeline kanban, a
 * collection, a watchdog result set — hold every row in memory and sort
 * client-side. Those two can never share a comparator, but they MUST share:
 *
 *   - the URL serialization (`-field` for descending, bare for ascending), so
 *     `?sort=` means the same thing on every page and a link is portable;
 *   - null handling (NULLS LAST in both directions, matching Postgres's
 *     `NULLS LAST` and the `nullsFirst: false` Browse already passes);
 *   - Czech collation for every string key (see ./collator).
 *
 * This module owns that shared half plus the in-memory executor. It is
 * deliberately generic over the row type and the field union so a surface
 * declares its own sortable fields without widening Browse's `SortField`
 * (which is constrained by what `browse_list` actually has a column for).
 */

import { compareCs } from './collator';

export type SortDirection = 'asc' | 'desc';

export interface Sort<F extends string> {
  field: F;
  direction: SortDirection;
}

/** One entry in a surface's sort menu. `value` is the URL token. */
export interface SortOption<F extends string> {
  value: string;
  label: string;
  field: F;
  direction: SortDirection;
}

/** A sort key extracted from a row. `null` always sorts last. */
export type SortKey = string | number | null;
export type Accessor<T> = (row: T) => SortKey;

/** `{field:'price_czk', direction:'desc'}` → `'-price_czk'`. */
export const sortParamOf = <F extends string>(s: Sort<F>): string =>
  `${s.direction === 'desc' ? '-' : ''}${s.field}`;

/** Inverse of `sortParamOf`, validated against the surface's own option list.
 *  An unknown or malformed token falls back to `fallback` rather than throwing —
 *  stored presets and hand-edited URLs both reach this. */
export function parseSortParam<F extends string>(
  raw: string | null | undefined,
  options: ReadonlyArray<SortOption<F>>,
  fallback: Sort<F>,
): Sort<F> {
  if (!raw) return fallback;
  const hit = options.find((o) => o.value === raw);
  return hit ? { field: hit.field, direction: hit.direction } : fallback;
}

/* NULLS LAST in BOTH directions. Postgres defaults to NULLS LAST for ASC and
 * NULLS FIRST for DESC; Browse overrides that to `nullsFirst: false`
 * everywhere. A card with no price must sit at the bottom whichever way the
 * operator flips the sort — never float to the top of "most expensive". */
function compareKeys(a: SortKey, b: SortKey, direction: SortDirection): number {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  const sign = direction === 'desc' ? -1 : 1;
  if (typeof a === 'number' && typeof b === 'number') {
    return a === b ? 0 : (a < b ? -1 : 1) * sign;
  }
  return compareCs(String(a), String(b)) * sign;
}

/** Build a stable sorter for one surface.
 *
 * `tiebreak` is REQUIRED and must be a total order on the row type. Without it
 * two cards with equal keys would keep whatever order the fetch happened to
 * return, which re-shuffles on every refetch — the exact non-determinism the
 * pipeline board shipped with (a single global `ORDER BY board_position` whose
 * values collide within a stage). Pass something guaranteed unique, e.g. the
 * primary key. */
export function makeSorter<T, F extends string>(
  accessors: Readonly<Record<F, Accessor<T>>>,
  tiebreak: (a: T, b: T) => number,
) {
  return (rows: readonly T[], spec: Sort<F>): T[] => {
    const get = accessors[spec.field];
    if (!get) return [...rows];
    // Copy first: Array.prototype.sort mutates, and these arrays come straight
    // out of the React Query cache.
    return [...rows].sort((a, b) => {
      const c = compareKeys(get(a), get(b), spec.direction);
      return c !== 0 ? c : tiebreak(a, b);
    });
  };
}

/** Numeric ascending comparator for a tiebreak on an id-like field. */
export const byNumber =
  <T>(get: (row: T) => number) =>
  (a: T, b: T): number =>
    get(a) - get(b);

/** Parse an ISO timestamp to epoch ms for use as a numeric sort key.
 *  Invalid / absent → null, so it lands under NULLS LAST like any other gap. */
export const timeKey = (iso: string | null | undefined): number | null => {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
};
