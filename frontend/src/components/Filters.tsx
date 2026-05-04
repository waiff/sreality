import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type ReactNode,
} from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchDistrictFacets } from '@/lib/queries';
import {
  type ListingFilters,
  type SeenWithin,
  type TriState,
  PRICE_BOUNDS,
  AREA_BOUNDS,
  DEFAULT_FILTERS,
  isDefault,
} from '@/lib/filters';
import { fmtCount } from '@/lib/format';
import type { Disposition } from '@/lib/types';

interface SidebarProps {
  filters: ListingFilters;
  onChange: (next: ListingFilters) => void;
}

export function FilterSidebar({ filters, onChange }: SidebarProps) {
  const set = <K extends keyof ListingFilters>(key: K, value: ListingFilters[K]) =>
    onChange({ ...filters, [key]: value });

  return (
    <aside className="w-[320px] shrink-0 border-r border-[var(--color-rule)] sticky top-14 self-start max-h-[calc(100dvh-3.5rem)] overflow-y-auto">
      <div className="px-5 py-4 flex items-center justify-between border-b border-[var(--color-rule-soft)]">
        <h2 className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Filters
        </h2>
        {!isDefault(filters) && (
          <button
            type="button"
            onClick={() => onChange(DEFAULT_FILTERS)}
            className="text-[0.7rem] tracking-wide uppercase text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
          >
            Reset
          </button>
        )}
      </div>

      <div className="px-5 py-5 space-y-7">
        <DistrictPicker
          value={filters.districts}
          onChange={(v) => set('districts', v)}
        />

        <DispositionPicker
          value={filters.dispositions}
          onChange={(v) => set('dispositions', v)}
        />

        <RangeFilter
          label="Price"
          unit="Kč"
          bounds={PRICE_BOUNDS}
          value={[filters.priceMin, filters.priceMax]}
          onChange={([min, max]) =>
            onChange({ ...filters, priceMin: min, priceMax: max })
          }
        />

        <RangeFilter
          label="Area"
          unit="m²"
          bounds={AREA_BOUNDS}
          value={[filters.areaMin, filters.areaMax]}
          onChange={([min, max]) =>
            onChange({ ...filters, areaMin: min, areaMax: max })
          }
        />

        <ActiveBlock
          activeOnly={filters.activeOnly}
          seenWithin={filters.seenWithin}
          onActive={(v) => set('activeOnly', v)}
          onSeen={(v) => set('seenWithin', v)}
        />

        <Section label="Has">
          <div className="space-y-2">
            <TriRow label="Balcony"  value={filters.hasBalcony} onChange={(v) => set('hasBalcony', v)} />
            <TriRow label="Lift"     value={filters.hasLift}    onChange={(v) => set('hasLift', v)} />
            <TriRow label="Parking"  value={filters.hasParking} onChange={(v) => set('hasParking', v)} />
          </div>
        </Section>
      </div>
    </aside>
  );
}

/* -------------------------------------------------------------------------- */
/* Section + label scaffolding                                                */
/* -------------------------------------------------------------------------- */

function Section({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <Label>{label}</Label>
      <div className="mt-2.5">{children}</div>
    </div>
  );
}

function Label({ children }: { children: ReactNode }) {
  return (
    <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
      {children}
    </p>
  );
}

/* -------------------------------------------------------------------------- */
/* District typeahead + chip display                                          */
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
    <Section label="District">
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

      {value.length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-1.5">
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
      )}
    </Section>
  );
}

/* -------------------------------------------------------------------------- */
/* Disposition multi-toggle                                                   */
/* -------------------------------------------------------------------------- */

const DISPOSITIONS: ReadonlyArray<Disposition> = [
  '1+kk', '1+1', '2+kk', '2+1',
  '3+kk', '3+1', '4+kk', '4+1',
  '5+kk', '5+1',
];

function DispositionPicker({
  value,
  onChange,
}: {
  value: Disposition[];
  onChange: (next: Disposition[]) => void;
}) {
  const toggle = (d: Disposition) => {
    onChange(value.includes(d) ? value.filter((x) => x !== d) : [...value, d]);
  };
  return (
    <Section label="Disposition">
      <div className="grid grid-cols-4 gap-1.5">
        {DISPOSITIONS.map((d) => {
          const on = value.includes(d);
          return (
            <button
              key={d}
              type="button"
              onClick={() => toggle(d)}
              className={[
                'px-2 py-1.5 text-xs rounded-[var(--radius-sm)] border transition-colors font-mono tabular-nums',
                on
                  ? 'bg-[var(--color-copper)] text-white border-[var(--color-copper)]'
                  : 'bg-[var(--color-paper-2)] text-[var(--color-ink-2)] border-[var(--color-rule)] hover:border-[var(--color-rule-strong)]',
              ].join(' ')}
              aria-pressed={on}
            >
              {d}
            </button>
          );
        })}
      </div>
    </Section>
  );
}

/* -------------------------------------------------------------------------- */
/* Range filter — dual-handle slider + paired number inputs                   */
/* -------------------------------------------------------------------------- */

function RangeFilter({
  label,
  unit,
  bounds,
  value,
  onChange,
}: {
  label: string;
  unit: string;
  bounds: { min: number; max: number; step: number };
  value: [number | null, number | null];
  onChange: (next: [number | null, number | null]) => void;
}) {
  const lo = value[0] ?? bounds.min;
  const hi = value[1] ?? bounds.max;
  const span = bounds.max - bounds.min;

  const setLo = (n: number) => {
    const clamped = Math.max(bounds.min, Math.min(n, hi));
    onChange([clamped === bounds.min ? null : clamped, value[1]]);
  };
  const setHi = (n: number) => {
    const clamped = Math.min(bounds.max, Math.max(n, lo));
    onChange([value[0], clamped === bounds.max ? null : clamped]);
  };

  const onNumber = (which: 0 | 1) => (e: ChangeEvent<HTMLInputElement>) => {
    const raw = e.target.value.replace(/\s/g, '');
    if (raw === '') {
      onChange(which === 0 ? [null, value[1]] : [value[0], null]);
      return;
    }
    const n = Number(raw);
    if (!Number.isFinite(n) || n < 0) return;
    if (which === 0) setLo(n);
    else setHi(n);
  };

  return (
    <Section label={label}>
      <div className="relative h-6">
        <div className="absolute inset-x-0 top-1/2 h-0.5 -translate-y-1/2 bg-[var(--color-rule-strong)] rounded-full" />
        <div
          className="absolute top-1/2 h-0.5 -translate-y-1/2 bg-[var(--color-copper)] rounded-full"
          style={{
            left:  `${((lo - bounds.min) / span) * 100}%`,
            right: `${100 - ((hi - bounds.min) / span) * 100}%`,
          }}
        />
        <input
          type="range"
          min={bounds.min}
          max={bounds.max}
          step={bounds.step}
          value={lo}
          onChange={(e) => setLo(Number(e.target.value))}
          className="range-slider"
          aria-label={`${label} minimum`}
        />
        <input
          type="range"
          min={bounds.min}
          max={bounds.max}
          step={bounds.step}
          value={hi}
          onChange={(e) => setHi(Number(e.target.value))}
          className="range-slider"
          style={{ zIndex: 1 }}
          aria-label={`${label} maximum`}
        />
      </div>
      <div className="mt-3 flex items-center gap-2">
        <NumberCell value={value[0]} placeholder={String(bounds.min)} onChange={onNumber(0)} />
        <span className="text-[var(--color-ink-3)] text-sm">—</span>
        <NumberCell value={value[1]} placeholder={String(bounds.max)} onChange={onNumber(1)} />
        <span className="text-[var(--color-ink-3)] text-xs ml-1 tracking-wide">{unit}</span>
      </div>
    </Section>
  );
}

function NumberCell({
  value,
  placeholder,
  onChange,
}: {
  value: number | null;
  placeholder: string;
  onChange: (e: ChangeEvent<HTMLInputElement>) => void;
}) {
  return (
    <input
      type="text"
      inputMode="numeric"
      value={value ?? ''}
      placeholder={placeholder}
      onChange={onChange}
      className="w-full min-w-0 px-2 py-1.5 text-sm font-mono tabular-nums rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
    />
  );
}

/* -------------------------------------------------------------------------- */
/* Active + Last-seen-within combined block                                   */
/* -------------------------------------------------------------------------- */

const SEEN_OPTS: ReadonlyArray<{ value: SeenWithin; label: string }> = [
  { value: '1d',  label: '24 h'  },
  { value: '7d',  label: '7 d'   },
  { value: '30d', label: '30 d'  },
  { value: 'any', label: 'any'   },
];

function ActiveBlock({
  activeOnly,
  seenWithin,
  onActive,
  onSeen,
}: {
  activeOnly: boolean;
  seenWithin: SeenWithin;
  onActive: (v: boolean) => void;
  onSeen: (v: SeenWithin) => void;
}) {
  return (
    <Section label="Status">
      <label className="flex items-center gap-2.5 cursor-pointer">
        <input
          type="checkbox"
          checked={activeOnly}
          onChange={(e) => onActive(e.target.checked)}
          className="w-3.5 h-3.5 accent-[var(--color-copper)] cursor-pointer"
        />
        <span className="text-sm text-[var(--color-ink-2)]">Active only</span>
      </label>
      <p className="mt-3 text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        Last seen within
      </p>
      <div className="mt-1.5 grid grid-cols-4 gap-1">
        {SEEN_OPTS.map((opt) => {
          const on = seenWithin === opt.value;
          return (
            <button
              key={opt.value}
              type="button"
              onClick={() => onSeen(opt.value)}
              className={[
                'px-2 py-1 text-xs rounded-[var(--radius-sm)] border transition-colors',
                on
                  ? 'bg-[var(--color-copper-soft)] text-[var(--color-copper)] border-[var(--color-copper)]'
                  : 'bg-[var(--color-paper-2)] text-[var(--color-ink-3)] border-[var(--color-rule)] hover:text-[var(--color-ink-2)]',
              ].join(' ')}
              aria-pressed={on}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
    </Section>
  );
}

/* -------------------------------------------------------------------------- */
/* Tri-state row (any / yes / no)                                             */
/* -------------------------------------------------------------------------- */

const TRI_OPTS: ReadonlyArray<{ value: TriState; label: string }> = [
  { value: 'any', label: 'any' },
  { value: 'yes', label: 'yes' },
  { value: 'no',  label: 'no'  },
];

function TriRow({
  label,
  value,
  onChange,
}: {
  label: string;
  value: TriState;
  onChange: (v: TriState) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-sm text-[var(--color-ink-2)]">{label}</span>
      <div className="grid grid-cols-3 gap-0.5 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-0.5">
        {TRI_OPTS.map((opt) => {
          const on = value === opt.value;
          return (
            <button
              key={opt.value}
              type="button"
              onClick={() => onChange(opt.value)}
              className={[
                'px-2.5 py-0.5 text-[0.7rem] rounded-[var(--radius-xs)] transition-colors',
                on
                  ? 'bg-[var(--color-copper)] text-white'
                  : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
              ].join(' ')}
              aria-pressed={on}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
