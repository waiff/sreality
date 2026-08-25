/* growthToFeatureCollection — W10b split geometry (shapesByObec) from the
 * window-scoped numbers (rows). The two are joined here by obec_id; a row
 * with no matching shape is skipped rather than crashing the map. */
import { describe, expect, it } from 'vitest';
import { buildHoverData, growthToFeatureCollection } from './growthChoropleth';
import { PPM2_VALUE_LABEL } from './measure';
import type { PriceStatGrowthRow } from './priceStats';

function row(obec_id: number, overrides: Partial<PriceStatGrowthRow> = {}): PriceStatGrowthRow {
  return {
    obec_id,
    locality_name: `Obec ${obec_id}`,
    sale_latest_price: 100_000,
    sale_cagr_pct: 2.5,
    sale_min_active: 5,
    rent_latest_price: 15_000,
    rent_cagr_pct: 1.2,
    rent_min_active: 5,
    gross_yield_pct: 5,
    yield_change_pp_pa: 0.1,
    ...overrides,
  };
}

const SQUARE = JSON.stringify({
  type: 'Polygon',
  coordinates: [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]],
});

describe('growthToFeatureCollection', () => {
  it('joins each row to its shape by obec_id', () => {
    const shapes = new Map([[1, SQUARE]]);
    const fc = growthToFeatureCollection([row(1)], shapes);
    expect(fc.features).toHaveLength(1);
    expect(fc.features[0].id).toBe(1);
    expect(fc.features[0].geometry.type).toBe('Polygon');
    expect(fc.features[0].properties.obec_name).toBe('Obec 1');
  });

  it('skips a row with no matching shape instead of crashing', () => {
    const shapes = new Map([[1, SQUARE]]);
    const fc = growthToFeatureCollection([row(1), row(2)], shapes);
    expect(fc.features.map((f) => f.id)).toEqual([1]);
  });

  it('skips a shape whose geojson fails to parse', () => {
    const shapes = new Map([[1, 'not json']]);
    const fc = growthToFeatureCollection([row(1)], shapes);
    expect(fc.features).toHaveLength(0);
  });

  it('skips a non-polygon geometry', () => {
    const point = JSON.stringify({ type: 'Point', coordinates: [0, 0] });
    const shapes = new Map([[1, point]]);
    const fc = growthToFeatureCollection([row(1)], shapes);
    expect(fc.features).toHaveLength(0);
  });
});

/* These two labels were the repo's ONLY correct per-m² basis labels and had
 * zero test coverage. They are now the shared vocabulary every surface renders,
 * so a change here is a change to the Browse pins, the table, the box-plot axis
 * and the choropleth legend at once. */
describe('buildHoverData value labels', () => {
  const series = (metric: 'rent_cagr_pct' | 'sale_cagr_pct') =>
    buildHoverData(
      [{
        obec_id: 1, year: 2026, month: 1,
        sale_price: 100_000, rent_price: 300,
      } as Parameters<typeof buildHoverData>[0][number]],
      metric,
    );

  it('labels the rent series with its MONTHLY period', () => {
    expect(series('rent_cagr_pct').valueLabel).toBe('Nájem Kč/m²/měs');
    expect(series('rent_cagr_pct').valueLabel).toBe(PPM2_VALUE_LABEL.rent);
  });

  it('labels the sale series as a capital price, with no period', () => {
    expect(series('sale_cagr_pct').valueLabel).toBe('Cena Kč/m²');
    expect(series('sale_cagr_pct').valueLabel).toBe(PPM2_VALUE_LABEL.sale);
  });

  it('never gives the two series the same label', () => {
    expect(series('rent_cagr_pct').valueLabel).not.toBe(series('sale_cagr_pct').valueLabel);
  });
});
