import { Suspense, lazy, useCallback, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import LocationSearchBox from '@/components/LocationSearchBox';
import RegionPicker, { type PickerMode } from '@/components/region/RegionPicker';
import RangeStrip from '@/components/region/RangeStrip';
import DispositionBoxPlots from '@/components/region/DispositionBoxPlots';
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
import type { LocationResolution } from '@/lib/maps';
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

/* -------------------------------------------------------------------------- */
/* State machine                                                              */
/* -------------------------------------------------------------------------- */

type PolygonLevel = 'obec' | 'okres' | 'kraj' | 'ku';

export type RegionState =
  | { mode: 'none' }
  | {
      mode: 'polygon';
      level: PolygonLevel;
      polygonId: number;
      label: string;
      lat: number;
      lng: number;
      defaultRadiusM: number;
    }
  | { mode: 'radius'; lat: number; lng: number; radiusM: number; label: string }
  | { mode: 'legacy_districts'; districts: string[] }
  | { mode: 'legacy_radius'; lat: number; lng: number; radiusM: number };

const POLYGON_LEVELS: ReadonlyArray<PolygonLevel> = ['obec', 'okres', 'kraj', 'ku'];

const num = (raw: string | null): number | null => {
  if (raw == null) return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
};

const parseUrlState = (sp: URLSearchParams): RegionState => {
  const mode = sp.get('mode');
  const lat = num(sp.get('lat'));
  const lng = num(sp.get('lng'));
  const r = num(sp.get('r'));
  const q = sp.get('q') ?? '';
  const districtsRaw = sp.get('districts');
  const districts = districtsRaw
    ? districtsRaw.split(',').map(decodeURIComponent).filter(Boolean)
    : [];

  if (mode === 'polygon') {
    const level = sp.get('polygon_level');
    const id = num(sp.get('polygon_id'));
    if (
      level &&
      (POLYGON_LEVELS as ReadonlyArray<string>).includes(level) &&
      id != null &&
      lat != null &&
      lng != null &&
      r != null
    ) {
      return {
        mode: 'polygon',
        level: level as PolygonLevel,
        polygonId: id,
        label: q,
        lat,
        lng,
        defaultRadiusM: r,
      };
    }
    return { mode: 'none' };
  }

  if (mode === 'radius' && lat != null && lng != null && r != null) {
    return { mode: 'radius', lat, lng, radiusM: r, label: q };
  }

  if (mode === 'legacy_districts' && districts.length > 0) {
    return { mode: 'legacy_districts', districts };
  }

  if (mode === 'legacy_radius' && lat != null && lng != null && r != null) {
    return { mode: 'legacy_radius', lat, lng, radiusM: r };
  }

  /* Backwards-compat for browse-1 URLs that didn't encode an explicit mode. */
  if (mode == null) {
    if (districts.length > 0) return { mode: 'legacy_districts', districts };
    if (lat != null && lng != null && r != null) {
      return { mode: 'legacy_radius', lat, lng, radiusM: r };
    }
  }

  return { mode: 'none' };
};

const writeUrlState = (state: RegionState): URLSearchParams => {
  const sp = new URLSearchParams();
  if (state.mode === 'none') return sp;
  sp.set('mode', state.mode);

  if (state.mode === 'polygon') {
    sp.set('polygon_level', state.level);
    sp.set('polygon_id', String(state.polygonId));
    sp.set('lat', state.lat.toFixed(5));
    sp.set('lng', state.lng.toFixed(5));
    sp.set('r', String(Math.round(state.defaultRadiusM)));
    if (state.label) sp.set('q', state.label);
  } else if (state.mode === 'radius') {
    sp.set('lat', state.lat.toFixed(5));
    sp.set('lng', state.lng.toFixed(5));
    sp.set('r', String(Math.round(state.radiusM)));
    if (state.label) sp.set('q', state.label);
  } else if (state.mode === 'legacy_districts') {
    sp.set('districts', state.districts.map(encodeURIComponent).join(','));
  } else if (state.mode === 'legacy_radius') {
    sp.set('lat', state.lat.toFixed(5));
    sp.set('lng', state.lng.toFixed(5));
    sp.set('r', String(Math.round(state.radiusM)));
  }
  return sp;
};

/* Until map-1 ships, polygon mode degrades to a centred radius query
 * using the resolution's default_radius_m. The polygon id is preserved
 * in the URL/state so this becomes a one-line swap once the
 * polygon-aware RPC lands. */
const stateToRegionMode = (state: RegionState): RegionMode | null => {
  switch (state.mode) {
    case 'none':
      return null;
    case 'polygon':
      return { kind: 'radius', lat: state.lat, lng: state.lng, radiusM: state.defaultRadiusM };
    case 'radius':
      return { kind: 'radius', lat: state.lat, lng: state.lng, radiusM: state.radiusM };
    case 'legacy_districts':
      return { kind: 'districts', districts: state.districts };
    case 'legacy_radius':
      return { kind: 'radius', lat: state.lat, lng: state.lng, radiusM: state.radiusM };
  }
};

const stateLabel = (state: RegionState): string => {
  switch (state.mode) {
    case 'none':
      return '';
    case 'polygon':
      return state.label || 'Polygon';
    case 'radius':
      return state.label || 'Radius';
    case 'legacy_districts':
      return describeDistricts(state.districts);
    case 'legacy_radius':
      return `${(state.radiusM / 1000).toFixed(state.radiusM % 1000 === 0 ? 0 : 2)} km around ${state.lat.toFixed(4)}, ${state.lng.toFixed(4)}`;
  }
};

/* Levels in CLAUDE.md / map-1 terms: obec=municipality, okres=district,
 * kraj=region, ku=cadastral unit. */
const polygonLevelLabel = (level: PolygonLevel): string => {
  if (level === 'obec') return 'Obec';
  if (level === 'okres') return 'Okres';
  if (level === 'kraj') return 'Kraj';
  return 'Katastrální území';
};

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */

export default function Region() {
  const [searchParams, setSearchParams] = useSearchParams();
  const state = useMemo(() => parseUrlState(searchParams), [searchParams]);
  const [advancedOpen, setAdvancedOpen] = useState(
    () => state.mode === 'legacy_districts' || state.mode === 'legacy_radius',
  );
  const [unconfigured, setUnconfigured] = useState(false);

  const update = useCallback(
    (next: RegionState) => setSearchParams(writeUrlState(next), { replace: false }),
    [setSearchParams],
  );

  const onResolve = useCallback(
    (res: LocationResolution) => {
      if (res.kind === 'admin_polygon') {
        update({
          mode: 'polygon',
          level: res.level,
          polygonId: res.id,
          label: res.label,
          lat: res.lat,
          lng: res.lng,
          defaultRadiusM: res.default_radius_m,
        });
      } else if (res.kind === 'point_with_radius') {
        update({
          mode: 'radius',
          lat: res.lat,
          lng: res.lng,
          radiusM: res.radius_m,
          label: res.label,
        });
      }
      /* unresolved: leave state untouched; the search box surfaces the error. */
    },
    [update],
  );

  const onUnconfigured = useCallback(() => {
    setUnconfigured(true);
    setAdvancedOpen(true);
  }, []);

  const clear = useCallback(() => update({ mode: 'none' }), [update]);

  const regionMode = useMemo(() => stateToRegionMode(state), [state]);
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
    <div className="px-6 py-6 max-w-screen-lg mx-auto space-y-7">
      <Header state={state} stats={stats} loading={statsQuery.isLoading} />

      <LocationSearchBox onResolve={onResolve} onUnconfigured={onUnconfigured} />

      {state.mode !== 'none' && state.mode !== 'legacy_districts' && state.mode !== 'legacy_radius' && (
        <SelectedLocationCard state={state} onClear={clear} />
      )}

      <Advanced
        open={advancedOpen || unconfigured}
        onToggle={() => setAdvancedOpen((v) => !v)}
        state={state}
        onChange={update}
      />

      {!enabled && <EmptyHint />}

      {enabled && statsQuery.error && <ErrorBanner error={statsQuery.error} />}
      {enabled && statsQuery.isLoading && stats == null && <Skeleton />}
      {enabled && stats != null && stats.total_active === 0 && stats.total_ever === 0 && (
        <NoListingsHint />
      )}
      {enabled && stats != null && (stats.total_active > 0 || stats.total_ever > 0) && (
        <Report
          stats={stats}
          series={series}
          seriesLoading={seriesQuery.isLoading}
          seriesError={seriesQuery.error}
        />
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Header                                                                     */
/* -------------------------------------------------------------------------- */

function Header({
  state,
  stats,
  loading,
}: {
  state: RegionState;
  stats: RegionStats | null;
  loading: boolean;
}) {
  const label = stateLabel(state);
  const tail = stats == null
    ? loading && state.mode !== 'none'
      ? '· loading…'
      : ''
    : ` · ${fmtCount(stats.total_ever)} listings ever seen`;
  return (
    <div>
      <h1 className="text-2xl leading-tight">
        {label ? (
          <>
            <span className="text-[var(--color-ink-3)] font-normal">Region · </span>
            <span>{label}</span>
          </>
        ) : (
          'Region'
        )}
      </h1>
      {state.mode !== 'none' && (
        <p className="mt-1 text-sm text-[var(--color-ink-3)]">{tail.replace(/^· /, '')}</p>
      )}
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
/* Selected-location card                                                     */
/* -------------------------------------------------------------------------- */

function SelectedLocationCard({
  state,
  onClear,
}: {
  state: Extract<RegionState, { mode: 'polygon' | 'radius' }>;
  onClear: () => void;
}) {
  let badge: string;
  let detail: string;
  if (state.mode === 'polygon') {
    badge = polygonLevelLabel(state.level);
    detail = `Polygon · ${state.lat.toFixed(4)}, ${state.lng.toFixed(4)}`;
  } else {
    badge = 'Radius';
    detail = `${(state.radiusM / 1000).toFixed(state.radiusM % 1000 === 0 ? 0 : 2)} km · ${state.lat.toFixed(4)}, ${state.lng.toFixed(4)}`;
  }
  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-2)] px-4 py-3 flex items-center gap-3">
      <span className="text-[0.65rem] tracking-[0.08em] uppercase text-[var(--color-ink-3)] border border-[var(--color-rule)] rounded-[var(--radius-xs)] px-1.5 py-0.5">
        {badge}
      </span>
      <div className="flex-1 min-w-0">
        <p className="text-sm text-[var(--color-ink)] truncate">{state.label || '—'}</p>
        <p className="text-[0.7rem] text-[var(--color-ink-3)] tabular-nums">{detail}</p>
      </div>
      <button
        type="button"
        onClick={onClear}
        className="text-[0.7rem] tracking-wide uppercase text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
      >
        Clear
      </button>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Advanced disclosure — wraps the legacy district + radius pickers           */
/* -------------------------------------------------------------------------- */

function Advanced({
  open,
  onToggle,
  state,
  onChange,
}: {
  open: boolean;
  onToggle: () => void;
  state: RegionState;
  onChange: (next: RegionState) => void;
}) {
  /* Map the (possibly non-legacy) state into legacy picker props so toggling
   * the picker tabs always has a sensible starting point. */
  const legacy = legacyView(state);

  const onPickerModeChange = (next: PickerMode) => {
    if (next === 'districts') {
      onChange({ mode: 'legacy_districts', districts: legacy.districts });
    } else {
      onChange({ mode: 'legacy_radius', lat: legacy.center.lat, lng: legacy.center.lng, radiusM: legacy.radiusM });
    }
  };

  const onDistrictsChange = (next: string[]) => {
    if (next.length === 0) {
      onChange({ mode: 'none' });
    } else {
      onChange({ mode: 'legacy_districts', districts: next });
    }
  };

  const onCenterChange = (next: { lng: number; lat: number }) =>
    onChange({ mode: 'legacy_radius', lat: next.lat, lng: next.lng, radiusM: legacy.radiusM });

  const onRadiusChange = (next: number) =>
    onChange({ mode: 'legacy_radius', lat: legacy.center.lat, lng: legacy.center.lng, radiusM: next });

  return (
    <section className="border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-2)]">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="w-full flex items-center justify-between px-4 py-3 text-left"
      >
        <span className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Pokročilé · district & radius picker
        </span>
        <span className="text-[var(--color-ink-3)]" aria-hidden>
          {open ? '−' : '+'}
        </span>
      </button>
      {open && (
        <div className="border-t border-[var(--color-rule-soft)] p-4">
          <RegionPicker
            mode={legacy.mode}
            districts={legacy.districts}
            center={legacy.center}
            radiusM={legacy.radiusM}
            onModeChange={onPickerModeChange}
            onDistrictsChange={onDistrictsChange}
            onCenterChange={onCenterChange}
            onRadiusChange={onRadiusChange}
          />
        </div>
      )}
    </section>
  );
}

interface LegacyView {
  mode: PickerMode;
  districts: string[];
  center: { lng: number; lat: number };
  radiusM: number;
}

const legacyView = (state: RegionState): LegacyView => {
  if (state.mode === 'legacy_districts') {
    return { mode: 'districts', districts: state.districts, center: PRAGUE, radiusM: DEFAULT_RADIUS_M };
  }
  if (state.mode === 'legacy_radius') {
    return {
      mode: 'radius',
      districts: [],
      center: { lat: state.lat, lng: state.lng },
      radiusM: state.radiusM,
    };
  }
  if (state.mode === 'radius' || state.mode === 'polygon') {
    /* Inherit the search-resolved point as a starting centre for the legacy
     * radius picker — gives the user a sensible default if they switch into
     * the picker after a search. */
    return {
      mode: 'radius',
      districts: [],
      center: { lat: state.lat, lng: state.lng },
      radiusM: state.mode === 'radius' ? state.radiusM : state.defaultRadiusM,
    };
  }
  return { mode: 'districts', districts: [], center: PRAGUE, radiusM: DEFAULT_RADIUS_M };
};

/* -------------------------------------------------------------------------- */
/* Report body — unchanged from browse-1                                      */
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

          <Section
            title="Price per m² · by disposition"
            subtitle="Tukey 1.5×IQR whiskers clipped to min/max. Median in copper. Hover a box for the full numeric breakdown."
          >
            <DispositionBoxPlots rows={stats.dispositions} />
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
      <Stat label="Active" value={fmtCount(stats.total_active)} />
      <Stat label="Ever seen" value={fmtCount(stats.total_ever)} />
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

function EmptyHint() {
  return (
    <section className="p-12 rounded-[var(--radius-md)] border border-dashed border-[var(--color-rule)] text-center">
      <p className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-4)]">
        No location selected
      </p>
      <p className="mt-2 text-sm text-[var(--color-ink-3)]">
        Search for a location to see statistics. Open Pokročilé for the legacy district / radius pickers.
      </p>
    </section>
  );
}

function NoListingsHint() {
  return (
    <section className="p-10 rounded-[var(--radius-md)] border border-dashed border-[var(--color-rule)] text-center">
      <p className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-4)]">
        Empty cohort
      </p>
      <p className="mt-2 text-sm text-[var(--color-ink-3)]">
        No active listings found in this area.
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

