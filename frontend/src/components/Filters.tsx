import {
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchDistrictFacets, curationKeys } from '@/lib/queries';
import { listTags } from '@/lib/api';
import TagEditPopover from '@/components/curation/TagEditPopover';
import {
  type ListingFilters,
  DEFAULT_FILTERS,
  isDefault,
  listingFiltersToRegistryView,
  applyRegistryUpdate,
} from '@/lib/filters';
import { fmtCount } from '@/lib/format';
import type { Tag } from '@/lib/types';
import {
  ControlGroup,
  Section,
} from '@/components/controls';
import { FilterForm } from '@/components/FilterForm';

interface SidebarProps {
  filters: ListingFilters;
  onChange: (next: ListingFilters) => void;
}

export function FilterSidebar({ filters, onChange }: SidebarProps) {
  const set = <K extends keyof ListingFilters>(key: K, value: ListingFilters[K]) =>
    onChange({ ...filters, [key]: value });

  // <FilterForm> reads snake_case registry ids; Browse keeps the
  // camelCase `ListingFilters` shape its queries / URL serialisation
  // already use. The adapter in lib/filters bridges both directions
  // (tri-state amenities pivot bool|null ↔ 'any'|'yes'|'no' inside it).
  const registryView = listingFiltersToRegistryView(filters);
  const handleRegistryChange = (id: string, value: unknown) =>
    onChange(applyRegistryUpdate(filters, id, value));

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
        {/* Category, disposition, price, size, status & velocity,
            building, amenities — all driven by <FilterForm>. The
            registry decides which widget renders per filter; touching
            `toolkit/filter_registry.py` flows through here without
            edits. The remaining hand-written sections (district picker,
            tags picker) wrap rich widgets `<FilterForm>` doesn't yet
            cover. */}

        <ControlGroup title="Category">
          <FilterForm
            scope="browse"
            state={registryView}
            onChange={handleRegistryChange}
            includeOnly={['category_main', 'category_type']}
            labels={{
              category_main: 'Type',
              category_type: 'Listing for',
            }}
            flat
          />
        </ControlGroup>

        <ControlGroup title="Location">
          {/* District picker stays hand-written — typeahead with live
              suggestions isn't a primitive in filter-controls yet. */}
          <DistrictPicker
            value={filters.districts}
            onChange={(v) => set('districts', v)}
          />
        </ControlGroup>

        <ControlGroup title="Disposition">
          <FilterForm
            scope="browse"
            state={registryView}
            onChange={handleRegistryChange}
            includeOnly={['dispositions', 'category_sub_cb']}
            labels={{
              dispositions: 'Disposition',
              category_sub_cb: 'Subtype',
            }}
            flat
          />
        </ControlGroup>

        <ControlGroup title="Price">
          <FilterForm
            scope="browse"
            state={registryView}
            onChange={handleRegistryChange}
            includeOnly={['min_price_czk']}
            labels={{ min_price_czk: 'Price' }}
            flat
          />
        </ControlGroup>

        <ControlGroup title="Size">
          <FilterForm
            scope="browse"
            state={registryView}
            onChange={handleRegistryChange}
            includeOnly={['min_area_m2', 'min_estate_area', 'min_usable_area']}
            labels={{
              min_area_m2: 'Area',
              min_estate_area: 'Lot area',
              min_usable_area: 'Usable area',
            }}
            flat
          />
        </ControlGroup>

        <ControlGroup title="Status & velocity">
          <FilterForm
            scope="browse"
            state={registryView}
            onChange={handleRegistryChange}
            includeOnly={[
              'status',
              'first_seen_min_days',
              'last_seen_min_days',
              'tom_days_min',
            ]}
            labels={{
              status: 'Status',
              first_seen_min_days: 'First seen (days ago)',
              last_seen_min_days: 'Last seen (days ago)',
              tom_days_min: 'Turned in (days)',
            }}
            flat
          />
        </ControlGroup>

        <ControlGroup title="Building">
          <FilterForm
            scope="browse"
            state={registryView}
            onChange={handleRegistryChange}
            includeOnly={['furnished', 'ownership', 'building_material']}
            labels={{
              furnished: 'Furnished',
              ownership: 'Ownership',
              building_material: 'Building material',
            }}
            flat
          />
        </ControlGroup>

        <ControlGroup title="Amenities">
          <FilterForm
            scope="browse"
            state={registryView}
            onChange={handleRegistryChange}
            includeOnly={[
              'has_balcony', 'has_lift', 'has_parking',
              'terrace', 'cellar', 'garage',
              'min_parking_lots',
            ]}
            labels={{
              has_balcony: 'Balcony',
              has_lift: 'Lift',
              has_parking: 'Parking',
              terrace: 'Terrace',
              cellar: 'Cellar',
              garage: 'Garage',
              min_parking_lots: 'Min parking spaces',
            }}
            flat
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
