import { useEffect, useRef, useState } from 'react';
import maplibregl, { type GeoJSONSource } from 'maplibre-gl';
import { fmtArea, fmtCzk } from '@/lib/format';

const TILE_STYLE = 'https://tiles.openfreemap.org/styles/positron';

export interface ComparablePoint {
  sreality_id: number;
  lat: number;
  lng: number;
  price_czk: number | null;
  area_m2: number | null;
  disposition: string | null;
  district: string | null;
}

interface Subject {
  lat: number;
  lng: number;
}

interface Props {
  subject: Subject;
  comparables: ComparablePoint[];
  onPick?: (sreality_id: number) => void;
}

export default function ComparablesMap({ subject, comparables, onPick }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const popupRef = useRef<maplibregl.Popup | null>(null);
  const onPickRef = useRef(onPick);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    onPickRef.current = onPick;
  }, [onPick]);

  useEffect(() => {
    if (!containerRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: TILE_STYLE,
      center: [subject.lng, subject.lat],
      zoom: 13.5,
      attributionControl: { compact: true },
    });
    mapRef.current = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: 'metric' }), 'bottom-left');

    map.on('load', () => {
      map.addSource('subject', {
        type: 'geojson',
        data: {
          type: 'Feature',
          properties: {},
          geometry: { type: 'Point', coordinates: [subject.lng, subject.lat] },
        },
      });
      map.addSource('comparables', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });

      map.addLayer({
        id: 'comparables-point',
        type: 'circle',
        source: 'comparables',
        paint: {
          'circle-radius': 7,
          'circle-color': '#3c6e63',
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 2,
          'circle-opacity': 0.95,
        },
      });

      map.addLayer({
        id: 'subject-halo',
        type: 'circle',
        source: 'subject',
        paint: {
          'circle-radius': 14,
          'circle-color': '#b6612d',
          'circle-opacity': 0.18,
        },
      });
      map.addLayer({
        id: 'subject-pin',
        type: 'circle',
        source: 'subject',
        paint: {
          'circle-radius': 8,
          'circle-color': '#b6612d',
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 2,
        },
      });

      map.on('mouseenter', 'comparables-point', () => (map.getCanvas().style.cursor = 'pointer'));
      map.on('mouseleave', 'comparables-point', () => (map.getCanvas().style.cursor = ''));

      map.on('click', 'comparables-point', (e) => {
        const f = e.features?.[0];
        if (!f || f.geometry.type !== 'Point') return;
        const props = f.properties as unknown as ComparablePoint;
        popupRef.current?.remove();
        popupRef.current = new maplibregl.Popup({
          closeButton: true,
          closeOnClick: true,
          maxWidth: '260px',
          className: 'listing-popup',
        })
          .setLngLat(f.geometry.coordinates as [number, number])
          .setHTML(popupHtml(props))
          .addTo(map);

        const root = popupRef.current.getElement();
        const btn = root?.querySelector<HTMLButtonElement>('.lp-link');
        if (btn) {
          btn.addEventListener('click', (ev) => {
            ev.preventDefault();
            popupRef.current?.remove();
            onPickRef.current?.(props.sreality_id);
          });
        }
      });

      setReady(true);
    });

    return () => {
      popupRef.current?.remove();
      map.remove();
      mapRef.current = null;
    };
  }, [subject.lat, subject.lng]);

  useEffect(() => {
    if (!ready || !mapRef.current) return;
    const src = mapRef.current.getSource('comparables') as GeoJSONSource | undefined;
    if (!src) return;
    src.setData({
      type: 'FeatureCollection',
      features: comparables.map((c) => ({
        type: 'Feature',
        properties: c,
        geometry: { type: 'Point', coordinates: [c.lng, c.lat] },
      })),
    });

    const bounds = new maplibregl.LngLatBounds();
    bounds.extend([subject.lng, subject.lat]);
    for (const c of comparables) bounds.extend([c.lng, c.lat]);
    if (!bounds.isEmpty()) {
      mapRef.current.fitBounds(bounds, { padding: 64, maxZoom: 15, duration: 600 });
    }
  }, [comparables, ready, subject.lat, subject.lng]);

  return (
    <div className="relative h-80 rounded-[var(--radius-md)] overflow-hidden border border-[var(--color-rule)]">
      <div
        ref={containerRef}
        className="absolute inset-0"
        style={{ position: 'absolute', top: 0, right: 0, bottom: 0, left: 0, width: '100%', height: '100%' }}
      />
      <Legend />
    </div>
  );
}

function Legend() {
  return (
    <div className="pointer-events-none absolute bottom-9 left-3 px-2.5 py-1.5 rounded-[var(--radius-sm)] bg-[var(--color-paper-3)]/95 backdrop-blur-sm border border-[var(--color-rule)] text-[0.7rem] tracking-wide text-[var(--color-ink-2)] flex items-center gap-3">
      <span className="inline-flex items-center gap-1.5">
        <span className="w-2 h-2 rounded-full" style={{ background: '#b6612d' }} aria-hidden />
        subject
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="w-2 h-2 rounded-full" style={{ background: '#3c6e63' }} aria-hidden />
        comparable
      </span>
    </div>
  );
}

function popupHtml(c: ComparablePoint): string {
  const price = fmtCzk(c.price_czk);
  const area = fmtArea(c.area_m2);
  const disposition = c.disposition ?? '—';
  const district = c.district ?? '';
  return `
    <div class="lp">
      <p class="lp-price">${escape(price)}</p>
      <p class="lp-meta">
        <span class="lp-mono">${escape(disposition)}</span>
        <span class="lp-sep">·</span>
        <span class="lp-mono">${escape(area)}</span>
      </p>
      ${district ? `<p class="lp-district">${escape(district)}</p>` : ''}
      <a href="#" class="lp-link" data-id="${c.sreality_id}">View details →</a>
    </div>
  `;
}

function escape(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
