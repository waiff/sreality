import { Suspense, lazy, useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import Tabs, { type Tab } from '@/components/Tabs';
import { FilterSidebar } from '@/components/Filters';
import {
  fromSearchParams,
  toSearchParams,
  summarise,
  type ListingFilters,
} from '@/lib/filters';
import { fetchListingsForMap, type MapResult } from '@/lib/queries';

const ListingMap = lazy(() => import('@/components/ListingMap'));

type TabKey = 'map' | 'table' | 'stats';

export default function Browse() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = useMemo(() => fromSearchParams(searchParams), [searchParams]);

  const setFilters = useCallback(
    (next: ListingFilters) => {
      const sp = toSearchParams(next);
      const tab = searchParams.get('tab');
      if (tab) sp.set('tab', tab);
      setSearchParams(sp, { replace: false });
    },
    [searchParams, setSearchParams],
  );

  const tabFromUrl = (searchParams.get('tab') ?? 'map') as TabKey;
  const setTab = (next: TabKey) => {
    const sp = new URLSearchParams(searchParams);
    if (next === 'map') sp.delete('tab');
    else sp.set('tab', next);
    setSearchParams(sp, { replace: true });
  };

  const mapQuery = useQuery<MapResult, Error>({
    queryKey: ['map', filters],
    queryFn: () => fetchListingsForMap(filters),
    placeholderData: (prev) => prev,
  });

  const total = mapQuery.data?.total ?? null;
  const tabs: ReadonlyArray<Tab<TabKey>> = [
    { key: 'map',   label: 'Map',   badge: total != null ? total.toLocaleString('cs-CZ') : undefined },
    { key: 'table', label: 'Table' },
    { key: 'stats', label: 'Stats' },
  ];

  return (
    <div className="flex">
      <FilterSidebar filters={filters} onChange={setFilters} />

      <div className="flex-1 min-w-0 px-6 pt-5 pb-8">
        <FilterSummary filters={filters} count={total} loading={mapQuery.isLoading} />

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
          {tabFromUrl === 'table' && <NotYet kind="Table" />}
          {tabFromUrl === 'stats' && <NotYet kind="Stats" />}
        </div>

        {mapQuery.error && <ErrorBanner error={mapQuery.error} />}
      </div>
    </div>
  );
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
    <div className="h-[calc(100dvh-14rem)] min-h-[480px] rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] flex items-center justify-center">
      <p className="text-sm text-[var(--color-ink-3)] tracking-wide">Loading map…</p>
    </div>
  );
}

function NotYet({ kind }: { kind: string }) {
  return (
    <section className="p-12 rounded-[var(--radius-md)] border border-dashed border-[var(--color-rule)] text-center">
      <p className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-4)]">
        Up next
      </p>
      <p className="mt-2 text-sm text-[var(--color-ink-3)]">
        {kind} tab lands in the next checkpoint. Filters above already apply
        globally — switching tabs preserves them.
      </p>
    </section>
  );
}

function ErrorBanner({ error }: { error: Error }) {
  return (
    <div className="mt-4 p-3 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)] text-sm text-[var(--color-brick)]">
      <strong className="font-medium">Query failed:</strong> {error.message}
    </div>
  );
}
