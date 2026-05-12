import { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import Tabs, { type Tab } from '@/components/Tabs';
import { Section } from '@/components/controls';
import { fetchDistrictFacets } from '@/lib/queries';
import { fmtCount } from '@/lib/format';

const RegionMap = lazy(() => import('./RegionMap'));

export type PickerMode = 'districts' | 'radius';

interface Props {
  mode: PickerMode;
  districts: string[];
  center: { lng: number; lat: number };
  radiusM: number;
  onModeChange: (next: PickerMode) => void;
  onDistrictsChange: (next: string[]) => void;
  onCenterChange: (next: { lng: number; lat: number }) => void;
  onRadiusChange: (next: number) => void;
}

const TABS: ReadonlyArray<Tab<PickerMode>> = [
  { key: 'districts', label: 'District(s)' },
  { key: 'radius',    label: 'Radius'      },
];

export default function RegionPicker(props: Props) {
  return (
    <aside className="lg:w-[360px] lg:shrink-0 lg:sticky lg:top-14 lg:self-start border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-2)]">
      <div className="px-4 pt-4 pb-1">
        <h2 className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Region
        </h2>
      </div>
      <div className="px-4">
        <Tabs tabs={TABS} active={props.mode} onChange={props.onModeChange} />
      </div>
      <div className="p-4">
        {props.mode === 'districts' ? (
          <DistrictPicker
            value={props.districts}
            onChange={props.onDistrictsChange}
          />
        ) : (
          <RadiusPicker
            center={props.center}
            radiusM={props.radiusM}
            onCenterChange={props.onCenterChange}
            onRadiusChange={props.onRadiusChange}
          />
        )}
      </div>
    </aside>
  );
}

/* -------------------------------------------------------------------------- */
/* District picker — same fetch pattern as Browse, lighter chrome             */
/* -------------------------------------------------------------------------- */

function DistrictPicker({
  value,
  onChange,
}: {
  value: string[];
  onChange: (next: string[]) => void;
}) {
  const { data: facets, isLoading } = useQuery({
    queryKey: ['district-facets'],
    queryFn: fetchDistrictFacets,
    staleTime: 10 * 60_000,
  });

  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const matches = useMemo(() => {
    if (!facets) return [];
    const q = query.trim().toLowerCase();
    const remaining = facets.filter((f) => !value.includes(f.district));
    if (!q) return remaining.slice(0, 60);
    return remaining
      .filter((f) => f.district.toLowerCase().includes(q))
      .slice(0, 60);
  }, [facets, query, value]);

  const add = (d: string) => {
    onChange([...value, d]);
    setQuery('');
  };
  const remove = (d: string) => {
    onChange(value.filter((x) => x !== d));
  };

  return (
    <div>
      <div ref={ref} className="relative">
        <input
          type="text"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          placeholder={isLoading ? 'Loading…' : value.length === 0 ? 'Type to search…' : 'Add another…'}
          className="w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
        />
        {open && matches.length > 0 && (
          <ul
            role="listbox"
            className="absolute z-20 mt-1 w-full max-h-72 overflow-y-auto rounded-[var(--radius-md)] bg-[var(--color-paper-3)] border border-[var(--color-rule-strong)] shadow-[0_4px_16px_rgba(0,0,0,0.06)] py-1"
          >
            {matches.map((m) => (
              <li key={m.district}>
                <button
                  type="button"
                  onClick={() => add(m.district)}
                  className="w-full flex items-center justify-between px-3 py-1.5 text-sm text-left hover:bg-[var(--color-copper-soft)]"
                >
                  <span className="truncate text-[var(--color-ink)]">{m.district}</span>
                  <span className="font-mono text-[0.75rem] text-[var(--color-ink-3)] tabular-nums ml-3">
                    {fmtCount(m.count)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {value.length > 0 ? (
        <ul className="mt-3 flex flex-wrap gap-1.5">
          {value.map((d) => (
            <li key={d}>
              <button
                type="button"
                onClick={() => remove(d)}
                className="group inline-flex items-center gap-1.5 px-2 py-1 text-xs rounded-[var(--radius-sm)] bg-[var(--color-copper-soft)] text-[var(--color-copper)] hover:bg-[var(--color-copper)]/15 transition-colors"
                aria-label={`Remove ${d}`}
              >
                <span>{d}</span>
                <span className="text-[var(--color-copper)]/60 group-hover:text-[var(--color-copper)]" aria-hidden>
                  ×
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-3 text-[0.75rem] text-[var(--color-ink-3)] tracking-wide">
          Pick one or more districts to summarise.
        </p>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Radius picker — lazy map + slider                                          */
/* -------------------------------------------------------------------------- */

const RADIUS_MIN = 250;
const RADIUS_MAX = 5000;
const RADIUS_STEP = 50;

const fmtRadius = (m: number): string =>
  m >= 1000 ? `${(m / 1000).toFixed(m % 1000 === 0 ? 0 : 1)} km` : `${m} m`;

const fmtLatLng = (n: number): string => n.toFixed(4);

function RadiusPicker({
  center,
  radiusM,
  onCenterChange,
  onRadiusChange,
}: {
  center: { lng: number; lat: number };
  radiusM: number;
  onCenterChange: (next: { lng: number; lat: number }) => void;
  onRadiusChange: (next: number) => void;
}) {
  return (
    <div className="space-y-5">
      <Suspense fallback={<MapSkeleton />}>
        <RegionMap center={center} radiusM={radiusM} onCenterChange={onCenterChange} />
      </Suspense>

      <Section label="Radius">
        <div className="flex items-baseline justify-between">
          <input
            type="range"
            min={RADIUS_MIN}
            max={RADIUS_MAX}
            step={RADIUS_STEP}
            value={radiusM}
            onChange={(e) => onRadiusChange(Number(e.target.value))}
            aria-label="Radius (metres)"
            className="flex-1 h-2 appearance-none bg-[var(--color-rule-strong)] rounded-full accent-[var(--color-copper)]"
          />
          <p className="ml-3 font-mono tabular-nums text-sm text-[var(--color-ink)] min-w-[3.5rem] text-right">
            {fmtRadius(radiusM)}
          </p>
        </div>
        <div className="flex justify-between mt-0.5 text-[0.65rem] tabular-nums text-[var(--color-ink-4)] font-mono pr-[4.25rem]">
          <span>250 m</span>
          <span>5 km</span>
        </div>
      </Section>

      <Section label="Centre">
        <p className="text-sm tabular-nums text-[var(--color-ink)] font-mono">
          {fmtLatLng(center.lat)}, {fmtLatLng(center.lng)}
        </p>
        <p className="mt-1 text-[0.7rem] text-[var(--color-ink-3)] tracking-wide">
          Drag the pin or click the map to move the centre.
        </p>
      </Section>
    </div>
  );
}

function MapSkeleton() {
  return (
    <div className="h-[280px] rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-3)] flex items-center justify-center">
      <p className="text-xs text-[var(--color-ink-3)] tracking-wide">Loading map…</p>
    </div>
  );
}
