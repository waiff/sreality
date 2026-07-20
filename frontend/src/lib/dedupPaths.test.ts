import { describe, expect, it } from 'vitest';

import type { EligibilityBucket } from './api';
import {
  PATH_BY_KEY,
  applicablePaths,
  cellFilter,
  drillIsExact,
  inputsInScope,
  pivotPath,
  pivotSummary,
} from './dedupPaths';

/* Builds a bucket the way the SERVER builds one: the three `elig_*` verdicts are
 * computed from the inputs with the engine's own logic (including its three-valued
 * behaviour on a NULL category_main), so a fixture can never be internally
 * inconsistent in a way production data could not be. */
function bucket(p: Partial<EligibilityBucket> & { n: number }): EligibilityBucket {
  const b: EligibilityBucket = {
    source: 'sreality',
    category_main: 'byt',
    is_active: true,
    has_street: true,
    has_disposition: true,
    has_geom: true,
    has_obec: true,
    has_area: true,
    elig_street: null,
    elig_geo: null,
    elig_byt_geo: null,
    ...p,
  };
  const cat = b.category_main;
  const geoFam = cat !== null && ['dum', 'pozemek', 'komercni', 'ostatni'].includes(cat);
  b.elig_street = b.has_street && b.has_disposition;
  // `category_main IN (...)` / `= 'byt'` are NULL, not false, for a NULL category.
  b.elig_geo =
    cat === null ? null : b.is_active && geoFam && b.has_geom && b.has_obec && b.has_area;
  b.elig_byt_geo =
    cat === null
      ? null
      : b.is_active && cat === 'byt' && b.has_geom && b.has_obec && b.has_area && b.has_disposition;
  return b;
}

const street = PATH_BY_KEY.street;
const geo = PATH_BY_KEY.geo;

describe('pivotPath', () => {
  it('splits a pass into eligible / ineligible with an exclusive reason breakdown', () => {
    const view = pivotPath(
      [
        bucket({ n: 100 }), // complete -> eligible
        bucket({ n: 30, has_street: false }), // street only
        bucket({ n: 20, has_disposition: false }), // disposition only
        bucket({ n: 5, has_street: false, has_disposition: false }), // both -> multi
      ],
      street,
      'active',
    );
    const cell = view.cells.byt.sreality;
    expect(cell.scope).toBe(155);
    expect(cell.eligible).toBe(100);
    expect(cell.ineligible).toBe(55);
    expect(cell.reasons.street).toBe(30);
    expect(cell.reasons.disposition).toBe(20);
    expect(cell.multi).toBe(5);
    expect(cell.unexplained).toBe(0);
  });

  it('keeps the reason buckets summing to the ineligible total', () => {
    const view = pivotPath(
      [
        bucket({ n: 7, has_geom: false, category_main: 'dum' }),
        bucket({ n: 11, has_obec: false, category_main: 'dum' }),
        bucket({ n: 3, has_area: false, category_main: 'dum' }),
        bucket({ n: 2, has_geom: false, has_area: false, category_main: 'dum' }),
        bucket({ n: 40, category_main: 'dum' }),
      ],
      geo,
      'active',
    );
    const cell = view.cells.dum.sreality;
    const summed =
      Object.values(cell.reasons).reduce((a, b) => a + b, 0) + cell.multi + cell.unexplained;
    expect(summed).toBe(cell.ineligible);
    expect(cell.eligible + cell.ineligible).toBe(cell.scope);
    expect(cell.unexplained).toBe(0);
  });

  it('restricts a pass to its domain', () => {
    // A byt is not in the geo pass's domain and must not appear in its matrix at all.
    const view = pivotPath(
      [bucket({ n: 9, category_main: 'byt' }), bucket({ n: 4, category_main: 'pozemek' })],
      geo,
      'active',
    );
    expect(view.categories).toEqual(['pozemek']);
    expect(view.cells.byt).toBeUndefined();
    expect(view.cells[''][''].scope).toBe(4);
  });

  it('never treats a NULL-category row as ineligible for a category-gated pass', () => {
    // Regression: `!b.elig_geo` would count a NULL verdict as a failure of a pass whose
    // domain the row is not even in. The row belongs to the street pass only.
    const nullCat = bucket({ n: 6, category_main: null, has_street: false });
    const geoView = pivotPath([nullCat], geo, 'active');
    expect(geoView.categories).toEqual([]);

    const streetView = pivotPath([nullCat], street, 'active');
    expect(streetView.cells['(bez typu)'].sreality.ineligible).toBe(6);
    expect(streetView.cells['(bez typu)'].sreality.reasons.street).toBe(6);
  });

  it('adds the active gate as an input only when the scope includes inactive rows', () => {
    expect(inputsInScope(geo, 'active').map((i) => i.key)).toEqual(['geom', 'obec', 'area']);
    expect(inputsInScope(geo, 'all').map((i) => i.key)).toEqual([
      'geom',
      'obec',
      'area',
      'active',
    ]);
    // The street pass has no active gate at all — an inactive row still merges there.
    expect(inputsInScope(street, 'all').map((i) => i.key)).toEqual(['street', 'disposition']);
  });

  it('attributes an inactive-only failure to the active input under scope=all', () => {
    const view = pivotPath(
      [bucket({ n: 8, category_main: 'dum', is_active: false })],
      geo,
      'all',
    );
    const cell = view.cells.dum.sreality;
    expect(cell.ineligible).toBe(8);
    expect(cell.reasons.active).toBe(8);
    expect(cell.multi).toBe(0);
  });

  it('drops inactive rows entirely under scope=active', () => {
    const view = pivotPath(
      [bucket({ n: 8, category_main: 'dum', is_active: false }), bucket({ n: 2, category_main: 'dum' })],
      geo,
      'active',
    );
    expect(view.cells.dum.sreality.scope).toBe(2);
  });

  it('totals rows and columns from the same buckets as the cells', () => {
    const view = pivotPath(
      [
        bucket({ n: 10, source: 'sreality', category_main: 'dum', has_obec: false }),
        bucket({ n: 5, source: 'idnes', category_main: 'dum' }),
        bucket({ n: 7, source: 'idnes', category_main: 'pozemek', has_area: false }),
      ],
      geo,
      'active',
    );
    expect(view.cells.dum[''].scope).toBe(15); // row total
    expect(view.cells[''].idnes.scope).toBe(12); // column total
    expect(view.cells[''][''].scope).toBe(22); // grand total
    expect(view.cells[''][''].ineligible).toBe(17);
    expect(view.cells[''][''].reasons.obec).toBe(10);
    expect(view.cells[''][''].reasons.area).toBe(7);
  });
});

describe('pivotSummary', () => {
  it('counts only listings no pass can reach', () => {
    const view = pivotSummary(
      [
        bucket({ n: 12, has_street: false }), // street-less byt: byt-geo still reaches it
        bucket({ n: 4, has_street: false, has_obec: false }), // no pass reaches it
      ],
      'active',
    );
    const cell = view.cells.byt.sreality;
    expect(cell.scope).toBe(16);
    expect(cell.eligible).toBe(12);
    expect(cell.ineligible).toBe(4);
  });

  it('attributes an unreachable pozemek to the geo gap, not to its missing disposition', () => {
    // A pozemek will never carry a disposition, so "missing disposition" is noise —
    // the honest answer is the one field that would actually unlock it.
    const view = pivotSummary(
      [bucket({ n: 9, category_main: 'pozemek', has_disposition: false, has_obec: false })],
      'active',
    );
    const cell = view.cells.pozemek.sreality;
    expect(cell.ineligible).toBe(9);
    expect(cell.reasons.obec).toBe(9);
    expect(cell.reasons.disposition).toBeUndefined();
  });

  it('reports only the inputs that explain something', () => {
    const view = pivotSummary(
      [bucket({ n: 3, category_main: 'dum', has_disposition: false, has_area: false })],
      'active',
    );
    expect(view.inputs.map((i) => i.key)).toEqual(['area']);
  });

  it('prefers the family pass on ties', () => {
    expect(applicablePaths('byt')).toEqual(['street', 'byt_geo']);
    expect(applicablePaths('dum')).toEqual(['geo', 'street']);
    expect(applicablePaths(null)).toEqual(['street']);
  });
});

describe('cellFilter', () => {
  it('reproduces a reason bucket exactly: that input absent, the others present', () => {
    const f = cellFilter(geo, 'active', 'dum', 'idnes', { reason: 'obec' });
    expect(f).toMatchObject({
      source: 'idnes',
      category_main: 'dum',
      active: 'active',
      path: 'geo',
      path_state: 'ineligible',
    });
    expect(f.missing).toEqual(['obec_id']);
    expect(f.has.sort()).toEqual(['area', 'geom']);
  });

  it('maps the active input onto the state tab, not a presence chip', () => {
    const f = cellFilter(geo, 'all', 'dum', 'remax', { reason: 'active' });
    expect(f.active).toBe('inactive');
    expect(f.missing).toEqual([]);
    expect(f.has.sort()).toEqual(['area', 'geom', 'obec_id']);
  });

  it('leaves the type filter open for the NULL-category stand-in', () => {
    const f = cellFilter(street, 'active', '(bez typu)', 'idnes', 'ineligible');
    expect(f.category_main).toBeUndefined();
    expect(f.source).toBe('idnes');
  });

  it('does not constrain state for a scope click', () => {
    const f = cellFilter(geo, 'active', 'dum', '', 'scope');
    expect(f.path).toBe('geo');
    expect(f.path_state).toBeUndefined();
    expect(f.source).toBeUndefined(); // '' = the totals column, i.e. every portal
  });

  it('uses whole-engine reachability on the summary tab', () => {
    expect(cellFilter(null, 'active', 'byt', 'bazos', 'ineligible').dedup).toBe('unreachable');
    expect(cellFilter(null, 'active', 'byt', 'bazos', 'eligible').dedup).toBe('reachable');
    expect(cellFilter(null, 'active', 'byt', 'bazos', 'scope').dedup).toBeUndefined();
    // No pass filter: the summary is not about one pass.
    expect(cellFilter(null, 'active', 'byt', 'bazos', 'ineligible').path).toBeUndefined();
  });

  it('marks the summary reason drill-down as a superset, not an identity', () => {
    expect(drillIsExact(geo, { reason: 'obec' })).toBe(true);
    expect(drillIsExact(null, 'ineligible')).toBe(true);
    expect(drillIsExact(null, { reason: 'obec' })).toBe(false);
    expect(drillIsExact(null, 'multi')).toBe(false);
  });
});
