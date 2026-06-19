/* The Browse "experience": the filter sidebar + Map/Table/Stats tabs + the
 * map-overlay layers, with all data fetching and transient view-state (hover
 * sync, growth pickers, resizable columns, merge mode, on-card estimates).
 *
 * Lifted out of pages/Browse.tsx so the SAME experience powers both the Browse
 * page (URL-backed via useUrlBrowseState) and the "Explore area" modal
 * (in-memory via useMemoryBrowseState). The page-vs-modal differences are two
 * small props — `layout` (sticky full-page vs contained) and `features` (which
 * page-chrome bits to show). Everything cohort-related is identical, so the two
 * surfaces can never drift. */
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
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Tabs, { type Tab } from '@/components/Tabs';
import ResizeHandle from '@/components/ResizeHandle';
import { useSidebarWidth, useMapSplitFraction } from '@/lib/browseLayout';
import { FilterSidebar } from '@/components/Filters';
import ListingTable from '@/components/ListingTable';
import ListingCards from '@/components/ListingCards';
import BrowseStatsView from '@/components/BrowseStats';
import type { MapFlyToCommand } from '@/components/ListingMap';
import type { MapySuggestion } from '@/lib/maps';
import { fetchDatasets, fetchGrowth, fetchSeries, priceStatsKeys } from '@/lib/priceStats';
import { buildHoverData, type GrowthMetric } from '@/lib/growthChoropleth';
import {
  summarise,
  isDefault,
  regionKeyFromFilters,
  regionLabelFromFilters,
  filtersToWatchdogSpec,
  watchdogNameSuggestion,
  toSearchParams,
  DEFAULT_FILTERS,
  type ListingFilters,
} from '@/lib/filters';
import CreateWatchdogModal from '@/components/CreateWatchdogModal';
import PresetBar from '@/components/PresetBar';
import type { ListingEstimate } from '@/lib/types';
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
  sortToParam,
  CARD_PAGE_SIZE,
  TABLE_PAGE_SIZE,
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
  type TableRow,
} from '@/lib/queries';
import { useInfiniteList } from '@/lib/useInfiniteList';
import type { KeysetCursor } from '@/lib/keyset';
import type { BrowseViewState, TabKey } from '@/lib/browseState';

const ListingMap = lazy(() => import('@/components/ListingMap'));

export interface BrowseFeatures {
  /* Saved-filter preset bar — page only. */
  presetBar?: boolean;
  /* "Create watchdog" CTA + name-prompt modal. */
  watchdog?: boolean;
  /* Dedup merge mode toggle on the Listings cards — page only. */
  mergeMode?: boolean;
  /* The big "Browse" heading in the summary block — hidden in the modal,
   * which has its own header. */
  title?: boolean;
}

const DEFAULT_FEATURES: Required<BrowseFeatures> = {
  presetBar: true,
  watchdog: true,
  mergeMode: true,
  title: true,
};

export default function BrowseExperience({
  view,
  layout = 'page',
  features,
}: {
  view: BrowseViewState;
  layout?: 'page' | 'modal';
  features?: BrowseFeatures;
}) {
  const f = { ...DEFAULT_FEATURES, ...features };
  const isModal = layout === 'modal';
  const { filters, sort, tab, overlay, activePresetId } = view;

  /* Price-stats growth overlay control state — transient map-exploration knob,
   * kept in component state (not the view contract) on both surfaces. */
  const [showGrowth, setShowGrowth] = useState(false);
  const [growthDatasetId, setGrowthDatasetId] = useState<number | null>(null);
  const [growthMetric, setGrowthMetric] = useState<GrowthMetric>('rent_cagr_pct');
  const [growthFrom, setGrowthFrom] = useState('2015-01');
  const [growthTo, setGrowthTo] = useState(
    () => `${new Date().getFullYear()}-${String(new Date().getMonth() + 1).padStart(2, '0')}`,
  );
  const [growthChartOnHover, setGrowthChartOnHover] = useState(false);

  const setSortByField = (field: SortField) => {
    const next =
      sort.field === field
        ? { field, direction: (sort.direction === 'asc' ? 'desc' : 'asc') as 'asc' | 'desc' }
        : { field, direction: defaultDirectionFor(field) };
    view.setSort(next);
  };

  /* Map flyTo command. Set when the operator picks a place in the District
   * typeahead; ListingMap animates to the new centre. */
  const [mapFlyTo, setMapFlyTo] = useState<MapFlyToCommand | null>(null);
  const [watchdogModalOpen, setWatchdogModalOpen] = useState(false);

  /* Dedup merge mode (page only). */
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

  /* Hover-sync between cards / table / map. Ephemeral component state. */
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

  /* Operator-resizable columns. */
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
      mapSplit.set((r.right - clientX) / r.width);
    },
    [mapSplit],
  );

  /* Map tab fetches the map cohort (capped) + the paginated card slice. */
  const mapQuery = useQuery<MapResult, Error>({
    queryKey: ['map', filters],
    queryFn: () => fetchListingsForMap(filters),
    placeholderData: (prev) => prev,
    enabled: tab === 'map',
  });

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

  const cityPolygonsQuery = useQuery<CityPolygon[], Error>({
    queryKey: ['curated_city_polygons'],
    queryFn: fetchCuratedCityPolygons,
    enabled: tab === 'map',
    staleTime: Infinity,
    gcTime: Infinity,
  });
  const cityPolygonsMap = useMemo(() => {
    const m = new Map<number, string>();
    for (const p of cityPolygonsQuery.data ?? []) m.set(p.city_id, p.geojson);
    return m;
  }, [cityPolygonsQuery.data]);

  const rentMapQuery = useQuery<RentMapPolygon[], Error>({
    queryKey: ['rent_map_choropleth'],
    queryFn: fetchRentMapChoropleth,
    enabled: tab === 'map' && overlay.showRentMap,
    staleTime: Infinity,
    gcTime: Infinity,
  });
  const rentKrajeQuery = useQuery<RentMapKraj[], Error>({
    queryKey: ['rent_map_kraje'],
    queryFn: fetchRentMapKraje,
    enabled: tab === 'map' && overlay.showRentMap,
    staleTime: Infinity,
    gcTime: Infinity,
  });

  const psDatasetsQuery = useQuery({
    queryKey: priceStatsKeys.datasets,
    queryFn: fetchDatasets,
    enabled: tab === 'map' && showGrowth,
    staleTime: 60_000,
  });
  const psGrowthDatasetId = growthDatasetId ?? psDatasetsQuery.data?.[0]?.id ?? null;
  const psGrowthQuery = useQuery({
    queryKey: priceStatsKeys.growth(psGrowthDatasetId ?? -1, growthFrom, growthTo),
    queryFn: () => fetchGrowth(psGrowthDatasetId as number, growthFrom, growthTo),
    enabled: tab === 'map' && showGrowth && psGrowthDatasetId != null,
    staleTime: 60_000,
  });
  const psSeriesQuery = useQuery({
    queryKey: priceStatsKeys.obecSeries(psGrowthDatasetId ?? -1, growthFrom, growthTo),
    queryFn: () => fetchSeries(psGrowthDatasetId as number, growthFrom, growthTo),
    enabled: tab === 'map' && showGrowth && growthChartOnHover && psGrowthDatasetId != null,
    staleTime: 60_000,
  });
  const psHoverData = useMemo(
    () => (psSeriesQuery.data ? buildHoverData(psSeriesQuery.data, growthMetric) : null),
    [psSeriesQuery.data, growthMetric],
  );

  const cards = useInfiniteList<CardRow>({
    queryKey: ['cards', filters, sort],
    queryFn: (cursor) =>
      fetchListingsForCards(filters, sort, cursor as KeysetCursor | null),
    pageSize: CARD_PAGE_SIZE,
    getRowId: (r) => r.property_id,
    enabled: tab === 'map',
    gcTime: 10 * 60_000,
  });

  const browseCountQuery = useQuery<number, Error>({
    queryKey: ['browse-count', filters],
    queryFn: () => fetchBrowseCount(filters),
    enabled: tab === 'map' || tab === 'table',
    placeholderData: (prev) => prev,
    staleTime: 60_000,
  });
  const browseTotal = browseCountQuery.data ?? null;

  const cardsRestorationKey = useMemo(() => {
    const sp = toSearchParams(filters);
    sp.set('sort', sortToParam(sort));
    return `cards:${sp.toString()}`;
  }, [filters, sort]);

  const cardIds = useMemo(
    () => cards.rows.map((r) => r.sreality_id),
    [cards.rows],
  );
  const [estimatingIds, setEstimatingIds] = useState<ReadonlySet<number>>(
    () => new Set(),
  );
  const [estimates, setEstimates] = useState<Record<number, ListingEstimate>>(
    {},
  );
  const requestedEstIdsRef = useRef<Set<number>>(new Set());
  const estimatesEnabled = tab === 'map' && isApiConfigured();

  useEffect(() => {
    setEstimates({});
    requestedEstIdsRef.current = new Set();
  }, [cardsRestorationKey]);

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
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [cardIds, estimatesEnabled]);

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
    enabled: tab === 'table',
    gcTime: 10 * 60_000,
  });

  const statsQuery = useQuery<BrowseStats, Error>({
    queryKey: ['stats', filters],
    queryFn: () => fetchBrowseStats(filters),
    placeholderData: (prev) => prev,
    enabled: tab === 'stats',
  });

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
      tab === 'stats' && isApiConfigured() && boxDispositions.length > 0,
    staleTime: 60 * 60 * 1000,
    retry: false,
  });

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
    const colorDef = overlay.colorByIndexName
      ? defs.find((d) => d.index_name === overlay.colorByIndexName) ?? null
      : null;
    const colorMap = new Map<number, number>();
    if (colorDef) {
      for (const v of values) {
        if (v.index_name === colorDef.index_name) {
          colorMap.set(v.city_id, v.value);
        }
      }
    }
    const rulesPass = (city: CuratedCity): boolean => {
      for (const r of filters.cityIndexRules) {
        const v = allMap.get(`${city.city_id}:${r.index_name}`);
        if (v == null) return false;
        const op = r.op ?? '>=';
        if (op === '>=' && !(v >= r.value)) return false;
        if (op === '<=' && !(v <= r.value)) return false;
        if (op === '>' && !(v > r.value)) return false;
        if (op === '<' && !(v < r.value)) return false;
        if (op === '==' && !(v === r.value)) return false;
        if (op === '!=' && !(v !== r.value)) return false;
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
    overlay.colorByIndexName,
    filters.cityIndexRules,
    filters.minCityPopulation,
    filters.maxCityPopulation,
  ]);

  const totalForBadge =
    tab === 'stats' ? statsQuery.data?.total ?? null : browseTotal;

  const tabs: ReadonlyArray<Tab<TabKey>> = [
    { key: 'map', label: 'Listings', badge: totalForBadge != null ? totalForBadge.toLocaleString('cs-CZ') : undefined },
    { key: 'table', label: 'Table' },
    { key: 'stats', label: 'Stats' },
  ];

  const activeError =
    tab === 'map' ? mapQuery.error ?? cards.error :
    tab === 'table' ? table.error :
    tab === 'stats' ? statsQuery.error :
    null;

  return (
    <div className={`flex${isModal ? ' h-full' : ''}`} ref={outerRef}>
      <FilterSidebar
        filters={filters}
        onChange={view.setFilters}
        onLocationPick={handleLocationPick}
        width={sidebar.value}
        layout={layout}
      />

      <ResizeHandle
        ariaLabel="Resize the filters sidebar"
        onMove={onSidebarDrag}
        onEnd={sidebar.persist}
        onReset={sidebar.reset}
        className={
          isModal
            ? 'hidden lg:flex self-stretch h-full w-3 -mx-1.5'
            : 'hidden lg:flex sticky top-14 self-start h-[calc(100dvh-3.5rem)] w-3 -mx-1.5'
        }
      />

      <div
        className={`flex-1 min-w-0 flex flex-col${
          isModal
            ? ' h-full min-h-0'
            : tab === 'map'
              ? ' h-[calc(100dvh-3.5rem)]'
              : ''
        }`}
      >
        <div className="px-6 pt-5">
          <FilterSummary
            filters={filters}
            count={totalForBadge}
            showTitle={f.title}
            loading={
              tab === 'map' ? mapQuery.isLoading || cards.isLoading :
              tab === 'table' ? table.isLoading :
              tab === 'stats' ? statsQuery.isLoading :
              false
            }
            onClearBounds={filters.bounds ? () => view.setBounds(null) : undefined}
            onCreateWatchdog={f.watchdog ? () => setWatchdogModalOpen(true) : undefined}
          />
          {f.presetBar && (
            <div className="mt-3">
              <PresetBar
                filters={filters}
                sort={sort}
                activePresetId={activePresetId}
                onLoad={view.loadPreset}
                onActivePresetIdChange={view.setActivePresetId}
              />
            </div>
          )}
          <div className="mt-4 flex items-center justify-between gap-3">
            <Tabs tabs={tabs} active={tab} onChange={view.setTab} />
            {tab === 'map' && f.mergeMode && (
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

        {tab === 'map' && (
          <div className="px-6 pt-5 pb-6 flex-1 min-h-0">
            <div
              ref={innerRef}
              className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_1rem_var(--map-w)] h-full min-h-[560px]"
              style={{ '--map-w': `${mapSplit.value * 100}%` } as CSSProperties}
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
                onSort={view.setSort}
                onClearFilters={() => view.setFilters(DEFAULT_FILTERS)}
                onClearBounds={() => view.setBounds(null)}
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
                    onBoundsChange={view.setBounds}
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
                    showCities={overlay.showCities}
                    onToggleShowCities={(next) => view.setOverlay({ showCities: next })}
                    colorByIndex={cityOverlay.colorByIndex}
                    cityIndexValues={cityOverlay.cityIndexValues}
                    cityIndexValuesAll={cityOverlay.cityIndexValuesAll}
                    cityIndexDefinitions={cityDefsQuery.data ?? []}
                    onColorByIndexChange={(name) => view.setOverlay({ colorByIndexName: name })}
                    rentMapPolygons={rentMapQuery.data ?? []}
                    rentMapKraje={rentKrajeQuery.data ?? []}
                    showRentMap={overlay.showRentMap}
                    rentVk={overlay.rentVk}
                    showKraje={overlay.showKraje}
                    onToggleShowRentMap={(next) => view.setOverlay({ showRentMap: next })}
                    onRentVkChange={(vk) => view.setOverlay({ rentVk: vk })}
                    onToggleShowKraje={(next) => view.setOverlay({ showKraje: next })}
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

        {tab !== 'map' && (
          <div className={`px-6 pt-5 pb-8${isModal ? ' flex-1 min-h-0 overflow-y-auto' : ''}`}>
            {tab === 'table' && (
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
                onClearFilters={() => view.setFilters(DEFAULT_FILTERS)}
              />
            )}
            {tab === 'stats' && (
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

      {f.watchdog && watchdogModalOpen
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
  showTitle,
  onClearBounds,
  onCreateWatchdog,
}: {
  filters: ListingFilters;
  count: number | null;
  loading: boolean;
  showTitle: boolean;
  onClearBounds?: () => void;
  onCreateWatchdog?: () => void;
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0">
        {showTitle && <h1 className="text-2xl leading-tight">Browse</h1>}
        <div className={showTitle ? 'mt-1 flex items-center gap-2 flex-wrap' : 'flex items-center gap-2 flex-wrap'}>
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
      {onCreateWatchdog && (
        <button
          type="button"
          onClick={onCreateWatchdog}
          className="shrink-0 px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors"
          title="Save the current filters as a watchdog — get notified when a new listing matches"
        >
          + Create watchdog
        </button>
      )}
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
