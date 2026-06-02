/* Obec choropleth for a price-stats dataset: rent-growth / sale-growth /
 * gross-yield per municipality. Mirrors the MF rent-map choropleth pattern in
 * ListingMap (geojson source + inline interpolate paint). Each metric is its
 * own fill layer toggled by visibility, so switching metric never rebuilds a
 * paint expression — the FeatureCollection carries all three values. */
import { useEffect, useRef, useState } from 'react';
import maplibregl, { type GeoJSONSource } from 'maplibre-gl';
import type { PriceStatPolygon } from '@/lib/priceStats';

export type DatasetMetric = 'rent_cagr_pct' | 'sale_cagr_pct' | 'gross_yield_pct';

type Ramp = ReadonlyArray<readonly [number, string]>;

const GROWTH_RAMP: Ramp = [
  [-8, '#b2182b'], [-4, '#e8845f'], [0, '#efe9dd'],
  [4, '#6fae72'], [8, '#1a7a4c'],
];
const YIELD_RAMP: Ramp = [
  [2, '#efe9dd'], [4, '#bcd9a6'], [6, '#7fae5a'],
  [8, '#3f8f48'], [10, '#1a5e2e'],
];
const NO_DATA = 'rgba(150, 145, 132, 0.35)';

/* A market is too thin to trust below this many active offers in the window —
 * the real cause of wild month-to-month swings; greyed out on the map. */
const MIN_ACTIVE = 3;

export const METRICS: Record<
  DatasetMetric,
  { label: string; ramp: Ramp; suffix: string; layer: string; vProp: string; hasProp: string }
> = {
  rent_cagr_pct: { label: 'Rent growth p.a.', ramp: GROWTH_RAMP, suffix: '%', layer: 'obce-rent', vProp: 'v_rent', hasProp: 'h_rent' },
  sale_cagr_pct: { label: 'Sale-price growth p.a.', ramp: GROWTH_RAMP, suffix: '%', layer: 'obce-sale', vProp: 'v_sale', hasProp: 'h_sale' },
  gross_yield_pct: { label: 'Gross yield', ramp: YIELD_RAMP, suffix: '%', layer: 'obce-yield', vProp: 'v_yield', hasProp: 'h_yield' },
};

function value(p: PriceStatPolygon, metric: DatasetMetric): number | null {
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

const toFC = (polygons: PriceStatPolygon[]): FC => ({
  type: 'FeatureCollection',
  features: polygons.flatMap((p) => {
    let geometry: GeoJSON.Geometry;
    try {
      geometry = JSON.parse(p.geojson) as GeoJSON.Geometry;
    } catch {
      return [];
    }
    if (geometry.type !== 'Polygon' && geometry.type !== 'MultiPolygon') return [];
    const r = value(p, 'rent_cagr_pct');
    const s = value(p, 'sale_cagr_pct');
    const y = value(p, 'gross_yield_pct');
    return [{
      type: 'Feature' as const,
      id: p.obec_id,
      geometry,
      properties: {
        obec_name: p.obec_name,
        v_rent: r ?? 0, h_rent: r == null ? 0 : 1,
        v_sale: s ?? 0, h_sale: s == null ? 0 : 1,
        v_yield: y ?? 0, h_yield: y == null ? 0 : 1,
      },
    }];
  }),
});

interface Props {
  polygons: PriceStatPolygon[];
  metric: DatasetMetric;
}

export default function DatasetMap({ polygons, metric }: Props) {
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
              [
                'interpolate', ['linear'], ['get', m.vProp],
                ...m.ramp.flatMap(([stop, color]) => [stop, color]),
              ],
            ],
            'fill-opacity': 0.7,
          },
        });
      }
      map.addLayer({
        id: 'obce-line',
        type: 'line',
        source: 'obce',
        paint: { 'line-color': 'rgba(60,60,90,0.25)', 'line-width': 0.4 },
      });

      popupRef.current = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
      for (const m of Object.values(METRICS)) {
        map.on('mousemove', m.layer, (e) => {
          const f = e.features?.[0];
          if (!f) return;
          map.getCanvas().style.cursor = 'pointer';
          const pr = f.properties as Record<string, unknown>;
          const has = Number(pr[m.hasProp]);
          const v = Number(pr[m.vProp]);
          const txt = has ? `${v.toFixed(1)}${m.suffix}` : 'thin / no data';
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

  // Apply features once the map is ready AND whenever the data changes. Gating
  // on `ready` as STATE (not a ref) is what makes this re-run after the map's
  // async load completes — data usually arrives before then.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    (map.getSource('obce') as GeoJSONSource | undefined)?.setData(toFC(polygons));
  }, [polygons, ready]);

  // Toggle which metric's fill layer is visible.
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
      <div ref={containerRef} className="h-[560px] w-full rounded-sm border border-[var(--color-line)]" />
      <Legend metric={metric} />
    </div>
  );
}

function Legend({ metric }: { metric: DatasetMetric }) {
  const { ramp, label, suffix } = METRICS[metric];
  const stops = ramp.map(([s]) => s);
  const gradient = `linear-gradient(to right, ${ramp.map(([, c]) => c).join(', ')})`;
  return (
    <div className="absolute bottom-3 left-3 bg-[var(--color-paper)]/95 border border-[var(--color-line)] rounded-sm px-3 py-2 text-[0.7rem]">
      <div className="mb-1 text-[var(--color-ink-2)]">{label}</div>
      <div className="h-1.5 w-44 rounded-sm" style={{ background: gradient }} />
      <div className="mt-1 flex justify-between text-[var(--color-ink-3)] tabular-nums">
        <span>{stops[0]}{suffix}</span>
        <span>{stops[stops.length - 1]}{suffix}</span>
      </div>
      <div className="mt-1 flex items-center gap-1 text-[var(--color-ink-3)]">
        <span className="inline-block h-2 w-2 rounded-sm" style={{ background: NO_DATA }} />
        thin / no data
      </div>
    </div>
  );
}
