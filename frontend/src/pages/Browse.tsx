import { Suspense, lazy, useCallback, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import Tabs, { type Tab } from '@/components/Tabs';
import { FilterSidebar } from '@/components/Filters';
import ListingTable from '@/components/ListingTable';
import ListingCards from '@/components/ListingCards';
import BrowseStatsView from '@/components/BrowseStats';
import type { MapFlyToCommand } from '@/components/ListingMap';
import type { MapySuggestion } from '@/lib/maps';
import {
  fromSearchParams,
  toSearchParams,
  summarise,
  isDefault,
  regionKeyFromFilters,
  regionLabelFromFilters,
  DEFAULT_FILTERS,
  type ListingFilters,
  type MapBounds,
} from '@/lib/filters';
import { fetchRegionDispositionAnnotations, isApiConfigured } from '@/lib/api';
import {
  fetchCityIndexDefinitions,
  fetchCityIndexValues,
  fetchCuratedCities,
  fetchListingsForCards,
  fetchListingsForMap,
  fetchListingsForTable,
  fetchBrowseStats,
  parseSort,
  sortToParam,
  DEFAULT_SORT,
  type BrowseStats,
  type CardsResult,
  type CityIndexDefinition,
  type CityIndexValue,
  type CuratedCity,
  type MapResult,
  type SortField,
  type SortSpec,
  type TableResult,
} from '@/lib/queries';

const ListingMap = lazy(() => import('@/components/ListingMap'));

type TabKey = 'map' | 'table' | 'stats';

export default function Browse() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = useMemo(() => fromSearchParams(searchParams), [searchParams]);
  const sort: SortSpec = useMemo(
    () => parseSort(searchParams.get('sort')),
    [searchParams],
  );
  const page = Math.max(1, parseInt(searchParams.get('page') ?? '1', 10) || 1);
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

  /* Copy URL keys that live outside `toSearchParams` (tab, sort, the
   * city-overlay knobs). Used by every URL rewriter on this page so
   * `setBounds` / `setFilters` / `writeSort` / `setPage` can't drop
   * them by accident. `page` is deliberately omitted — callers that
   * want to reset paging should not call `preserveExtras`. */
  const preserveExtras = useCallback(
    (sp: URLSearchParams): URLSearchParams => {
      for (const key of ['tab', 'sort', 'cities', 'colorby']) {
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

  const setFilters = useCallback(
    (next: ListingFilters) => {
      const sp = preserveExtras(toSearchParams(next));
      // page is intentionally dropped — new filter set, reset to first page.
      setSearchParams(sp, { replace: false });
    },
    [preserveExtras, setSearchParams],
  );

  const setTab = (next: TabKey) => {
    const sp = new URLSearchParams(searchParams);
    if (next === 'map') sp.delete('tab');
    else sp.set('tab', next);
    sp.delete('page');
    setSearchParams(sp, { replace: true });
  };

  const writeSort = (next: SortSpec) => {
    const sp = new URLSearchParams(searchParams);
    sp.delete('page');
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

  const setPage = (next: number) => {
    const sp = new URLSearchParams(searchParams);
    if (next <= 1) sp.delete('page');
    else sp.set('page', String(next));
    setSearchParams(sp, { replace: false });
  };

  /* Map flyTo command. Set when the operator picks a place in the
   * District typeahead; ListingMap watches this prop and animates to
   * the new centre. The `ts` field guarantees identity changes on
   * each pick so re-picking the same place still triggers a flyTo. */
  const [mapFlyTo, setMapFlyTo] = useState<MapFlyToCommand | null>(null);
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
   * highlight together. */
  const [hoveredIds, setHoveredIds] = useState<ReadonlySet<number>>(
    () => new Set(),
  );
  const setHovered = useCallback(
    (ids: ReadonlyArray<number> | null) => {
      setHoveredIds((prev) => {
        if (ids == null || ids.length === 0) {
          return prev.size === 0 ? prev : new Set();
        }
        // Avoid spurious re-renders if the same id is reported twice
        // (maplibre emits mouseenter on every move within the layer).
        if (prev.size === ids.length && ids.every((id) => prev.has(id))) {
          return prev;
        }
        return new Set(ids);
      });
    },
    [],
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

  const cardsQuery = useQuery<CardsResult, Error>({
    queryKey: ['cards', filters, sort, page],
    queryFn: () => fetchListingsForCards(filters, sort, page),
    placeholderData: (prev) => prev,
    enabled: tabFromUrl === 'map',
  });

  const tableQuery = useQuery<TableResult, Error>({
    queryKey: ['table', filters, sort, page],
    queryFn: () => fetchListingsForTable(filters, sort, page),
    placeholderData: (prev) => prev,
    enabled: tabFromUrl === 'table',
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

  const totalForBadge =
    tabFromUrl === 'table'
      ? tableQuery.data?.total ?? null
      : tabFromUrl === 'stats'
        ? statsQuery.data?.total ?? null
        : mapQuery.data?.total ?? null;

  const tabs: ReadonlyArray<Tab<TabKey>> = [
    { key: 'map',   label: 'Listings', badge: totalForBadge != null ? totalForBadge.toLocaleString('cs-CZ') : undefined },
    { key: 'table', label: 'Table' },
    { key: 'stats', label: 'Stats' },
  ];

  const activeError =
    tabFromUrl === 'map'   ? mapQuery.error ?? cardsQuery.error :
    tabFromUrl === 'table' ? tableQuery.error :
    tabFromUrl === 'stats' ? statsQuery.error :
    null;

  return (
    <div className="flex">
      <FilterSidebar
        filters={filters}
        onChange={setFilters}
        onLocationPick={handleLocationPick}
      />

      <div className="flex-1 min-w-0 flex flex-col">
        <div className="px-6 pt-5">
          <FilterSummary
            filters={filters}
            count={totalForBadge}
            loading={
              tabFromUrl === 'map'   ? mapQuery.isLoading || cardsQuery.isLoading :
              tabFromUrl === 'table' ? tableQuery.isLoading :
              tabFromUrl === 'stats' ? statsQuery.isLoading :
              false
            }
            onClearBounds={filters.bounds ? () => setBounds(null) : undefined}
          />
          <div className="mt-4">
            <Tabs tabs={tabs} active={tabFromUrl} onChange={setTab} />
          </div>
        </div>

        {tabFromUrl === 'map' && (
          <div className="px-6 pt-5 pb-6 flex-1 min-h-0">
            {/* 3-column inner layout: cards (left) | map (right). The
              * outer FilterSidebar is column 1. Heights are pinned so
              * each column scrolls independently — the map stays put
              * while the cards list scrolls. */}
            <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_minmax(360px,42%)] gap-5 h-[calc(100dvh-12rem)] min-h-[560px]">
              <ListingCards
                rows={cardsQuery.data?.rows ?? null}
                total={cardsQuery.data?.total ?? null}
                page={page}
                sort={sort}
                isLoading={cardsQuery.isLoading}
                hasFilters={!isDefault(filters)}
                hasBounds={filters.bounds != null}
                hoveredIds={hoveredIds}
                onHover={setHovered}
                onPage={setPage}
                onSort={writeSort}
                onClearFilters={() => setFilters(DEFAULT_FILTERS)}
                onClearBounds={() => setBounds(null)}
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
                    onHover={setHovered}
                    centerCircle={
                      filters.locationMode === 'center_radius'
                        ? filters.centerRadius
                        : null
                    }
                    flyTo={mapFlyTo}
                    cities={cityOverlay.cities}
                    showCities={showCities}
                    onToggleShowCities={setShowCities}
                    colorByIndex={cityOverlay.colorByIndex}
                    cityIndexValues={cityOverlay.cityIndexValues}
                    cityIndexValuesAll={cityOverlay.cityIndexValuesAll}
                    cityIndexDefinitions={cityDefsQuery.data ?? []}
                    onColorByIndexChange={setColorByIndex}
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
                rows={tableQuery.data?.rows ?? null}
                total={tableQuery.data?.total ?? null}
                page={page}
                sort={sort}
                isLoading={tableQuery.isLoading}
                hasFilters={!isDefault(filters)}
                hoveredIds={hoveredIds}
                onHover={setHovered}
                onSort={setSortByField}
                onPage={setPage}
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
}: {
  filters: ListingFilters;
  count: number | null;
  loading: boolean;
  onClearBounds?: () => void;
}) {
  return (
    <div>
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
  );
}

function MapSkeleton() {
  return (
    <div className="h-full min-h-[480px] rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] flex items-center justify-center">
      <p className="text-sm text-[var(--color-ink-3)] tracking-wide">Loading map…</p>
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
