import { useEffect, useMemo, useRef, useState } from 'react';
import maplibregl, { type GeoJSONSource } from 'maplibre-gl';
import { createMap } from '@/lib/basemap';
import { useMapFeatureHover } from '@/lib/useMapFeatureHover';
import type {
  CityIndexDefinition,
  CuratedCity,
  MapRow,
  RentMapKraj,
  RentMapPolygon,
} from '@/lib/queries';
import type { CenterRadius, MapBounds } from '@/lib/filters';
import { groupForPicker, indexLabel, pinnedFirst } from '@/lib/cityIndexes';
import { fmtCzk, fmtArea, fmtRelative, fmtAbsolute } from '@/lib/format';
import { listingKindLabel } from '@/lib/enums';
import type { PriceStatDataset, PriceStatGrowthRow } from '@/lib/priceStats';
import {
  GROWTH_METRICS,
  GROWTH_METRIC_ORDER,
  GROWTH_NO_DATA,
  buildMetricByObec,
  formatGrowthMetric,
  growthToFeatureCollection,
  type GrowthMetric,
  type HoverData,
} from '@/lib/growthChoropleth';
import HoverChart from '@/components/HoverChart';
import { listingPath } from '@/lib/listingUrl';

const psgLayerId = (m: GrowthMetric) => `psg-${m}`;

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

/* Fallback shape for a curated city that has no municipality boundary
 * (every city has one today, but a future operator upload might add one
 * before its admin_boundary link is backfilled). Same haversine ring as
 * the centre+radius overlay, returned as a bare Polygon geometry. */
const circlePolygonGeom = (
  lat: number,
  lng: number,
  radiusM: number,
): GeoJSON.Polygon => {
  const latRad = (lat * Math.PI) / 180;
  const coords: [number, number][] = [];
  for (let i = 0; i <= CENTER_CIRCLE_POINTS; i++) {
    const theta = (i / CENTER_CIRCLE_POINTS) * 2 * Math.PI;
    const dx = radiusM * Math.cos(theta);
    const dy = radiusM * Math.sin(theta);
    const dLng = (dx / (EARTH_RADIUS_M * Math.cos(latRad))) * (180 / Math.PI);
    const dLat = (dy / EARTH_RADIUS_M) * (180 / Math.PI);
    coords.push([lng + dLng, lat + dLat]);
  }
  return { type: 'Polygon', coordinates: [coords] };
};

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

/* Imperative "fly to this place" command. `ts` is a monotonic stamp
 * (Date.now() at the originating event) so back-to-back picks of the
 * same place still trigger a flyTo — the prop's identity changes
 * even when lat/lng/zoom don't. */
export interface MapFlyToCommand {
  lat: number;
  lng: number;
  zoom?: number;
  ts: number;
}

/* Phase QUAL — one city's GeoJSON feature properties. The geometry is
 * the municipality boundary polygon. `value` is the active color-by-index
 * reading (or the -1 sentinel when no index is selected / the city has no
 * reading). `value_label` is the pre-formatted figure drawn inside the
 * shape (empty string → no label). `popup_html` backs the click popup. */
type CityFeatureProps = {
  city_id: number;
  name: string;
  kraj_name: string;
  population: number | null;
  /* -1 sentinel for "no reading" (MapLibre paint expressions can't
   * read literal null). The fill/line paint expressions gate on `< 0`
   * to render a neutral grey. */
  value: number;
  value_label: string;
  popup_html: string;
};

type CityFC = GeoJSON.FeatureCollection<
  GeoJSON.Polygon | GeoJSON.MultiPolygon,
  CityFeatureProps
>;

/* City choropleth ramp (red → yellow → green), shared by the fill and
 * the outline so the two never drift; matches the legend gradient in
 * CityMapControls and the popup highlight. Flattened inline into the
 * MapLibre `interpolate` expressions below. */
const CITY_INDEX_RAMP: ReadonlyArray<readonly [number, string]> = [
  [0, '#c0392b'],
  [5, '#f1c40f'],
  [10, '#2ecc71'],
];
/* Neutral grey for a city with no reading for the selected index (or no
 * index selected at all) — gated on the `< 0` value sentinel. */
const CITY_NULL_COLOR = '#8c9196';

/* -------------------------------------------------------------------------- */
/* MF rent-price choropleth ("Cenová mapa nájemného"). One polygon per Czech  */
/* obec / katastrální území, coloured by the selected size category's         */
/* reference rent (Kč/m²). VK1..VK4 is a SINGLE-select; switching VK does not */
/* refetch — it re-derives the `rent` property on each feature and re-setData.*/
/* -------------------------------------------------------------------------- */

export type RentVk = 1 | 2 | 3 | 4;

/* Radio labels mirror the official MF map exactly. */
const RENT_VK_LABELS: Record<RentVk, string> = {
  1: '1+kk, 1+1',
  2: '2+kk, 2+1',
  3: '3+kk, 3+1',
  4: '4+kk, 4+1',
};

/* Light-blue → indigo continuous ramp (Kč/m²), reproducing the MF scale.
 * The same stops feed both the MapLibre fill `interpolate` expression and
 * the inline legend gradient so the two never drift. */
const RENT_RAMP: ReadonlyArray<readonly [number, string]> = [
  [150, '#eaf0f6'],
  [250, '#9ec3e6'],
  [350, '#5a7fd6'],
  [450, '#3b4fb5'],
  [550, '#2a2a8a'],
  [600, '#1a1a5e'],
];

/* NULL rent → neutral grey (no reference value for that territory). */
const RENT_NULL_COLOR = 'rgba(140, 140, 140, 0.35)';

/* -1 sentinel for "no rent" — MapLibre paint expressions can't compare
 * against literal null, so the fill expression gates on `< 0`. */
const rentForVk = (p: RentMapPolygon, vk: RentVk): number | null => {
  switch (vk) {
    case 1: return p.vk1_per_m2;
    case 2: return p.vk2_per_m2;
    case 3: return p.vk3_per_m2;
    case 4: return p.vk4_per_m2;
  }
};

const czIntFmt = new Intl.NumberFormat('cs-CZ', { maximumFractionDigits: 0 });

type RentFeatureProps = {
  ruian_code: number;
  name: string;
  kraj: string;
  /* -1 sentinel for "no reference rent" (see rentForVk). */
  rent: number;
};

type RentFC = GeoJSON.FeatureCollection<
  GeoJSON.Polygon | GeoJSON.MultiPolygon,
  RentFeatureProps
>;

/* Parse each row's ST_AsGeoJSON geometry string once, attach the
 * selected-VK rent as a flat `rent` property, and build one
 * FeatureCollection. Rows whose geometry fails to parse are skipped
 * rather than crashing the whole layer. */
const toRentFC = (polygons: RentMapPolygon[], vk: RentVk): RentFC => ({
  type: 'FeatureCollection',
  features: polygons.flatMap((p) => {
    let geometry: GeoJSON.Geometry;
    try {
      geometry = JSON.parse(p.geojson) as GeoJSON.Geometry;
    } catch {
      return [];
    }
    if (geometry.type !== 'Polygon' && geometry.type !== 'MultiPolygon') {
      return [];
    }
    const rent = rentForVk(p, vk);
    return [{
      type: 'Feature' as const,
      id: p.ruian_code,
      geometry,
      properties: {
        ruian_code: p.ruian_code,
        name: p.name,
        kraj: p.kraj ?? '',
        rent: typeof rent === 'number' && Number.isFinite(rent) ? rent : -1,
      },
    }];
  }),
});

type KrajFC = GeoJSON.FeatureCollection<
  GeoJSON.Polygon | GeoJSON.MultiPolygon,
  { name: string }
>;

const toKrajFC = (kraje: RentMapKraj[]): KrajFC => ({
  type: 'FeatureCollection',
  features: kraje.flatMap((k) => {
    let geometry: GeoJSON.Geometry;
    try {
      geometry = JSON.parse(k.geojson) as GeoJSON.Geometry;
    } catch {
      return [];
    }
    if (geometry.type !== 'Polygon' && geometry.type !== 'MultiPolygon') {
      return [];
    }
    return [{
      type: 'Feature' as const,
      geometry,
      properties: { name: k.name },
    }];
  }),
});

const rentPopupHtml = (p: RentFeatureProps): string => {
  const rentText = p.rent >= 0 ? `${czIntFmt.format(p.rent)} Kč/m²` : '—';
  const place = p.kraj ? `${p.name}, ${p.kraj}` : p.name;
  return `
    <div class="lp">
      <p class="lp-kicker">Cenová mapa nájemného</p>
      <div class="lp-row">
        <p class="lp-price">${escape(p.name)}</p>
      </div>
      <p class="lp-meta">Obec: ${escape(place)}, Nájemné referenčního bytu: ${escape(rentText)}</p>
    </div>
  `;
};

interface Props {
  rows: MapRow[];
  /* Mappable cohort size — properties with coordinates (the map query filters
   * lat/lng NOT NULL). This is a SUBSET of cohortTotal, not a rival total. */
  total: number | null;
  /* The full cohort total (incl. coordinate-less properties), from the one
   * canonical count. When it exceeds `total` the pill shows "X of Y mapped"
   * so the map never silently understates the cohort. */
  cohortTotal: number | null;
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
  /* Which pane originated the current hover. The locator halo renders
   * only for 'list' hovers — echoing a map-origin hover back onto the
   * map would just redraw under the cursor (and a cluster hover would
   * drop dozens of halos at once). */
  hoverOrigin?: 'map' | 'list' | null;
  /* When set (i.e. the operator chose centre+radius mode in the
   * sidebar) the map renders a dashed copper circle around the
   * point so the cohort's geographic scope is visible. The circle
   * is purely visual — the cohort filtering happens client-side
   * via an approximate bbox in queries.effectiveBbox. */
  centerCircle: CenterRadius | null;
  /* Latest "fly to this place" command from outside (e.g. a District
   * typeahead pick). The map watches the prop's identity and animates
   * to (lat, lng, zoom) when it changes. Programmatic moves don't
   * write back to the URL bbox — the existing chip-based district
   * filter handles cohort narrowing. */
  flyTo?: MapFlyToCommand | null;
  /* Phase QUAL — curated-city overlay. The Browse page hands in the
   * subset of `curated_cities_public` that survives the active city-
   * quality filter (or the full ~206 when no filter is active). The
   * map renders them as a separate pin layer above the listing dots.
   * When `showCities` is false the layer is hidden but the source
   * stays loaded so toggling is instant. */
  cities?: CuratedCity[];
  /* Municipality boundary per city, `city_id` → raw ST_AsGeoJSON string
   * (JSON.parsed into the feature geometry). A city missing from the map
   * falls back to a radius circle around its centroid. */
  cityPolygons?: Map<number, string>;
  showCities?: boolean;
  /* If set, paint pins with a linear gradient between
   * `cityIndexDefinition.scale_min` (red) and `.scale_max` (green).
   * `cityIndexValues` is `{[city_id]: value}` for the chosen index;
   * cities missing a value render as a neutral grey dot. */
  colorByIndex?: CityIndexDefinition | null;
  cityIndexValues?: Map<number, number>;
  /* Every-index values for the currently-visible cities, keyed
   * `${city_id}:${index_name}` → value. Used to render the popup
   * detail when a pin is clicked. */
  cityIndexValuesAll?: Map<string, number>;
  cityIndexDefinitions?: CityIndexDefinition[];
  onToggleShowCities?: (next: boolean) => void;
  onColorByIndexChange?: (indexName: string | null) => void;
  /* MF rent-price choropleth ("Cenová mapa nájemného"). The Browse page
   * hands in the full ~7.6K territory polygons + the 14 kraj borders
   * (fetched once, cached forever) when the layer is enabled. The fill
   * sits BELOW the listing markers and city pins so those stay clickable
   * on top. `rentVk` selects which size category's reference rent colours
   * the choropleth; `showKraje` toggles the kraj boundary overlay. */
  rentMapPolygons?: RentMapPolygon[];
  rentMapKraje?: RentMapKraj[];
  showRentMap?: boolean;
  rentVk?: RentVk;
  showKraje?: boolean;
  onToggleShowRentMap?: (next: boolean) => void;
  onRentVkChange?: (vk: RentVk) => void;
  onToggleShowKraje?: (next: boolean) => void;
  /* Price-stats growth overlay ("Růst cen a nájmů"). The Browse page hands in
   * the per-obec growth rows for the chosen dataset + window (price_stat_growth
   * RPC), the dataset list for the picker, and the active metric. Fill sits
   * BELOW the listing markers + city pins. Mirrors the rent-map overlay. */
  growthRows?: PriceStatGrowthRow[];
  growthDatasets?: PriceStatDataset[];
  showGrowth?: boolean;
  growthDatasetId?: number | null;
  growthMetric?: GrowthMetric;
  growthFrom?: string;
  growthTo?: string;
  onToggleShowGrowth?: (next: boolean) => void;
  onGrowthDatasetChange?: (id: number) => void;
  onGrowthMetricChange?: (m: GrowthMetric) => void;
  onGrowthFromChange?: (ym: string) => void;
  onGrowthToChange?: (ym: string) => void;
  growthChartOnHover?: boolean;
  growthHoverData?: HoverData | null;
  onToggleGrowthChartOnHover?: (next: boolean) => void;
}

export default function ListingMap({
  rows,
  total,
  cohortTotal,
  capped,
  isLoading,
  bounds,
  onBoundsChange,
  hoveredIds,
  onHover,
  hoverOrigin = null,
  centerCircle,
  flyTo,
  cities,
  cityPolygons,
  showCities = true,
  colorByIndex,
  cityIndexValues,
  cityIndexValuesAll,
  cityIndexDefinitions,
  onToggleShowCities,
  onColorByIndexChange,
  rentMapPolygons,
  rentMapKraje,
  showRentMap = false,
  rentVk = 1,
  showKraje = false,
  onToggleShowRentMap,
  onRentVkChange,
  onToggleShowKraje,
  growthRows,
  growthDatasets,
  showGrowth = false,
  growthDatasetId = null,
  growthMetric = 'rent_cagr_pct',
  growthFrom = '',
  growthTo = '',
  onToggleShowGrowth,
  onGrowthDatasetChange,
  onGrowthMetricChange,
  onGrowthFromChange,
  onGrowthToChange,
  growthChartOnHover = false,
  growthHoverData = null,
  onToggleGrowthChartOnHover,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const popupRef = useRef<maplibregl.Popup | null>(null);
  const [ready, setReady] = useState(false);
  const psgChartHoverRef = useRef(growthChartOnHover);
  psgChartHoverRef.current = growthChartOnHover;
  const [psgHover, setPsgHover] = useState<{ obecId: number; name: string; x: number; y: number } | null>(null);
  /* Computed growth/yield figure per obec for the active metric — the same
   * value the choropleth fill + the Datasets table show, surfaced on the hover
   * chart. Memoized so the frequent hover-driven re-renders don't rebuild it. */
  const psgMetricByObec = useMemo(
    () => buildMetricByObec(growthRows ?? [], growthMetric),
    [growthRows, growthMetric],
  );
  /* City-overlay min-value threshold. When set, cities whose selected
   * color-by-index reading is below it render grey ("off"). Purely a map
   * visual, so it lives here rather than in URL/Browse state. Resets when
   * the color-by index changes — each index has its own scale. */
  const [colorByMin, setColorByMin] = useState<number | null>(null);
  useEffect(() => {
    setColorByMin(null);
  }, [colorByIndex?.index_name]);
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
  /* Initial bbox from URL captured once at mount time — applying it
   * on the load event is what restores a shared link's exact viewport.
   * Reading the live `bounds` prop instead would refit every time the
   * URL changes (i.e. every pan), which defeats the point. */
  const initialBoundsRef = useRef<MapBounds | null>(bounds);

  useEffect(() => {
    if (!containerRef.current) return;
    /* Scale sits bottom-LEFT (createMap default) so it never collides with the
     * OpenFreeMap attribution; the bottom-left city controls are lifted clear
     * of it below. */
    const map = createMap(containerRef.current, {
      center: [PRAGUE.lng, PRAGUE.lat],
      zoom: PRAGUE.zoom,
    });
    mapRef.current = map;

    map.on('load', () => {
      /* MF rent-price choropleth. Added FIRST of all overlays so it sits
       * UNDER the city pins and listing dots — it's a background layer.
       * Initially empty; the rent-map-data effect below populates the
       * GeoJSON when props arrive. Switching VK re-derives the per-feature
       * `rent` and re-setsData (no refetch). */
      map.addSource('rent-map', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });
      map.addLayer({
        id: 'rent-map-fill',
        type: 'fill',
        source: 'rent-map',
        layout: { visibility: 'none' },
        paint: {
          /* -1 sentinel → neutral grey; otherwise interpolate the shared
           * RENT_RAMP. Inline literal, matching the other paint
           * expressions in this file. */
          'fill-color': [
            'case',
            ['<', ['get', 'rent'], 0], RENT_NULL_COLOR,
            [
              'interpolate', ['linear'], ['get', 'rent'],
              ...RENT_RAMP.flatMap(([stop, color]) => [stop, color]),
            ],
          ],
          'fill-opacity': 0.62,
        },
      });
      map.addLayer({
        id: 'rent-map-line',
        type: 'line',
        source: 'rent-map',
        layout: { visibility: 'none' },
        paint: {
          'line-color': 'rgba(60, 60, 90, 0.25)',
          'line-width': 0.4,
        },
      });

      /* Optional kraj-boundary overlay (the "Kraje" checkbox). Stronger
       * line than the per-obec borders so region edges read clearly. */
      map.addSource('rent-kraje', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });
      map.addLayer({
        id: 'rent-kraje-line',
        type: 'line',
        source: 'rent-kraje',
        layout: { visibility: 'none' },
        paint: {
          'line-color': 'rgba(40, 40, 70, 0.55)',
          'line-width': 1.4,
        },
      });

      /* Price-stats growth overlay — one fill layer per metric, all hidden
       * until the data-effect flips the active one on. Same background slot as
       * the rent-map choropleth (below the listing dots + city pins). */
      map.addSource('ps-growth', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });
      for (const gm of GROWTH_METRIC_ORDER) {
        const cfg = GROWTH_METRICS[gm];
        map.addLayer({
          id: psgLayerId(gm),
          type: 'fill',
          source: 'ps-growth',
          layout: { visibility: 'none' },
          paint: {
            'fill-color': [
              'case',
              ['==', ['get', cfg.hasProp], 0], GROWTH_NO_DATA,
              ['interpolate', ['linear'], ['get', cfg.vProp],
                ...cfg.ramp.flatMap(([stop, color]) => [stop, color])],
            ],
            /* confident → full wash; thin (limited listings) → faded tint of
             * the same hue; no-data grey carries its own low alpha. */
            'fill-opacity': [
              'case',
              ['==', ['get', cfg.hasProp], 0], 0.7,
              ['==', ['get', cfg.thinProp], 1], 0.26,
              0.7,
            ],
          },
        });
      }
      map.addLayer({
        id: 'ps-growth-line',
        type: 'line',
        source: 'ps-growth',
        layout: { visibility: 'none' },
        paint: { 'line-color': 'rgba(26,28,34,0.16)', 'line-width': 0.4 },
      });
      for (const gm of GROWTH_METRIC_ORDER) {
        map.on('mousemove', psgLayerId(gm), (e) => {
          if (!psgChartHoverRef.current) return;
          const f = e.features?.[0];
          if (!f) return;
          map.getCanvas().style.cursor = 'pointer';
          setPsgHover({
            obecId: Number(f.id),
            name: String((f.properties as Record<string, unknown>).obec_name),
            x: e.point.x, y: e.point.y,
          });
        });
        map.on('mouseleave', psgLayerId(gm), () => {
          map.getCanvas().style.cursor = '';
          setPsgHover(null);
        });
      }

      /* Hover popup for the choropleth. The fill is the hit target;
       * mirrors the city-pin popup approach. Only wired here — the layer
       * visibility toggle handles whether it's actually reachable. */
      /* Listing dots and clusters always win the click. The fill layers
       * underneath (rent map, city polygons) only open their popups when
       * nothing from the listings source sits under the cursor — without
       * this, clicking a dot inside a polygon fired BOTH handlers and the
       * later-registered one replaced the listing popup with the city /
       * rent popup ("I clicked a listing and got city info"). */
      const listingUnderCursor = (p: maplibregl.PointLike): boolean =>
        map.queryRenderedFeatures(p, { layers: ['point', 'clusters'] })
          .length > 0;

      map.on('mouseenter', 'rent-map-fill', () => {
        map.getCanvas().style.cursor = 'pointer';
      });
      map.on('mouseleave', 'rent-map-fill', () => {
        map.getCanvas().style.cursor = '';
      });
      map.on('click', 'rent-map-fill', (e) => {
        if (listingUnderCursor(e.point)) return;
        const f = e.features?.[0];
        if (!f) return;
        const props = f.properties as unknown as RentFeatureProps;
        popupRef.current?.remove();
        popupRef.current = new maplibregl.Popup({
          closeButton: true,
          closeOnClick: true,
          maxWidth: '300px',
          className: 'listing-popup rent-map-popup',
        })
          .setLngLat(e.lngLat)
          .setHTML(rentPopupHtml(props))
          .addTo(map);
      });

      /* Phase QUAL — curated city overlay, drawn as municipality
       * boundary polygons. Added FIRST (and the value label LAST, just
       * after this block) so the listing dots render above the fill but
       * the index figure stays readable. Initially empty; the
       * cities-data effect below populates the GeoJSON when props arrive.
       *
       * `value` carries the selected index reading, or the -1 sentinel
       * when the city has no reading / no index is selected (MapLibre
       * paint can't compare against literal null). The shared
       * `interpolate` expression colours the fill (translucent) and the
       * outline (thicker, opaque) with the SAME tone, gating on `< 0`
       * for the neutral grey. Inlined in both layers (rather than shared
       * via a const) so each gets the layer's contextual paint typing —
       * the proven rent-map-fill pattern. */
      map.addSource('cities', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });
      /* Translucent same-tone fill. */
      map.addLayer({
        id: 'city-fill',
        type: 'fill',
        source: 'cities',
        paint: {
          'fill-color': [
            'case',
            ['<', ['get', 'value'], 0], CITY_NULL_COLOR,
            [
              'interpolate', ['linear'], ['get', 'value'],
              ...CITY_INDEX_RAMP.flatMap(([stop, color]) => [stop, color]),
            ],
          ],
          'fill-opacity': [
            'case',
            ['<', ['get', 'value'], 0], 0.12,
            0.32,
          ],
        },
      });
      /* Thick conditional-coloured border so each municipality stands
       * out. Split into two layers because line-dasharray cannot be
       * data-driven: cities WITH a reading keep the solid choropleth
       * stroke, cities WITHOUT one get the dashed administrative-
       * boundary stroke — the cartographic "this is territory, not a
       * listing" cue that holds even when the polygon shrinks to a
       * speck at country zoom. */
      map.addLayer({
        id: 'city-outline',
        type: 'line',
        source: 'cities',
        filter: ['>=', ['get', 'value'], 0],
        paint: {
          'line-color': [
            'interpolate', ['linear'], ['get', 'value'],
            ...CITY_INDEX_RAMP.flatMap(([stop, color]) => [stop, color]),
          ],
          'line-width': [
            'interpolate', ['linear'], ['zoom'],
            6, 1.6,
            10, 2.4,
            14, 3.2,
          ],
          'line-opacity': 0.9,
        },
      });
      map.addLayer({
        id: 'city-outline-null',
        type: 'line',
        source: 'cities',
        filter: ['<', ['get', 'value'], 0],
        paint: {
          'line-color': CITY_NULL_COLOR,
          'line-width': [
            'interpolate', ['linear'], ['zoom'],
            6, 1.6,
            10, 2.4,
            14, 3.2,
          ],
          'line-opacity': 0.55,
          'line-dasharray': [2, 1.6],
        },
      });
      /* The selected index figure, drawn at each municipality's
       * centroid. `value_label` is empty (→ no label) when no index is
       * selected or the city has no reading. Added before the listing
       * layers so it wins placement over the cluster counts; the white
       * halo keeps it legible where a listing cluster sits on top. */
      map.addLayer({
        id: 'city-label',
        type: 'symbol',
        source: 'cities',
        layout: {
          'text-field': ['get', 'value_label'],
          'text-font': ['Noto Sans Bold'],
          'text-size': [
            'interpolate', ['linear'], ['zoom'],
            6, 11,
            12, 14,
          ],
          'text-allow-overlap': false,
        },
        paint: {
          'text-color': '#1f2a28',
          'text-halo-color': '#ffffff',
          'text-halo-width': 1.6,
          'text-halo-blur': 0.3,
        },
      });

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

      /* Phase QUAL — city polygon interactions. Click pops up the city
       * card with population + every active index value, anchored where
       * the operator clicked (the fill is the hit target). Hover shows
       * pointer cursor; we do NOT push city hovers to onHover (which
       * is wired to the cross-source listing highlight). */
      map.on('mouseenter', 'city-fill', () => {
        map.getCanvas().style.cursor = 'pointer';
      });
      map.on('mouseleave', 'city-fill', () => {
        map.getCanvas().style.cursor = '';
      });
      map.on('click', 'city-fill', (e) => {
        if (listingUnderCursor(e.point)) return;
        const f = e.features?.[0];
        if (!f) return;
        const props = f.properties as unknown as CityFeatureProps;
        popupRef.current?.remove();
        popupRef.current = new maplibregl.Popup({
          closeButton: true,
          closeOnClick: true,
          maxWidth: '320px',
          className: 'listing-popup city-popup',
        })
          .setLngLat(e.lngLat)
          .setHTML(props.popup_html)
          .addTo(map);
      });

      /* Locator halo — the card→map "where is it" answer. A separate
       * (unclustered) source fed from the hovered rows' coordinates and
       * drawn above every listing layer, so the highlight lands at the
       * property's true position even when its dot is currently absorbed
       * into a cluster bubble: the ochre pool + ring sit on top of the
       * group and the pin-dot marks the exact spot inside it. */
      map.addSource('hover-halo', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });
      map.addLayer({
        id: 'hover-halo-glow',
        type: 'circle',
        source: 'hover-halo',
        paint: {
          'circle-radius': 22,
          'circle-color': '#b58438',
          'circle-blur': 1,
          'circle-opacity': 0.4,
        },
      });
      map.addLayer({
        id: 'hover-halo-ring',
        type: 'circle',
        source: 'hover-halo',
        paint: {
          'circle-radius': 11,
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

  /* MF rent choropleth — rebuild the `rent-map` FeatureCollection when
   * the polygons, the selected VK, or the enabled flag changes. Switching
   * VK re-derives the flat `rent` property and re-setsData; the data is
   * already loaded so this never refetches. Visibility toggling keeps the
   * source loaded so flipping the layer on/off is instant. */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource('rent-map') as GeoJSONSource | undefined;
    if (!src) return;
    const polygons = rentMapPolygons ?? [];
    if (showRentMap && polygons.length > 0) {
      src.setData(toRentFC(polygons, rentVk));
    }
    const vis = showRentMap && polygons.length > 0 ? 'visible' : 'none';
    if (map.getLayer('rent-map-fill')) {
      map.setLayoutProperty('rent-map-fill', 'visibility', vis);
    }
    if (map.getLayer('rent-map-line')) {
      map.setLayoutProperty('rent-map-line', 'visibility', vis);
    }
  }, [rentMapPolygons, rentVk, showRentMap, ready]);

  /* MF rent choropleth — kraj-boundary overlay. Independent of VK; gated
   * on both the rent map being shown AND the "Kraje" checkbox. */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource('rent-kraje') as GeoJSONSource | undefined;
    if (!src) return;
    const kraje = rentMapKraje ?? [];
    if (kraje.length > 0) {
      src.setData(toKrajFC(kraje));
    }
    if (map.getLayer('rent-kraje-line')) {
      map.setLayoutProperty(
        'rent-kraje-line',
        'visibility',
        showRentMap && showKraje && kraje.length > 0 ? 'visible' : 'none',
      );
    }
  }, [rentMapKraje, showRentMap, showKraje, ready]);

  /* Price-stats growth overlay — rebuild the FeatureCollection on data change,
   * and show only the active-metric fill layer when the overlay is enabled. */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource('ps-growth') as GeoJSONSource | undefined;
    if (!src) return;
    const data = growthRows ?? [];
    if (showGrowth && data.length > 0) src.setData(growthToFeatureCollection(data));
    const on = showGrowth && data.length > 0;
    for (const gm of GROWTH_METRIC_ORDER) {
      if (map.getLayer(psgLayerId(gm))) {
        map.setLayoutProperty(
          psgLayerId(gm), 'visibility',
          on && gm === growthMetric ? 'visible' : 'none',
        );
      }
    }
    if (map.getLayer('ps-growth-line')) {
      map.setLayoutProperty('ps-growth-line', 'visibility', on ? 'visible' : 'none');
    }
  }, [growthRows, showGrowth, growthMetric, ready]);

  /* Phase QUAL — push the filtered city set into the `cities` source
   * whenever the operator changes the city-quality filter, the color-
   * by-index, the boundary polygons, or the underlying data. The shared
   * `interpolate` paint expression reads `properties.value`, so a change
   * of colorByIndex is implemented as a fresh setData with new values +
   * labels. Geometry is the municipality boundary (JSON.parsed from the
   * ST_AsGeoJSON string), falling back to a radius circle if a city has
   * no boundary. */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource('cities') as GeoJSONSource | undefined;
    if (!src) return;
    const list = cities ?? [];
    const idxVals = cityIndexValues ?? new Map<number, number>();
    const allDefs = cityIndexDefinitions ?? [];
    const allVals = cityIndexValuesAll ?? new Map<string, number>();
    const polys = cityPolygons ?? new Map<number, string>();
    /* MapLibre paint expressions can't read literal null, so we use
     * -1 as the "no reading" sentinel. The fill/line paint checks `< 0`
     * and switches to a neutral grey; `value_label` is left blank in
     * that case (or when no index is selected) so no figure is drawn. */
    const fc: CityFC = {
      type: 'FeatureCollection',
      features: list.map((c) => {
        let geometry: GeoJSON.Polygon | GeoJSON.MultiPolygon | null = null;
        const raw = polys.get(c.city_id);
        if (raw) {
          try {
            const parsed = JSON.parse(raw) as GeoJSON.Geometry;
            if (parsed.type === 'Polygon' || parsed.type === 'MultiPolygon') {
              geometry = parsed;
            }
          } catch {
            geometry = null;
          }
        }
        if (!geometry) {
          geometry = circlePolygonGeom(c.lat, c.lng, c.default_radius_m);
        }
        const v = idxVals.get(c.city_id);
        const hasReading = typeof v === 'number' && Number.isFinite(v);
        /* Below the operator's min threshold → treat as "no reading" so
         * it falls onto the existing grey path (grey fill, no label). */
        const passesMin =
          hasReading && (colorByMin == null || (v as number) >= colorByMin);
        const value = passesMin ? (v as number) : -1;
        const valueLabel = colorByIndex && passesMin ? value.toFixed(1) : '';
        return {
          type: 'Feature',
          id: c.city_id,
          geometry,
          properties: {
            city_id: c.city_id,
            name: c.name,
            kraj_name: c.kraj_name,
            population: c.population,
            value,
            value_label: valueLabel,
            popup_html: cityPopupHtml(c, allDefs, allVals, colorByIndex ?? null),
          },
        };
      }),
    };
    src.setData(fc);

    /* Toggle layer visibility. The source data stays loaded so flipping
     * `showCities` is instant. Also gate visibility on whether there
     * are any cities to show — avoids drawing a stale empty layer
     * during the initial load. */
    const vis = showCities && list.length > 0 ? 'visible' : 'none';
    for (const id of ['city-fill', 'city-outline', 'city-outline-null', 'city-label']) {
      if (map.getLayer(id)) {
        map.setLayoutProperty(id, 'visibility', vis);
      }
    }
  }, [
    cities,
    cityPolygons,
    showCities,
    colorByIndex,
    colorByMin,
    cityIndexValues,
    cityIndexValuesAll,
    cityIndexDefinitions,
    ready,
  ]);

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

  /* Imperative flyTo on each new command (identity-based via ts).
   * Marks didInitialFitRef so the rows-effect doesn't subsequently
   * fitBounds-over the freshly-flown viewport. The rows refetch from
   * URL filters (notably the district chip the typeahead also added)
   * keeps the on-screen pins in sync with the new place. */
  const lastFlyToTsRef = useRef<number | null>(null);
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map || !flyTo) return;
    if (lastFlyToTsRef.current === flyTo.ts) return;
    lastFlyToTsRef.current = flyTo.ts;
    map.flyTo({
      center: [flyTo.lng, flyTo.lat],
      zoom: flyTo.zoom ?? map.getZoom(),
      duration: 700,
    });
    didInitialFitRef.current = true;
  }, [flyTo, ready]);

  /* Defensive resize once the map is ready. MapLibre observes its container,
   * but a map mounted into a container that settles its size after first paint
   * (e.g. the "Explore area" modal panel) can initialise mis-sized; a single
   * next-frame resize covers that. No-op on the Browse page (already sized). */
  useEffect(() => {
    if (!ready || !mapRef.current) return;
    const id = requestAnimationFrame(() => mapRef.current?.resize());
    return () => cancelAnimationFrame(id);
  }, [ready]);

  /* Project the shared hoveredIds set onto maplibre's feature-state so the
   * 'point' layer paint expressions light up. Extracted to a shared hook so the
   * estimation comparables map rides the same mechanism; `rows` re-applies the
   * state after the source data is replaced. */
  useMapFeatureHover(mapRef.current, ready, 'listings', hoveredIds, rows);

  /* Locator halo data: list-origin hovers project the hovered rows'
   * coordinates into the halo source; everything else clears it (a
   * map-origin hover means the cursor is already on the spot). */
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map) return;
    const src = map.getSource('hover-halo') as GeoJSONSource | undefined;
    if (!src) return;
    const features: GeoJSON.Feature<GeoJSON.Point>[] =
      hoverOrigin === 'list' && hoveredIds.size > 0
        ? rows
            .filter((r) => hoveredIds.has(r.sreality_id))
            .map((r) => ({
              type: 'Feature',
              geometry: { type: 'Point', coordinates: [r.lng, r.lat] },
              properties: {},
            }))
        : [];
    src.setData({ type: 'FeatureCollection', features });
  }, [hoveredIds, hoverOrigin, rows, ready]);

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
      {growthChartOnHover && showGrowth && psgHover && growthHoverData?.byObec.get(psgHover.obecId) && (
        <div
          className="pointer-events-none absolute z-20"
          style={{
            left: psgHover.x + 270 > (containerRef.current?.clientWidth ?? 900) ? psgHover.x - 262 : psgHover.x + 14,
            top: psgHover.y + 200 > (containerRef.current?.clientHeight ?? 560) ? psgHover.y - 190 : psgHover.y + 14,
          }}
        >
          <HoverChart
            title={psgHover.name}
            points={growthHoverData.byObec.get(psgHover.obecId)!}
            xMin={growthHoverData.xMin} xMax={growthHoverData.xMax}
            yMin={growthHoverData.yMin} yMax={growthHoverData.yMax}
            valueLabel={growthHoverData.valueLabel}
            format={growthHoverData.format}
            metricLabel={GROWTH_METRICS[growthMetric].label}
            metricValue={psgMetricByObec.get(psgHover.obecId)?.value ?? null}
            metricTier={psgMetricByObec.get(psgHover.obecId)?.tier ?? 0}
            metricFormat={(v) => formatGrowthMetric(growthMetric, v)}
          />
        </div>
      )}
      <div className="pointer-events-none absolute top-3 left-3 right-3 flex items-start justify-between gap-3">
        <Pill>
          {isLoading
            ? 'Loading…'
            : total == null
              ? '—'
              : cohortTotal != null && cohortTotal > total
                ? `${total.toLocaleString('cs-CZ')} of ${cohortTotal.toLocaleString('cs-CZ')} mapped`
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
      {cities && cities.length > 0 && (
        <CityMapControls
          showCities={showCities}
          onToggleShowCities={onToggleShowCities}
          colorByIndex={colorByIndex ?? null}
          cityIndexDefinitions={cityIndexDefinitions ?? []}
          onColorByIndexChange={onColorByIndexChange}
          colorByMin={colorByMin}
          onColorByMinChange={setColorByMin}
          cityCount={cities.length}
        />
      )}
      {/* Bottom-right overlay stack: growth control sits ABOVE the rent-map
          control. Both are flex children here so they never overlap. */}
      <div className="pointer-events-none absolute bottom-10 right-3 flex flex-col gap-2 items-end">
        <GrowthMapControls
          showGrowth={showGrowth}
          datasets={growthDatasets ?? []}
          datasetId={growthDatasetId}
          metric={growthMetric}
          from={growthFrom}
          to={growthTo}
          rowCount={growthRows?.length ?? 0}
          onToggle={onToggleShowGrowth}
          onDatasetChange={onGrowthDatasetChange}
          onMetricChange={onGrowthMetricChange}
          onFromChange={onGrowthFromChange}
          onToChange={onGrowthToChange}
          chartOnHover={growthChartOnHover}
          onToggleChartOnHover={onToggleGrowthChartOnHover}
        />
        <RentMapControls
          showRentMap={showRentMap}
          rentVk={rentVk}
          showKraje={showKraje}
          polygonCount={rentMapPolygons?.length ?? 0}
          onToggleShowRentMap={onToggleShowRentMap}
          onRentVkChange={onRentVkChange}
          onToggleShowKraje={onToggleShowKraje}
        />
      </div>
    </div>
  );
}

const PSG_FIRST_YEAR = 2015;
const PSG_NOW = new Date();
const PSG_YEARS = Array.from(
  { length: PSG_NOW.getFullYear() - PSG_FIRST_YEAR + 1 },
  (_, i) => String(PSG_FIRST_YEAR + i),
);
const PSG_MONTHS = Array.from({ length: 12 }, (_, i) => String(i + 1).padStart(2, '0'));
const PSG_SELECT_CLS =
  'text-[0.7rem] bg-[var(--color-paper-2)] border border-[var(--color-rule)] rounded px-1 py-0.5';

function PsgYmPicker({ value, onChange }: { value: string; onChange?: (v: string) => void }) {
  const [y, m] = (value || `${PSG_FIRST_YEAR}-01`).split('-');
  return (
    <span className="inline-flex items-center gap-0.5">
      <select value={y} onChange={(e) => onChange?.(`${e.target.value}-${m}`)} className={PSG_SELECT_CLS}>
        {PSG_YEARS.map((yr) => <option key={yr} value={yr}>{yr}</option>)}
      </select>
      <select value={m} onChange={(e) => onChange?.(`${y}-${e.target.value}`)} className={PSG_SELECT_CLS}>
        {PSG_MONTHS.map((mo) => <option key={mo} value={mo}>{mo}</option>)}
      </select>
    </span>
  );
}

function GrowthMapControls({
  showGrowth, datasets, datasetId, metric, from, to, rowCount,
  onToggle, onDatasetChange, onMetricChange, onFromChange, onToChange,
  chartOnHover, onToggleChartOnHover,
}: {
  showGrowth: boolean;
  datasets: PriceStatDataset[];
  datasetId: number | null;
  metric: GrowthMetric;
  from: string;
  to: string;
  rowCount: number;
  onToggle?: (next: boolean) => void;
  onDatasetChange?: (id: number) => void;
  onMetricChange?: (m: GrowthMetric) => void;
  onFromChange?: (ym: string) => void;
  onToChange?: (ym: string) => void;
  chartOnHover?: boolean;
  onToggleChartOnHover?: (next: boolean) => void;
}) {
  const cfg = GROWTH_METRICS[metric];
  const gradient = `linear-gradient(to right, ${cfg.ramp.map(([, c]) => c).join(', ')})`;
  return (
    <div className="flex flex-col gap-2 items-end">
      <div className="pointer-events-auto flex items-center gap-2 px-2.5 py-1.5 rounded-[var(--radius-sm)] bg-[var(--color-paper-3)]/95 backdrop-blur-sm border border-[var(--color-rule)] shadow-[0_2px_6px_rgba(0,0,0,0.04)]">
        <label className="inline-flex items-center gap-1.5 text-[0.75rem] text-[var(--color-ink-2)] cursor-pointer">
          <input type="checkbox" checked={showGrowth} onChange={(e) => onToggle?.(e.target.checked)} />
          <span>Růst cen a nájmů</span>
        </label>
      </div>
      {showGrowth && (
        <div className="pointer-events-auto flex flex-col gap-2 px-2.5 py-2 rounded-[var(--radius-sm)] bg-[var(--color-paper-3)]/95 backdrop-blur-sm border border-[var(--color-rule)] shadow-[0_2px_6px_rgba(0,0,0,0.04)] min-w-[210px]">
          {datasets.length === 0 ? (
            <span className="text-[0.7rem] text-[var(--color-ink-3)]">Žádné datasety</span>
          ) : (
            <select
              className={PSG_SELECT_CLS + ' max-w-[200px]'}
              value={datasetId ?? ''}
              onChange={(e) => onDatasetChange?.(Number(e.target.value))}
            >
              {datasets.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
          )}
          <div className="flex flex-col gap-1">
            {GROWTH_METRIC_ORDER.map((gm) => (
              <label key={gm} className="inline-flex items-center gap-1.5 text-[0.75rem] text-[var(--color-ink-2)] cursor-pointer">
                <input type="radio" name="psg-metric" checked={metric === gm} onChange={() => onMetricChange?.(gm)} />
                <span>{GROWTH_METRICS[gm].label}</span>
              </label>
            ))}
          </div>
          <div className="flex items-center gap-1 border-t border-[var(--color-rule)] pt-1.5 text-[0.7rem] text-[var(--color-ink-2)]">
            <PsgYmPicker value={from} onChange={onFromChange} />
            <span className="text-[var(--color-ink-3)]">→</span>
            <PsgYmPicker value={to} onChange={onToChange} />
          </div>
          <label className="inline-flex items-center gap-1.5 text-[0.75rem] text-[var(--color-ink-2)] cursor-pointer border-t border-[var(--color-rule)] pt-1.5">
            <input type="checkbox" checked={!!chartOnHover} onChange={(e) => onToggleChartOnHover?.(e.target.checked)} />
            <span>Chart on hover</span>
          </label>
          <div className="flex flex-col gap-1 border-t border-[var(--color-rule)] pt-1.5">
            <div className="h-1.5 rounded-sm" style={{ background: gradient }} />
            <div className="flex justify-between text-[0.65rem] text-[var(--color-ink-3)] tabular-nums">
              <span>{cfg.ramp[0][0].toFixed(cfg.digits)}{cfg.suffix}</span>
              <span>+{cfg.ramp[cfg.ramp.length - 1][0].toFixed(cfg.digits)}{cfg.suffix}</span>
            </div>
            <div className="flex items-center gap-2 text-[0.62rem] text-[var(--color-ink-3)]">
              <span className="flex items-center gap-1">
                <span className="inline-block h-2 w-2 rounded-[2px]" style={{ background: 'rgba(94,122,74,0.3)' }} />
                málo dat
              </span>
              <span className="flex items-center gap-1">
                <span className="inline-block h-2 w-2 rounded-[2px]" style={{ background: GROWTH_NO_DATA }} />
                bez dat
              </span>
            </div>
            {rowCount === 0 && <span className="text-[0.65rem] text-[var(--color-ink-3)]">Načítání…</span>}
          </div>
        </div>
      )}
    </div>
  );
}

function RentMapControls({
  showRentMap,
  rentVk,
  showKraje,
  polygonCount,
  onToggleShowRentMap,
  onRentVkChange,
  onToggleShowKraje,
}: {
  showRentMap: boolean;
  rentVk: RentVk;
  showKraje: boolean;
  polygonCount: number;
  onToggleShowRentMap?: (next: boolean) => void;
  onRentVkChange?: (vk: RentVk) => void;
  onToggleShowKraje?: (next: boolean) => void;
}) {
  const rampGradient = `linear-gradient(to right, ${RENT_RAMP
    .map(([, color]) => color)
    .join(', ')})`;
  return (
    <div className="flex flex-col gap-2 items-end">
      <div className="pointer-events-auto flex items-center gap-2 px-2.5 py-1.5 rounded-[var(--radius-sm)] bg-[var(--color-paper-3)]/95 backdrop-blur-sm border border-[var(--color-rule)] shadow-[0_2px_6px_rgba(0,0,0,0.04)]">
        <label className="inline-flex items-center gap-1.5 text-[0.75rem] text-[var(--color-ink-2)] cursor-pointer">
          <input
            type="checkbox"
            checked={showRentMap}
            onChange={(e) => onToggleShowRentMap?.(e.target.checked)}
          />
          <span>Cenová mapa nájemného</span>
        </label>
      </div>
      {showRentMap && (
        <div className="pointer-events-auto flex flex-col gap-2 px-2.5 py-2 rounded-[var(--radius-sm)] bg-[var(--color-paper-3)]/95 backdrop-blur-sm border border-[var(--color-rule)] shadow-[0_2px_6px_rgba(0,0,0,0.04)] min-w-[180px]">
          <div className="flex flex-col gap-1">
            {([1, 2, 3, 4] as RentVk[]).map((vk) => (
              <label
                key={vk}
                className="inline-flex items-center gap-1.5 text-[0.75rem] text-[var(--color-ink-2)] cursor-pointer"
              >
                <input
                  type="radio"
                  name="rent-vk"
                  checked={rentVk === vk}
                  onChange={() => onRentVkChange?.(vk)}
                />
                <span>{RENT_VK_LABELS[vk]}</span>
              </label>
            ))}
          </div>
          <div className="border-t border-[var(--color-rule)] pt-1.5">
            <label className="inline-flex items-center gap-1.5 text-[0.75rem] text-[var(--color-ink-2)] cursor-pointer">
              <input
                type="checkbox"
                checked={showKraje}
                onChange={(e) => onToggleShowKraje?.(e.target.checked)}
              />
              <span>Kraje</span>
            </label>
          </div>
          <div className="flex flex-col gap-1 border-t border-[var(--color-rule)] pt-1.5">
            <span className="text-[0.65rem] text-[var(--color-ink-3)]">
              Nájemné (Kč/m²)
            </span>
            <div
              className="h-1.5 rounded-sm"
              style={{ background: rampGradient }}
            />
            <div className="flex justify-between text-[0.65rem] text-[var(--color-ink-3)] tabular-nums">
              <span>{czIntFmt.format(RENT_RAMP[0][0])}</span>
              <span>{czIntFmt.format(RENT_RAMP[RENT_RAMP.length - 1][0])}</span>
            </div>
            {polygonCount === 0 && (
              <span className="text-[0.65rem] text-[var(--color-ink-3)]">
                Načítání…
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function CityMapControls({
  showCities,
  onToggleShowCities,
  colorByIndex,
  cityIndexDefinitions,
  onColorByIndexChange,
  colorByMin,
  onColorByMinChange,
  cityCount,
}: {
  showCities: boolean;
  onToggleShowCities?: (next: boolean) => void;
  colorByIndex: CityIndexDefinition | null;
  cityIndexDefinitions: ReadonlyArray<CityIndexDefinition>;
  onColorByIndexChange?: (indexName: string | null) => void;
  colorByMin: number | null;
  onColorByMinChange?: (next: number | null) => void;
  cityCount: number;
}) {
  return (
    <div className="pointer-events-none absolute bottom-9 left-3 flex flex-col gap-2 items-start">
      <div className="pointer-events-auto flex items-center gap-2 px-2.5 py-1.5 rounded-[var(--radius-sm)] bg-[var(--color-paper-3)]/95 backdrop-blur-sm border border-[var(--color-rule)] shadow-[0_2px_6px_rgba(0,0,0,0.04)]">
        <label className="inline-flex items-center gap-1.5 text-[0.75rem] text-[var(--color-ink-2)] cursor-pointer">
          <input
            type="checkbox"
            checked={showCities}
            onChange={(e) => onToggleShowCities?.(e.target.checked)}
          />
          <span>Show cities</span>
          <span className="text-[var(--color-ink-3)] tabular-nums">({cityCount})</span>
        </label>
      </div>
      {showCities && (
        <div className="pointer-events-auto flex items-center gap-2 px-2.5 py-1.5 rounded-[var(--radius-sm)] bg-[var(--color-paper-3)]/95 backdrop-blur-sm border border-[var(--color-rule)] shadow-[0_2px_6px_rgba(0,0,0,0.04)] text-[0.75rem]">
          <span className="text-[var(--color-ink-2)] font-medium">Color by:</span>
          <select
            className="text-[0.75rem] bg-[var(--color-paper-2)] border border-[var(--color-rule)] rounded px-1.5 py-0.5 max-w-[200px]"
            value={colorByIndex?.index_name ?? ''}
            onChange={(e) => onColorByIndexChange?.(e.target.value || null)}
          >
            <option value="">Žádné</option>
            {groupForPicker(cityIndexDefinitions).map((g) => (
              <optgroup key={g.label} label={g.label}>
                {g.defs.map((d) => (
                  <option key={d.index_name} value={d.index_name}>
                    {g.prefix}{indexLabel(d)}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </div>
      )}
      {showCities && colorByIndex && (
        <div className="pointer-events-auto flex flex-col gap-1 px-2.5 py-1.5 rounded-[var(--radius-sm)] bg-[var(--color-paper-3)]/95 backdrop-blur-sm border border-[var(--color-rule)] shadow-[0_2px_6px_rgba(0,0,0,0.04)] min-w-[160px]">
          <div
            className="h-1.5 rounded-sm"
            style={{
              background: 'linear-gradient(to right, #c0392b 0%, #f1c40f 50%, #2ecc71 100%)',
            }}
          />
          <div className="flex justify-between text-[0.65rem] text-[var(--color-ink-3)] tabular-nums">
            <span>{colorByIndex.scale_min}</span>
            <span className="text-[var(--color-ink-2)]">{indexLabel(colorByIndex)}</span>
            <span>{colorByIndex.scale_max}</span>
          </div>
          <div className="flex items-center gap-1.5 pt-0.5">
            <span className="text-[0.65rem] text-[var(--color-ink-2)]">Min:</span>
            <input
              type="number"
              inputMode="decimal"
              step="any"
              placeholder="—"
              value={colorByMin ?? ''}
              onChange={(e) => {
                const raw = e.target.value.trim();
                const n = Number(raw);
                onColorByMinChange?.(
                  raw === '' || Number.isNaN(n) ? null : n,
                );
              }}
              className="w-16 text-[0.7rem] bg-[var(--color-paper-2)] border border-[var(--color-rule)] rounded px-1 py-0.5 tabular-nums"
            />
            <span className="text-[0.6rem] text-[var(--color-ink-3)]">
              below → grey
            </span>
          </div>
        </div>
      )}
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
  const disposition = listingKindLabel(r) ?? '—';
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
      <a href="${listingPath(r.sreality_id)}" class="lp-link">View details →</a>
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

function cityPopupHtml(
  c: CuratedCity,
  defs: ReadonlyArray<CityIndexDefinition>,
  values: Map<string, number>,
  highlighted: CityIndexDefinition | null,
): string {
  const popLabel = c.population != null
    ? `${c.population.toLocaleString('cs-CZ')} obyv.${c.population_as_of_year ? ` (${c.population_as_of_year})` : ''}`
    : '';
  /* Sort: highlighted index first (when the operator colour-codes by
   * one), then the seven pinned indexes (shared with the rule picker
   * and the colour-by dropdown), then everything else by category +
   * registry sort_order. Cap at 8 rows so a 33-index popup doesn't
   * sprawl — the cap comfortably fits the pinned set, guaranteeing
   * the operator always sees the headline metrics. */
  const sortedDefs = pinnedFirst(defs, highlighted);
  const rows = sortedDefs.slice(0, 8).map((d) => {
    const key = `${c.city_id}:${d.index_name}`;
    const v = values.get(key);
    const label = indexLabel(d);
    const valueText = typeof v === 'number'
      ? v.toFixed(1)
      : '—';
    const isHi = highlighted && d.index_name === highlighted.index_name;
    return `
      <p class="lp-meta${isHi ? ' lp-strong' : ''}">
        <span>${escape(label)}</span>
        <span class="lp-mono">${escape(valueText)}</span>
      </p>`;
  }).join('');
  return `
    <div class="lp">
      <p class="lp-kicker">Kvalita města</p>
      <div class="lp-row">
        <p class="lp-price">${escape(c.name)}</p>
      </div>
      <p class="lp-district">${escape(c.kraj_name)}${popLabel ? ` · ${escape(popLabel)}` : ''}</p>
      ${rows}
    </div>
  `;
}
