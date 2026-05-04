import { Suspense, lazy, useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import Tabs, { type Tab } from '@/components/Tabs';
import { FilterSidebar } from '@/components/Filters';
import ListingTable from '@/components/ListingTable';
import BrowseStatsView from '@/components/BrowseStats';
import {
  fromSearchParams,
  toSearchParams,
  summarise,
  isDefault,
  DEFAULT_FILTERS,
  type ListingFilters,
} from '@/lib/filters';
import {
  fetchListingsForMap,
  fetchListingsForTable,
  fetchBrowseStats,
  parseSort,
  sortToParam,
  DEFAULT_SORT,
  type BrowseStats,
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

  const setFilters = useCallback(
    (next: ListingFilters) => {
      const sp = toSearchParams(next);
      const tab = searchParams.get('tab');
      const sortRaw = searchParams.get('sort');
      if (tab) sp.set('tab', tab);
      if (sortRaw) sp.set('sort', sortRaw);
      // page is intentionally dropped — new filter set, reset to first page.
      setSearchParams(sp, { replace: false });
    },
    [searchParams, setSearchParams],
  );

  const setTab = (next: TabKey) => {
    const sp = new URLSearchParams(searchParams);
    if (next === 'map') sp.delete('tab');
    else sp.set('tab', next);
    setSearchParams(sp, { replace: true });
  };

  const setSort = (field: SortField) => {
    const next: SortSpec =
      sort.field === field
        ? { field, direction: sort.direction === 'asc' ? 'desc' : 'asc' }
        : { field, direction: defaultDirectionFor(field) };
    const sp = new URLSearchParams(searchParams);
    sp.delete('page');
    if (sortToParam(next) === sortToParam(DEFAULT_SORT)) sp.delete('sort');
    else sp.set('sort', sortToParam(next));
    setSearchParams(sp, { replace: false });
  };

  const setPage = (next: number) => {
    const sp = new URLSearchParams(searchParams);
    if (next <= 1) sp.delete('page');
    else sp.set('page', String(next));
    setSearchParams(sp, { replace: false });
  };

  const mapQuery = useQuery<MapResult, Error>({
    queryKey: ['map', filters],
    queryFn: () => fetchListingsForMap(filters),
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

  const totalForBadge =
    tabFromUrl === 'table'
      ? tableQuery.data?.total ?? null
      : tabFromUrl === 'stats'
        ? statsQuery.data?.total ?? null
        : mapQuery.data?.total ?? null;

  const tabs: ReadonlyArray<Tab<TabKey>> = [
    { key: 'map',   label: 'Map',   badge: totalForBadge != null ? totalForBadge.toLocaleString('cs-CZ') : undefined },
    { key: 'table', label: 'Table' },
    { key: 'stats', label: 'Stats' },
  ];

  const activeError =
    tabFromUrl === 'map'   ? mapQuery.error   :
    tabFromUrl === 'table' ? tableQuery.error :
    tabFromUrl === 'stats' ? statsQuery.error :
    null;

  return (
    <div className="flex">
      <FilterSidebar filters={filters} onChange={setFilters} />

      <div className="flex-1 min-w-0 px-6 pt-5 pb-8">
        <FilterSummary
          filters={filters}
          count={totalForBadge}
          loading={
            tabFromUrl === 'map'   ? mapQuery.isLoading   :
            tabFromUrl === 'table' ? tableQuery.isLoading :
            tabFromUrl === 'stats' ? statsQuery.isLoading :
            false
          }
        />

        <div className="mt-4">
          <Tabs tabs={tabs} active={tabFromUrl} onChange={setTab} />
        </div>

        <div className="mt-5">
          {tabFromUrl === 'map' && (
            <Suspense fallback={<MapSkeleton />}>
              <ListingMap
                rows={mapQuery.data?.rows ?? []}
                total={mapQuery.data?.total ?? null}
                capped={mapQuery.data?.capped ?? false}
                isLoading={mapQuery.isLoading}
              />
            </Suspense>
          )}
          {tabFromUrl === 'table' && (
            <ListingTable
              rows={tableQuery.data?.rows ?? null}
              total={tableQuery.data?.total ?? null}
              page={page}
              sort={sort}
              isLoading={tableQuery.isLoading}
              hasFilters={!isDefault(filters)}
              onSort={setSort}
              onPage={setPage}
              onClearFilters={() => setFilters(DEFAULT_FILTERS)}
            />
          )}
          {tabFromUrl === 'stats' && (
            <BrowseStatsView
              stats={statsQuery.data ?? null}
              isLoading={statsQuery.isLoading}
              isEmpty={!statsQuery.isLoading && (statsQuery.data?.total ?? 0) === 0}
            />
          )}
        </div>

        {activeError && <ErrorBanner error={activeError} />}
      </div>
    </div>
  );
}

function defaultDirectionFor(field: SortField): 'asc' | 'desc' {
  if (field === 'price_czk' || field === 'area_m2' || field === 'last_seen_at') return 'desc';
  return 'asc';
}

function FilterSummary({
  filters,
  count,
  loading,
}: {
  filters: ListingFilters;
  count: number | null;
  loading: boolean;
}) {
  return (
    <div>
      <h1 className="text-2xl leading-tight">Browse</h1>
      <p className="mt-1 text-sm text-[var(--color-ink-2)]">
        {loading && count == null ? 'Loading…' : summarise(filters, count)}
      </p>
    </div>
  );
}

function MapSkeleton() {
  return (
    <div className="h-[calc(100dvh-16rem)] min-h-[480px] rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] flex items-center justify-center">
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
