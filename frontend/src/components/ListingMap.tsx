import { useEffect, useRef, useState } from 'react';
import maplibregl, { type GeoJSONSource } from 'maplibre-gl';
import type { MapRow } from '@/lib/queries';
import type { CenterRadius, MapBounds } from '@/lib/filters';
import { fmtCzk, fmtArea, fmtRelative, fmtAbsolute } from '@/lib/format';

/* Polygon approximation of a metres-radius circle around (lat, lng).
 * Same haversine ring the small <LocationControl> uses — 96 points is
 * smooth enough to read as a circle while staying cheap. */
const CENTER_CIRCLE_POINTS = 96;
const EARTH_RADIUS_M = 6_371_000;

const centerRadiusPolygon = (
  cr: CenterRadius,
): GeoJSON.Feature<GeoJSON.Polygon> => {
  const latRad = (cr.lat * Math.PI) / 180;
  const coords: [number, number][] = [];
  for (let i = 0; i <= CENTER_CIRCLE_POINTS; i++) {
    const theta = (i / CENTER_CIRCLE_POINTS) * 2 * Math.PI;
    const dx = cr.radius_m * Math.cos(theta);
    const dy = cr.radius_m * Math.sin(theta);
    const dLng =
      (dx / (EARTH_RADIUS_M * Math.cos(latRad))) * (180 / Math.PI);
    const dLat = (dy / EARTH_RADIUS_M) * (180 / Math.PI);
    coords.push([cr.lng + dLng, cr.lat + dLat]);
  }
  return {
    type: 'Feature',
    properties: {},
    geometry: { type: 'Polygon', coordinates: [coords] },
  };
};

const emptyCenterCircle: GeoJSON.Feature<GeoJSON.Polygon> = {
  type: 'Feature',
  properties: {},
  geometry: { type: 'Polygon', coordinates: [[]] },
};

const TILE_STYLE = 'https://tiles.openfreemap.org/styles/positron';
const PRAGUE = { lng: 14.4378, lat: 50.0755, zoom: 9.5 };
/* Below this zoom only the round point dot is shown; at and above it
 * each listing's price label is also drawn. Tuned so the labels appear
 * roughly when a single city block is on screen — close enough that the
 * labels don't pile up but far enough to still see a neighbourhood. */
const PRICE_LABEL_MIN_ZOOM = 13;

const czPriceCompact = new Intl.NumberFormat('cs-CZ', {
  notation: 'compact',
  maximumFractionDigits: 1,
});

/* Pre-formats a compact Kč price into the GeoJSON feature properties so
 * the map symbol layer can use it directly via ['get', 'price_label'].
 * Listings without a price are blanked rather than dropped — the dot
 * still anchors them on the map. */
const formatPriceLabel = (n: number | null): string => {
  if (n == null) return '';
  return `${czPriceCompact.format(n)} Kč`;
};

type MapFeatureProps = MapRow & { price_label: string };
type FC = GeoJSON.FeatureCollection<GeoJSON.Point, MapFeatureProps>;

const toFeatureCollection = (rows: MapRow[]): FC => ({
  type: 'FeatureCollection',
  features: rows.map((r) => ({
    type: 'Feature',
    /* Stable feature id lets maplibre's setFeatureState target this
     * point even after the source data is replaced — that's what
     * powers the cross-source hover highlight (cards / table → map). */
    id: r.sreality_id,
    geometry: { type: 'Point', coordinates: [r.lng, r.lat] },
    properties: { ...r, price_label: formatPriceLabel(r.price_czk) },
  })),
});

interface Props {
  rows: MapRow[];
  total: number | null;
  capped: boolean;
  isLoading: boolean;
  /* Bounds the URL says the map should be showing. The map applies it
   * once on mount and then ignores future updates — it's the source
   * of truth for its own viewport. */
  bounds: MapBounds | null;
  /* Fires on user-driven pan/zoom (we ignore programmatic moves).
   * `null` means "the operator cleared the map area" (Reset-view).
   * Suppressed when `locationMode === 'center_radius'`: the
   * spatial predicate comes from the sidebar's centre+radius
   * widget, not the viewport. */
  onBoundsChange?: (b: MapBounds | null) => void;
  /* Cross-source hover sync. Listings whose ids appear here render
   * with the highlight paint expressions; the map pushes its own
   * mouseenter/mouseleave events outward through onHover. */
  hoveredIds: ReadonlySet<number>;
  onHover: (ids: ReadonlyArray<number> | null) => void;
  /* When set (i.e. the operator chose centre+radius mode in the
   * sidebar) the map renders a dashed copper circle around the
   * point so the cohort's geographic scope is visible. The circle
   * is purely visual — the cohort filtering happens client-side
   * via an approximate bbox in queries.effectiveBbox. */
  centerCircle: CenterRadius | null;
}

export default function ListingMap({
  rows,
  total,
  capped,
  isLoading,
  bounds,
  onBoundsChange,
  hoveredIds,
  onHover,
  centerCircle,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const popupRef = useRef<maplibregl.Popup | null>(null);
  const [ready, setReady] = useState(false);
  /* The map fits to results exactly once — the first time a non-empty
   * row set arrives after the map is ready. Subsequent filter changes
   * never re-zoom, so the operator stays anchored on whatever area
   * they're examining. The Reset-view control offers an explicit
   * opt-in if they want to widen back to the full cohort. */
  const didInitialFitRef = useRef(false);
  /* Latest onBoundsChange handler stashed in a ref so the maplibre
   * `moveend` listener (registered once at mount time) always reads
   * the current callback without needing to rebind. */
  const onBoundsChangeRef = useRef(onBoundsChange);
  onBoundsChangeRef.current = onBoundsChange;
  /* Same ref trick for the hover emitter — the maplibre listeners
   * are registered once at mount and must keep reading the latest
   * callback without rebinding. */
  const onHoverRef = useRef(onHover);
  onHoverRef.current = onHover;
  /* When centre+radius mode is active the `moveend` handler must
   * skip emitting bounds — the cohort filters by the dashed circle,
   * not the viewport. Suppression flag rather than removing the
   * listener so toggling the mode at runtime doesn't require a
   * map remount. */
  const suppressBoundsRef = useRef(centerCircle != null);
  suppressBoundsRef.current = centerCircle != null;
  /* Tracks which feature ids currently carry feature-state.hovered =
   * true so we know which to clear before applying the next set. */
  const styledIdsRef = useRef<Set<number>>(new Set());
  /* Initial bbox from URL captured once at mount time — applying it
   * on the load event is what restores a shared link's exact viewport.
   * Reading the live `bounds` prop instead would refit every time the
   * URL changes (i.e. every pan), which defeats the point. */
  const initialBoundsRef = useRef<MapBounds | null>(bounds);

  useEffect(() => {
    if (!containerRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: TILE_STYLE,
      center: [PRAGUE.lng, PRAGUE.lat],
      zoom: PRAGUE.zoom,
      attributionControl: { compact: true },
    });
    mapRef.current = map;

    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');

    map.on('load', () => {
      map.addSource('listings', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
        cluster: true,
        clusterMaxZoom: 13,
        clusterRadius: 48,
      });

      map.addLayer({
        id: 'clusters',
        type: 'circle',
        source: 'listings',
        filter: ['has', 'point_count'],
        paint: {
          'circle-color': [
            'step', ['get', 'point_count'],
            'rgba(60, 110, 99, 0.35)',  10,
            'rgba(60, 110, 99, 0.55)',  50,
            'rgba(47, 87, 80, 0.75)',  200,
            'rgba(47, 87, 80, 0.90)',
          ],
          'circle-radius': [
            'step', ['get', 'point_count'],
            14, 10,
            18, 50,
            24, 200,
            32,
          ],
          'circle-stroke-color': 'rgba(60, 110, 99, 1)',
          'circle-stroke-width': 1,
        },
      });

      map.addLayer({
        id: 'cluster-count',
        type: 'symbol',
        source: 'listings',
        filter: ['has', 'point_count'],
        layout: {
          'text-field': ['get', 'point_count_abbreviated'],
          'text-font': ['Noto Sans Regular'],
          'text-size': 11,
        },
        paint: {
          'text-color': '#ffffff',
        },
      });

      map.addLayer({
        id: 'point',
        type: 'circle',
        source: 'listings',
        filter: ['!', ['has', 'point_count']],
        paint: {
          /* feature-state.hovered drives the bumped radius / ochre
           * stroke. Same paint values as the resting dot otherwise —
           * the highlight reads as "selected", not "different kind
           * of pin". */
          'circle-radius': [
            'case',
            ['boolean', ['feature-state', 'hovered'], false], 8,
            5,
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
            1.5,
          ],
          'circle-opacity': [
            'case',
            ['get', 'is_active'], 1, 0.55,
          ],
        },
      });

      /* Zoomed-in price labels. Drawn on top of the dot so the dot
       * stays the click target; the symbol layer is non-interactive.
       * `text-allow-overlap: false` plus the small padding lets MapLibre
       * thin out collisions automatically when listings stack. */
      map.addLayer({
        id: 'point-price',
        type: 'symbol',
        source: 'listings',
        filter: ['all', ['!', ['has', 'point_count']], ['!=', ['get', 'price_label'], '']],
        minzoom: PRICE_LABEL_MIN_ZOOM,
        layout: {
          'text-field': ['get', 'price_label'],
          'text-font': ['Noto Sans Bold'],
          'text-size': 11,
          'text-offset': [0, 0.9],
          'text-anchor': 'top',
          'text-padding': 2,
          'text-allow-overlap': false,
          'text-ignore-placement': false,
        },
        paint: {
          'text-color': '#2f5750',
          'text-halo-color': '#ffffff',
          'text-halo-width': 1.4,
          'text-halo-blur': 0.2,
          'text-opacity': [
            'case',
            ['get', 'is_active'], 1, 0.6,
          ],
        },
      });

      map.on('click', 'clusters', (e) => {
        const features = map.queryRenderedFeatures(e.point, { layers: ['clusters'] });
        const clusterId = features[0]?.properties?.cluster_id;
        if (clusterId == null) return;
        const src = map.getSource('listings') as GeoJSONSource;
        src.getClusterExpansionZoom(clusterId).then((zoom) => {
          const geom = features[0].geometry;
          if (geom.type !== 'Point') return;
          map.easeTo({
            center: geom.coordinates as [number, number],
            zoom,
          });
        });
      });

      map.on('mouseenter', 'clusters', (e) => {
        map.getCanvas().style.cursor = 'pointer';
        const f = e.features?.[0];
        const clusterId = f?.properties?.cluster_id as number | undefined;
        if (clusterId == null) return;
        /* Resolve every listing inside the cluster so all matching
         * cards / rows light up together — the "if there's still a
         * group at this zoom, highlight them all" requirement. */
        const src = map.getSource('listings') as GeoJSONSource;
        const pointCount = (f?.properties?.point_count as number | undefined) ?? 100;
        src.getClusterLeaves(clusterId, pointCount, 0)
          .then((leaves) => {
            const ids = leaves
              .map((leaf) => leaf.properties?.sreality_id as number | undefined)
              .filter((x): x is number => typeof x === 'number');
            onHoverRef.current?.(ids);
          })
          .catch(() => { /* swallow — leaves load may race with unmount */ });
      });
      map.on('mouseleave', 'clusters', () => {
        map.getCanvas().style.cursor = '';
        onHoverRef.current?.(null);
      });
      map.on('mouseenter', 'point', (e) => {
        map.getCanvas().style.cursor = 'pointer';
        const f = e.features?.[0];
        const id = f?.id as number | undefined;
        if (typeof id === 'number') onHoverRef.current?.([id]);
      });
      map.on('mouseleave', 'point', () => {
        map.getCanvas().style.cursor = '';
        onHoverRef.current?.(null);
      });

      map.on('click', 'point', (e) => {
        const f = e.features?.[0];
        if (!f || f.geometry.type !== 'Point') return;
        const props = f.properties as unknown as MapRow;
        popupRef.current?.remove();
        popupRef.current = new maplibregl.Popup({
          closeButton: true,
          closeOnClick: true,
          maxWidth: '280px',
          className: 'listing-popup',
        })
          .setLngLat(f.geometry.coordinates as [number, number])
          .setHTML(popupHtml(props))
          .addTo(map);
      });

      /* Centre+radius overlay. The polygon source carries an empty
       * ring when the operator hasn't picked a centre yet; the
       * effect below populates it when `centerCircle` is non-null. */
      map.addSource('center-circle', {
        type: 'geojson',
        data: emptyCenterCircle,
      });
      map.addLayer({
        id: 'center-circle-fill',
        type: 'fill',
        source: 'center-circle',
        paint: {
          'fill-color': '#b58438',
          'fill-opacity': 0.08,
        },
      });
      map.addLayer({
        id: 'center-circle-outline',
        type: 'line',
        source: 'center-circle',
        paint: {
          'line-color': '#b58438',
          'line-width': 1.5,
          'line-dasharray': [2, 2],
        },
      });

      setReady(true);

      /* Restore the exact viewport captured in the URL on mount.
       * Marks the initial-fit ref so the rows effect doesn't fight us
       * with its own fitBounds. */
      const initial = initialBoundsRef.current;
      if (initial) {
        map.fitBounds(
          [
            [initial.west, initial.south],
            [initial.east, initial.north],
          ],
          { padding: 0, duration: 0 },
        );
        didInitialFitRef.current = true;
      }
    });

    /* Only user-driven moveends propagate to the URL. Programmatic
     * fitBounds / easeTo calls produce events with `originalEvent ===
     * undefined`, which we skip — otherwise the initial-fit refit and
     * the Reset-view animation would both write to the URL.
     * Also skip when centre+radius mode is on; the dashed circle owns
     * the spatial predicate in that mode. */
    map.on('moveend', (e) => {
      if (e.originalEvent == null) return;
      if (suppressBoundsRef.current) return;
      const cb = onBoundsChangeRef.current;
      if (!cb) return;
      const b = map.getBounds();
      cb({
        west:  b.getWest(),
        south: b.getSouth(),
        east:  b.getEast(),
        north: b.getNorth(),
      });
    });

    return () => {
      popupRef.current?.remove();
      map.remove();
      mapRef.current = null;
    };
  }, []);

  // Sync the centre+radius overlay polygon whenever the prop changes.
  // No-op until the map's `load` event has run — until then there's
  // no `center-circle` source to call setData on.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource('center-circle') as
      | GeoJSONSource | undefined;
    if (!src) return;
    src.setData(
      centerCircle ? centerRadiusPolygon(centerCircle) : emptyCenterCircle,
    );
  }, [centerCircle, ready]);

  // Push fresh rows to the source whenever the filter result changes.
  // Only the very first non-empty result gets a fitBounds call; after
  // that, the user's pan/zoom is preserved across filter edits.
  useEffect(() => {
    if (!ready || !mapRef.current) return;
    const src = mapRef.current.getSource('listings') as GeoJSONSource | undefined;
    if (!src) return;
    const fc = toFeatureCollection(rows);
    src.setData(fc);

    if (rows.length === 0 || didInitialFitRef.current) return;
    const bounds = new maplibregl.LngLatBounds();
    for (const r of rows) bounds.extend([r.lng, r.lat]);
    mapRef.current.fitBounds(bounds, {
      padding: 56,
      maxZoom: 14,
      duration: 700,
    });
    didInitialFitRef.current = true;
  }, [rows, ready]);

  /* Project the shared hoveredIds set onto maplibre's feature-state
   * so the paint expressions on the 'point' layer light up. Clearing
   * the previous set before applying the new one keeps the styled
   * features in sync without scanning the whole source. */
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map) return;
    const prev = styledIdsRef.current;
    for (const id of prev) {
      if (!hoveredIds.has(id)) {
        map.setFeatureState({ source: 'listings', id }, { hovered: false });
      }
    }
    for (const id of hoveredIds) {
      if (!prev.has(id)) {
        map.setFeatureState({ source: 'listings', id }, { hovered: true });
      }
    }
    styledIdsRef.current = new Set(hoveredIds);
  }, [hoveredIds, ready, rows]);

  /* Reset-view clears the bbox URL param, which triggers the parent
   * to refetch the unbounded cohort. Once those rows arrive the
   * rows-effect below will refit because we also reset
   * didInitialFitRef. The actual map zoom happens reactively, not
   * imperatively, so the operator only ever sees one animation. */
  const resetView = () => {
    if (!mapRef.current) return;
    onBoundsChange?.(null);
    didInitialFitRef.current = false;
  };

  return (
    <div className="relative h-full min-h-[480px] rounded-[var(--radius-md)] overflow-hidden border border-[var(--color-rule)]">
      <div
        ref={containerRef}
        className="absolute inset-0"
        style={{ position: 'absolute', top: 0, right: 0, bottom: 0, left: 0, width: '100%', height: '100%' }}
      />
      <div className="pointer-events-none absolute top-3 left-3 right-3 flex items-start justify-between gap-3">
        <Pill>
          {isLoading
            ? 'Loading…'
            : total == null
              ? '—'
              : `${total.toLocaleString('cs-CZ')} ${total === 1 ? 'listing' : 'listings'}`}
          {capped && (
            <span className="ml-2 text-[var(--color-ochre)]">
              · capped at 50 000 — refine filters
            </span>
          )}
        </Pill>
        {bounds && (
          <button
            type="button"
            onClick={resetView}
            className="pointer-events-auto inline-flex items-center gap-1 px-2 py-1 text-[0.7rem] tracking-wide rounded-[var(--radius-sm)] bg-[var(--color-paper-3)]/95 backdrop-blur-sm border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:text-[var(--color-ink)] hover:border-[var(--color-rule-strong)] shadow-[0_2px_6px_rgba(0,0,0,0.04)] transition-colors"
            title="Clear the map area filter"
          >
            Show all
          </button>
        )}
      </div>
    </div>
  );
}

function Pill({ children }: { children: React.ReactNode }) {
  return (
    <span className="pointer-events-auto inline-flex items-center gap-1.5 px-2.5 py-1 text-[0.75rem] font-medium tracking-wide rounded-[var(--radius-sm)] bg-[var(--color-paper-3)]/95 backdrop-blur-sm border border-[var(--color-rule)] text-[var(--color-ink-2)] shadow-[0_2px_6px_rgba(0,0,0,0.04)] tabular-nums">
      {children}
    </span>
  );
}

function popupHtml(r: MapRow): string {
  const price = fmtCzk(r.price_czk);
  const area = fmtArea(r.area_m2);
  const disposition = r.disposition ?? '—';
  const district = r.district ?? '';
  const seen = fmtRelative(r.last_seen_at);
  const seenAbs = fmtAbsolute(r.last_seen_at);
  const inactive = !r.is_active;
  return `
    <div class="lp">
      <div class="lp-row">
        <p class="lp-price">${escape(price)}</p>
        ${inactive ? '<span class="lp-inactive">Inactive</span>' : ''}
      </div>
      <p class="lp-meta">
        <span class="lp-mono">${escape(disposition)}</span>
        <span class="lp-sep">·</span>
        <span class="lp-mono">${escape(area)}</span>
      </p>
      ${district ? `<p class="lp-district">${escape(district)}</p>` : ''}
      <p class="lp-seen" title="${escape(seenAbs)}">last seen ${escape(seen)}</p>
      <a href="/listing/${r.sreality_id}" class="lp-link">View details →</a>
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
