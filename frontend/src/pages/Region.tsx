import { Suspense, lazy, useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import RegionPicker, { type PickerMode } from '@/components/region/RegionPicker';
import RangeStrip from '@/components/region/RangeStrip';
import DispositionTable from '@/components/region/DispositionTable';
import {
  fetchRegionStats,
  fetchRegionActiveByDay,
  isRegionDefined,
  type RegionMode,
} from '@/lib/queries';
import {
  fmtCount,
  fmtCzk,
  fmtAbsolute,
} from '@/lib/format';
import type { ActiveByDayRow, RegionStats } from '@/lib/types';

const RegionCharts = lazy(() =>
  import('@/components/region/RegionCharts').then((m) => ({
    default: function Both({ data }: { data: ActiveByDayRow[] }) {
      return (
        <>
          <ChartSection
            title="Active per day · last 90 days"
            subtitle="Inferred census — listings are counted from first_seen_at and reappearance, not from per-day audits."
          >
            <m.ActiveByDayChart data={data} />
          </ChartSection>
          <ChartSection title="New listings · last 12 weeks" subtitle="By first_seen_at, rolled up to ISO weeks.">
            <m.NewByWeekChart data={data} />
          </ChartSection>
        </>
      );
    },
  })),
);

const PRAGUE = { lng: 14.4378, lat: 50.0755 };
const DEFAULT_RADIUS_M = 1000;

const NBSP = ' ';
const fmtPpm2 = (n: number): string =>
  `${new Intl.NumberFormat('cs-CZ').format(n)}${NBSP}Kč/m²`;

const SMALL_SAMPLE_THRESHOLD = 10;

interface UrlState {
  mode: PickerMode;
  districts: string[];
  center: { lng: number; lat: number };
  radiusM: number;
}

const parseUrlState = (sp: URLSearchParams): UrlState => {
  const districts = sp.get('districts');
  const lat = sp.get('lat');
  const lng = sp.get('lng');
  const r = sp.get('r');
  const explicit = sp.get('mode');
  const mode: PickerMode =
    explicit === 'radius' || explicit === 'districts'
      ? explicit
      : lat != null && lng != null
        ? 'radius'
        : 'districts';
  const parsedDistricts = districts
    ? districts.split(',').map(decodeURIComponent).filter(Boolean)
    : [];
  const parsedLat = lat != null ? Number(lat) : NaN;
  const parsedLng = lng != null ? Number(lng) : NaN;
  const parsedR = r != null ? Number(r) : NaN;
  return {
    mode,
    districts: parsedDistricts,
    center: {
      lng: Number.isFinite(parsedLng) ? parsedLng : PRAGUE.lng,
      lat: Number.isFinite(parsedLat) ? parsedLat : PRAGUE.lat,
    },
    radiusM: Number.isFinite(parsedR) ? parsedR : DEFAULT_RADIUS_M,
  };
};

const writeUrlState = (state: UrlState): URLSearchParams => {
  const sp = new URLSearchParams();
  sp.set('mode', state.mode);
  if (state.mode === 'districts' && state.districts.length > 0) {
    sp.set('districts', state.districts.map(encodeURIComponent).join(','));
  }
  if (state.mode === 'radius') {
    sp.set('lat', state.center.lat.toFixed(5));
    sp.set('lng', state.center.lng.toFixed(5));
    sp.set('r', String(Math.round(state.radiusM)));
  }
  return sp;
};

export default function Region() {
  const [searchParams, setSearchParams] = useSearchParams();
  const state = useMemo(() => parseUrlState(searchParams), [searchParams]);

  const update = useCallback(
    (next: UrlState) => setSearchParams(writeUrlState(next), { replace: false }),
    [setSearchParams],
  );

  const regionMode: RegionMode | null = useMemo(() => {
    if (state.mode === 'districts') {
      return state.districts.length > 0
        ? { kind: 'districts', districts: state.districts }
        : null;
    }
    return {
      kind: 'radius',
      lng: state.center.lng,
      lat: state.center.lat,
      radiusM: state.radiusM,
    };
  }, [state]);

  const enabled = regionMode != null && isRegionDefined(regionMode);

  const statsQuery = useQuery<RegionStats, Error>({
    queryKey: ['region-stats', regionMode],
    queryFn: () => fetchRegionStats(regionMode!),
    enabled,
    placeholderData: (prev) => prev,
  });

  const seriesQuery = useQuery<ActiveByDayRow[], Error>({
    queryKey: ['region-active-by-day', regionMode],
    queryFn: () => fetchRegionActiveByDay(regionMode!, 90),
    enabled,
    placeholderData: (prev) => prev,
  });

  const stats = statsQuery.data ?? null;
  const series = seriesQuery.data ?? null;

  return (
    <div className="px-6 py-6 max-w-screen-2xl mx-auto">
      <div className="lg:grid lg:grid-cols-[360px_minmax(0,1fr)] lg:gap-10 space-y-6 lg:space-y-0">
        <RegionPicker
          mode={state.mode}
          districts={state.districts}
          center={state.center}
          radiusM={state.radiusM}
          onModeChange={(mode) => update({ ...state, mode })}
          onDistrictsChange={(districts) => update({ ...state, districts })}
          onCenterChange={(center) => update({ ...state, center })}
          onRadiusChange={(radiusM) => update({ ...state, radiusM })}
        />

        <div className="min-w-0 space-y-8">
          <Header state={state} stats={stats} loading={statsQuery.isLoading} />

          {!enabled && (
            <EmptyHint mode={state.mode} />
          )}

          {enabled && statsQuery.error && (
            <ErrorBanner error={statsQuery.error} />
          )}

          {enabled && statsQuery.isLoading && stats == null && (
            <Skeleton />
          )}

          {enabled && stats != null && (
            <Report
              stats={stats}
              series={series}
              seriesLoading={seriesQuery.isLoading}
              seriesError={seriesQuery.error}
            />
          )}
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Header — page title + the definition line                                  */
/* -------------------------------------------------------------------------- */

function Header({
  state,
  stats,
  loading,
}: {
  state: UrlState;
  stats: RegionStats | null;
  loading: boolean;
}) {
  const definition =
    state.mode === 'districts'
      ? state.districts.length === 0
        ? 'Pick one or more districts.'
        : describeDistricts(state.districts)
      : `${(state.radiusM / 1000).toFixed(state.radiusM % 1000 === 0 ? 0 : 2)} km around ${state.center.lat.toFixed(4)}, ${state.center.lng.toFixed(4)}`;
  const tail = stats == null
    ? loading
      ? '· loading…'
      : ''
    : ` · ${fmtCount(stats.total_ever)} listings ever seen`;
  return (
    <div>
      <h1 className="text-2xl leading-tight">Region</h1>
      <p className="mt-1 text-sm text-[var(--color-ink-2)]">
        <span className="text-[var(--color-ink)]">{definition}</span>
        <span className="text-[var(--color-ink-3)]">{tail}</span>
      </p>
    </div>
  );
}

const describeDistricts = (ds: string[]): string => {
  if (ds.length === 0) return '—';
  if (ds.length === 1) return ds[0];
  if (ds.length <= 3) return ds.join(' · ');
  return `${ds.slice(0, 2).join(' · ')} +${ds.length - 2} more`;
};

/* -------------------------------------------------------------------------- */
/* Report body                                                                */
/* -------------------------------------------------------------------------- */

function Report({
  stats,
  series,
  seriesLoading,
  seriesError,
}: {
  stats: RegionStats;
  series: ActiveByDayRow[] | null;
  seriesLoading: boolean;
  seriesError: Error | null;
}) {
  const small = stats.total_active < SMALL_SAMPLE_THRESHOLD;

  return (
    <>
      <Section>
        <Census stats={stats} />
      </Section>

      {small ? (
        <p className="text-sm text-[var(--color-ink-3)] italic">
          Small sample (n &lt; {SMALL_SAMPLE_THRESHOLD}) — distribution and charts suppressed.
        </p>
      ) : (
        <>
          <Section title="Price overview">
            <div className="space-y-5">
              {stats.price ? (
                <RangeStrip label="Total CZK" triple={stats.price} format={fmtCzk} />
              ) : (
                <NotEnough label="Total CZK" />
              )}
              {stats.ppm2 ? (
                <RangeStrip label="Per m²" triple={stats.ppm2} format={fmtPpm2} />
              ) : (
                <NotEnough label="Per m²" />
              )}
            </div>
          </Section>

          <Section title="By disposition">
            <DispositionTable rows={stats.dispositions} />
          </Section>

          <Suspense fallback={<ChartsFallback />}>
            {seriesError ? (
              <ErrorBanner error={seriesError} />
            ) : seriesLoading && series == null ? (
              <ChartsFallback />
            ) : series ? (
              <RegionCharts data={series} />
            ) : null}
          </Suspense>

          <Section title="Time on market — delisted only">
            <TimeOnMarket stats={stats} />
          </Section>
        </>
      )}
    </>
  );
}

function Census({ stats }: { stats: RegionStats }) {
  return (
    <div className="grid grid-cols-3 gap-6">
      <Stat
        label="Active"
        value={fmtCount(stats.total_active)}
      />
      <Stat
        label="Ever seen"
        value={fmtCount(stats.total_ever)}
      />
      <Stat
        label="Last new"
        value={
          stats.last_new_first_seen
            ? fmtAbsolute(stats.last_new_first_seen).slice(0, 10)
            : '—'
        }
        subtle
      />
    </div>
  );
}

function Stat({
  label,
  value,
  subtle = false,
}: {
  label: string;
  value: string;
  subtle?: boolean;
}) {
  return (
    <div>
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        {label}
      </p>
      <p
        className={[
          'mt-1.5 font-display tabular-nums',
          subtle ? 'text-[1.25rem] text-[var(--color-ink-2)]' : 'text-[1.6rem] text-[var(--color-ink)]',
        ].join(' ')}
      >
        {value}
      </p>
    </div>
  );
}

function TimeOnMarket({ stats }: { stats: RegionStats }) {
  if (stats.tom_n === 0 || stats.tom_median_days == null) {
    return (
      <p className="text-sm text-[var(--color-ink-3)] italic">
        No delisted listings in this region yet.
      </p>
    );
  }
  return (
    <p className="font-display text-[1.4rem] tabular-nums text-[var(--color-ink)]">
      {stats.tom_median_days.toLocaleString('cs-CZ', { maximumFractionDigits: 1 })}{' '}
      <span className="text-sm text-[var(--color-ink-3)] font-sans">
        median days · n = {stats.tom_n}
      </span>
    </p>
  );
}

/* -------------------------------------------------------------------------- */
/* Layout primitives                                                          */
/* -------------------------------------------------------------------------- */

function Section({
  title,
  subtitle,
  children,
}: {
  title?: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="pt-6 first:pt-0 border-t first:border-t-0 border-[var(--color-rule-soft)]">
      {title && (
        <div className="mb-3">
          <h2 className="text-[0.85rem] tracking-[0.04em] font-medium text-[var(--color-ink-2)]">
            {title}
          </h2>
          {subtitle && (
            <p className="mt-1 text-[0.75rem] text-[var(--color-ink-3)]">
              {subtitle}
            </p>
          )}
        </div>
      )}
      {children}
    </section>
  );
}

function ChartSection({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: React.ReactNode;
}) {
  return (
    <Section title={title} subtitle={subtitle}>
      {children}
    </Section>
  );
}

function NotEnough({ label }: { label: string }) {
  return (
    <p className="text-sm text-[var(--color-ink-3)] italic">
      Not enough data for {label} percentiles.
    </p>
  );
}

function EmptyHint({ mode }: { mode: PickerMode }) {
  return (
    <section className="p-12 rounded-[var(--radius-md)] border border-dashed border-[var(--color-rule)] text-center">
      <p className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-4)]">
        Region undefined
      </p>
      <p className="mt-2 text-sm text-[var(--color-ink-3)]">
        {mode === 'districts'
          ? 'Pick one or more districts in the panel to summarise.'
          : 'Click the map or drag the pin to set a centre.'}
      </p>
    </section>
  );
}

function Skeleton() {
  return (
    <div className="space-y-4">
      <div className="h-16 rounded-[var(--radius-sm)] bg-[var(--color-paper-2)] border border-[var(--color-rule-soft)] animate-pulse" />
      <div className="h-24 rounded-[var(--radius-sm)] bg-[var(--color-paper-2)] border border-[var(--color-rule-soft)] animate-pulse" />
      <div className="h-40 rounded-[var(--radius-sm)] bg-[var(--color-paper-2)] border border-[var(--color-rule-soft)] animate-pulse" />
    </div>
  );
}

function ChartsFallback() {
  return (
    <div className="h-[260px] rounded-[var(--radius-sm)] bg-[var(--color-paper-2)] border border-[var(--color-rule-soft)] animate-pulse" />
  );
}

function ErrorBanner({ error }: { error: Error }) {
  return (
    <div className="p-3 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)] text-sm text-[var(--color-brick)]">
      <strong className="font-medium">Query failed:</strong> {error.message}
    </div>
  );
}
