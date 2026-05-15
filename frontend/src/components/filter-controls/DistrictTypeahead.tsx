/* DistrictTypeahead — autocomplete chip picker over the live district list.
 *
 * Extracted from the original Browse `DistrictPicker` so both
 * `<FilterForm>` consumers (Browse + Watchdog) can share the widget.
 * Backend supplies the list via `fetchDistrictFacets` keyed by
 * `['district-facets']`; the typeahead caches with React Query
 * (`staleTime: 10 min`) so reopening the sidebar doesn't refetch.
 *
 * Interface mirrors the rest of `filter-controls/`:
 *   value: string[] | null  // null = no selection (registry's "no constraint"),
 *                           //   [] is normalised to null on emission
 *   onChange(next): caller persists.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { fetchDistrictFacets } from '@/lib/queries';
import { fmtCount } from '@/lib/format';

export function DistrictTypeahead({
  value,
  onChange,
}: {
  value: ReadonlyArray<string> | null;
  onChange: (next: string[] | null) => void;
}) {
  const selected = useMemo(() => value ?? [], [value]);

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
    const remaining = facets.filter((f) => !selected.includes(f.district));
    if (!q) return remaining.slice(0, 60);
    return remaining
      .filter((f) => f.district.toLowerCase().includes(q))
      .slice(0, 60);
  }, [facets, query, selected]);

  const emit = (next: string[]) =>
    onChange(next.length === 0 ? null : next);

  const add = (d: string) => {
    emit([...selected, d]);
    setQuery('');
  };

  const remove = (d: string) => {
    emit(selected.filter((x) => x !== d));
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
          placeholder={
            isLoading
              ? 'Loading…'
              : selected.length === 0
                ? 'Type to search…'
                : 'Add another…'
          }
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
                  <span className="truncate text-[var(--color-ink)]">
                    {m.district}
                  </span>
                  <span className="font-mono text-[0.75rem] text-[var(--color-ink-3)] tabular-nums ml-3">
                    {fmtCount(m.count)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {selected.length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-1.5">
          {selected.map((d) => (
            <li key={d}>
              <button
                type="button"
                onClick={() => remove(d)}
                className="group inline-flex items-center gap-1.5 px-2 py-1 text-xs rounded-[var(--radius-sm)] bg-[var(--color-copper-soft)] text-[var(--color-copper)] hover:bg-[var(--color-copper)]/15 transition-colors"
                aria-label={`Remove ${d}`}
              >
                <span>{d}</span>
                <span
                  className="text-[var(--color-copper)]/60 group-hover:text-[var(--color-copper)]"
                  aria-hidden
                >
                  ×
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
