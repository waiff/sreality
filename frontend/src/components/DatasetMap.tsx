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
  type HoverData,
} from '@/lib/growthChoropleth';
import HoverChart from '@/components/HoverChart';

export { GROWTH_METRICS as METRICS };
export type DatasetMetric = GrowthMetric;

const layerId = (m: GrowthMetric) => `obce-${m}`;

interface Props {
  rows: PriceStatGrowthRow[];
  metric: GrowthMetric;
  chartOnHover?: boolean;
  hoverData?: HoverData | null;
}

export default function DatasetMap({ rows, metric, chartOnHover = false, hoverData = null }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const [ready, setReady] = useState(false);
  const popupRef = useRef<maplibregl.Popup | null>(null);
  const chartOnHoverRef = useRef(chartOnHover);
  chartOnHoverRef.current = chartOnHover;
  const [hover, setHover] = useState<{ obecId: number; name: string; x: number; y: number } | null>(null);

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
            /* thin (limited listings) → faded tint of the same hue. */
            'fill-opacity': [
              'case',
              ['==', ['get', cfg.hasProp], 0], 0.72,
              ['==', ['get', cfg.thinProp], 1], 0.26,
              0.72,
            ],
          },
        });
        map.on('mousemove', layerId(m), (e) => {
          const f = e.features?.[0];
          if (!f) return;
          map.getCanvas().style.cursor = 'pointer';
          const pr = f.properties as Record<string, unknown>;
          if (chartOnHoverRef.current) {
            popupRef.current?.remove();
            setHover({
              obecId: Number(f.id), name: String(pr.obec_name),
              x: e.point.x, y: e.point.y,
            });
            return;
          }
          setHover(null);
          const cm = GROWTH_METRICS[m];
          const has = Number(pr[cm.hasProp]);
          const thin = Number(pr[cm.thinProp]) === 1;
          const v = Number(pr[cm.vProp]);
          const txt = has
            ? `${v.toFixed(cm.digits)} ${cm.suffix}${thin ? ' · málo dat' : ''}`
            : 'bez dat';
          popupRef.current!
            .setLngLat(e.lngLat)
            .setHTML(`<strong>${String(pr.obec_name)}</strong><br/>${cm.label}: ${txt}`)
            .addTo(map);
        });
        map.on('mouseleave', layerId(m), () => {
          map.getCanvas().style.cursor = '';
          popupRef.current?.remove();
          setHover(null);
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

  const hoverPoints = hover && hoverData ? hoverData.byObec.get(hover.obecId) : undefined;
  const cw = containerRef.current?.clientWidth ?? 900;
  const chartLeft = hover ? (hover.x + 270 > cw ? hover.x - 262 : hover.x + 14) : 0;
  const chartTop = hover ? (hover.y + 200 > 560 ? hover.y - 190 : hover.y + 14) : 0;

  return (
    <div className="relative">
      <div ref={containerRef} className="h-[560px] w-full rounded-[var(--radius-md)] border border-[var(--color-rule)]" />
      <Legend metric={metric} />
      {chartOnHover && hover && hoverPoints && (
        <div className="pointer-events-none absolute z-10" style={{ left: chartLeft, top: chartTop }}>
          <HoverChart
            title={hover.name}
            points={hoverPoints}
            xMin={hoverData!.xMin} xMax={hoverData!.xMax}
            yMin={hoverData!.yMin} yMax={hoverData!.yMax}
            valueLabel={hoverData!.valueLabel}
            format={hoverData!.format}
          />
        </div>
      )}
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
      <div className="mt-1 flex flex-col gap-0.5 text-[var(--color-ink-3)]">
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-[2px]" style={{ background: 'rgba(94,122,74,0.3)' }} />
          málo dat (limited listings)
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-[2px]" style={{ background: GROWTH_NO_DATA }} />
          bez dat
        </span>
      </div>
    </div>
  );
}
