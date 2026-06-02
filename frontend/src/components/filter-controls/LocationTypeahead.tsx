/* LocationTypeahead — Mapy.cz-powered address autocomplete.
 *
 * Supersedes DistrictTypeahead. Suggestions come from the FastAPI
 * proxy at /maps/suggest (which keeps the MAPY_CZ_API_KEY server-side)
 * so the dropdown can return streets, cities, kraj, POIs, and addresses
 * — anything Mapy.cz indexes — rather than just the limited set of
 * district names already in our listings table.
 *
 * Interaction contract matches the rest of filter-controls:
 *   value: DistrictChip[] | null  — selected chips
 *   onChange(next)                — null normalises to no constraint
 *
 * Each chip is `{name, context, excluded?}`. On pick, the suggestion's
 * `name` field becomes the chip's `name`; `deriveContext` walks
 * `regionalStructure` for the nearest `regional.municipality` and
 * sets that as `context` (or null for picks already at the
 * municipality / region / country level). The downstream filter
 * (queries.ts applyFilters + browse_stats migration 146 + the
 * Watchdog matcher in api/notifications.py) matches each chip as
 *   (district/locality/okres/region ILIKE *name*)
 *   AND (context IS NULL OR district/locality/okres/region ILIKE *context*)
 * INCLUDE chips OR'd (match any), then AND NOT-(OR of EXCLUDE chips). The
 * per-chip `−`/`+` button toggles `excluded` (red chip = subtract this
 * locality). This is the registry-aligned widget for both Browse and
 * Watchdog — the same component renders in both surfaces through
 * `customWidgets={{districts: LocationTypeahead}}`.
 *
 * onPick (independent of the chip filter) fires once per pick so the
 * Browse map can fly the viewport to the picked place's centre —
 * useful even when the chip itself narrows the cohort tightly.
 *
 * No count column — Mapy.cz doesn't return one, and the previous
 * count (from `fetchDistrictFacets`) was misleading: it counted every
 * listing ever seen with that district name across all categories /
 * states, not the count under the current filter set.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import {
  fetchSuggest,
  type MapySuggestion,
  SUGGEST_NOT_CONFIGURED,
  typeBadge,
} from '@/lib/maps';
import type { DistrictChip } from '@/lib/filters';

const QUERY_DEBOUNCE_MS = 150;
const MIN_QUERY_LEN = 2;

/* Mapy.cz suggestion types that ARE municipality-or-coarser. Picks
 * at any of these get `context: null` — they already name the
 * municipality (or are above it), so there's nothing finer-grained
 * to narrow against. Picks below this granularity (street, address,
 * část obce, POI) inherit the nearest `regional.municipality` from
 * the `regionalStructure` chain. */
const MUNICIPALITY_OR_COARSER = new Set([
  'regional.municipality',
  'regional.region',
  'regional.country',
]);

export function deriveContext(s: MapySuggestion): string | null {
  if (MUNICIPALITY_OR_COARSER.has(s.type)) return null;
  const muni = (s.regionalStructure ?? []).find(
    (e) => e.type === 'regional.municipality',
  );
  return muni?.name ?? null;
}

const sameChip = (a: DistrictChip, b: DistrictChip): boolean =>
  a.name === b.name && a.context === b.context;

export function LocationTypeahead({
  value,
  onChange,
  onPick,
}: {
  value: ReadonlyArray<DistrictChip> | null;
  onChange: (next: DistrictChip[] | null) => void;
  /* Fires once per picked suggestion. The Browse map listens for this
   * to fly the viewport to the picked place — independent of the chip
   * filter (which may or may not narrow the cohort, depending on
   * whether the suggestion's `name` matches a real listings.district
   * value). Picks without a `position` are still emitted; the
   * receiver should no-op when `position` is missing. */
  onPick?: (s: MapySuggestion) => void;
}) {
  const selected = useMemo(() => value ?? [], [value]);

  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(query.trim()), QUERY_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [query]);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const suggestQ = useQuery({
    queryKey: ['maps', 'suggest', debounced],
    queryFn: ({ signal }) => fetchSuggest(debounced, signal),
    enabled: debounced.length >= MIN_QUERY_LEN,
    staleTime: 60_000,
    retry: false,
  });

  const notConfigured =
    suggestQ.error instanceof Error &&
    suggestQ.error.message === SUGGEST_NOT_CONFIGURED;

  const matches = useMemo(() => {
    if (!suggestQ.data) return [];
    return suggestQ.data.filter((s) => {
      const chip: DistrictChip = { name: s.name, context: deriveContext(s) };
      return !selected.some((c) => sameChip(c, chip));
    });
  }, [suggestQ.data, selected]);

  const emit = (next: DistrictChip[]) =>
    onChange(next.length === 0 ? null : next);

  const add = (s: MapySuggestion) => {
    onPick?.(s);
    const chip: DistrictChip = { name: s.name, context: deriveContext(s) };
    if (selected.some((c) => sameChip(c, chip))) return;
    emit([...selected, chip]);
    setQuery('');
    setDebounced('');
  };

  const remove = (chip: DistrictChip) => {
    emit(selected.filter((c) => !sameChip(c, chip)));
  };

  /* Flip a chip between INCLUDE and EXCLUDE. Identity is name+context
   * (sameChip), so toggling never duplicates or drops the chip — it only
   * subtracts/adds its matches from the cohort. */
  const toggleExclude = (chip: DistrictChip) => {
    emit(
      selected.map((c) =>
        sameChip(c, chip) ? { ...c, excluded: !(c.excluded === true) } : c,
      ),
    );
  };

  const isLoading = suggestQ.isFetching && debounced.length >= MIN_QUERY_LEN;
  const tooShort = debounced.length > 0 && debounced.length < MIN_QUERY_LEN;

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
            selected.length === 0 ? 'Type a city, district, or street…' : 'Add another…'
          }
          disabled={notConfigured}
          className="w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)] disabled:opacity-60"
        />
        {open && !notConfigured && (
          <SuggestPanel
            isLoading={isLoading}
            tooShort={tooShort}
            matches={matches}
            onPick={add}
          />
        )}
      </div>

      {notConfigured && (
        <p className="mt-1 text-[0.7rem] text-[var(--color-ink-4)]">
          Address search isn&apos;t configured for this deploy. Type a
          place name manually below and press Enter, or use the
          map&apos;s centre + radius mode.
        </p>
      )}

      {selected.length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-1.5">
          {selected.map((chip) => {
            const label = chip.context
              ? `${chip.name} · ${chip.context}`
              : chip.name;
            const key = `${chip.name}::${chip.context ?? ''}`;
            const excluded = chip.excluded === true;
            /* Excluded chips read as a negative filter: brick (the
             * inactive/failures semantic colour) + a leading minus. The
             * −/+ button toggles the mode; × removes the chip. */
            const palette = excluded
              ? 'bg-[var(--color-brick-soft)] text-[var(--color-brick)]'
              : 'bg-[var(--color-copper-soft)] text-[var(--color-copper)]';
            return (
              <li key={key}>
                <span
                  className={`inline-flex items-center gap-1 pl-1 pr-1 py-0.5 text-xs rounded-[var(--radius-sm)] ${palette}`}
                >
                  <button
                    type="button"
                    onClick={() => toggleExclude(chip)}
                    className="inline-flex h-4 w-4 items-center justify-center rounded-[var(--radius-xs)] font-semibold leading-none opacity-70 hover:opacity-100 transition-opacity"
                    aria-label={excluded ? `Include ${label}` : `Exclude ${label}`}
                    title={excluded ? 'Click to include' : 'Click to exclude'}
                  >
                    {excluded ? '+' : '−'}
                  </button>
                  <span className="px-0.5">{excluded ? `− ${label}` : label}</span>
                  <button
                    type="button"
                    onClick={() => remove(chip)}
                    className="inline-flex h-4 w-4 items-center justify-center rounded-[var(--radius-xs)] leading-none opacity-60 hover:opacity-100 transition-opacity"
                    aria-label={`Remove ${label}`}
                    title="Remove"
                  >
                    ×
                  </button>
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function SuggestPanel({
  isLoading,
  tooShort,
  matches,
  onPick,
}: {
  isLoading: boolean;
  tooShort: boolean;
  matches: ReadonlyArray<MapySuggestion>;
  onPick: (s: MapySuggestion) => void;
}) {
  if (tooShort) {
    return (
      <div className="absolute z-20 mt-1 w-full rounded-[var(--radius-md)] bg-[var(--color-paper-3)] border border-[var(--color-rule-strong)] shadow-[0_4px_16px_rgba(0,0,0,0.06)] px-3 py-2 text-[0.75rem] text-[var(--color-ink-4)]">
        Type at least 2 characters…
      </div>
    );
  }
  if (isLoading) {
    return (
      <div className="absolute z-20 mt-1 w-full rounded-[var(--radius-md)] bg-[var(--color-paper-3)] border border-[var(--color-rule-strong)] shadow-[0_4px_16px_rgba(0,0,0,0.06)] px-3 py-2 text-[0.75rem] text-[var(--color-ink-4)]">
        Searching…
      </div>
    );
  }
  if (matches.length === 0) return null;
  return (
    <ul
      role="listbox"
      className="absolute z-20 mt-1 w-full max-h-72 overflow-y-auto rounded-[var(--radius-md)] bg-[var(--color-paper-3)] border border-[var(--color-rule-strong)] shadow-[0_4px_16px_rgba(0,0,0,0.06)] py-1"
    >
      {matches.map((s, idx) => (
        <li key={`${s.name}-${idx}`}>
          <button
            type="button"
            onClick={() => onPick(s)}
            className="w-full flex items-center justify-between gap-3 px-3 py-1.5 text-left hover:bg-[var(--color-copper-soft)]"
          >
            <span className="min-w-0 flex-1 truncate">
              {/* Mapy.cz puts the place name in `name` (e.g. "Ostrava")
                * and the regional context in `location`
                * (e.g. "okres Ostrava-město, Moravskoslezský kraj").
                * `label` is the type-in-Czech ("Obec", "Vesnice") which
                * the type badge already shows. Display name + location
                * stacked so the dropdown reads as a real address list. */}
              <span className="block text-sm text-[var(--color-ink)] truncate">
                {s.name}
              </span>
              {s.location ? (
                <span className="block text-[0.7rem] text-[var(--color-ink-3)] truncate">
                  {s.location}
                </span>
              ) : null}
            </span>
            <span className="shrink-0 text-[0.65rem] tracking-[0.12em] uppercase text-[var(--color-ink-3)] border border-[var(--color-rule)] rounded-[var(--radius-xs)] px-1.5 py-0.5">
              {typeBadge(s.type)}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}
