/* LocationControl — composite spatial filter widget.
 *
 * Renders a small interactive map with a draggable centre marker and a
 * radius circle. The operator can:
 *   - drag the dot to move the centre
 *   - click anywhere on the map to teleport the dot there
 *   - type lat / lng / radius_m directly into the inputs below
 *   - use the radius slider for coarse adjustments
 *
 * State shape (`CenterRadius | null`):
 *   { lat, lng, radius_m }  — all three set
 *   null                   — filter cleared
 *
 * The component leans on maplibre-gl, already a dependency of
 * ListingMap.tsx. The radius circle is rendered as an N-point polygon
 * approximation in meters (maplibre has no native circle-in-meters
 * layer); the polygon is recomputed whenever lat/lng/radius change.
 *
 * Designed to be embedded inside <FilterForm> in a future commit; for
 * this PR the Watchdog form drops it in directly to replace its three
 * separate lat / lng / radius_m number inputs.
 */

import { useCallback, useEffect, useMemo, useRef } from 'react';
import maplibregl from 'maplibre-gl';

import { NumberCell } from '@/components/controls';
import { createMap } from '@/lib/basemap';

const PRAGUE = { lng: 14.4378, lat: 50.0755, zoom: 11 };
const EARTH_RADIUS_M = 6_371_000;
const CIRCLE_POINTS = 96;

export interface CenterRadius {
  lat: number;
  lng: number;
  radius_m: number;
}

interface LocationControlProps {
  value: CenterRadius | null;
  onChange: (next: CenterRadius | null) => void;
  /** Lower / upper / step bounds for the radius slider. Defaults match
   *  the registry's `radius_m` constraints (100m – 10km, step 100m). */
  radiusBounds?: { min: number; max: number; step: number };
  /** Optional explanation rendered under the inputs. */
  hint?: string;
}

const DEFAULT_RADIUS_BOUNDS = { min: 100, max: 10_000, step: 100 };

export function LocationControl({
  value,
  onChange,
  radiusBounds = DEFAULT_RADIUS_BOUNDS,
  hint,
}: LocationControlProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markerRef = useRef<maplibregl.Marker | null>(null);

  // Latest onChange in a ref so the maplibre event handlers don't get
  // stale closures when the parent re-renders.
  const onChangeRef = useRef(onChange);
  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  // Latest value in a ref so the click handler can read current radius
  // without re-binding listeners every render.
  const valueRef = useRef(value);
  useEffect(() => {
    valueRef.current = value;
  }, [value]);

  // Initialise the map exactly once.
  useEffect(() => {
    if (!containerRef.current) return;
    const initialCenter = value
      ? [value.lng, value.lat]
      : [PRAGUE.lng, PRAGUE.lat];

    const map = createMap(containerRef.current, {
      center: initialCenter as [number, number],
      zoom: PRAGUE.zoom,
      // Keep the widget self-contained.
      keyboard: false,
    });
    mapRef.current = map;

    map.on('load', () => {
      map.addSource('radius-circle', {
        type: 'geojson',
        data: emptyPolygon(),
      });
      map.addLayer({
        id: 'radius-circle-fill',
        type: 'fill',
        source: 'radius-circle',
        paint: {
          'fill-color': '#b58438',
          'fill-opacity': 0.10,
        },
      });
      map.addLayer({
        id: 'radius-circle-outline',
        type: 'line',
        source: 'radius-circle',
        paint: {
          'line-color': '#b58438',
          'line-width': 1.4,
          'line-dasharray': [2, 2],
        },
      });

      // If we already have a value at mount time, render the marker
      // and circle now.
      const v = valueRef.current;
      if (v) {
        upsertMarker(map, markerRef, v.lat, v.lng, onChangeRef);
        setCircle(map, v.lat, v.lng, v.radius_m);
      }
    });

    // Click teleports the centre to the click point. Preserves the
    // current radius (or defaults to the lower bound if no value yet).
    map.on('click', (e) => {
      const cur = valueRef.current;
      const radius = cur?.radius_m ?? DEFAULT_RADIUS_BOUNDS.min;
      const next: CenterRadius = {
        lat: e.lngLat.lat,
        lng: e.lngLat.lng,
        radius_m: radius,
      };
      onChangeRef.current(next);
    });

    return () => {
      markerRef.current?.remove();
      markerRef.current = null;
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sync marker + circle whenever the controlled value changes.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    if (value == null) {
      markerRef.current?.remove();
      markerRef.current = null;
      const src = map.getSource('radius-circle') as
        | maplibregl.GeoJSONSource | undefined;
      if (src) src.setData(emptyPolygon());
      return;
    }
    upsertMarker(map, markerRef, value.lat, value.lng, onChangeRef);
    setCircle(map, value.lat, value.lng, value.radius_m);
  }, [value]);

  const setLat = useCallback(
    (lat: number | null) => {
      if (lat == null) {
        onChange(null);
        return;
      }
      onChange({
        lat,
        lng: value?.lng ?? PRAGUE.lng,
        radius_m: value?.radius_m ?? DEFAULT_RADIUS_BOUNDS.min,
      });
    },
    [onChange, value],
  );
  const setLng = useCallback(
    (lng: number | null) => {
      if (lng == null) {
        onChange(null);
        return;
      }
      onChange({
        lat: value?.lat ?? PRAGUE.lat,
        lng,
        radius_m: value?.radius_m ?? DEFAULT_RADIUS_BOUNDS.min,
      });
    },
    [onChange, value],
  );
  const setRadius = useCallback(
    (radius_m: number | null) => {
      if (radius_m == null || !value) {
        onChange(null);
        return;
      }
      onChange({ ...value, radius_m });
    },
    [onChange, value],
  );

  const radiusForSlider = useMemo(
    () => value?.radius_m ?? radiusBounds.min,
    [value, radiusBounds.min],
  );

  return (
    <div className="space-y-2">
      <div
        ref={containerRef}
        className="w-full h-[240px] rounded-[var(--radius-sm)] overflow-hidden border border-[var(--color-rule)]"
      />
      <div className="grid grid-cols-[1fr_1fr_1fr_auto] gap-2 items-center">
        <NumberCell
          value={value?.lat ?? null}
          placeholder="lat"
          ariaLabel="latitude"
          onChange={(e) => setLat(parseCoord(e.target.value))}
        />
        <NumberCell
          value={value?.lng ?? null}
          placeholder="lng"
          ariaLabel="longitude"
          onChange={(e) => setLng(parseCoord(e.target.value))}
        />
        <NumberCell
          value={value?.radius_m ?? null}
          placeholder="radius m"
          ariaLabel="radius in metres"
          onChange={(e) => {
            const n = parseCoord(e.target.value);
            setRadius(n == null ? null : Math.trunc(n));
          }}
        />
        <button
          type="button"
          onClick={() => onChange(null)}
          className="px-2 py-1.5 text-xs rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] transition-colors"
          aria-label="Clear spatial filter"
          disabled={value == null}
        >
          Clear
        </button>
      </div>
      <input
        type="range"
        min={radiusBounds.min}
        max={radiusBounds.max}
        step={radiusBounds.step}
        value={radiusForSlider}
        onChange={(e) => setRadius(Number(e.target.value))}
        disabled={value == null}
        className="w-full accent-[var(--color-copper)]"
        aria-label="radius slider"
      />
      {hint ? (
        <p className="text-[0.7rem] text-[var(--color-ink-4)]">{hint}</p>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* maplibre helpers                                                           */
/* -------------------------------------------------------------------------- */

function upsertMarker(
  map: maplibregl.Map,
  markerRef: React.MutableRefObject<maplibregl.Marker | null>,
  lat: number,
  lng: number,
  onChangeRef: React.MutableRefObject<(v: CenterRadius | null) => void>,
) {
  if (markerRef.current) {
    markerRef.current.setLngLat([lng, lat]);
    return;
  }
  const marker = new maplibregl.Marker({
    color: '#b58438',
    draggable: true,
  })
    .setLngLat([lng, lat])
    .addTo(map);
  marker.on('dragend', () => {
    const { lng: newLng, lat: newLat } = marker.getLngLat();
    // Preserve the current radius — `dragend` only moves the centre.
    // The parent component's value is the source of truth via the ref.
    const cur = (marker as unknown as { _curRadius?: number })._curRadius;
    onChangeRef.current({
      lat: newLat,
      lng: newLng,
      radius_m: cur ?? DEFAULT_RADIUS_BOUNDS.min,
    });
  });
  markerRef.current = marker;
}

function setCircle(
  map: maplibregl.Map,
  lat: number,
  lng: number,
  radius_m: number,
) {
  const src = map.getSource('radius-circle') as
    | maplibregl.GeoJSONSource | undefined;
  if (!src) return;
  src.setData(circlePolygon(lat, lng, radius_m));
  // Stash the current radius on the marker so its `dragend` callback
  // can preserve it without going through React state.
  const marker = (map as unknown as { _markerCache?: maplibregl.Marker })
    ._markerCache;
  if (marker) (marker as unknown as { _curRadius?: number })._curRadius = radius_m;
}

/** GeoJSON polygon approximating a circle of `radius_m` around (lat, lng).
 *  Uses an N-point ring; 96 points is smooth enough for the eye while
 *  staying cheap to render. */
function circlePolygon(
  lat: number,
  lng: number,
  radius_m: number,
): GeoJSON.Feature<GeoJSON.Polygon> {
  const latRad = (lat * Math.PI) / 180;
  const coords: [number, number][] = [];
  for (let i = 0; i <= CIRCLE_POINTS; i++) {
    const theta = (i / CIRCLE_POINTS) * 2 * Math.PI;
    const dx = radius_m * Math.cos(theta);
    const dy = radius_m * Math.sin(theta);
    const dLng = (dx / (EARTH_RADIUS_M * Math.cos(latRad))) * (180 / Math.PI);
    const dLat = (dy / EARTH_RADIUS_M) * (180 / Math.PI);
    coords.push([lng + dLng, lat + dLat]);
  }
  return {
    type: 'Feature',
    properties: {},
    geometry: { type: 'Polygon', coordinates: [coords] },
  };
}

function emptyPolygon(): GeoJSON.Feature<GeoJSON.Polygon> {
  return {
    type: 'Feature',
    properties: {},
    geometry: { type: 'Polygon', coordinates: [[]] },
  };
}

function parseCoord(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === '') return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}
