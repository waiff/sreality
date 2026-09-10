/* Two MapLibre maps side by side, viewports synced, for the W6 old-vs-new
 * location review.
 *
 * LEFT is the legacy position (browse_list lat/lng — one dot, no uncertainty
 * has ever been expressed there). RIGHT is the serving projection rendered by
 * its OWN rule (design 05 §5.2.2): a point only when the row says
 * `renderable_as_point`, otherwise a true-radius circle of
 * `uncertainty_radius_m`. Rows the new engine cannot place at all are not
 * drawn — the page counts them in a chip, because inventing a dot for them is
 * exactly the lie this review exists to expose.
 *
 * The two panes are one component used twice; only the layer set differs. */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { GeoJSONSource, Map as MlMap, MapLayerMouseEvent } from 'maplibre-gl';
import { createMap } from '@/lib/basemap';
import { useMapFeatureHover } from '@/lib/useMapFeatureHover';
import { useTokenColors } from '@/lib/useTokenColors';
import { granBucket, type GranBucket, type MapRow } from '@/lib/locationCompare';

export type Centre = { lat: number; lng: number };
export type Side = 'old' | 'new';

const EARTH_RADIUS_M = 6_371_000;
const CIRCLE_POINTS = 48;

/* Same haversine ring ListingMap draws for the centre+radius overlay — a true
 * metric circle, never a ST_Buffer-shaped approximation. */
function circleRing(lat: number, lng: number, radiusM: number): [number, number][] {
  const latRad = (lat * Math.PI) / 180;
  const coords: [number, number][] = [];
  for (let i = 0; i <= CIRCLE_POINTS; i++) {
    const theta = (i / CIRCLE_POINTS) * 2 * Math.PI;
    const dLng =
      ((radiusM * Math.cos(theta)) / (EARTH_RADIUS_M * Math.cos(latRad))) * (180 / Math.PI);
    const dLat = ((radiusM * Math.sin(theta)) / EARTH_RADIUS_M) * (180 / Math.PI);
    coords.push([lng + dLng, lat + dLat]);
  }
  return coords;
}

const FALLBACK: Record<GranBucket, string> = {
  point: '#5e7a4a',
  street: '#3c6e63',
  area: '#b58438',
  none: '#a04b3d',
};

const TOKEN_KEYS = [
  '--color-sage', '--color-copper', '--color-ochre', '--color-brick',
] as const;

export const BUCKET_LABELS: Record<GranBucket, string> = {
  point: 'address point / building',
  street: 'street / parcel',
  area: 'obec and coarser',
  none: 'unresolved',
};

export function useBucketColors(): Record<GranBucket, string> {
  const t = useTokenColors(TOKEN_KEYS);
  return useMemo(
    () => ({
      point: t['--color-sage'] || FALLBACK.point,
      street: t['--color-copper'] || FALLBACK.street,
      area: t['--color-ochre'] || FALLBACK.area,
      none: t['--color-brick'] || FALLBACK.none,
    }),
    [t],
  );
}

type Feature = GeoJSON.Feature<GeoJSON.Geometry, Record<string, unknown>>;

const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] };
const fc = (features: Feature[]): GeoJSON.FeatureCollection => ({
  type: 'FeatureCollection',
  features,
});

/* Row → the marker each side draws for it, or null when that side has no
 * position. The NEW side's choice is read off the projection's own booleans;
 * nothing here re-derives renderability. */
function oldPoint(r: MapRow): Feature | null {
  if (r.old_lat == null || r.old_lng == null) return null;
  return {
    type: 'Feature',
    id: r.property_id,
    properties: { property_id: r.property_id },
    geometry: { type: 'Point', coordinates: [r.old_lng, r.old_lat] },
  };
}

function newIsPoint(r: MapRow): boolean {
  if (r.new_lat == null || r.new_lng == null) return false;
  return r.render_as === 'point' || (r.render_as == null && r.renderable_as_point === true);
}

interface PaneProps {
  side: Side;
  rows: MapRow[];
  hoveredId: number | null;
  onHover: (id: number | null) => void;
  onReady: (side: Side, map: MlMap) => void;
  onMoveEnd: (side: Side, map: MlMap) => void;
  centre: Centre | null;
  radiusM: number;
  pickMode: boolean;
  onPickCentre: (c: Centre) => void;
}

const START_CENTRE: [number, number] = [14.44, 50.05];
const START_ZOOM = 8.6;

function ComparePane({
  side, rows, hoveredId, onHover, onReady, onMoveEnd,
  centre, radiusM, pickMode, onPickCentre,
}: PaneProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MlMap | null>(null);
  const [ready, setReady] = useState(false);
  const [hoverRow, setHoverRow] = useState<MapRow | null>(null);
  const [hoverPos, setHoverPos] = useState<{ x: number; y: number } | null>(null);
  const colors = useBucketColors();

  const rowsById = useMemo(() => {
    const m = new Map<number, MapRow>();
    for (const r of rows) m.set(r.property_id, r);
    return m;
  }, [rows]);
  const rowsRef = useRef(rowsById);
  rowsRef.current = rowsById;
  const onHoverRef = useRef(onHover);
  onHoverRef.current = onHover;
  const pickRef = useRef({ pickMode, onPickCentre });
  pickRef.current = { pickMode, onPickCentre };
  const moveRef = useRef(onMoveEnd);
  moveRef.current = onMoveEnd;
  const readyRef = useRef(onReady);
  readyRef.current = onReady;

  useEffect(() => {
    if (!containerRef.current) return;
    const map = createMap(containerRef.current, {
      center: START_CENTRE,
      zoom: START_ZOOM,
    });
    mapRef.current = map;

    map.on('load', () => {
      map.addSource('points', { type: 'geojson', data: EMPTY });
      map.addSource('areas', { type: 'geojson', data: EMPTY });
      map.addSource('centre', { type: 'geojson', data: EMPTY });

      map.addLayer({
        id: 'area-fill', type: 'fill', source: 'areas',
        paint: {
          'fill-color': ['get', 'color'],
          'fill-opacity': ['case', ['boolean', ['feature-state', 'hovered'], false], 0.28, 0.1],
        },
      });
      map.addLayer({
        id: 'area-line', type: 'line', source: 'areas',
        paint: {
          'line-color': ['get', 'color'],
          'line-width': ['case', ['boolean', ['feature-state', 'hovered'], false], 2, 0.8],
          'line-opacity': 0.8,
        },
      });
      map.addLayer({
        id: 'point', type: 'circle', source: 'points',
        paint: {
          'circle-radius': ['case', ['boolean', ['feature-state', 'hovered'], false], 7, 4],
          'circle-color': ['get', 'color'],
          'circle-stroke-color': [
            'case', ['boolean', ['feature-state', 'hovered'], false],
            '#ffffff', 'rgba(255,255,255,0.6)',
          ],
          'circle-stroke-width': ['case', ['boolean', ['feature-state', 'hovered'], false], 2, 0.8],
          'circle-opacity': 0.9,
        },
      });
      map.addLayer({
        id: 'centre-fill', type: 'fill', source: 'centre',
        paint: { 'fill-color': '#b58438', 'fill-opacity': 0.08 },
      });
      map.addLayer({
        id: 'centre-line', type: 'line', source: 'centre',
        paint: { 'line-color': '#b58438', 'line-width': 1.5, 'line-dasharray': [2, 2] },
      });

      const enter = (e: MapLayerMouseEvent) => {
        const id = e.features?.[0]?.id;
        if (typeof id !== 'number') return;
        map.getCanvas().style.cursor = pickRef.current.pickMode ? 'crosshair' : 'pointer';
        setHoverRow(rowsRef.current.get(id) ?? null);
        setHoverPos({ x: e.point.x, y: e.point.y });
        onHoverRef.current(id);
      };
      const leave = () => {
        map.getCanvas().style.cursor = pickRef.current.pickMode ? 'crosshair' : '';
        setHoverRow(null);
        setHoverPos(null);
        onHoverRef.current(null);
      };
      for (const layer of ['point', 'area-fill']) {
        map.on('mousemove', layer, enter);
        map.on('mouseleave', layer, leave);
      }

      map.on('click', (e) => {
        const { pickMode, onPickCentre } = pickRef.current;
        if (pickMode) onPickCentre({ lat: e.lngLat.lat, lng: e.lngLat.lng });
      });

      setReady(true);
      readyRef.current(side, map);
      /* MapLibre fires no move event for the constructor's own viewport, so the
       * first bbox has to be published here — otherwise nothing is ever fetched
       * until the operator pans. */
      moveRef.current(side, map);
    });

    map.on('moveend', () => moveRef.current(side, map));

    return () => {
      map.remove();
      mapRef.current = null;
      setReady(false);
    };
  }, [side]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    map.getCanvas().style.cursor = pickMode ? 'crosshair' : '';
  }, [pickMode, ready]);

  const [points, areas] = useMemo(() => {
    const pts: Feature[] = [];
    const ars: Feature[] = [];
    for (const r of rows) {
      const color = colors[granBucket(r.granularity)];
      if (side === 'old') {
        const f = oldPoint(r);
        if (f) pts.push({ ...f, properties: { ...f.properties, color } });
        continue;
      }
      if (r.new_lat == null || r.new_lng == null) continue;
      if (newIsPoint(r)) {
        pts.push({
          type: 'Feature', id: r.property_id,
          properties: { property_id: r.property_id, color },
          geometry: { type: 'Point', coordinates: [r.new_lng, r.new_lat] },
        });
      } else {
        const radius = r.uncertainty_radius_m ?? 0;
        if (radius <= 0) continue;
        ars.push({
          type: 'Feature', id: r.property_id,
          properties: { property_id: r.property_id, color },
          geometry: { type: 'Polygon', coordinates: [circleRing(r.new_lat, r.new_lng, radius)] },
        });
      }
    }
    return [pts, ars];
  }, [rows, side, colors]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    (map.getSource('points') as GeoJSONSource | undefined)?.setData(fc(points));
    (map.getSource('areas') as GeoJSONSource | undefined)?.setData(fc(areas));
  }, [points, areas, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource('centre') as GeoJSONSource | undefined;
    if (!src) return;
    src.setData(
      centre
        ? fc([{
            type: 'Feature', properties: {},
            geometry: { type: 'Polygon', coordinates: [circleRing(centre.lat, centre.lng, radiusM)] },
          }])
        : EMPTY,
    );
  }, [centre, radiusM, ready]);

  const hoveredSet = useMemo<ReadonlySet<number>>(
    () => (hoveredId == null ? new Set<number>() : new Set([hoveredId])),
    [hoveredId],
  );
  useMapFeatureHover(mapRef.current, ready, 'points', hoveredSet, points);
  useMapFeatureHover(mapRef.current, ready, 'areas', hoveredSet, areas);

  return (
    <div className="relative h-[26rem] rounded-[var(--radius-md)] overflow-hidden border border-[var(--color-rule)]">
      <div
        ref={containerRef}
        className="absolute inset-0"
        // inline, as DetailMap/ComparablesMap do: maplibre-gl.css sets `.maplibregl-map
        // { position: relative }` on the container, which beats Tailwind's `absolute` and
        // collapses the pane to height 0 (measured on production 2026-09-10)
        style={{ position: 'absolute', top: 0, right: 0, bottom: 0, left: 0, width: '100%', height: '100%' }}
      />
      <div className="absolute left-2 top-2 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-3)]/95 px-2 py-1 text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        {side === 'old' ? 'Old — browse_list' : 'New — projection'}
      </div>
      {hoverRow && hoverPos ? (
        <div
          className="pointer-events-none absolute z-20 max-w-56 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-3)] px-2 py-1.5 text-[0.7rem] text-[var(--color-ink-2)] shadow-none"
          style={{
            left: Math.max(8, Math.min(hoverPos.x + 12, (containerRef.current?.clientWidth ?? 320) - 232)),
            top: Math.max(hoverPos.y - 12, 8),
          }}
        >
          <div className="font-mono text-[0.68rem] text-[var(--color-ink)]">
            {hoverRow.source ?? '—'} · #{hoverRow.property_id}
          </div>
          <div>{hoverRow.granularity ?? '∅'} · {hoverRow.admin_assignment_method ?? '∅'}</div>
          <div className="text-[var(--color-ink-3)]">
            delta {hoverRow.delta_m == null ? '—' : `${Math.round(hoverRow.delta_m)} m`}
          </div>
        </div>
      ) : null}
    </div>
  );
}

interface Props {
  rows: MapRow[];
  hoveredId: number | null;
  onHover: (id: number | null) => void;
  onBboxChange: (b: { west: number; south: number; east: number; north: number }) => void;
  centre: Centre | null;
  radiusM: number;
  pickMode: boolean;
  onPickCentre: (c: Centre) => void;
}

export default function CompareMapPair({
  rows, hoveredId, onHover, onBboxChange, centre, radiusM, pickMode, onPickCentre,
}: Props) {
  const maps = useRef<Record<Side, MlMap | null>>({ old: null, new: null });
  /* Ping-pong guard: jumpTo fires moveend synchronously on the target, so the
   * flag only has to survive the call itself. */
  const syncing = useRef(false);
  const bboxRef = useRef(onBboxChange);
  bboxRef.current = onBboxChange;

  const handleReady = useCallback((side: Side, map: MlMap) => {
    maps.current[side] = map;
  }, []);

  const handleMoveEnd = useCallback((side: Side, map: MlMap) => {
    if (side === 'old') {
      const b = map.getBounds();
      bboxRef.current({
        west: b.getWest(), south: b.getSouth(), east: b.getEast(), north: b.getNorth(),
      });
    }
    if (syncing.current) return;
    const other = maps.current[side === 'old' ? 'new' : 'old'];
    if (!other) return;
    syncing.current = true;
    other.jumpTo({ center: map.getCenter(), zoom: map.getZoom() });
    syncing.current = false;
  }, []);

  const paneProps = {
    rows, hoveredId, onHover, centre, radiusM, pickMode, onPickCentre,
    onReady: handleReady, onMoveEnd: handleMoveEnd,
  };

  return (
    <div className="grid gap-3 lg:grid-cols-2">
      <ComparePane side="old" {...paneProps} />
      <ComparePane side="new" {...paneProps} />
    </div>
  );
}
