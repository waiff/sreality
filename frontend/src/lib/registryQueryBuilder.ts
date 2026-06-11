/* Registry-driven PostgREST filter dispatcher for the Browse page.
 *
 * Before this module existed, every filter had to be hand-wired in
 * `queries.ts:applyFilters` to the matching PostgREST `.gte()` /
 * `.lte()` / `.in()` / `.eq()` call. That was easy to forget — when
 * the condition-level filters landed in B3 the registry, API schemas,
 * and `ComparableFilters` were all wired, but the PostgREST
 * translation step was missed and the cohort silently never narrowed.
 *
 * `applyRegistryFilters` walks `FILTER_REGISTRY.filters` at runtime
 * and dispatches each BROWSE-eligible entry to the right PostgREST
 * call based on its `type` and id suffix:
 *
 *   - `tristate` (`has_balcony`, `terrace`, …)           → `.eq(col, true|false)` when not 'any'
 *   - `string_list` (`condition_match`, `dispositions`)  → `.in(col, values)` when non-empty
 *   - id starts with `min_` OR ends with `_min`          → `.gte(col, value)`
 *   - id starts with `max_` OR ends with `_max`          → `.lte(col, value)`
 *   - plain `string` / `int` / `float` / `bool`          → `.eq(col, value)`
 *
 * Irregular filter shapes (status enum → boolean column predicate,
 * days-ago → ISO timestamp, building_material → many-buckets-to-many
 * building_type expansion, districts → multi-column ILIKE OR predicate, tags →
 * not yet wired in PostgREST) stay hand-coded in `queries.ts` and
 * are listed in HAND_CODED_BROWSE_FILTERS so the dispatcher skips
 * them. The drift test (`registryQueryBuilder.test.ts`) asserts
 * every BROWSE-eligible registry filter is either in that set or
 * matches one of the auto-dispatch patterns above — so a new
 * registry entry that fits no path fails CI loudly instead of
 * silently no-op'ing in the UI. */

import { FILTER_REGISTRY, type FilterDef } from './filterRegistry.generated';
import { REGISTRY_KEY_MAP, type ListingFilters } from './filters';

/* Registry IDs whose shape is too irregular for the auto-dispatcher.
 * `queries.ts:applyFilters` handles these directly. */
export const HAND_CODED_BROWSE_FILTERS: ReadonlySet<string> = new Set([
  // Multi-value enum → boolean column predicate.
  'status',
  // Days-ago integers → ISO timestamp predicates on first/last seen.
  'last_seen_min_days',
  'last_seen_max_days',
  'first_seen_min_days',
  'first_seen_max_days',
  // Status-section recency presets → days-ago ISO timestamp on
  // first_seen_at / last_change_at.
  'recently_added_days',
  'recently_changed_days',
  // One enum → IN over multiple building_type values (cihla/panel/smisena/ostatni-bucket).
  'building_material',
  // Multi-chip → multi-column ILIKE OR predicate.
  'districts',
  // Multi-select enums whose '__unknown__' sentinel needs an `.or(is.null,…)`
  // predicate the plain `.in` auto-path can't express.
  'furnished',
  'ownership',
  // Operator tags — not yet translated to PostgREST (no public-view column).
  'tags',
  // Merged price-history pair (migration 173): the window picks which
  // precomputed count column the threshold reads, and the signed
  // total-change predicate flips direction on sign — both hand-coded in
  // queries.ts:applyFilters.
  'price_change_count_min',
  'price_change_window_days',
  'total_price_change_pct',
  // With-estimates is a property-id allowlist prefilter
  // (property_estimates_public), not a column predicate.
  'with_estimates',
]);

/* Minimal PostgREST builder shape we need to call into. The real
 * supabase-js builder satisfies this structurally. */
type PostgrestBuilder = {
  eq:  (col: string, v: unknown) => PostgrestBuilder;
  gte: (col: string, v: unknown) => PostgrestBuilder;
  lte: (col: string, v: unknown) => PostgrestBuilder;
  in:  (col: string, v: readonly unknown[]) => PostgrestBuilder;
};

/* True when `id` matches the `_min` suffix pattern (`min_X` or `X_min`). */
const isMinId = (id: string): boolean =>
  id.startsWith('min_') || id.endsWith('_min');

/* True when `id` matches the `_max` suffix pattern. */
const isMaxId = (id: string): boolean =>
  id.startsWith('max_') || id.endsWith('_max');

/* Tristates are a UI control wrapping a `bool` column, not a separate
 * FilterType — the registry encodes them as `type: "bool"` +
 * `ui_control: "tristate"`. Detecting them by ui_control keeps the
 * three-way enum semantics ('any' → no clause, 'yes'/'no' → eq bool). */
const isTristate = (f: FilterDef): boolean => f.ui_control === 'tristate';

/* True iff the registry filter can be dispatched by the auto-builder.
 * Used by the drift test to assert every browse filter is either
 * hand-coded or auto-dispatchable. */
export const isAutoDispatchable = (f: FilterDef): boolean => {
  if (f.pg_column == null) return false;
  if (isTristate(f)) return true;
  if (f.type === 'string_list') return true;
  if (f.type === 'string' || f.type === 'int' || f.type === 'float' || f.type === 'bool') {
    return true; // eq / gte / lte branches handle all of these
  }
  return false;
};

/* Apply every BROWSE-eligible registry filter that isn't in the
 * hand-coded set. Idempotent on each call; returns the chained builder. */
export const applyRegistryFilters = <T>(q: T, f: ListingFilters): T => {
  let r = q as unknown as PostgrestBuilder;
  const filterRecord = f as unknown as Record<string, unknown>;

  for (const filter of FILTER_REGISTRY.filters) {
    if (HAND_CODED_BROWSE_FILTERS.has(filter.id)) continue;
    if (!filter.agendas.includes('browse')) continue;
    if (filter.pg_column == null) continue;

    const key = REGISTRY_KEY_MAP[filter.id as keyof typeof REGISTRY_KEY_MAP];
    if (key === undefined) continue;

    const value = filterRecord[key];
    if (value === null || value === undefined) continue;

    // Tristate first — has to win over `type === 'bool'` below.
    if (isTristate(filter)) {
      if (value === 'any') continue;
      r = r.eq(filter.pg_column, value === 'yes');
      continue;
    }

    switch (filter.type) {
      case 'string_list': {
        if (!Array.isArray(value) || value.length === 0) break;
        r = r.in(filter.pg_column, value);
        break;
      }
      case 'int':
      case 'float': {
        if (isMinId(filter.id)) {
          r = r.gte(filter.pg_column, value);
        } else if (isMaxId(filter.id)) {
          r = r.lte(filter.pg_column, value);
        } else {
          r = r.eq(filter.pg_column, value);
        }
        break;
      }
      case 'string':
      case 'bool': {
        r = r.eq(filter.pg_column, value);
        break;
      }
      default: {
        // Unknown type — skip. The drift test would have caught it
        // earlier so this branch should be unreachable at runtime.
        break;
      }
    }
  }

  return r as unknown as T;
};
