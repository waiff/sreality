import { useEffect, useRef } from 'react';
import maplibregl from 'maplibre-gl';

const TILE_STYLE = 'https://tiles.openfreemap.org/styles/positron';
const PRAGUE = { lng: 14.4378, lat: 50.0755, zoom: 11 };

interface Props {
  center: { lng: number; lat: number };
  radiusM: number;
  onCenterChange: (next: { lng: number; lat: number }) => void;
}

const buildCircle = (
  centre: { lng: number; lat: number },
  radiusM: number,
  steps = 64,
): GeoJSON.Feature<GeoJSON.Polygon> => {
  const coords: [number, number][] = [];
  const distLat = (radiusM / 6_378_137) * (180 / Math.PI);
  const distLng = distLat / Math.cos((centre.lat * Math.PI) / 180);
  for (let i = 0; i <= steps; i++) {
    const theta = (i / steps) * 2 * Math.PI;
    coords.push([
      centre.lng + distLng * Math.cos(theta),
      centre.lat + distLat * Math.sin(theta),
    ]);
  }
  return {
    type: 'Feature',
    geometry: { type: 'Polygon', coordinates: [coords] },
    properties: {},
  };
};

export default function RegionMap({ center, radiusM, onCenterChange }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markerRef = useRef<maplibregl.Marker | null>(null);
  const readyRef = useRef(false);

  useEffect(() => {
    if (!containerRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: TILE_STYLE,
      center: [center.lng, center.lat],
      zoom: PRAGUE.zoom,
      attributionControl: { compact: true },
    });
    mapRef.current = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');

    const el = document.createElement('div');
    el.className = 'rm-pin';
    el.setAttribute('aria-label', 'Region centre');
    const marker = new maplibregl.Marker({ element: el, draggable: true })
      .setLngLat([center.lng, center.lat])
      .addTo(map);
    markerRef.current = marker;

    marker.on('dragend', () => {
      const ll = marker.getLngLat();
      onCenterChange({ lng: ll.lng, lat: ll.lat });
    });

    map.on('click', (e) => {
      onCenterChange({ lng: e.lngLat.lng, lat: e.lngLat.lat });
    });

    map.on('load', () => {
      map.addSource('circle', {
        type: 'geojson',
        data: buildCircle(center, radiusM),
      });
      map.addLayer({
        id: 'circle-fill',
        type: 'fill',
        source: 'circle',
        paint: {
          'fill-color': '#3c6e63',
          'fill-opacity': 0.10,
        },
      });
      map.addLayer({
        id: 'circle-line',
        type: 'line',
        source: 'circle',
        paint: {
          'line-color': '#3c6e63',
          'line-width': 1.25,
        },
      });
      readyRef.current = true;
    });

    return () => {
      readyRef.current = false;
      marker.remove();
      map.remove();
      mapRef.current = null;
      markerRef.current = null;
    };
  // Init only — subsequent prop changes handled in effects below.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sync the marker + circle when centre or radius change externally.
  useEffect(() => {
    const map = mapRef.current;
    const marker = markerRef.current;
    if (!map || !marker || !readyRef.current) return;
    marker.setLngLat([center.lng, center.lat]);
    const src = map.getSource('circle') as maplibregl.GeoJSONSource | undefined;
    src?.setData(buildCircle(center, radiusM) as GeoJSON.FeatureCollection | GeoJSON.Feature);
  }, [center.lng, center.lat, radiusM]);

  return (
    <div className="relative h-[280px] rounded-[var(--radius-md)] overflow-hidden border border-[var(--color-rule)]">
      <div
        ref={containerRef}
        className="absolute inset-0"
        style={{ position: 'absolute', top: 0, right: 0, bottom: 0, left: 0, width: '100%', height: '100%' }}
      />
    </div>
  );
}
