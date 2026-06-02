/* Shared obec growth-choropleth helpers, used by the Datasets-page map
 * (DatasetMap) and the Browse-tab overlay (ListingMap). Diverging brick→sage
 * ramp = the app's negative/positive semantics; thin/no-data greyed. Each map
 * owns its source + layer ids and writes the (inline, contextually-typed) paint
 * expression itself; this module owns the ramps, the metric config, the
 * feature-collection builder, and the sparsity rule. */
import type { PriceStatGrowthRow } from './priceStats';

export type GrowthMetric = 'rent_cagr_pct' | 'sale_cagr_pct' | 'yield_change_pp_pa';

type Ramp = ReadonlyArray<readonly [number, string]>;

const GROWTH_RAMP: Ramp = [
  [-8, '#a04b3d'], [-3, '#c89a8e'], [0, '#dcd6c8'], [3, '#9aae84'], [8, '#5e7a4a'],
];
const YIELD_RAMP: Ramp = [
  [-0.6, '#a04b3d'], [-0.2, '#c89a8e'], [0, '#dcd6c8'], [0.2, '#9aae84'], [0.6, '#5e7a4a'],
];
export const GROWTH_NO_DATA = 'rgba(122, 125, 134, 0.28)';
const MIN_ACTIVE = 3;

export interface GrowthMetricConfig {
  label: string;
  ramp: Ramp;
  suffix: string;
  digits: number;
  vProp: string;   // feature property holding the value
  hasProp: string; // feature property: 1 if value present, 0 if thin/missing
}

export const GROWTH_METRICS: Record<GrowthMetric, GrowthMetricConfig> = {
  rent_cagr_pct: { label: 'Rent growth p.a.', ramp: GROWTH_RAMP, suffix: '%', digits: 1, vProp: 'v_rent', hasProp: 'h_rent' },
  sale_cagr_pct: { label: 'Sale-price growth p.a.', ramp: GROWTH_RAMP, suffix: '%', digits: 1, vProp: 'v_sale', hasProp: 'h_sale' },
  yield_change_pp_pa: { label: 'Yield change p.a.', ramp: YIELD_RAMP, suffix: 'pp', digits: 2, vProp: 'v_yield', hasProp: 'h_yield' },
};

export const GROWTH_METRIC_ORDER: GrowthMetric[] = ['rent_cagr_pct', 'sale_cagr_pct', 'yield_change_pp_pa'];

export function growthValue(p: PriceStatGrowthRow, metric: GrowthMetric): number | null {
  const raw = p[metric];
  if (raw == null || !Number.isFinite(raw)) return null;
  const minActive =
    metric === 'rent_cagr_pct'
      ? p.rent_min_active
      : metric === 'sale_cagr_pct'
        ? p.sale_min_active
        : Math.min(p.sale_min_active ?? 0, p.rent_min_active ?? 0);
  if (minActive != null && minActive < MIN_ACTIVE) return null;
  return raw;
}

export interface GrowthFeatureProps {
  obec_name: string;
  v_rent: number; h_rent: number;
  v_sale: number; h_sale: number;
  v_yield: number; h_yield: number;
}
export type GrowthFC = GeoJSON.FeatureCollection<GeoJSON.Polygon | GeoJSON.MultiPolygon, GrowthFeatureProps>;

export function growthToFeatureCollection(rows: PriceStatGrowthRow[]): GrowthFC {
  return {
    type: 'FeatureCollection',
    features: rows.flatMap((p) => {
      let geometry: GeoJSON.Geometry;
      try {
        geometry = JSON.parse(p.geojson) as GeoJSON.Geometry;
      } catch {
        return [];
      }
      if (geometry.type !== 'Polygon' && geometry.type !== 'MultiPolygon') return [];
      const r = growthValue(p, 'rent_cagr_pct');
      const s = growthValue(p, 'sale_cagr_pct');
      const y = growthValue(p, 'yield_change_pp_pa');
      return [{
        type: 'Feature' as const,
        id: p.obec_id,
        geometry,
        properties: {
          obec_name: p.locality_name,
          v_rent: r ?? 0, h_rent: r == null ? 0 : 1,
          v_sale: s ?? 0, h_sale: s == null ? 0 : 1,
          v_yield: y ?? 0, h_yield: y == null ? 0 : 1,
        },
      }];
    }),
  };
}
