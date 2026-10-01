/* Tests for the registry-driven PostgREST filter dispatcher.
 *
 * The drift guard is the most important test here: without it, a new
 * registry filter could land that fits no dispatch path AND is not
 * in HAND_CODED_BROWSE_FILTERS, and the Browse cohort would silently
 * fail to narrow when the operator sets it (exactly the
 * apartment/building_condition_level_min bug that motivated this
 * module — see PR history around #140 / #146). */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import { FILTER_REGISTRY, type Agenda, type FilterDef } from './filterRegistry.generated';
import { DEFAULT_FILTERS, REGISTRY_KEY_MAP } from './filters';
import {
  BROWSE_FILTERS_NOT_ON_THE_LIST_QUERY,
  HAND_CODED_BROWSE_FILTERS,
  applyAgendaFilters,
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

  it('a browse filter with no pg_column must be declared hand-coded', () => {
    /* THE HOLE THIS CLOSES (W21). `applyRegistryFilters` skips any filter whose
     * `pg_column` is null — that is how a filter becomes a PostgREST predicate —
     * and the drift guard above skipped the same filters, so "no pg_column" was
     * indistinguishable from "handled elsewhere". Setting `pg_column: null` on
     * min/max_estate_area turned the Lot-area inputs into a SILENT NO-OP with CI
     * green: the UI still offered them, the query no longer applied them.
     *
     * A null pg_column is now a CLAIM that queries.ts:applyFilters handles the
     * filter by hand, and the claim has to be registered. */
    const undeclared: string[] = [];
    for (const filter of FILTER_REGISTRY.filters) {
      if (!filter.agendas.includes('browse')) continue;
      if (filter.pg_column != null) continue;
      if (HAND_CODED_BROWSE_FILTERS.has(filter.id)) continue;
      if (BROWSE_FILTERS_NOT_ON_THE_LIST_QUERY.has(filter.id)) continue;
      undeclared.push(filter.id);
    }
    if (undeclared.length) {
      throw new Error(
        `Browse filters with pg_column: null that nothing applies:\n  ` +
        undeclared.join('\n  ') +
        `\nEither give the filter a column the read model PUBLISHES (see ` +
        `migration 535 — a derived measure can be a projection column), or ` +
        `hand-code it in queries.ts:applyFilters and list it in ` +
        `HAND_CODED_BROWSE_FILTERS, or — if it genuinely reaches no list ` +
        `predicate — record it in BROWSE_FILTERS_NOT_ON_THE_LIST_QUERY with ` +
        `the reason. Do NOT use HAND_CODED_BROWSE_FILTERS for the last case: ` +
        `that set is a claim that something applies the filter.`,
      );
    }
  });

  it('the plot-area filters read the published measure, not the raw column', () => {
    /* Rule 16 + rule 23: `area_m2` IS the parcel for pozemek, so `estate_area`
     * alone drops ~32k active land rows. Browse must filter the same definition
     * the watchdog matcher and comparables call (toolkit.measures.plot_area_sql). */
    for (const id of ['min_estate_area', 'max_estate_area']) {
      const f = FILTER_REGISTRY.filters.find((x) => x.id === id);
      expect(f, `${id} missing from the registry`).toBeTruthy();
      expect(f!.pg_column).toBe('plot_area_m2');
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
      areaMax: 100,
      apartmentConditionLevelMax: 4,
    });
    const cols = r.calls
      .filter((c) => c.op === 'lte')
      .map((c) => `${c.col}=${String(c.value)}`);
    expect(cols).toContain('area_m2=100');
    expect(cols).toContain('apartment_condition_level=4');
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

  it('emits in() for the multi-select category filter', () => {
    const r = new _Recorder();
    applyRegistryFilters(r, {
      ...DEFAULT_FILTERS,
      categoryMain: ['byt', 'dum'],
    });
    const ins = r.calls.filter((c) => c.op === 'in');
    expect(ins).toContainEqual({ op: 'in', col: 'category_main', value: ['byt', 'dum'] });
  });

  /* A one-element list MUST emit eq, never in. `in` is SQL `= ANY($1)`, whose
   * ScalarArrayOp disqualifies the browse index from satisfying the ORDER BY:
   * the planner then scans the whole cohort and top-N sorts it (measured on
   * the default view: 105,011 rows / ~15,900 buffers, and past the 8 s
   * statement_timeout when cold -> HTTP 500 on the flagship surface) instead
   * of an early-stopping ordered index scan (24 rows / 5 buffers / 0.2 ms).
   * These three tests are that defect's permanent rail. */
  it('emits eq (not in) for a single-value string_list', () => {
    const r = new _Recorder();
    applyRegistryFilters(r, {
      ...DEFAULT_FILTERS,
      categoryMain: ['byt'],
      dispositions: ['3+kk'],
    });
    expect(r.calls).toContainEqual({ op: 'eq', col: 'category_main', value: 'byt' });
    expect(r.calls).toContainEqual({ op: 'eq', col: 'disposition', value: '3+kk' });
    expect(r.calls.filter((c) => c.op === 'in')).toEqual([]);
  });

  it('emits eq for EVERY single-value string_list the registry can dispatch', () => {
    const singles = FILTER_REGISTRY.filters.filter(
      (f) =>
        f.type === 'string_list' &&
        f.pg_column != null &&
        f.agendas.includes('browse') &&
        !HAND_CODED_BROWSE_FILTERS.has(f.id) &&
        REGISTRY_KEY_MAP[f.id as keyof typeof REGISTRY_KEY_MAP] !== undefined,
    );
    expect(singles.length).toBeGreaterThan(0);
    for (const f of singles) {
      const key = REGISTRY_KEY_MAP[f.id as keyof typeof REGISTRY_KEY_MAP];
      const r = new _Recorder();
      applyRegistryFilters(r, { ...DEFAULT_FILTERS, [key]: ['solo'] });
      const emitted = r.calls.find((c) => c.col === f.pg_column);
      expect(emitted, `${f.id} -> ${f.pg_column}`).toEqual({
        op: 'eq', col: f.pg_column, value: 'solo',
      });
    }
  });

  it('the DEFAULT browse cohort emits no ScalarArrayOp at all', () => {
    const r = new _Recorder();
    applyRegistryFilters(r, { ...DEFAULT_FILTERS });
    expect(r.calls.filter((c) => c.op === 'in')).toEqual([]);
  });

  it('skips null-valued filters (no spurious clauses)', () => {
    const r = new _Recorder();
    applyRegistryFilters(r, {
      ...DEFAULT_FILTERS,
      areaMin: null,
      areaMax: null,
      buildingConditionLevelMin: null,
    });
    expect(r.calls.find((c) => c.col === 'area_m2')).toBeUndefined();
    expect(r.calls.find((c) => c.col === 'building_condition_level')).toBeUndefined();
  });
});


// --- Hand-coded filters are NOT dispatched by the auto-builder -----------


describe('hand-coded skip set', () => {
  it('does not auto-dispatch price bounds (NULL-tolerant branch is hand-coded)', () => {
    const r = new _Recorder();
    applyRegistryFilters(r, {
      ...DEFAULT_FILTERS,
      priceMin: 1_000_000,
      priceMax: 5_000_000,
      includeNoPrice: true,
    });
    // price_czk is owned by queries.ts:applyFilters so it can emit the
    // `.or(...,price_czk.is.null)` form when includeNoPrice is set, NOT here.
    expect(r.calls.find((c) => c.col === 'price_czk')).toBeUndefined();
  });

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

  it('does not auto-dispatch furnished / ownership (unknown-sentinel enums)', () => {
    const r = new _Recorder();
    applyRegistryFilters(r, {
      ...DEFAULT_FILTERS,
      furnished: ['ano', '__unknown__'],
      ownership: ['osobni'],
    });
    // furnished / ownership → `.or(in.(…),is.null,not.in.(…))` is handled in
    // queries.ts:applyFilters, NOT here.
    expect(r.calls.find((c) => c.col === 'furnished')).toBeUndefined();
    expect(r.calls.find((c) => c.col === 'ownership')).toBeUndefined();
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


// --- The SOLD agenda ------------------------------------------------------


describe('the sold agenda', () => {
  /* The sold surface has no queries.ts twin applying a filter by hand, so
     blessing one with the browse escape set would mean it is never applied at
     all. Every sold filter must reach PostgREST by shape. */
  it('every sold filter is column-backed and auto-dispatchable', () => {
    const sold = FILTER_REGISTRY.filters.filter((f) => f.agendas.includes('sold'));
    expect(sold.length).toBeGreaterThan(0);
    for (const f of sold) {
      expect(f.pg_column, `${f.id} has no pg_column`).not.toBeNull();
      expect(isAutoDispatchable(f), `${f.id} fits no dispatch path`).toBe(true);
      expect(HAND_CODED_BROWSE_FILTERS.has(f.id), `${f.id} is hand-coded`).toBe(false);
    }
  });

  it('dispatches the sold filters by registry id, with no key map in between', () => {
    const r = new _Recorder();
    applyAgendaFilters(r, 'sold', (id) =>
      ({
        category_main_in: ['byt'],
        dispositions: ['2+kk', '3+kk'],
        min_area_m2: 40,
        max_area_m2: 80,
        max_sold_age_days: 730,
      } as Record<string, unknown>)[id] ?? null,
    );
    expect(r.calls).toContainEqual({ op: 'eq', col: 'category_main', value: 'byt' });
    expect(r.calls).toContainEqual({
      op: 'in', col: 'disposition', value: ['2+kk', '3+kk'],
    });
    expect(r.calls).toContainEqual({ op: 'gte', col: 'area_m2', value: 40 });
    expect(r.calls).toContainEqual({ op: 'lte', col: 'area_m2', value: 80 });
    expect(r.calls).toContainEqual({ op: 'lte', col: 'sold_age_days', value: 730 });
  });

  it('applies nothing for a browse-only filter, whatever the state holds', () => {
    const r = new _Recorder();
    applyAgendaFilters(r, 'sold', () => 5_000_000);
    // price_czk is not on the sold relation; a value for it must not reach
    // PostgREST as a predicate on a column the function never returns.
    expect(r.calls.find((c) => c.col === 'price_czk')).toBeUndefined();
  });
});


// --- sql_kind agreement (C4, rule 16) --------------------------------------
//
// `sql_kind` is derived ONCE, in toolkit/filter_registry.sql_kind, and codegen hands
// it to this file. The Python compiler renders each kind from
// tests/fixtures/filter_sql_kinds.json's `sql` (pytest pins that); this pins the
// TS dispatch to the same table's PostgREST ops, so the chain Python template ->
// generated kind -> TS dispatch is closed. A one-element list must be `.eq()`,
// never `.in()` (`postgrest_single`; the planner rule in registryQueryBuilder.ts).

interface KindRow {
  sql: string | null;
  postgrest: Call['op'] | null;
  postgrest_single: Call['op'] | null;
}

const KINDS = JSON.parse(
  readFileSync(join(process.cwd(), '..', 'tests', 'fixtures', 'filter_sql_kinds.json'), 'utf-8'),
) as { kinds: Record<string, KindRow>; browse_hand_coded_kinds: Array<string | null> };

const samplesFor = (f: FilterDef): Array<{ input: unknown; value: unknown; single: boolean }> => {
  if (f.ui_control === 'tristate') return [{ input: 'yes', value: true, single: true }];
  if (f.type === 'string_list') {
    const [a, b] = (f.enum_values ?? []).map((o) => o.value);
    return [
      { input: [a, b], value: [a, b], single: false },
      { input: [a], value: a, single: true },
    ];
  }
  if (f.type === 'string') {
    const first = f.enum_values?.[0]?.value;
    return [{ input: first, value: first, single: true }];
  }
  return [{ input: 5, value: 5, single: true }];
};

describe('sql_kind agreement', () => {
  it('the hand-coded browse set is exactly the kinds Browse does not auto-dispatch', () => {
    for (const f of FILTER_REGISTRY.filters) {
      if (!f.agendas.includes('browse') || f.pg_column == null) continue;
      expect(
        HAND_CODED_BROWSE_FILTERS.has(f.id),
        `${f.id} (sql_kind=${f.sql_kind})`,
      ).toBe(KINDS.browse_hand_coded_kinds.includes(f.sql_kind));
    }
  });

  for (const agenda of ['browse', 'sold'] as Agenda[]) {
    it(`every auto-dispatched ${agenda} filter emits its kind's PostgREST op`, () => {
      let checked = 0;
      for (const f of FILTER_REGISTRY.filters) {
        if (!f.agendas.includes(agenda) || f.pg_column == null) continue;
        if (agenda === 'browse' && HAND_CODED_BROWSE_FILTERS.has(f.id)) continue;
        expect(f.sql_kind, f.id).not.toBeNull();
        const row = KINDS.kinds[f.sql_kind as string];
        for (const sample of samplesFor(f)) {
          const r = new _Recorder();
          applyAgendaFilters(r, agenda, (id) => (id === f.id ? sample.input : undefined));
          expect(r.calls, `${agenda}:${f.id}`).toEqual([
            {
              op: sample.single ? row.postgrest_single : row.postgrest,
              col: f.pg_column,
              value: sample.value,
            },
          ]);
          checked += 1;
        }
      }
      expect(checked).toBeGreaterThan(0);
    });
  }
});
