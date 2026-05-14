import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
} from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchDistrictFacets, curationKeys } from '@/lib/queries';
import { listTags } from '@/lib/api';
import type { Tag } from '@/lib/types';
import TagEditPopover from '@/components/curation/TagEditPopover';
import {
  type ListingFilters,
  type ListingStatus,
  type SeenWithin,
  type CategoryMain,
  type CategoryType,
  PRICE_BOUNDS,
  AREA_BOUNDS,
  ESTATE_AREA_BOUNDS,
  USABLE_AREA_BOUNDS,
  DEFAULT_FILTERS,
  isDefault,
} from '@/lib/filters';
import { fmtCount, FURNISHED_LABELS, OWNERSHIP_LABELS, CATEGORY_SUB_LABELS } from '@/lib/format';
import type { Disposition, Furnished, Ownership } from '@/lib/types';
import {
  ControlGroup,
  Section,
  NumberCell,
  PickButton,
  TriRow,
} from '@/components/controls';

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

      <div className="px-5 py-5 space-y-9">
        <ControlGroup title="Category">
          <CategoryMainPicker
            value={filters.categoryMain}
            onChange={(v) => set('categoryMain', v)}
          />
          <CategoryTypePicker
            value={filters.categoryType}
            onChange={(v) => set('categoryType', v)}
          />
        </ControlGroup>

        <ControlGroup title="Where">
          <DistrictPicker
            value={filters.districts}
            onChange={(v) => set('districts', v)}
          />
        </ControlGroup>

        <ControlGroup title="Listing">
          <DispositionPicker
            value={filters.dispositions}
            onChange={(v) => set('dispositions', v)}
          />
          <SubcategoryPicker
            value={filters.categorySubCb}
            onChange={(v) => set('categorySubCb', v)}
          />
        </ControlGroup>

        <ControlGroup title="Price & size">
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
          <RangeFilter
            label="Lot area"
            unit="m²"
            bounds={ESTATE_AREA_BOUNDS}
            value={[filters.estateAreaMin, filters.estateAreaMax]}
            onChange={([min, max]) =>
              onChange({ ...filters, estateAreaMin: min, estateAreaMax: max })
            }
          />
          <RangeFilter
            label="Usable area"
            unit="m²"
            bounds={USABLE_AREA_BOUNDS}
            value={[filters.usableAreaMin, filters.usableAreaMax]}
            onChange={([min, max]) =>
              onChange({ ...filters, usableAreaMin: min, usableAreaMax: max })
            }
          />
        </ControlGroup>

        <ControlGroup title="Status">
          <ActiveBlock
            status={filters.status}
            seenWithin={filters.seenWithin}
            onStatus={(v) => set('status', v)}
            onSeen={(v) => set('seenWithin', v)}
          />
        </ControlGroup>

        <ControlGroup title="Amenities">
          <Section label="Has">
            <div className="space-y-2">
              <TriRow label="Balcony"  value={filters.hasBalcony} onChange={(v) => set('hasBalcony', v)} />
              <TriRow label="Lift"     value={filters.hasLift}    onChange={(v) => set('hasLift', v)} />
              <TriRow label="Parking"  value={filters.hasParking} onChange={(v) => set('hasParking', v)} />
              <TriRow label="Terrace"  value={filters.terrace}    onChange={(v) => set('terrace', v)} />
              <TriRow label="Cellar"   value={filters.cellar}     onChange={(v) => set('cellar', v)} />
              <TriRow label="Garage"   value={filters.garage}     onChange={(v) => set('garage', v)} />
            </div>
          </Section>

          <Section label="Min parking spaces">
            <NumberCell
              value={filters.parkingLotsMin}
              placeholder="0"
              onChange={(e) => {
                const raw = e.target.value.replace(/\s/g, '');
                if (raw === '') {
                  set('parkingLotsMin', null);
                  return;
                }
                const n = Number(raw);
                if (Number.isFinite(n) && n >= 0) set('parkingLotsMin', Math.trunc(n));
              }}
            />
          </Section>

          <EnumPicker<Furnished>
            label="Furnished"
            value={filters.furnished}
            options={FURNISHED_OPTIONS}
            onChange={(v) => set('furnished', v)}
          />

          <EnumPicker<Ownership>
            label="Ownership"
            value={filters.ownership}
            options={OWNERSHIP_OPTIONS}
            onChange={(v) => set('ownership', v)}
          />
        </ControlGroup>

        <ControlGroup title="Curation">
          <TagsPicker
            value={filters.tags}
            onChange={(v) => set('tags', v)}
          />
        </ControlGroup>
      </div>
    </aside>
  );
}

/* -------------------------------------------------------------------------- */
/* Tags picker — operator tags from migration 024. AND-semantics: a listing    */
/* must carry every selected tag id (enforced server-side by the              */
/* listings_with_tags RPC). The Browse stats panel does NOT filter by tags    */
/* in v1 — only the map / table cohorts do.                                   */
/* -------------------------------------------------------------------------- */

function TagsPicker({
  value,
  onChange,
}: {
  value: number[];
  onChange: (next: number[]) => void;
}) {
  const tagsQ = useQuery({
    queryKey: curationKeys.tags,
    queryFn: listTags,
    staleTime: 60_000,
  });
  const tags = tagsQ.data?.data ?? [];
  const byId = useMemo(() => {
    const m = new Map<number, Tag>();
    for (const t of tags) m.set(t.id, t);
    return m;
  }, [tags]);

  const remaining = tags.filter((t) => !value.includes(t.id));

  const add = (id: number) => onChange([...value, id]);
  const remove = (id: number) => onChange(value.filter((x) => x !== id));

  if (tagsQ.isLoading) {
    return (
      <Section label="Tags">
        <p className="text-[0.75rem] text-[var(--color-ink-4)]">Loading…</p>
      </Section>
    );
  }

  if (tags.length === 0) {
    return (
      <Section label="Tags">
        <p className="text-[0.75rem] text-[var(--color-ink-4)]">
          No tags yet. Add one from any listing's detail page.
        </p>
      </Section>
    );
  }

  return (
    <Section label="Tags">
      {value.length > 0 && (
        <ul className="flex flex-wrap gap-1.5">
          {value.map((id) => {
            const t = byId.get(id);
            if (!t) return null;
            return (
              <li key={id}>
                <FilterTagChip t={t} onRemove={() => remove(id)} />
              </li>
            );
          })}
        </ul>
      )}
      {remaining.length > 0 && (
        <div className={value.length > 0 ? 'mt-2' : ''}>
          <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
            Add
          </p>
          <ul className="mt-1.5 flex flex-wrap gap-1.5">
            {remaining.map((t) => (
              <li key={t.id}>
                <FilterTagAddButton
                  t={t}
                  onAdd={() => add(t.id)}
                  otherNames={tags
                    .filter((x) => x.id !== t.id)
                    .map((x) => x.name.toLowerCase())}
                  onDeleted={(id) => {
                    if (value.includes(id)) remove(id);
                  }}
                />
              </li>
            ))}
          </ul>
        </div>
      )}
      {value.length > 0 && (
        <p className="mt-2 text-[0.65rem] text-[var(--color-ink-4)]">
          A listing must carry every selected tag.
        </p>
      )}
    </Section>
  );
}

function FilterTagChip({ t, onRemove }: { t: Tag; onRemove: () => void }) {
  return (
    <button
      type="button"
      onClick={onRemove}
      aria-label={`Remove ${t.name}`}
      className="group inline-flex items-center gap-1.5 px-2 py-1 text-xs rounded-[var(--radius-sm)] border transition-colors"
      style={{
        background: `var(--color-tag-${t.color}-soft)`,
        color: `var(--color-tag-${t.color})`,
        borderColor: `var(--color-tag-${t.color})`,
      }}
    >
      <span>{t.name}</span>
      <span aria-hidden className="opacity-60 group-hover:opacity-100">
        ×
      </span>
    </button>
  );
}

function FilterTagAddButton({
  t,
  onAdd,
  otherNames,
  onDeleted,
}: {
  t: Tag;
  onAdd: () => void;
  otherNames: string[];
  onDeleted: (id: number) => void;
}) {
  return (
    <span className="inline-flex items-center gap-0.5 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] hover:border-[var(--color-rule-strong)] transition-colors">
      <button
        type="button"
        onClick={onAdd}
        className="inline-flex items-center gap-1.5 px-2 py-1 text-xs text-[var(--color-ink-3)] hover:text-[var(--color-ink)]"
      >
        <span
          aria-hidden
          className="w-2 h-2 rounded-full"
          style={{ background: `var(--color-tag-${t.color})` }}
        />
        <span>{t.name}</span>
      </button>
      <span className="pr-0.5">
        <TagEditPopover
          tag={t}
          otherNames={otherNames}
          onDeleted={onDeleted}
        />
      </span>
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Category pickers — top-of-sidebar mode switch. CategoryMain narrows to one  */
/* of byt / dum / komercni; CategoryType to pronajem / prodej. Solid-variant   */
/* PickButtons because these define the cohort everything else filters within. */
/* -------------------------------------------------------------------------- */

const CATEGORY_MAIN_OPTS: ReadonlyArray<{ value: CategoryMain; label: string }> = [
  { value: 'byt',      label: 'Byty' },
  { value: 'dum',      label: 'Domy' },
  { value: 'komercni', label: 'Komerční' },
];

const CATEGORY_TYPE_OPTS: ReadonlyArray<{ value: CategoryType; label: string }> = [
  { value: 'pronajem', label: 'Pronájem' },
  { value: 'prodej',   label: 'Prodej'   },
];

function CategoryMainPicker({
  value,
  onChange,
}: {
  value: CategoryMain;
  onChange: (v: CategoryMain) => void;
}) {
  return (
    <Section label="Type">
      <div className="grid grid-cols-3 gap-1">
        {CATEGORY_MAIN_OPTS.map((opt) => (
          <PickButton
            key={opt.value}
            on={value === opt.value}
            onClick={() => onChange(opt.value)}
            variant="solid"
          >
            {opt.label}
          </PickButton>
        ))}
      </div>
    </Section>
  );
}

function CategoryTypePicker({
  value,
  onChange,
}: {
  value: CategoryType;
  onChange: (v: CategoryType) => void;
}) {
  return (
    <Section label="Listing for">
      <div className="grid grid-cols-2 gap-1">
        {CATEGORY_TYPE_OPTS.map((opt) => (
          <PickButton
            key={opt.value}
            on={value === opt.value}
            onClick={() => onChange(opt.value)}
            variant="solid"
          >
            {opt.label}
          </PickButton>
        ))}
      </div>
    </Section>
  );
}

/* -------------------------------------------------------------------------- */
/* Generic enum-button picker (any | each option). Used for Furnished /        */
/* Ownership where the value space is small and discrete.                     */
/* -------------------------------------------------------------------------- */

interface EnumOption<T extends string> {
  value: T;
  label: string;
}

const FURNISHED_OPTIONS: ReadonlyArray<EnumOption<Furnished>> =
  (Object.keys(FURNISHED_LABELS) as Furnished[]).map((v) => ({
    value: v,
    label: FURNISHED_LABELS[v],
  }));

const OWNERSHIP_OPTIONS: ReadonlyArray<EnumOption<Ownership>> =
  (Object.keys(OWNERSHIP_LABELS) as Ownership[]).map((v) => ({
    value: v,
    label: OWNERSHIP_LABELS[v],
  }));

function EnumPicker<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T | null;
  options: ReadonlyArray<EnumOption<T>>;
  onChange: (v: T | null) => void;
}) {
  return (
    <Section label={label}>
      <div className="grid grid-cols-2 gap-1">
        <PickButton on={value == null} onClick={() => onChange(null)}>
          any
        </PickButton>
        {options.map((opt) => (
          <PickButton
            key={opt.value}
            on={value === opt.value}
            onClick={() => onChange(opt.value)}
          >
            {opt.label}
          </PickButton>
        ))}
      </div>
    </Section>
  );
}

/* -------------------------------------------------------------------------- */
/* Subcategory dropdown (sreality category_sub_cb). The full taxonomy is       */
/* large (~30 codes) so a <select> is the right shape rather than a button     */
/* grid; same UX pattern lib/format.fmtCategorySub already settled on.         */
/* -------------------------------------------------------------------------- */

function SubcategoryPicker({
  value,
  onChange,
}: {
  value: number | null;
  onChange: (v: number | null) => void;
}) {
  const codes = (Object.keys(CATEGORY_SUB_LABELS) as string[])
    .map(Number)
    .sort((a, b) => a - b);
  return (
    <Section label="Subtype">
      <select
        value={value ?? ''}
        onChange={(e) =>
          onChange(e.target.value === '' ? null : Number(e.target.value))
        }
        className="w-full px-2.5 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] focus:outline-none focus:border-[var(--color-rule-strong)]"
      >
        <option value="">any</option>
        {codes.map((cb) => (
          <option key={cb} value={cb}>
            {CATEGORY_SUB_LABELS[cb]}
          </option>
        ))}
      </select>
    </Section>
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
        {DISPOSITIONS.map((d) => (
          <PickButton
            key={d}
            on={value.includes(d)}
            onClick={() => toggle(d)}
            variant="solid"
            className="font-mono tabular-nums"
          >
            {d}
          </PickButton>
        ))}
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
    const clamped = Math.max(bounds.min, Math.min(n, bounds.max));
    if (which === 0) {
      onChange([clamped === bounds.min ? null : clamped, value[1]]);
    } else {
      onChange([value[0], clamped === bounds.max ? null : clamped]);
    }
  };

  const onCommit = () => {
    const a = value[0];
    const b = value[1];
    if (a != null && b != null && a > b) onChange([b, a]);
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
        <NumberCell value={value[0]} placeholder={String(bounds.min)} onChange={onNumber(0)} onBlur={onCommit} />
        <span className="text-[var(--color-ink-3)] text-sm">—</span>
        <NumberCell value={value[1]} placeholder={String(bounds.max)} onChange={onNumber(1)} onBlur={onCommit} />
        <span className="text-[var(--color-ink-3)] text-xs ml-1 tracking-wide">{unit}</span>
      </div>
    </Section>
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

const STATUS_OPTS: ReadonlyArray<{ value: ListingStatus; label: string }> = [
  { value: 'active',   label: 'Active'   },
  { value: 'inactive', label: 'Inactive' },
  { value: 'any',      label: 'Any'      },
];

function ActiveBlock({
  status,
  seenWithin,
  onStatus,
  onSeen,
}: {
  status: ListingStatus;
  seenWithin: SeenWithin;
  onStatus: (v: ListingStatus) => void;
  onSeen: (v: SeenWithin) => void;
}) {
  return (
    <Section label="Status">
      <div className="grid grid-cols-3 gap-1">
        {STATUS_OPTS.map((opt) => (
          <PickButton
            key={opt.value}
            on={status === opt.value}
            onClick={() => onStatus(opt.value)}
          >
            {opt.label}
          </PickButton>
        ))}
      </div>
      <p className="mt-3 text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        Last seen within
      </p>
      <div className="mt-1.5 grid grid-cols-4 gap-1">
        {SEEN_OPTS.map((opt) => (
          <PickButton
            key={opt.value}
            on={seenWithin === opt.value}
            onClick={() => onSeen(opt.value)}
            className="px-2 py-1"
          >
            {opt.label}
          </PickButton>
        ))}
      </div>
    </Section>
  );
}
