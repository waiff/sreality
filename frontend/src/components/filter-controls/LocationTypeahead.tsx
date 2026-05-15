/* LocationTypeahead — Mapy.cz-powered address autocomplete.
 *
 * Supersedes DistrictTypeahead. Suggestions come from the FastAPI
 * proxy at /maps/suggest (which keeps the MAPY_CZ_API_KEY server-side)
 * so the dropdown can return streets, cities, kraj, POIs, and addresses
 * — anything Mapy.cz indexes — rather than just the limited set of
 * district names already in our listings table.
 *
 * Interaction contract matches the rest of filter-controls:
 *   value: string[] | null  — selected place names (chips)
 *   onChange(next)         — null normalises to no constraint
 *
 * On pick, the suggestion's `name` field becomes the chip and is
 * appended to `value`. The downstream filter (listings.district text
 * match in queries.ts / browse_stats) is unchanged — picks of
 * district-shaped suggestions match cleanly; picks of street / POI
 * suggestions won't necessarily match `listings.district` text, but
 * they're surfaced so the operator gets a richer suggestion set and
 * can pick the part of the regional structure that actually filters
 * (typing "Vodičkova" surfaces "Praha 1" / "Praha 2" rows alongside
 * the street itself).
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

const QUERY_DEBOUNCE_MS = 150;
const MIN_QUERY_LEN = 2;

export function LocationTypeahead({
  value,
  onChange,
  onPick,
}: {
  value: ReadonlyArray<string> | null;
  onChange: (next: string[] | null) => void;
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
    return suggestQ.data.filter((s) => !selected.includes(s.name));
  }, [suggestQ.data, selected]);

  const emit = (next: string[]) =>
    onChange(next.length === 0 ? null : next);

  const add = (s: MapySuggestion) => {
    onPick?.(s);
    if (selected.includes(s.name)) return;
    emit([...selected, s.name]);
    setQuery('');
    setDebounced('');
  };

  const remove = (name: string) => {
    emit(selected.filter((x) => x !== name));
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
