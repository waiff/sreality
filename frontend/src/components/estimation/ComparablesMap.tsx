import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import maplibregl, { type GeoJSONSource } from 'maplibre-gl';
import { TILE_STYLE } from '@/lib/basemap';
import { fmtArea, fmtCzk } from '@/lib/format';
import { imageSrc, type ImageRef } from '@/lib/imageUrl';
import { useMapFeatureHover } from '@/lib/useMapFeatureHover';
import MapImagePreview from '../MapImagePreview';

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
  /* Photos per comparable sreality_id — fed by the existing imagesQ in
   * ComparablesSection (ImagePublic satisfies ImageRef structurally). */
  imagesById: ReadonlyMap<number, ImageRef[]>;
  /* Cross-pane hover sync (scalar — the estimation map has no clusters):
   * a table-row hover sets this to light the matching pin; a pin hover is
   * pushed out via onHover to light the matching row. */
  hoveredId: number | null;
  onHover: (id: number | null) => void;
  /* Pin click → open the full comparable modal. */
  onPick?: (sreality_id: number) => void;
}

const EMPTY_SET: ReadonlySet<number> = new Set<number>();
const CARD_W = 224; // matches MapImagePreview w-56
const CARD_H = 232; // ~4:3 image (168) + meta block

export default function ComparablesMap({
  subject,
  comparables,
  imagesById,
  hoveredId,
  onHover,
  onPick,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const onPickRef = useRef(onPick);
  const onHoverRef = useRef(onHover);
  const closeTimerRef = useRef<number | null>(null);
  const pinHoverRef = useRef<ComparablePoint | null>(null);
  const [ready, setReady] = useState(false);
  /* The pin currently under the cursor — drives the image preview card. Kept
   * separate from `hoveredId` (the controlled cross-pane id): the preview only
   * appears for a map-origin hover, never when a table row is hovered. */
  const [pinHover, setPinHover] = useState<ComparablePoint | null>(null);
  const [previewPos, setPreviewPos] = useState<{ x: number; y: number } | null>(null);

  useEffect(() => {
    onPickRef.current = onPick;
  }, [onPick]);
  useEffect(() => {
    onHoverRef.current = onHover;
  }, [onHover]);

  const clearCloseTimer = useCallback(() => {
    if (closeTimerRef.current != null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
  }, []);

  /* Hover-bridge close: a small delay so the cursor can travel from the pin to
   * the preview card without it vanishing; the card's onMouseEnter cancels it. */
  const scheduleClose = useCallback(() => {
    clearCloseTimer();
    closeTimerRef.current = window.setTimeout(() => {
      closeTimerRef.current = null;
      pinHoverRef.current = null;
      setPinHover(null);
      onHoverRef.current?.(null);
    }, 140);
  }, [clearCloseTimer]);

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
      map.addSource('hover-halo', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });

      map.addLayer({
        id: 'comparables-point',
        type: 'circle',
        source: 'comparables',
        paint: {
          /* feature-state.hovered bumps the dot + paints the ochre stroke —
           * the same "reads as selected" highlight Browse uses. */
          'circle-radius': [
            'case',
            ['boolean', ['feature-state', 'hovered'], false], 9,
            7,
          ],
          'circle-color': '#3c6e63',
          'circle-stroke-color': [
            'case',
            ['boolean', ['feature-state', 'hovered'], false], '#b58438',
            '#ffffff',
          ],
          'circle-stroke-width': [
            'case',
            ['boolean', ['feature-state', 'hovered'], false], 3,
            2,
          ],
          'circle-opacity': 0.95,
        },
      });

      /* Locator halo (glow + ring + dot) — the table→map "where is it" answer,
       * mirrors Browse's hover-halo. Only populated for a table-origin hover. */
      map.addLayer({
        id: 'hover-halo-glow',
        type: 'circle',
        source: 'hover-halo',
        paint: { 'circle-radius': 22, 'circle-color': '#b58438', 'circle-blur': 1, 'circle-opacity': 0.4 },
      });
      map.addLayer({
        id: 'hover-halo-ring',
        type: 'circle',
        source: 'hover-halo',
        paint: {
          'circle-radius': 12,
          'circle-opacity': 0,
          'circle-stroke-color': '#b58438',
          'circle-stroke-width': 2.5,
        },
      });
      map.addLayer({
        id: 'hover-halo-dot',
        type: 'circle',
        source: 'hover-halo',
        paint: {
          'circle-radius': 3,
          'circle-color': '#b58438',
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 1,
        },
      });

      map.addLayer({
        id: 'subject-halo',
        type: 'circle',
        source: 'subject',
        paint: { 'circle-radius': 14, 'circle-color': '#b6612d', 'circle-opacity': 0.18 },
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

      const onEnter = (e: maplibregl.MapLayerMouseEvent) => {
        map.getCanvas().style.cursor = 'pointer';
        const f = e.features?.[0];
        const id = f?.id;
        if (typeof id !== 'number') return;
        clearCloseTimer();
        if (pinHoverRef.current?.sreality_id === id) return;
        const p = f!.properties as unknown as ComparablePoint;
        pinHoverRef.current = p;
        setPinHover(p);
        onHoverRef.current?.(id);
      };
      map.on('mouseenter', 'comparables-point', onEnter);
      map.on('mousemove', 'comparables-point', onEnter);
      map.on('mouseleave', 'comparables-point', () => {
        map.getCanvas().style.cursor = '';
        scheduleClose();
      });

      map.on('click', 'comparables-point', (e) => {
        const id = e.features?.[0]?.id;
        if (typeof id === 'number') onPickRef.current?.(id);
      });

      setReady(true);
    });

    return () => {
      clearCloseTimer();
      map.remove();
      mapRef.current = null;
    };
  }, [subject.lat, subject.lng, clearCloseTimer, scheduleClose]);

  useEffect(() => {
    if (!ready || !mapRef.current) return;
    const src = mapRef.current.getSource('comparables') as GeoJSONSource | undefined;
    if (!src) return;
    src.setData({
      type: 'FeatureCollection',
      features: comparables.map((c) => ({
        type: 'Feature',
        id: c.sreality_id,
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

  /* Drive the pin highlight from pinHover first so the dot stays lit while the
   * cursor is on the preview card (where `hoveredId` may momentarily clear). */
  const effHovered = useMemo<ReadonlySet<number>>(() => {
    const id = pinHover?.sreality_id ?? hoveredId;
    return id == null ? EMPTY_SET : new Set([id]);
  }, [pinHover, hoveredId]);
  useMapFeatureHover(mapRef.current, ready, 'comparables', effHovered, comparables);

  /* Locator halo: only for a table-origin hover (an id that isn't the one the
   * cursor is on) — a pin-origin hover already has the cursor on the spot. */
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map) return;
    const src = map.getSource('hover-halo') as GeoJSONSource | undefined;
    if (!src) return;
    const tableId =
      hoveredId != null && hoveredId !== pinHover?.sreality_id ? hoveredId : null;
    const c = tableId != null ? comparables.find((x) => x.sreality_id === tableId) : undefined;
    src.setData({
      type: 'FeatureCollection',
      features: c
        ? [{ type: 'Feature', geometry: { type: 'Point', coordinates: [c.lng, c.lat] }, properties: {} }]
        : [],
    });
  }, [hoveredId, pinHover, comparables, ready]);

  /* Glue the preview card to the pin: project lng/lat → screen pixels, and keep
   * it pinned as the map pans/zooms while the card is open. */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !pinHover) {
      setPreviewPos(null);
      return;
    }
    const update = () => {
      const p = map.project([pinHover.lng, pinHover.lat]);
      setPreviewPos({ x: p.x, y: p.y });
    };
    update();
    map.on('move', update);
    return () => {
      map.off('move', update);
    };
  }, [pinHover]);

  const previewUrls = useMemo(
    () => (pinHover ? (imagesById.get(pinHover.sreality_id) ?? []).map(imageSrc) : []),
    [pinHover, imagesById],
  );

  let cardStyle: { left: number; top: number } | null = null;
  if (pinHover && previewPos) {
    const cw = containerRef.current?.clientWidth ?? 600;
    const ch = containerRef.current?.clientHeight ?? 320;
    const left =
      previewPos.x + 16 + CARD_W > cw ? previewPos.x - 16 - CARD_W : previewPos.x + 16;
    const top = Math.max(8, Math.min(previewPos.y - CARD_H / 2, ch - CARD_H - 8));
    cardStyle = { left, top };
  }

  return (
    <div className="relative h-80 rounded-[var(--radius-md)] overflow-hidden border border-[var(--color-rule)]">
      <div
        ref={containerRef}
        className="absolute inset-0"
        style={{ position: 'absolute', top: 0, right: 0, bottom: 0, left: 0, width: '100%', height: '100%' }}
      />
      {pinHover && cardStyle && (
        <div className="absolute z-30" style={cardStyle}>
          <MapImagePreview
            urls={previewUrls}
            price={fmtCzk(pinHover.price_czk)}
            meta={`${pinHover.disposition ?? '—'} · ${fmtArea(pinHover.area_m2)}`}
            district={pinHover.district}
            onMouseEnter={() => {
              clearCloseTimer();
              onHoverRef.current?.(pinHover.sreality_id);
            }}
            onMouseLeave={scheduleClose}
          />
        </div>
      )}
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
