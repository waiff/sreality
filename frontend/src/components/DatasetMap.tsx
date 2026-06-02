/* Obec choropleth for a price-stats dataset: rent-growth / sale-growth /
 * yield-change per municipality, over the page's chosen [from,to] window.
 * Driven by the price_stat_growth RPC. Diverging ramp uses the system's own
 * brick (negative) → sage (positive) semantics; thin/no-data obce greyed.
 * Each metric is its own fill layer toggled by visibility (no paint rebuild). */
import { useEffect, useRef, useState } from 'react';
import maplibregl, { type GeoJSONSource } from 'maplibre-gl';
import type { PriceStatGrowthRow } from '@/lib/priceStats';

export type DatasetMetric = 'rent_cagr_pct' | 'sale_cagr_pct' | 'yield_change_pp_pa';

type Ramp = ReadonlyArray<readonly [number, string]>;

// Brick → paper-neutral → sage. The app's existing negative/positive hues.
const GROWTH_RAMP: Ramp = [
  [-8, '#a04b3d'], [-3, '#c89a8e'], [0, '#dcd6c8'], [3, '#9aae84'], [8, '#5e7a4a'],
];
const YIELD_RAMP: Ramp = [
  [-0.6, '#a04b3d'], [-0.2, '#c89a8e'], [0, '#dcd6c8'], [0.2, '#9aae84'], [0.6, '#5e7a4a'],
];
const NO_DATA = 'rgba(122, 125, 134, 0.28)';
const MIN_ACTIVE = 3;

export const METRICS: Record<
  DatasetMetric,
  { label: string; ramp: Ramp; suffix: string; layer: string; vProp: string; hasProp: string; digits: number }
> = {
  rent_cagr_pct: { label: 'Rent growth p.a.', ramp: GROWTH_RAMP, suffix: '%', layer: 'obce-rent', vProp: 'v_rent', hasProp: 'h_rent', digits: 1 },
  sale_cagr_pct: { label: 'Sale-price growth p.a.', ramp: GROWTH_RAMP, suffix: '%', layer: 'obce-sale', vProp: 'v_sale', hasProp: 'h_sale', digits: 1 },
  yield_change_pp_pa: { label: 'Yield change p.a.', ramp: YIELD_RAMP, suffix: 'pp', layer: 'obce-yield', vProp: 'v_yield', hasProp: 'h_yield', digits: 2 },
};

function value(p: PriceStatGrowthRow, metric: DatasetMetric): number | null {
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

interface FeatureProps {
  obec_name: string;
  v_rent: number; h_rent: number;
  v_sale: number; h_sale: number;
  v_yield: number; h_yield: number;
}
type FC = GeoJSON.FeatureCollection<GeoJSON.Polygon | GeoJSON.MultiPolygon, FeatureProps>;

const toFC = (rows: PriceStatGrowthRow[]): FC => ({
  type: 'FeatureCollection',
  features: rows.flatMap((p) => {
    let geometry: GeoJSON.Geometry;
    try {
      geometry = JSON.parse(p.geojson) as GeoJSON.Geometry;
    } catch {
      return [];
    }
    if (geometry.type !== 'Polygon' && geometry.type !== 'MultiPolygon') return [];
    const r = value(p, 'rent_cagr_pct');
    const s = value(p, 'sale_cagr_pct');
    const y = value(p, 'yield_change_pp_pa');
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
});

interface Props {
  rows: PriceStatGrowthRow[];
  metric: DatasetMetric;
}

export default function DatasetMap({ rows, metric }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const [ready, setReady] = useState(false);
  const popupRef = useRef<maplibregl.Popup | null>(null);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: 'https://tiles.openfreemap.org/styles/positron',
      center: [15.47, 49.82],
      zoom: 6.6,
      attributionControl: { compact: true },
    });
    mapRef.current = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');

    map.on('load', () => {
      map.addSource('obce', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
      for (const m of Object.values(METRICS)) {
        map.addLayer({
          id: m.layer,
          type: 'fill',
          source: 'obce',
          layout: { visibility: m.layer === METRICS[metric].layer ? 'visible' : 'none' },
          paint: {
            'fill-color': [
              'case',
              ['==', ['get', m.hasProp], 0], NO_DATA,
              ['interpolate', ['linear'], ['get', m.vProp],
                ...m.ramp.flatMap(([stop, color]) => [stop, color])],
            ],
            'fill-opacity': 0.72,
          },
        });
      }
      map.addLayer({
        id: 'obce-line',
        type: 'line',
        source: 'obce',
        paint: { 'line-color': 'rgba(26,28,34,0.16)', 'line-width': 0.4 },
      });

      popupRef.current = new maplibregl.Popup({ closeButton: false, closeOnClick: false, className: 'ds-popup' });
      for (const m of Object.values(METRICS)) {
        map.on('mousemove', m.layer, (e) => {
          const f = e.features?.[0];
          if (!f) return;
          map.getCanvas().style.cursor = 'pointer';
          const pr = f.properties as Record<string, unknown>;
          const has = Number(pr[m.hasProp]);
          const v = Number(pr[m.vProp]);
          const txt = has ? `${v.toFixed(m.digits)} ${m.suffix}` : 'thin / no data';
          popupRef.current!
            .setLngLat(e.lngLat)
            .setHTML(`<strong>${String(pr.obec_name)}</strong><br/>${m.label}: ${txt}`)
            .addTo(map);
        });
        map.on('mouseleave', m.layer, () => {
          map.getCanvas().style.cursor = '';
          popupRef.current?.remove();
        });
      }
      setReady(true);
    });

    return () => {
      map.remove();
      mapRef.current = null;
      setReady(false);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    (map.getSource('obce') as GeoJSONSource | undefined)?.setData(toFC(rows));
  }, [rows, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    for (const m of Object.values(METRICS)) {
      if (map.getLayer(m.layer)) {
        map.setLayoutProperty(
          m.layer, 'visibility',
          m.layer === METRICS[metric].layer ? 'visible' : 'none',
        );
      }
    }
  }, [metric, ready]);

  return (
    <div className="relative">
      <div ref={containerRef} className="h-[560px] w-full rounded-[var(--radius-md)] border border-[var(--color-rule)]" />
      <Legend metric={metric} />
    </div>
  );
}

function Legend({ metric }: { metric: DatasetMetric }) {
  const { ramp, label, suffix, digits } = METRICS[metric];
  const lo = ramp[0][0];
  const hi = ramp[ramp.length - 1][0];
  const gradient = `linear-gradient(to right, ${ramp.map(([, c]) => c).join(', ')})`;
  return (
    <div className="absolute bottom-3 left-3 bg-[var(--color-paper-3)]/95 border border-[var(--color-rule)] rounded-[var(--radius-sm)] px-3 py-2 text-[0.7rem]">
      <div className="mb-1 text-[var(--color-ink-2)]">{label}</div>
      <div className="h-1.5 w-44 rounded-[var(--radius-xs)]" style={{ background: gradient }} />
      <div className="mt-1 flex justify-between text-[var(--color-ink-3)] tabular-nums">
        <span>{lo.toFixed(digits)}{suffix}</span>
        <span>0</span>
        <span>+{hi.toFixed(digits)}{suffix}</span>
      </div>
      <div className="mt-1 flex items-center gap-1 text-[var(--color-ink-3)]">
        <span className="inline-block h-2 w-2 rounded-[2px]" style={{ background: NO_DATA }} />
        thin / no data
      </div>
    </div>
  );
}
