/* The pin-loss audit map (migration 510's cohort, drawn where it actually is).
 *
 * WHY NOT THE BROWSE MAP. `ListingMap` is not a point renderer — it is the
 * Browse cohort contract: server-side density cells, a viewport `bounds` the
 * parent must own and echo back, cohort totals (exact and approximate), an
 * off-grid remainder, hover origin arbitration, city polygons and a flyTo
 * anchor. Feeding it a plain list would mean faking a dozen fields it reads as
 * meaning. So this reuses the piece that IS shared and is the reason both maps
 * look alike: `createMap` from lib/basemap — one tile style, one set of
 * disabled gestures, one zoom/attribution posture — with the DetailMap
 * single-layer pattern widened to many points. Colours come from the theme
 * tokens at mount; nothing here defines a colour of its own.
 */

import { useEffect, useRef } from 'react';
import maplibregl from 'maplibre-gl';

import { createMap } from '@/lib/basemap';
import type { PinAuditPoint } from '@/lib/pinAudit';

interface Props {
  points: ReadonlyArray<PinAuditPoint>;
  selectedId: number | null;
  onPick: (listingId: number) => void;
  heightClass?: string;
}

function toFeatureCollection(
  points: ReadonlyArray<PinAuditPoint>,
): GeoJSON.FeatureCollection<GeoJSON.Point> {
  return {
    type: 'FeatureCollection',
    features: points.map((p) => ({
      type: 'Feature',
      id: p.listing_id,
      properties: { listing_id: p.listing_id, is_active: p.is_active },
      geometry: { type: 'Point', coordinates: [p.legacy_lng, p.legacy_lat] },
    })),
  };
}

function token(name: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name);
  return v.trim() || fallback;
}

export default function PinAuditMap({
  points,
  selectedId,
  onPick,
  heightClass,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const readyRef = useRef(false);
  const onPickRef = useRef(onPick);
  onPickRef.current = onPick;

  useEffect(() => {
    if (!containerRef.current) return;
    const map = createMap(containerRef.current, {
      // Czechia, whole-country — the cohort is nationwide.
      center: [15.4, 49.8],
      zoom: 6.2,
      interactive: true,
      cooperativeGestures: false,
    });
    mapRef.current = map;

    map.on('load', () => {
      map.addSource('pins', { type: 'geojson', data: toFeatureCollection([]) });
      map.addLayer({
        id: 'pins',
        type: 'circle',
        source: 'pins',
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 5, 2.5, 12, 6],
          'circle-color': [
            'case',
            ['get', 'is_active'],
            token('--color-copper', '#3c6e63'),
            token('--color-ink-4', '#b0b1b7'),
          ],
          'circle-opacity': 0.75,
        },
      });
      map.addLayer({
        id: 'pin-selected',
        type: 'circle',
        source: 'pins',
        filter: ['==', ['get', 'listing_id'], -1],
        paint: {
          'circle-radius': 8,
          'circle-color': token('--color-brick', '#a04b3d'),
          'circle-stroke-color': token('--color-paper-3', '#ffffff'),
          'circle-stroke-width': 2,
        },
      });
      readyRef.current = true;
      map.on('click', 'pins', (e) => {
        const id = e.features?.[0]?.properties?.listing_id;
        if (typeof id === 'number') onPickRef.current(id);
      });
      map.on('mouseenter', 'pins', () => {
        map.getCanvas().style.cursor = 'pointer';
      });
      map.on('mouseleave', 'pins', () => {
        map.getCanvas().style.cursor = '';
      });
    });

    return () => {
      readyRef.current = false;
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const write = () => {
      const src = map.getSource('pins') as maplibregl.GeoJSONSource | undefined;
      if (!src) return;
      src.setData(toFeatureCollection(points));
    };
    if (readyRef.current) write();
    else map.once('load', write);
  }, [points]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !readyRef.current || !map.getLayer('pin-selected')) return;
    map.setFilter('pin-selected', [
      '==',
      ['get', 'listing_id'],
      selectedId ?? -1,
    ]);
  }, [selectedId]);

  return (
    <div
      className={[
        'relative rounded-[var(--radius-md)] overflow-hidden border border-[var(--color-rule)]',
        heightClass ?? 'h-[24rem]',
      ].join(' ')}
    >
      <div ref={containerRef} className="absolute inset-0" />
    </div>
  );
}
