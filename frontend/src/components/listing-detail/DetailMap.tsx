import { useEffect, useRef } from 'react';
import maplibregl from 'maplibre-gl';

const TILE_STYLE = 'https://tiles.openfreemap.org/styles/positron';

interface Props {
  lat: number;
  lng: number;
  isActive: boolean;
}

export default function DetailMap({ lat, lng, isActive }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: TILE_STYLE,
      center: [lng, lat],
      zoom: 14.5,
      attributionControl: { compact: true },
      interactive: true,
      cooperativeGestures: false,
    });
    mapRef.current = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: 'metric' }), 'bottom-left');

    map.on('load', () => {
      map.addSource('pin', {
        type: 'geojson',
        data: {
          type: 'Feature',
          properties: {},
          geometry: { type: 'Point', coordinates: [lng, lat] },
        },
      });
      map.addLayer({
        id: 'pin',
        type: 'circle',
        source: 'pin',
        paint: {
          'circle-radius': 7,
          'circle-color': '#3c6e63',
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 2,
          'circle-opacity': isActive ? 1 : 0.6,
        },
      });
    });

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, [lat, lng, isActive]);

  return (
    <div className="relative h-40 rounded-[var(--radius-md)] overflow-hidden border border-[var(--color-rule)]">
      <div
        ref={containerRef}
        className="absolute inset-0"
        style={{ position: 'absolute', top: 0, right: 0, bottom: 0, left: 0, width: '100%', height: '100%' }}
      />
    </div>
  );
}
