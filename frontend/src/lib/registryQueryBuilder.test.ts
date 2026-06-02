/* Tests for the registry-driven PostgREST filter dispatcher.
 *
 * The drift guard is the most important test here: without it, a new
 * registry filter could land that fits no dispatch path AND is not
 * in HAND_CODED_BROWSE_FILTERS, and the Browse cohort would silently
 * fail to narrow when the operator sets it (exactly the
 * apartment/building_condition_level_min bug that motivated this
 * module — see PR history around #140 / #146). */

import { describe, expect, it } from 'vitest';

import { FILTER_REGISTRY } from './filterRegistry.generated';
import { DEFAULT_FILTERS, REGISTRY_KEY_MAP } from './filters';
import {
  HAND_CODED_BROWSE_FILTERS,
  applyRegistryFilters,
  isAutoDispatchable,
} from './registryQueryBuilder';


// --- A tiny PostgREST-shaped recorder so we can assert what was called ---

interface Call {
  op: 'eq' | 'gte' | 'lte' | 'in';
  col: string;
  value: unknown;
}

class _Recorder {
  calls: Call[] = [];
  eq(col: string, v: unknown):  _Recorder { this.calls.push({ op: 'eq',  col, value: v }); return this; }
  gte(col: string, v: unknown): _Recorder { this.calls.push({ op: 'gte', col, value: v }); return this; }
  lte(col: string, v: unknown): _Recorder { this.calls.push({ op: 'lte', col, value: v }); return this; }
  in(col: string, v: readonly unknown[]): _Recorder { this.calls.push({ op: 'in', col, value: [...v] }); return this; }
}


// --- Drift guard ----------------------------------------------------------


describe('drift guard', () => {
  it('every browse filter is either hand-coded or auto-dispatchable', () => {
    const orphans: string[] = [];
    for (const filter of FILTER_REGISTRY.filters) {
      if (!filter.agendas.includes('browse')) continue;
      if (filter.pg_column == null) continue;
      if (HAND_CODED_BROWSE_FILTERS.has(filter.id)) continue;

      // Must be a recognised auto-dispatch shape AND have a
      // REGISTRY_KEY_MAP entry, otherwise the runtime would silently
      // drop it.
      const hasKey = filter.id in REGISTRY_KEY_MAP;
      const auto = isAutoDispatchable(filter);
      if (!hasKey || !auto) {
        orphans.push(
          `${filter.id} (type=${filter.type}, pg_column=${filter.pg_column}, ` +
          `hasKey=${hasKey}, autoDispatchable=${auto})`,
        );
      }
    }
    if (orphans.length) {
      throw new Error(
        `Browse filters fitting no PostgREST path:\n  ` +
        orphans.join('\n  ') +
        `\nAdd to HAND_CODED_BROWSE_FILTERS (and handle in ` +
        `queries.ts:applyFilters) or extend isAutoDispatchable + ` +
        `applyRegistryFilters in registryQueryBuilder.ts.`,
      );
    }
  });

  it('hand-coded set only contains real registry filter ids', () => {
    const known = new Set(FILTER_REGISTRY.filters.map((f) => f.id));
    for (const id of HAND_CODED_BROWSE_FILTERS) {
      expect(known.has(id), `${id} not in registry`).toBe(true);
    }
  });
});


// --- Auto-dispatch shapes -------------------------------------------------


describe('auto-dispatch', () => {
  it('emits gte for `_min` numeric filters', () => {
    const r = new _Recorder();
    applyRegistryFilters(r, {
      ...DEFAULT_FILTERS,
      buildingConditionLevelMin: 4,
      apartmentConditionLevelMin: 3,
    });
    const cols = r.calls
      .filter((c) => c.op === 'gte')
      .map((c) => `${c.col}=${String(c.value)}`);
    expect(cols).toContain('building_condition_level=4');
    expect(cols).toContain('apartment_condition_level=3');
  });

  it('emits lte for `_max` numeric filters', () => {
    const r = new _Recorder();
    applyRegistryFilters(r, {
      ...DEFAULT_FILTERS,
      priceMax: 50_000,
      areaMax: 100,
    });
    const cols = r.calls
      .filter((c) => c.op === 'lte')
      .map((c) => `${c.col}=${String(c.value)}`);
    expect(cols).toContain('price_czk=50000');
    expect(cols).toContain('area_m2=100');
  });

  it('emits in for string_list filters with values', () => {
    const r = new _Recorder();
    applyRegistryFilters(r, {
      ...DEFAULT_FILTERS,
      conditionMatch: ['po_rekonstrukci', 'velmi_dobry'],
      dispositions: ['2+kk', '3+kk'],
    });
    const ins = r.calls.filter((c) => c.op === 'in');
    expect(ins).toContainEqual({ op: 'in', col: 'condition', value: ['po_rekonstrukci', 'velmi_dobry'] });
    expect(ins).toContainEqual({ op: 'in', col: 'disposition', value: ['2+kk', '3+kk'] });
  });

  it('skips empty string_list filters (no clause = no narrowing)', () => {
    const r = new _Recorder();
    applyRegistryFilters(r, {
      ...DEFAULT_FILTERS,
      conditionMatch: [],
      dispositions: [],
    });
    expect(r.calls.find((c) => c.col === 'condition')).toBeUndefined();
    expect(r.calls.find((c) => c.col === 'disposition')).toBeUndefined();
  });

  it('emits eq with boolean for tristate filters set to yes/no', () => {
    const r = new _Recorder();
    applyRegistryFilters(r, {
      ...DEFAULT_FILTERS,
      hasBalcony: 'yes',
      garage: 'no',
    });
    const eqs = r.calls.filter((c) => c.op === 'eq');
    expect(eqs).toContainEqual({ op: 'eq', col: 'has_balcony', value: true });
    expect(eqs).toContainEqual({ op: 'eq', col: 'garage', value: false });
  });

  it('skips tristate filters at the default `any`', () => {
    const r = new _Recorder();
    applyRegistryFilters(r, { ...DEFAULT_FILTERS });
    const tristateCols = ['has_balcony', 'has_lift', 'has_parking', 'terrace', 'cellar', 'garage'];
    for (const col of tristateCols) {
      expect(r.calls.find((c) => c.col === col)).toBeUndefined();
    }
  });

  it('emits eq with the value for single-value enum filters', () => {
    const r = new _Recorder();
    applyRegistryFilters(r, {
      ...DEFAULT_FILTERS,
      furnished: 'ano',
      ownership: 'osobni',
      categoryMain: 'dum',
    });
    const eqs = r.calls.filter((c) => c.op === 'eq');
    expect(eqs).toContainEqual({ op: 'eq', col: 'furnished', value: 'ano' });
    expect(eqs).toContainEqual({ op: 'eq', col: 'ownership', value: 'osobni' });
    expect(eqs).toContainEqual({ op: 'eq', col: 'category_main', value: 'dum' });
  });

  it('skips null-valued filters (no spurious clauses)', () => {
    const r = new _Recorder();
    applyRegistryFilters(r, {
      ...DEFAULT_FILTERS,
      priceMin: null,
      areaMax: null,
      buildingConditionLevelMin: null,
    });
    expect(r.calls.find((c) => c.col === 'price_czk')).toBeUndefined();
    expect(r.calls.find((c) => c.col === 'area_m2')).toBeUndefined();
    expect(r.calls.find((c) => c.col === 'building_condition_level')).toBeUndefined();
  });
});


// --- Hand-coded filters are NOT dispatched by the auto-builder -----------


describe('hand-coded skip set', () => {
  it('does not auto-dispatch building_material (custom 1-to-many enum)', () => {
    const r = new _Recorder();
    applyRegistryFilters(r, {
      ...DEFAULT_FILTERS,
      buildingMaterial: ['cihla'],
    });
    // building_material → IN over building_type values is handled in
    // queries.ts:applyFilters, NOT here.
    expect(r.calls.find((c) => c.col === 'building_type')).toBeUndefined();
  });

  it('does not auto-dispatch last_seen / first_seen days-ago filters', () => {
    const r = new _Recorder();
    applyRegistryFilters(r, {
      ...DEFAULT_FILTERS,
      lastSeenMinDays: 3,
      lastSeenMaxDays: 14,
      firstSeenMinDays: 1,
      firstSeenMaxDays: 30,
    });
    // These need days-ago → ISO timestamp translation; stays hand-coded.
    expect(r.calls.find((c) => c.col === 'last_seen_at')).toBeUndefined();
    expect(r.calls.find((c) => c.col === 'first_seen_at')).toBeUndefined();
  });
});
