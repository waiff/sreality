import {
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from 'react';
import { useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Tabs, { type Tab } from '@/components/Tabs';
import ResizeHandle from '@/components/ResizeHandle';
import { useSidebarWidth, useMapSplitFraction } from '@/lib/browseLayout';
import { FilterSidebar } from '@/components/Filters';
import ListingTable from '@/components/ListingTable';
import ListingCards from '@/components/ListingCards';
import BrowseStatsView from '@/components/BrowseStats';
import type { MapFlyToCommand, RentVk } from '@/components/ListingMap';
import type { MapySuggestion } from '@/lib/maps';
import { fetchDatasets, fetchGrowth, fetchSeries, priceStatsKeys } from '@/lib/priceStats';
import { buildHoverData, type GrowthMetric } from '@/lib/growthChoropleth';
import {
  fromSearchParams,
  toSearchParams,
  summarise,
  isDefault,
  regionKeyFromFilters,
  regionLabelFromFilters,
  filtersToWatchdogSpec,
  watchdogNameSuggestion,
  readPresetSpec,
  DEFAULT_FILTERS,
  type ListingFilters,
  type MapBounds,
} from '@/lib/filters';
import CreateWatchdogModal from '@/components/CreateWatchdogModal';
import PresetBar from '@/components/PresetBar';
import type { FilterPreset, ListingEstimate } from '@/lib/types';
import {
  createEstimation,
  fetchRegionDispositionAnnotations,
  isApiConfigured,
  latestEstimationsByListing,
  mergeDedupPropertySet,
} from '@/lib/api';
import {
  fetchCityIndexDefinitions,
  fetchCityIndexValues,
  fetchCuratedCities,
  fetchCuratedCityPolygons,
  fetchListingsForCards,
  fetchListingsForMap,
  fetchListingsForTable,
  fetchBrowseCount,
  fetchBrowseStats,
  fetchRentMapChoropleth,
  fetchRentMapKraje,
  parseSort,
  sortToParam,
  CARD_PAGE_SIZE,
  TABLE_PAGE_SIZE,
  DEFAULT_SORT,
  type BrowseStats,
  type CardRow,
  type CityIndexDefinition,
  type CityIndexValue,
  type CityPolygon,
  type CuratedCity,
  type MapResult,
  type RentMapKraj,
  type RentMapPolygon,
  type SortField,
  type SortSpec,
  type TableRow,
} from '@/lib/queries';
import { useInfiniteList } from '@/lib/useInfiniteList';
import type { KeysetCursor } from '@/lib/keyset';

const ListingMap = lazy(() => import('@/components/ListingMap'));

type TabKey = 'map' | 'table' | 'stats';

export default function Browse() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = useMemo(() => fromSearchParams(searchParams), [searchParams]);
  const sort: SortSpec = useMemo(
    () => parseSort(searchParams.get('sort')),
    [searchParams],
  );
  const tabFromUrl = (searchParams.get('tab') ?? 'map') as TabKey;
  /* Phase QUAL — map-overlay UI state. Not part of the filter spec
   * (these don't narrow the cohort, they just paint city pins). Held
   * in the URL so a shared link reproduces what the operator saw.
   * These keys are NOT serialised by `toSearchParams`, so every other
   * URL rewriter on this page MUST preserve them explicitly (see
   * `preserveExtras`). The previous draft dropped them on pan/zoom,
   * which manifested as the operator's color-by selection vanishing
   * on every map gesture. */
  const showCities = searchParams.get('cities') !== '0';
  const colorByIndexName = searchParams.get('colorby') ?? null;
  /* MF rent-price choropleth ("Cenová mapa nájemného"). Off by default
   * so it doesn't clutter the listings view — only painted when the
   * operator explicitly enables it (`?rentmap=1`). `rentvk` selects the
   * size category (VK1..VK4, default 1); `kraje` toggles the kraj
   * boundary overlay. Like the city-overlay knobs these live in the URL
   * (so a shared link reproduces the view) and are NOT serialised by
   * `toSearchParams`, so `preserveExtras` must carry them. */
  const showRentMap = searchParams.get('rentmap') === '1';
  const rentVkParam = parseInt(searchParams.get('rentvk') ?? '1', 10);
  const rentVk = ([1, 2, 3, 4].includes(rentVkParam)
    ? rentVkParam
    : 1) as RentVk;
  const showKraje = searchParams.get('kraje') === '1';
  /* Active saved-filter-preset id. Lives in the URL (carried by
   * `preserveExtras`) so editing a filter keeps the preset "loaded"
   * — the PresetBar then shows it as dirty and offers an Update. */
  const activePresetId = searchParams.get('preset');

  /* Price-stats growth overlay control state — kept in component state (not
   * URL) since it's a transient map-exploration knob. */
  const [showGrowth, setShowGrowth] = useState(false);
  const [growthDatasetId, setGrowthDatasetId] = useState<number | null>(null);
  const [growthMetric, setGrowthMetric] = useState<GrowthMetric>('rent_cagr_pct');
  const [growthFrom, setGrowthFrom] = useState('2015-01');
  const [growthTo, setGrowthTo] = useState(
    () => `${new Date().getFullYear()}-${String(new Date().getMonth() + 1).padStart(2, '0')}`,
  );
  const [growthChartOnHover, setGrowthChartOnHover] = useState(false);

  /* Copy URL keys that live outside `toSearchParams` (tab, sort, the
   * city-overlay knobs). Used by every URL rewriter on this page so
   * `setBounds` / `setFilters` / `writeSort` can't drop them by accident.
   * There is no paging param: infinite scroll resets to the top whenever
   * the cohort changes, because the cards/table infinite queries are keyed
   * on (filters, sort) — a changed key starts a fresh accumulation. */
  const preserveExtras = useCallback(
    (sp: URLSearchParams): URLSearchParams => {
      for (const key of ['tab', 'sort', 'cities', 'colorby', 'rentmap', 'rentvk', 'kraje', 'preset']) {
        const v = searchParams.get(key);
        if (v != null) sp.set(key, v);
      }
      return sp;
    },
    [searchParams],
  );

  const setShowCities = useCallback(
    (next: boolean) => {
      const sp = new URLSearchParams(searchParams);
      if (next) sp.delete('cities');
      else sp.set('cities', '0');
      setSearchParams(sp, { replace: true });
    },
    [searchParams, setSearchParams],
  );
  const setColorByIndex = useCallback(
    (next: string | null) => {
      const sp = new URLSearchParams(searchParams);
      if (next) sp.set('colorby', next);
      else sp.delete('colorby');
      setSearchParams(sp, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const setShowRentMap = useCallback(
    (next: boolean) => {
      const sp = new URLSearchParams(searchParams);
      if (next) sp.set('rentmap', '1');
      else sp.delete('rentmap');
      setSearchParams(sp, { replace: true });
    },
    [searchParams, setSearchParams],
  );
  const setRentVk = useCallback(
    (next: RentVk) => {
      const sp = new URLSearchParams(searchParams);
      if (next === 1) sp.delete('rentvk');
      else sp.set('rentvk', String(next));
      setSearchParams(sp, { replace: true });
    },
    [searchParams, setSearchParams],
  );
  const setShowKraje = useCallback(
    (next: boolean) => {
      const sp = new URLSearchParams(searchParams);
      if (next) sp.set('kraje', '1');
      else sp.delete('kraje');
      setSearchParams(sp, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const setFilters = useCallback(
    (next: ListingFilters) => {
      const sp = preserveExtras(toSearchParams(next));
      // New filter set → new infinite-query key → accumulation resets to the
      // top automatically (and the cards scroll position resets via its key).
      setSearchParams(sp, { replace: false });
    },
    [preserveExtras, setSearchParams],
  );

  /* Load a saved preset: replace the whole filter set with the stored spec
   * (merged onto DEFAULT_FILTERS so an older spec missing a newer field
   * still resolves cleanly) and mark the preset active. */
  const loadPreset = useCallback(
    (p: FilterPreset) => {
      const { filters: pf, sort: ps } = readPresetSpec(p.filter_spec);
      const sp = preserveExtras(toSearchParams(pf));
      // Restore the preset's saved sort, overriding the carried-over current
      // one (`preserveExtras` copies the existing `sort`). Omit when default.
      const presetSort = ps ?? sortToParam(DEFAULT_SORT);
      if (presetSort === sortToParam(DEFAULT_SORT)) sp.delete('sort');
      else sp.set('sort', presetSort);
      sp.set('preset', p.id);
      setSearchParams(sp, { replace: false });
    },
    [preserveExtras, setSearchParams],
  );

  /* Set / clear the active preset id WITHOUT touching the filters — used
   * after creating a preset (mark it loaded) or deleting the active one. */
  const setActivePresetId = useCallback(
    (id: string | null) => {
      const sp = new URLSearchParams(searchParams);
      if (id) sp.set('preset', id);
      else sp.delete('preset');
      setSearchParams(sp, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const setTab = (next: TabKey) => {
    const sp = new URLSearchParams(searchParams);
    if (next === 'map') sp.delete('tab');
    else sp.set('tab', next);
    setSearchParams(sp, { replace: true });
  };

  const writeSort = (next: SortSpec) => {
    const sp = new URLSearchParams(searchParams);
    if (sortToParam(next) === sortToParam(DEFAULT_SORT)) sp.delete('sort');
    else sp.set('sort', sortToParam(next));
    setSearchParams(sp, { replace: false });
  };

  // Table column-header click: toggles direction on the same field,
  // or jumps to a fresh field with its preferred default direction.
  const setSortByField = (field: SortField) => {
    const next: SortSpec =
      sort.field === field
        ? { field, direction: sort.direction === 'asc' ? 'desc' : 'asc' }
        : { field, direction: defaultDirectionFor(field) };
    writeSort(next);
  };

  /* Map flyTo command. Set when the operator picks a place in the
   * District typeahead; ListingMap watches this prop and animates to
   * the new centre. The `ts` field guarantees identity changes on
   * each pick so re-picking the same place still triggers a flyTo. */
  const [mapFlyTo, setMapFlyTo] = useState<MapFlyToCommand | null>(null);
  /* "Create watchdog from Browse": held here so the button in FilterSummary
   * opens the name-prompt modal seeded from the current filter set. */
  const [watchdogModalOpen, setWatchdogModalOpen] = useState(false);

  /* Dedup merge mode: a toggle turns the cards into a multi-select; the picked
   * property_ids are merged into one via the existing dedup endpoint. */
  const queryClient = useQueryClient();
  const [mergeMode, setMergeMode] = useState(false);
  const [selectedForMerge, setSelectedForMerge] = useState<ReadonlySet<number>>(
    () => new Set(),
  );
  const toggleSelectForMerge = useCallback((propertyId: number) => {
    setSelectedForMerge((prev) => {
      const next = new Set(prev);
      if (next.has(propertyId)) next.delete(propertyId);
      else next.add(propertyId);
      return next;
    });
  }, []);
  const exitMergeMode = useCallback(() => {
    setMergeMode(false);
    setSelectedForMerge(new Set());
  }, []);
  const mergeMut = useMutation({
    mutationFn: (propertyIds: number[]) => mergeDedupPropertySet(propertyIds),
    onSuccess: () => {
      // The merged-away properties drop out of properties_public; refresh every
      // Browse cohort view so the survivor replaces the duplicates.
      for (const key of ['cards', 'map', 'table', 'stats']) {
        queryClient.invalidateQueries({ queryKey: [key] });
      }
      exitMergeMode();
    },
  });
  const handleLocationPick = useCallback((s: MapySuggestion) => {
    if (!s.position) return;
    setMapFlyTo({
      lat: s.position.lat,
      lng: s.position.lon,
      zoom: zoomForSuggestionType(s.type),
      ts: Date.now(),
    });
  }, []);

  /* Hover-sync between cards / table / map. The set is ephemeral —
   * lives only in component state, never in the URL. Hovering a single
   * card or pin produces a one-element set; hovering a cluster on the
   * map produces N elements (one per leaf) so the matching cards all
   * highlight together. `origin` records which pane is pointing so each
   * side can react differently: a map-origin hover dims the card grid
   * around the matches and scrolls the first one into view, while a
   * list-origin hover drops the locator halo on the map. */
  const [hoverState, setHoverState] = useState<{
    ids: ReadonlySet<number>;
    origin: 'map' | 'list' | null;
  }>({ ids: new Set(), origin: null });
  const hoveredIds = hoverState.ids;
  const setHovered = useCallback(
    (ids: ReadonlyArray<number> | null, origin: 'map' | 'list') => {
      setHoverState((prev) => {
        if (ids == null || ids.length === 0) {
          return prev.ids.size === 0
            ? prev
            : { ids: new Set<number>(), origin: null };
        }
        // Avoid spurious re-renders if the same id is reported twice
        // (maplibre emits mouseenter on every move within the layer).
        if (
          prev.origin === origin &&
          prev.ids.size === ids.length &&
          ids.every((id) => prev.ids.has(id))
        ) {
          return prev;
        }
        return { ids: new Set(ids), origin };
      });
    },
    [],
  );
  const setHoveredFromMap = useCallback(
    (ids: ReadonlyArray<number> | null) => setHovered(ids, 'map'),
    [setHovered],
  );
  const setHoveredFromList = useCallback(
    (ids: ReadonlyArray<number> | null) => setHovered(ids, 'list'),
    [setHovered],
  );

  /* Operator-resizable columns. `sidebar` is column 1 (filters, all
   * tabs); `mapSplit` is the cards|map divider on the Listings tab.
   * Both persist to localStorage. We measure live against the relevant
   * container's rect so the divider tracks the cursor exactly, then
   * commit to storage once on pointer-up (persist) — not every move. */
  const sidebar = useSidebarWidth();
  const mapSplit = useMapSplitFraction();
  const outerRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const onSidebarDrag = useCallback(
    (clientX: number) => {
      const left = outerRef.current?.getBoundingClientRect().left ?? 0;
      sidebar.set(clientX - left);
    },
    [sidebar],
  );
  const onMapSplitDrag = useCallback(
    (clientX: number) => {
      const r = innerRef.current?.getBoundingClientRect();
      if (!r || r.width === 0) return;
      // Map is the right column: its width is the distance from the
      // cursor to the container's right edge, as a fraction of total.
      mapSplit.set((r.right - clientX) / r.width);
    },
    [mapSplit],
  );

  /* Bounds round-trip through the URL via the existing `filters` shape
   * (see lib/filters.ts:MapBounds). The map calls this on each
   * user-driven pan/zoom; we keep `replace: true` so a continuous
   * gesture doesn't pile up history entries. Page resets to 1 because
   * a viewport change means a fresh cohort. Other URL keys (tab,
   * sort) are preserved by re-serialising the new filter shape on top
   * of the current params.*/
  const setBounds = useCallback(
    (b: MapBounds | null) => {
      const next: ListingFilters = { ...filters, bounds: b };
      const sp = preserveExtras(toSearchParams(next));
      setSearchParams(sp, { replace: true });
    },
    [filters, preserveExtras, setSearchParams],
  );

  /* Map tab fetches two cohorts in parallel: every (geo-located) listing
   * for the map (capped at MAP_CAP), plus the paginated card slice for
   * the left column. Same filter set, different shapes. */
  const mapQuery = useQuery<MapResult, Error>({
    queryKey: ['map', filters],
    queryFn: () => fetchListingsForMap(filters),
    placeholderData: (prev) => prev,
    enabled: tabFromUrl === 'map',
  });

  /* Phase QUAL — curated cities + index defs + index values are
   * small (~206 cities, ~33 defs, ~7K values) so fetch once per
   * session and cache aggressively. The map and the filter UI
   * both consume them. `staleTime: Infinity` is safe because the
   * data only changes on an operator-triggered upload, which
   * triggers a manual re-fetch via the cities-admin page. */
  const citiesQuery = useQuery<CuratedCity[], Error>({
    queryKey: ['curated_cities'],
    queryFn: fetchCuratedCities,
    staleTime: Infinity,
    gcTime: Infinity,
  });
  const cityDefsQuery = useQuery<CityIndexDefinition[], Error>({
    queryKey: ['city_index_definitions'],
    queryFn: fetchCityIndexDefinitions,
    staleTime: Infinity,
    gcTime: Infinity,
  });
  const cityValuesQuery = useQuery<CityIndexValue[], Error>({
    queryKey: ['city_index_values'],
    queryFn: fetchCityIndexValues,
    staleTime: Infinity,
    gcTime: Infinity,
  });

  /* Municipality boundary polygons for the city overlay (one simplified
   * GeoJSON per curated city). ~600 KB, so gated on the map tab and
   * cached forever — the map draws each city as its real shape instead
   * of a fixed-radius circle. Static reference data, like the rent map. */
  const cityPolygonsQuery = useQuery<CityPolygon[], Error>({
    queryKey: ['curated_city_polygons'],
    queryFn: fetchCuratedCityPolygons,
    enabled: tabFromUrl === 'map',
    staleTime: Infinity,
    gcTime: Infinity,
  });
  const cityPolygonsMap = useMemo(() => {
    const m = new Map<number, string>();
    for (const p of cityPolygonsQuery.data ?? []) m.set(p.city_id, p.geojson);
    return m;
  }, [cityPolygonsQuery.data]);

  /* MF rent-price choropleth. The ~7.6K polygons + 14 kraj borders are
   * an operator-static reference dataset, so fetch once and cache forever
   * (`staleTime: Infinity`). Gated on the map tab being active AND the
   * layer being enabled so we never pay the ~MB transfer unless the
   * operator actually turns the choropleth on. The kraj overlay is
   * fetched alongside (only when the rent map is shown) so toggling the
   * "Kraje" checkbox is instant once it's loaded. */
  const rentMapQuery = useQuery<RentMapPolygon[], Error>({
    queryKey: ['rent_map_choropleth'],
    queryFn: fetchRentMapChoropleth,
    enabled: tabFromUrl === 'map' && showRentMap,
    staleTime: Infinity,
    gcTime: Infinity,
  });
  const rentKrajeQuery = useQuery<RentMapKraj[], Error>({
    queryKey: ['rent_map_kraje'],
    queryFn: fetchRentMapKraje,
    enabled: tabFromUrl === 'map' && showRentMap,
    staleTime: Infinity,
    gcTime: Infinity,
  });

  /* Price-stats growth overlay data: the dataset list (for the picker) +
   * per-obec growth for the chosen dataset + window. */
  const psDatasetsQuery = useQuery({
    queryKey: priceStatsKeys.datasets,
    queryFn: fetchDatasets,
    enabled: tabFromUrl === 'map' && showGrowth,
    staleTime: 60_000,
  });
  const psGrowthDatasetId = growthDatasetId ?? psDatasetsQuery.data?.[0]?.id ?? null;
  const psGrowthQuery = useQuery({
    queryKey: priceStatsKeys.growth(psGrowthDatasetId ?? -1, growthFrom, growthTo),
    queryFn: () => fetchGrowth(psGrowthDatasetId as number, growthFrom, growthTo),
    enabled: tabFromUrl === 'map' && showGrowth && psGrowthDatasetId != null,
    staleTime: 60_000,
  });
  const psSeriesQuery = useQuery({
    queryKey: priceStatsKeys.obecSeries(psGrowthDatasetId ?? -1, growthFrom, growthTo),
    queryFn: () => fetchSeries(psGrowthDatasetId as number, growthFrom, growthTo),
    enabled: tabFromUrl === 'map' && showGrowth && growthChartOnHover && psGrowthDatasetId != null,
    staleTime: 60_000,
  });
  const psHoverData = useMemo(
    () => (psSeriesQuery.data ? buildHoverData(psSeriesQuery.data, growthMetric) : null),
    [psSeriesQuery.data, growthMetric],
  );

  /* Browse cards + table are KEYSET-paginated infinite lists over
   * properties_public (lib/keyset.ts): correct under the scraper's
   * last_seen_at churn (offset would dup/skip) and flat in latency at any
   * depth. The cohort total is a separate one-shot count (it doesn't change
   * per page) shared by both tabs. Keyed on (filters, sort) only — a
   * changed cohort starts a fresh accumulation from the top. */
  const cards = useInfiniteList<CardRow>({
    queryKey: ['cards', filters, sort],
    queryFn: (cursor) =>
      fetchListingsForCards(filters, sort, cursor as KeysetCursor | null),
    pageSize: CARD_PAGE_SIZE,
    getRowId: (r) => r.property_id,
    enabled: tabFromUrl === 'map',
    gcTime: 10 * 60_000,
  });

  const browseCountQuery = useQuery<number, Error>({
    queryKey: ['browse-count', filters],
    queryFn: () => fetchBrowseCount(filters),
    enabled: tabFromUrl === 'map' || tabFromUrl === 'table',
    placeholderData: (prev) => prev,
    staleTime: 60_000,
  });
  const browseTotal = browseCountQuery.data ?? null;

  /* Stable per-cohort key for the cards column's scroll restoration. Keyed
   * on (filters, sort) only — NOT the volatile map-overlay knobs — so
   * toggling a city/rent overlay doesn't reset the scroll, while a genuine
   * filter/sort change does (new key → reset to top). */
  const cardsRestorationKey = useMemo(() => {
    const sp = toSearchParams(filters);
    sp.set('sort', sortToParam(sort));
    return `cards:${sp.toString()}`;
  }, [filters, sort]);

  /* On-card estimate: latest rent estimate per visible listing, plus a
   * trigger that runs the standard (agent) rental estimate for one card.
   * Distinct from the card's statistical mf_gross_yield_pct — this is an
   * actual estimation_runs result. */
  const cardIds = useMemo(
    () => cards.rows.map((r) => r.sreality_id),
    [cards.rows],
  );
  const [estimatingIds, setEstimatingIds] = useState<ReadonlySet<number>>(
    () => new Set(),
  );
  /* On-card estimates accumulate as you scroll: each newly-loaded card's
   * latest rent estimate is fetched ONCE (infinite scroll makes cardIds grow
   * unbounded — re-requesting the whole set would blow the GET URL length and
   * be O(n²) over a session). Still-running runs are polled on their small id
   * set until they settle. */
  const [estimates, setEstimates] = useState<Record<number, ListingEstimate>>(
    {},
  );
  const requestedEstIdsRef = useRef<Set<number>>(new Set());
  const estimatesEnabled = tabFromUrl === 'map' && isApiConfigured();

  // A genuinely new cohort (filters/sort) is a different card set — drop the
  // accumulated chips so they don't bleed across. Keyed on the stable cohort
  // signature, NOT raw searchParams, so a map-overlay toggle doesn't reset.
  useEffect(() => {
    setEstimates({});
    requestedEstIdsRef.current = new Set();
  }, [cardsRestorationKey]);

  // Fetch estimates for the DELTA of newly-loaded cards (≤ one page of ids,
  // so the request URL stays bounded).
  useEffect(() => {
    if (!estimatesEnabled) return;
    const fresh = cardIds.filter((id) => !requestedEstIdsRef.current.has(id));
    if (fresh.length === 0) return;
    fresh.forEach((id) => requestedEstIdsRef.current.add(id));
    let cancelled = false;
    latestEstimationsByListing(fresh)
      .then((map) => {
        if (!cancelled) setEstimates((prev) => ({ ...prev, ...map }));
      })
      .catch(() => {
        /* best-effort chip — a failed lookup just leaves the card chip-less */
      });
    return () => {
      cancelled = true;
    };
  }, [cardIds, estimatesEnabled]);

  // Poll only the small set of still-running estimates (optimistic + any
  // pending/running in the accumulator) until they terminate.
  const pendingEstIds = useMemo(
    () => [
      ...estimatingIds,
      ...Object.values(estimates)
        .filter((e) => e.status === 'pending' || e.status === 'running')
        .map((e) => e.sreality_id),
    ],
    [estimatingIds, estimates],
  );
  useEffect(() => {
    if (!estimatesEnabled || pendingEstIds.length === 0) return;
    const timer = setInterval(() => {
      latestEstimationsByListing(pendingEstIds)
        .then((map) => setEstimates((prev) => ({ ...prev, ...map })))
        .catch(() => {});
    }, 4000);
    return () => clearInterval(timer);
  }, [estimatesEnabled, pendingEstIds]);
  const estimateMut = useMutation({
    mutationFn: (srealityId: number) =>
      createEstimation({
        sreality_id: srealityId,
        source: 'ui',
        mode: 'agent',
        provider: 'anthropic',
        estimate_kind: 'rent',
        lifecycle: 'active',
      }),
    onMutate: (srealityId) => {
      setEstimatingIds((prev) => new Set(prev).add(srealityId));
    },
    onSuccess: (run, srealityId) => {
      setEstimates((prev) => ({
        ...prev,
        [srealityId]: {
          sreality_id: srealityId,
          run_id: run.id,
          status: run.status,
          estimate_kind: run.estimate_kind,
          gross_yield_pct: run.gross_yield_pct,
          estimated_monthly_rent_czk: run.estimated_monthly_rent_czk,
          created_at: run.created_at,
        },
      }));
    },
    onSettled: (_run, _err, srealityId) => {
      setEstimatingIds((prev) => {
        const next = new Set(prev);
        next.delete(srealityId);
        return next;
      });
    },
  });

  const table = useInfiniteList<TableRow>({
    queryKey: ['table', filters, sort],
    queryFn: (cursor) =>
      fetchListingsForTable(filters, sort, cursor as KeysetCursor | null),
    pageSize: TABLE_PAGE_SIZE,
    getRowId: (r) => r.property_id,
    enabled: tabFromUrl === 'table',
    gcTime: 10 * 60_000,
  });

  const statsQuery = useQuery<BrowseStats, Error>({
    queryKey: ['stats', filters],
    queryFn: () => fetchBrowseStats(filters),
    placeholderData: (prev) => prev,
    enabled: tabFromUrl === 'stats',
  });

  /* summarize-1: natural-language annotations for the per-disposition
   * box plots. Keyed on the cohort's stable region key; the FastAPI
   * service caches them per (region, calendar day) so repeat sessions
   * don't re-bill. Only fires once the stats payload has at least one
   * box the chart will actually draw (n >= 5), and only when the API
   * is configured. */
  const regionKey = useMemo(() => regionKeyFromFilters(filters), [filters]);
  const boxDispositions = useMemo(
    () =>
      (statsQuery.data?.dispositions ?? []).filter(
        (d) => d.ppm2_box != null && d.ppm2_box.n >= 5,
      ),
    [statsQuery.data],
  );
  const annotationsQuery = useQuery({
    queryKey: ['region-annotations', regionKey],
    queryFn: () =>
      fetchRegionDispositionAnnotations({
        region_key: regionKey,
        region_label: regionLabelFromFilters(filters),
        ppm2_overall: statsQuery.data?.ppm2 ?? null,
        dispositions: boxDispositions.map((d) => ({
          disposition: d.disposition,
          n: d.n,
          ppm2_box: d.ppm2_box,
        })),
      }),
    enabled:
      tabFromUrl === 'stats' && isApiConfigured() && boxDispositions.length > 0,
    staleTime: 60 * 60 * 1000,
    retry: false,
  });

  /* Build the city overlay payload. Filter the 206 curated cities
   * client-side based on the active city-quality rules + population
   * bounds — same predicate the `listings_with_city_quality` RPC
   * applies server-side, just over a tiny in-memory dataset. The map
   * shows whichever cities survive (zero pins when nothing matches);
   * color coding paints them by the operator-selected index. */
  const cityOverlay = useMemo(() => {
    const cities = citiesQuery.data;
    const defs = cityDefsQuery.data;
    const values = cityValuesQuery.data;
    if (!cities || !defs || !values) {
      return {
        cities: [] as CuratedCity[],
        cityIndexValues: new Map<number, number>(),
        cityIndexValuesAll: new Map<string, number>(),
        colorByIndex: null as CityIndexDefinition | null,
      };
    }
    const allMap = new Map<string, number>();
    for (const v of values) {
      allMap.set(`${v.city_id}:${v.index_name}`, v.value);
    }
    const colorDef = colorByIndexName
      ? defs.find((d) => d.index_name === colorByIndexName) ?? null
      : null;
    const colorMap = new Map<number, number>();
    if (colorDef) {
      for (const v of values) {
        if (v.index_name === colorDef.index_name) {
          colorMap.set(v.city_id, v.value);
        }
      }
    }
    /* Apply the city-quality rules. A city passes when every rule
     * holds (AND). Population bounds gate the same set. */
    const rulesPass = (city: CuratedCity): boolean => {
      for (const r of filters.cityIndexRules) {
        const v = allMap.get(`${city.city_id}:${r.index_name}`);
        if (v == null) return false;
        const op = r.op ?? '>=';
        if (op === '>='  && !(v >= r.value)) return false;
        if (op === '<='  && !(v <= r.value)) return false;
        if (op === '>'   && !(v >  r.value)) return false;
        if (op === '<'   && !(v <  r.value)) return false;
        if (op === '=='  && !(v === r.value)) return false;
        if (op === '!='  && !(v !== r.value)) return false;
      }
      if (filters.minCityPopulation != null) {
        if (city.population == null || city.population < filters.minCityPopulation) return false;
      }
      if (filters.maxCityPopulation != null) {
        if (city.population == null || city.population > filters.maxCityPopulation) return false;
      }
      return true;
    };
    const matching = cities.filter(rulesPass);
    return {
      cities: matching,
      cityIndexValues: colorMap,
      cityIndexValuesAll: allMap,
      colorByIndex: colorDef,
    };
  }, [
    citiesQuery.data,
    cityDefsQuery.data,
    cityValuesQuery.data,
    colorByIndexName,
    filters.cityIndexRules,
    filters.minCityPopulation,
    filters.maxCityPopulation,
  ]);

  /* Cohort size for the tab badge + FilterSummary. Map/Listings + Table
   * both read the one-shot browse count (the exact cohort total, incl.
   * listings without coordinates that the map cap can't show); Stats has
   * its own aggregate total. */
  const totalForBadge =
    tabFromUrl === 'stats'
      ? statsQuery.data?.total ?? null
      : browseTotal;

  const tabs: ReadonlyArray<Tab<TabKey>> = [
    { key: 'map',   label: 'Listings', badge: totalForBadge != null ? totalForBadge.toLocaleString('cs-CZ') : undefined },
    { key: 'table', label: 'Table' },
    { key: 'stats', label: 'Stats' },
  ];

  const activeError =
    tabFromUrl === 'map'   ? mapQuery.error ?? cards.error :
    tabFromUrl === 'table' ? table.error :
    tabFromUrl === 'stats' ? statsQuery.error :
    null;

  return (
    <div className="flex" ref={outerRef}>
      <FilterSidebar
        filters={filters}
        onChange={setFilters}
        onLocationPick={handleLocationPick}
        width={sidebar.value}
      />

      {/* Divider between the filter sidebar (col 1) and the content.
          Desktop only — dragging is a pointer affordance. The 12px hit
          strip overlaps nothing; the sidebar's border-r remains the
          rest-state divider. */}
      <ResizeHandle
        ariaLabel="Resize the filters sidebar"
        onMove={onSidebarDrag}
        onEnd={sidebar.persist}
        onReset={sidebar.reset}
        className="hidden lg:flex sticky top-14 self-start h-[calc(100dvh-3.5rem)] w-3 -mx-1.5"
      />

      <div
        className={`flex-1 min-w-0 flex flex-col${
          tabFromUrl === 'map' ? ' h-[calc(100dvh-3.5rem)]' : ''
        }`}
      >
        <div className="px-6 pt-5">
          <FilterSummary
            filters={filters}
            count={totalForBadge}
            loading={
              tabFromUrl === 'map'   ? mapQuery.isLoading || cards.isLoading :
              tabFromUrl === 'table' ? table.isLoading :
              tabFromUrl === 'stats' ? statsQuery.isLoading :
              false
            }
            onClearBounds={filters.bounds ? () => setBounds(null) : undefined}
            onCreateWatchdog={() => setWatchdogModalOpen(true)}
          />
          <div className="mt-3">
            <PresetBar
              filters={filters}
              sort={sort}
              activePresetId={activePresetId}
              onLoad={loadPreset}
              onActivePresetIdChange={setActivePresetId}
            />
          </div>
          <div className="mt-4 flex items-center justify-between gap-3">
            <Tabs tabs={tabs} active={tabFromUrl} onChange={setTab} />
            {tabFromUrl === 'map' && (
              <MergeModeBar
                active={mergeMode}
                selectedCount={selectedForMerge.size}
                busy={mergeMut.isPending}
                onToggle={() => (mergeMode ? exitMergeMode() : setMergeMode(true))}
                onMerge={() => mergeMut.mutate([...selectedForMerge])}
              />
            )}
          </div>
        </div>

        {tabFromUrl === 'map' && (
          <div className="px-6 pt-5 pb-6 flex-1 min-h-0">
            {/* 3-column inner layout: cards (left) | map (right). The
              * outer FilterSidebar is column 1. Heights are pinned so
              * each column scrolls independently — the map stays put
              * while the cards list scrolls. The middle 1rem track holds
              * the drag handle; the map column width is the persisted
              * fraction (`--map-w`). Below lg the grid collapses to one
              * column and the handle's `display:none` drops it out. */}
            <div
              ref={innerRef}
              className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_1rem_var(--map-w)] h-full min-h-[560px]"
              style={
                { '--map-w': `${mapSplit.value * 100}%` } as CSSProperties
              }
            >
              <ListingCards
                rows={cards.isLoading ? null : cards.rows}
                total={browseTotal}
                sort={sort}
                isLoading={cards.isLoading}
                isFetchingNextPage={cards.isFetchingNextPage}
                hasNextPage={cards.hasNextPage}
                onReachEnd={cards.fetchNextPage}
                restorationKey={cardsRestorationKey}
                hasFilters={!isDefault(filters)}
                hasBounds={filters.bounds != null}
                hoveredIds={hoveredIds}
                hoverOrigin={hoverState.origin}
                onHover={setHoveredFromList}
                onSort={writeSort}
                onClearFilters={() => setFilters(DEFAULT_FILTERS)}
                onClearBounds={() => setBounds(null)}
                mergeMode={mergeMode}
                selectedPropertyIds={selectedForMerge}
                onToggleSelect={toggleSelectForMerge}
                estimates={estimates}
                estimatingIds={estimatingIds}
                onEstimate={(srealityId) => estimateMut.mutate(srealityId)}
              />
              <ResizeHandle
                ariaLabel="Resize the listings and map columns"
                onMove={onMapSplitDrag}
                onEnd={mapSplit.persist}
                onReset={mapSplit.reset}
                className="hidden lg:flex h-full"
              />
              <div className="min-h-0 h-full">
                <Suspense fallback={<MapSkeleton />}>
                  <ListingMap
                    rows={mapQuery.data?.rows ?? []}
                    total={mapQuery.data?.total ?? null}
                    capped={mapQuery.data?.capped ?? false}
                    isLoading={mapQuery.isLoading}
                    bounds={filters.bounds}
                    onBoundsChange={setBounds}
                    hoveredIds={hoveredIds}
                    hoverOrigin={hoverState.origin}
                    onHover={setHoveredFromMap}
                    centerCircle={
                      filters.locationMode === 'center_radius'
                        ? filters.centerRadius
                        : null
                    }
                    flyTo={mapFlyTo}
                    cities={cityOverlay.cities}
                    cityPolygons={cityPolygonsMap}
                    showCities={showCities}
                    onToggleShowCities={setShowCities}
                    colorByIndex={cityOverlay.colorByIndex}
                    cityIndexValues={cityOverlay.cityIndexValues}
                    cityIndexValuesAll={cityOverlay.cityIndexValuesAll}
                    cityIndexDefinitions={cityDefsQuery.data ?? []}
                    onColorByIndexChange={setColorByIndex}
                    rentMapPolygons={rentMapQuery.data ?? []}
                    rentMapKraje={rentKrajeQuery.data ?? []}
                    showRentMap={showRentMap}
                    rentVk={rentVk}
                    showKraje={showKraje}
                    onToggleShowRentMap={setShowRentMap}
                    onRentVkChange={setRentVk}
                    onToggleShowKraje={setShowKraje}
                    growthRows={psGrowthQuery.data ?? []}
                    growthDatasets={psDatasetsQuery.data ?? []}
                    showGrowth={showGrowth}
                    growthDatasetId={psGrowthDatasetId}
                    growthMetric={growthMetric}
                    growthFrom={growthFrom}
                    growthTo={growthTo}
                    onToggleShowGrowth={setShowGrowth}
                    onGrowthDatasetChange={setGrowthDatasetId}
                    onGrowthMetricChange={setGrowthMetric}
                    onGrowthFromChange={setGrowthFrom}
                    onGrowthToChange={setGrowthTo}
                    growthChartOnHover={growthChartOnHover}
                    growthHoverData={psHoverData}
                    onToggleGrowthChartOnHover={setGrowthChartOnHover}
                  />
                </Suspense>
              </div>
            </div>
          </div>
        )}

        {tabFromUrl !== 'map' && (
          <div className="px-6 pt-5 pb-8">
            {tabFromUrl === 'table' && (
              <ListingTable
                rows={table.isLoading ? null : table.rows}
                total={browseTotal}
                sort={sort}
                isLoading={table.isLoading}
                isFetchingNextPage={table.isFetchingNextPage}
                hasNextPage={table.hasNextPage}
                onReachEnd={table.fetchNextPage}
                hasFilters={!isDefault(filters)}
                hoveredIds={hoveredIds}
                onHover={setHoveredFromList}
                onSort={setSortByField}
                onClearFilters={() => setFilters(DEFAULT_FILTERS)}
              />
            )}
            {tabFromUrl === 'stats' && (
              <BrowseStatsView
                stats={statsQuery.data ?? null}
                isLoading={statsQuery.isLoading}
                isEmpty={!statsQuery.isLoading && (statsQuery.data?.total ?? 0) === 0}
                annotations={annotationsQuery.data?.data.annotations}
                annotationsLoading={annotationsQuery.isFetching}
              />
            )}
          </div>
        )}

        {activeError && (
          <div className="px-6 pb-6">
            <ErrorBanner error={activeError} />
          </div>
        )}
      </div>

      {watchdogModalOpen
        ? (() => {
            const { spec, unsupported } = filtersToWatchdogSpec(filters);
            return (
              <CreateWatchdogModal
                spec={spec}
                unsupported={unsupported}
                suggestedName={watchdogNameSuggestion(filters)}
                onClose={() => setWatchdogModalOpen(false)}
              />
            );
          })()
        : null}
    </div>
  );
}

/* Picks a sensible zoom level for a Mapy.cz suggestion. Country /
 * region suggestions zoom out far; address / POI picks zoom into the
 * specific block. The exact numbers were eyeballed against Prague and
 * Brno test picks — close enough to the right scale that the operator
 * doesn't have to immediately reach for the zoom controls. */
function zoomForSuggestionType(type: string): number {
  if (type.startsWith('regional.address')) return 16;
  if (type === 'regional.street') return 16;
  if (type === 'poi') return 16;
  if (type === 'regional.municipality_part') return 13;
  if (type === 'regional.municipality') return 11;
  if (type === 'regional.region') return 8;
  if (type === 'regional.country') return 6;
  return 13;
}

function defaultDirectionFor(field: SortField): 'asc' | 'desc' {
  if (
    field === 'price_czk' ||
    field === 'area_m2' ||
    field === 'last_seen_at' ||
    field === 'first_seen_at'
  ) return 'desc';
  return 'asc';
}

function FilterSummary({
  filters,
  count,
  loading,
  onClearBounds,
  onCreateWatchdog,
}: {
  filters: ListingFilters;
  count: number | null;
  loading: boolean;
  onClearBounds?: () => void;
  onCreateWatchdog: () => void;
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-2xl leading-tight">Browse</h1>
        <div className="mt-1 flex items-center gap-2 flex-wrap">
          <p className="text-sm text-[var(--color-ink-2)]">
            {loading && count == null ? 'Loading…' : summarise(filters, count)}
          </p>
          {onClearBounds && (
            <button
              type="button"
              onClick={onClearBounds}
              className="group inline-flex items-center gap-1 px-2 py-0.5 text-[0.7rem] tracking-wide rounded-[var(--radius-sm)] bg-[var(--color-copper-soft)] text-[var(--color-copper)] hover:bg-[var(--color-copper)]/15 transition-colors"
              title="Clear the map area filter and widen back to the full cohort"
            >
              <span>Map area applied</span>
              <span aria-hidden className="opacity-60 group-hover:opacity-100">
                ×
              </span>
            </button>
          )}
        </div>
      </div>
      <button
        type="button"
        onClick={onCreateWatchdog}
        className="shrink-0 px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors"
        title="Save the current filters as a watchdog — get notified when a new listing matches"
      >
        + Create watchdog
      </button>
    </div>
  );
}

function MapSkeleton() {
  return (
    <div className="h-full min-h-[480px] rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] flex items-center justify-center">
      <p className="text-sm text-[var(--color-ink-3)] tracking-wide">Loading map…</p>
    </div>
  );
}

/* Dedup merge-mode toolbar: a toggle that turns the Listings cards into a
 * multi-select, plus a "Merge N" CTA that folds the picked properties into one
 * via the dedup endpoint. Off → just the toggle. On → toggle (now "Cancel") +
 * the Merge CTA (disabled until ≥2 picked). */
function MergeModeBar({
  active,
  selectedCount,
  busy,
  onToggle,
  onMerge,
}: {
  active: boolean;
  selectedCount: number;
  busy: boolean;
  onToggle: () => void;
  onMerge: () => void;
}) {
  const btn = 'px-3 py-1.5 text-sm rounded-[var(--radius-sm)] transition-colors disabled:opacity-50';
  return (
    <div className="flex items-center gap-2 shrink-0">
      {active && (
        <>
          <span className="text-[0.75rem] text-[var(--color-ink-3)] tabular-nums">
            {selectedCount === 0
              ? 'Pick listings to merge'
              : `${selectedCount} selected`}
          </span>
          <button
            type="button"
            onClick={onMerge}
            disabled={busy || selectedCount < 2}
            className={`${btn} bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)]`}
          >
            {busy ? 'Merging…' : `Merge ${selectedCount >= 2 ? selectedCount : ''}`.trim()}
          </button>
        </>
      )}
      <button
        type="button"
        onClick={onToggle}
        disabled={busy}
        className={`${btn} border ${
          active
            ? 'border-[var(--color-rule)] text-[var(--color-ink-2)] hover:text-[var(--color-ink)] hover:border-[var(--color-rule-strong)]'
            : 'border-[var(--color-copper)] text-[var(--color-copper-2)] hover:bg-[var(--color-copper-soft)]'
        }`}
      >
        {active ? 'Cancel' : 'Merge mode'}
      </button>
    </div>
  );
}

function ErrorBanner({ error }: { error: Error }) {
  return (
    <div className="mt-4 p-3 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)] text-sm text-[var(--color-brick)]">
      <strong className="font-medium">Query failed:</strong> {error.message}
    </div>
  );
}
