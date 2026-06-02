/* Obec choropleth for the Datasets page: rent-growth / sale-growth /
 * yield-change per municipality over the chosen window. Shares the ramp +
 * feature-collection logic with the Browse overlay via lib/growthChoropleth.
 * Each metric is its own fill layer toggled by visibility. */
import { useEffect, useRef, useState } from 'react';
import maplibregl, { type GeoJSONSource } from 'maplibre-gl';
import type { PriceStatGrowthRow } from '@/lib/priceStats';
import {
  GROWTH_METRICS,
  GROWTH_METRIC_ORDER,
  GROWTH_NO_DATA,
  growthToFeatureCollection,
  type GrowthMetric,
} from '@/lib/growthChoropleth';

export { GROWTH_METRICS as METRICS };
export type DatasetMetric = GrowthMetric;

const layerId = (m: GrowthMetric) => `obce-${m}`;

interface Props {
  rows: PriceStatGrowthRow[];
  metric: GrowthMetric;
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
      for (const m of GROWTH_METRIC_ORDER) {
        const cfg = GROWTH_METRICS[m];
        map.addLayer({
          id: layerId(m),
          type: 'fill',
          source: 'obce',
          layout: { visibility: m === metric ? 'visible' : 'none' },
          paint: {
            'fill-color': [
              'case',
              ['==', ['get', cfg.hasProp], 0], GROWTH_NO_DATA,
              ['interpolate', ['linear'], ['get', cfg.vProp],
                ...cfg.ramp.flatMap(([stop, color]) => [stop, color])],
            ],
            'fill-opacity': 0.72,
          },
        });
        map.on('mousemove', layerId(m), (e) => {
          const f = e.features?.[0];
          if (!f) return;
          map.getCanvas().style.cursor = 'pointer';
          const pr = f.properties as Record<string, unknown>;
          const has = Number(pr[cfg.hasProp]);
          const v = Number(pr[cfg.vProp]);
          const txt = has ? `${v.toFixed(cfg.digits)} ${cfg.suffix}` : 'thin / no data';
          popupRef.current!
            .setLngLat(e.lngLat)
            .setHTML(`<strong>${String(pr.obec_name)}</strong><br/>${cfg.label}: ${txt}`)
            .addTo(map);
        });
        map.on('mouseleave', layerId(m), () => {
          map.getCanvas().style.cursor = '';
          popupRef.current?.remove();
        });
      }
      map.addLayer({
        id: 'obce-line',
        type: 'line',
        source: 'obce',
        paint: { 'line-color': 'rgba(26,28,34,0.16)', 'line-width': 0.4 },
      });
      popupRef.current = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
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
    (map.getSource('obce') as GeoJSONSource | undefined)?.setData(growthToFeatureCollection(rows));
  }, [rows, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    for (const m of GROWTH_METRIC_ORDER) {
      if (map.getLayer(layerId(m))) {
        map.setLayoutProperty(layerId(m), 'visibility', m === metric ? 'visible' : 'none');
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

function Legend({ metric }: { metric: GrowthMetric }) {
  const { ramp, label, suffix, digits } = GROWTH_METRICS[metric];
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
        <span className="inline-block h-2 w-2 rounded-[2px]" style={{ background: GROWTH_NO_DATA }} />
        thin / no data
      </div>
    </div>
  );
}
