/* Shared obec growth-choropleth helpers, used by the Datasets-page map
 * (DatasetMap) and the Browse-tab overlay (ListingMap). Diverging brick→sage
 * ramp = the app's negative/positive semantics; thin/no-data greyed. Each map
 * owns its source + layer ids and writes the (inline, contextually-typed) paint
 * expression itself; this module owns the ramps, the metric config, the
 * feature-collection builder, and the sparsity rule. */
import type { PriceStatGrowthRow, PriceStatSeriesRow } from './priceStats';

export type GrowthMetric = 'rent_cagr_pct' | 'sale_cagr_pct' | 'yield_change_pp_pa';

type Ramp = ReadonlyArray<readonly [number, string]>;

const GROWTH_RAMP: Ramp = [
  [-8, '#a04b3d'], [-3, '#c89a8e'], [0, '#dcd6c8'], [3, '#9aae84'], [8, '#5e7a4a'],
];
const YIELD_RAMP: Ramp = [
  [-0.6, '#a04b3d'], [-0.2, '#c89a8e'], [0, '#dcd6c8'], [0.2, '#9aae84'], [0.6, '#5e7a4a'],
];
export const GROWTH_NO_DATA = 'rgba(122, 125, 134, 0.28)';
/* Endpoint active-listing thresholds. Post-migration 161 the *_min_active
 * columns measure the START+END points the CAGR is actually built from (not
 * the whole-series minimum), so a single thin month no longer hides a solid
 * multi-year trend. Tiers: >= CONFIDENT_MIN → full colour; CONFIDENT_MIN > n
 * >= THIN_MIN → faded tint ("data exists but limited"); < THIN_MIN → no data. */
export const CONFIDENT_MIN = 3;
const THIN_MIN = 1;

export interface GrowthMetricConfig {
  label: string;
  ramp: Ramp;
  suffix: string;
  digits: number;
  vProp: string;    // feature property holding the value
  hasProp: string;  // 1 if value present (confident OR thin), else 0
  thinProp: string; // 1 if present-but-thin (limited listings), else 0
}

export const GROWTH_METRICS: Record<GrowthMetric, GrowthMetricConfig> = {
  rent_cagr_pct: { label: 'Rent growth p.a.', ramp: GROWTH_RAMP, suffix: '%', digits: 1, vProp: 'v_rent', hasProp: 'h_rent', thinProp: 'tn_rent' },
  sale_cagr_pct: { label: 'Sale-price growth p.a.', ramp: GROWTH_RAMP, suffix: '%', digits: 1, vProp: 'v_sale', hasProp: 'h_sale', thinProp: 'tn_sale' },
  yield_change_pp_pa: { label: 'Yield change p.a.', ramp: YIELD_RAMP, suffix: 'pp', digits: 2, vProp: 'v_yield', hasProp: 'h_yield', thinProp: 'tn_yield' },
};

export const GROWTH_METRIC_ORDER: GrowthMetric[] = ['rent_cagr_pct', 'sale_cagr_pct', 'yield_change_pp_pa'];

export type GrowthTier = 0 | 1 | 2; // 0 none (grey), 1 thin (faded), 2 confident (full)

function endpointActive(p: PriceStatGrowthRow, metric: GrowthMetric): number {
  if (metric === 'rent_cagr_pct') return p.rent_min_active ?? 0;
  if (metric === 'sale_cagr_pct') return p.sale_min_active ?? 0;
  return Math.min(p.sale_min_active ?? 0, p.rent_min_active ?? 0);
}

export function growthTier(
  p: PriceStatGrowthRow,
  metric: GrowthMetric,
): { value: number; tier: GrowthTier } {
  const raw = p[metric];
  if (raw == null || !Number.isFinite(raw)) return { value: 0, tier: 0 };
  const active = endpointActive(p, metric);
  if (active < THIN_MIN) return { value: 0, tier: 0 };
  return { value: raw, tier: active < CONFIDENT_MIN ? 1 : 2 };
}

/* The computed growth/yield figure per obec for the active metric — the SAME
 * value the Datasets table column + the choropleth fill read from the
 * price_stat_growth RPC. `value` is the raw RPC figure (null when absent — kept
 * even for a thin obec so its number still shows, matching the table); `tier`
 * carries the confident/thin/none classification for the chart's annotation. */
export interface GrowthMetricCell { value: number | null; tier: GrowthTier; }

export function buildMetricByObec(
  rows: PriceStatGrowthRow[],
  metric: GrowthMetric,
): Map<number, GrowthMetricCell> {
  const m = new Map<number, GrowthMetricCell>();
  for (const p of rows) {
    const raw = p[metric];
    m.set(p.obec_id, {
      value: raw != null && Number.isFinite(raw) ? raw : null,
      tier: growthTier(p, metric).tier,
    });
  }
  return m;
}

/* Format a computed growth/yield figure exactly as the Datasets table does:
 * rent/sale growth → "2.3%" (unsigned), yield change → "+0.15 pp" (signed). */
export function formatGrowthMetric(metric: GrowthMetric, v: number): string {
  const { digits, suffix } = GROWTH_METRICS[metric];
  if (metric === 'yield_change_pp_pa') {
    return `${v >= 0 ? '+' : ''}${v.toFixed(digits)} ${suffix}`;
  }
  return `${v.toFixed(digits)}${suffix}`;
}

export interface GrowthFeatureProps {
  obec_name: string;
  v_rent: number; h_rent: number; tn_rent: number;
  v_sale: number; h_sale: number; tn_sale: number;
  v_yield: number; h_yield: number; tn_yield: number;
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
      const r = growthTier(p, 'rent_cagr_pct');
      const s = growthTier(p, 'sale_cagr_pct');
      const y = growthTier(p, 'yield_change_pp_pa');
      return [{
        type: 'Feature' as const,
        id: p.obec_id,
        geometry,
        properties: {
          obec_name: p.locality_name,
          v_rent: r.value, h_rent: r.tier === 0 ? 0 : 1, tn_rent: r.tier === 1 ? 1 : 0,
          v_sale: s.value, h_sale: s.tier === 0 ? 0 : 1, tn_sale: s.tier === 1 ? 1 : 0,
          v_yield: y.value, h_yield: y.tier === 0 ? 0 : 1, tn_yield: y.tier === 1 ? 1 : 0,
        },
      }];
    }),
  };
}

/* ---- hover chart: per-obec time series of the metric's variable -------- */

export interface HoverPoint { ymi: number; value: number; }
export interface HoverData {
  byObec: Map<number, HoverPoint[]>;
  xMin: number; xMax: number; yMin: number; yMax: number;
  valueLabel: string;
  format: (v: number) => string;
}

/* The displayed variable per metric: rent growth → rent price, sale growth →
 * sale price, yield change → gross yield level. Domains are GLOBAL across all
 * obce so the hover line reads as one fixed chart as the cursor moves. */
export function buildHoverData(rows: PriceStatSeriesRow[], metric: GrowthMetric): HoverData {
  const valueOf = (r: PriceStatSeriesRow): number | null => {
    if (metric === 'rent_cagr_pct') return r.rent_price ?? null;
    if (metric === 'sale_cagr_pct') return r.sale_price ?? null;
    if (r.sale_price && r.rent_price && r.sale_price > 0) {
      return (12 * r.rent_price) / r.sale_price * 100;
    }
    return null;
  };
  const byObec = new Map<number, HoverPoint[]>();
  let xMin = Infinity, xMax = -Infinity, yMin = Infinity, yMax = -Infinity;
  for (const r of rows) {
    const v = valueOf(r);
    if (v == null || !Number.isFinite(v)) continue;
    const ymi = r.year * 12 + (r.month - 1);
    const pt = { ymi, value: v };
    const arr = byObec.get(r.obec_id);
    if (arr) arr.push(pt); else byObec.set(r.obec_id, [pt]);
    xMin = Math.min(xMin, ymi); xMax = Math.max(xMax, ymi);
    yMin = Math.min(yMin, v); yMax = Math.max(yMax, v);
  }
  for (const arr of byObec.values()) arr.sort((a, b) => a.ymi - b.ymi);
  if (!Number.isFinite(xMin)) { xMin = 0; xMax = 1; }
  if (!Number.isFinite(yMin)) { yMin = 0; yMax = 1; }
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const isYield = metric === 'yield_change_pp_pa';
  return {
    byObec, xMin, xMax, yMin, yMax,
    valueLabel:
      metric === 'rent_cagr_pct' ? 'Nájem Kč/m²/měs'
      : metric === 'sale_cagr_pct' ? 'Cena Kč/m²'
      : 'Gross yield %',
    format: isYield ? (v) => `${v.toFixed(1)} %` : (v) => Math.round(v).toLocaleString('cs-CZ'),
  };
}
