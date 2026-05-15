/* Mapy.cz-backed multi-select location chip picker.
 *
 * Single text input that fetches /maps/suggest as the user types and
 * renders the response as a keyboard-navigable dropdown. Picking an
 * item adds it as a chip; clicking the × on a chip removes it. The
 * `name` field of each chip is what the caller persists — typically
 * the listing's `district` column for verbatim matching.
 *
 * Renders a graceful fallback when /maps/suggest returns 503 (the
 * MAPY_CZ_API_KEY env var is unset): the input still accepts
 * comma-separated free text, matching the legacy CSV input.
 */

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from 'react';

import {
  fetchSuggest,
  type MapySuggestion,
  SUGGEST_NOT_CONFIGURED,
  typeBadge,
} from '@/lib/maps';

export interface LocationChip {
  /* The persisted value — what the backend matches verbatim against
   * the listing's district column. Equals `MapySuggestion.name` when
   * picked from the dropdown, equals the raw typed string when the
   * suggest endpoint isn't configured. */
  name: string;
  /* What the chip renders. Typically `MapySuggestion.label`, which
   * includes the regional context ("Praha 2, hlavní město Praha");
   * falls back to `name` for free-text entries. */
  label: string;
  /* Mapy.cz type code (e.g. "regional.municipality"); used by the
   * chip to render a small Czech badge via `typeBadge`. Empty string
   * for free-text entries — the chip then omits the badge. */
  type: string;
}

interface Props {
  value: LocationChip[];
  onChange: (next: LocationChip[]) => void;
  placeholder?: string;
}

const DEBOUNCE_MS = 180;

export function LocationChipPicker({
  value,
  onChange,
  placeholder = 'Start typing a district or municipality…',
}: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const listboxId = useId();

  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');
  const [suggestions, setSuggestions] = useState<MapySuggestion[]>([]);
  const [activeIdx, setActiveIdx] = useState(0);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [notConfigured, setNotConfigured] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(query.trim()), DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [query]);

  useEffect(() => {
    if (!debounced) {
      setSuggestions([]);
      setLoading(false);
      return;
    }
    const ctrl = new AbortController();
    setLoading(true);
    fetchSuggest(debounced, ctrl.signal)
      .then((items) => {
        setSuggestions(items);
        setActiveIdx(0);
        setNotConfigured(false);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name === 'AbortError') return;
        if ((err as Error)?.message === SUGGEST_NOT_CONFIGURED) {
          setNotConfigured(true);
          setSuggestions([]);
        }
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [debounced]);

  /* Close the dropdown on outside click. */
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  const chipKey = useCallback(
    (c: LocationChip) => `${c.type}:${c.name}`.toLowerCase(),
    [],
  );

  const existingKeys = useMemo(
    () => new Set(value.map(chipKey)),
    [value, chipKey],
  );

  const addChip = useCallback(
    (chip: LocationChip) => {
      if (!chip.name.trim()) return;
      if (existingKeys.has(chipKey(chip))) {
        setQuery('');
        return;
      }
      onChange([...value, chip]);
      setQuery('');
      setSuggestions([]);
      inputRef.current?.focus();
    },
    [value, onChange, existingKeys, chipKey],
  );

  const removeChipAt = useCallback(
    (i: number) => {
      onChange(value.filter((_, idx) => idx !== i));
      inputRef.current?.focus();
    },
    [value, onChange],
  );

  const commitFreeText = useCallback(() => {
    const raw = query.trim();
    if (!raw) return;
    /* Allow comma-separated paste even when the dropdown is in
     * suggest mode — same affordance as the legacy CSV input. */
    const parts = raw
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    if (parts.length === 0) return;
    const fresh: LocationChip[] = [];
    for (const p of parts) {
      const chip: LocationChip = { name: p, label: p, type: '' };
      if (!existingKeys.has(chipKey(chip))) fresh.push(chip);
    }
    if (fresh.length) onChange([...value, ...fresh]);
    setQuery('');
  }, [query, value, onChange, existingKeys, chipKey]);

  const onPick = useCallback(
    (s: MapySuggestion) => {
      addChip({
        name: s.name,
        label: s.label || s.name,
        type: s.type,
      });
    },
    [addChip],
  );

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (suggestions.length) {
        setOpen(true);
        setActiveIdx((i) => Math.min(i + 1, suggestions.length - 1));
      }
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIdx((i) => Math.max(i - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (open && suggestions.length > 0) {
        onPick(suggestions[activeIdx]);
      } else {
        commitFreeText();
      }
    } else if (e.key === 'Escape') {
      setOpen(false);
    } else if (e.key === 'Backspace' && query === '' && value.length > 0) {
      e.preventDefault();
      removeChipAt(value.length - 1);
    } else if (e.key === ',' || e.key === 'Tab') {
      /* Comma / Tab commits the current input as free text — same
       * paste-many-at-once UX as the legacy CSV input. Tab without
       * a query just moves focus normally. */
      if (query.trim()) {
        e.preventDefault();
        commitFreeText();
      }
    }
  };

  const showDropdown =
    open &&
    debounced.length > 0 &&
    !notConfigured &&
    (loading || suggestions.length > 0);

  return (
    <div ref={wrapRef} className="relative">
      <div
        className="flex flex-wrap items-center gap-1.5 min-h-[40px] px-2 py-1.5 rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] focus-within:border-[var(--color-rule-strong)]"
        onClick={() => inputRef.current?.focus()}
      >
        {value.map((chip, i) => (
          <Chip key={`${chipKey(chip)}-${i}`} chip={chip} onRemove={() => removeChipAt(i)} />
        ))}
        <input
          ref={inputRef}
          type="text"
          value={query}
          placeholder={value.length === 0 ? placeholder : ''}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
          }}
          onFocus={() => {
            if (debounced) setOpen(true);
          }}
          onKeyDown={onKeyDown}
          role="combobox"
          aria-expanded={showDropdown}
          aria-controls={listboxId}
          aria-autocomplete="list"
          className="flex-1 min-w-[120px] py-1 text-sm bg-transparent text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none"
        />
      </div>

      {showDropdown ? (
        <ul
          id={listboxId}
          role="listbox"
          className="absolute z-20 left-0 right-0 mt-1 max-h-[280px] overflow-y-auto rounded-[var(--radius-sm)] bg-[var(--color-paper-2)] border border-[var(--color-rule)] shadow-sm"
        >
          {loading && suggestions.length === 0 ? (
            <li className="px-3 py-2 text-[0.75rem] text-[var(--color-ink-3)]">
              Searching…
            </li>
          ) : (
            suggestions.map((s, i) => {
              const isActive = i === activeIdx;
              const already = existingKeys.has(
                chipKey({ name: s.name, label: s.label, type: s.type }),
              );
              return (
                <li
                  key={`${s.type}-${s.label}-${i}`}
                  role="option"
                  aria-selected={isActive}
                  onMouseDown={(e) => {
                    /* mousedown not click — fires before the input's
                     * blur, so the dropdown doesn't disappear under us. */
                    e.preventDefault();
                    if (!already) onPick(s);
                  }}
                  onMouseEnter={() => setActiveIdx(i)}
                  className={[
                    'flex items-center justify-between gap-2 px-3 py-1.5 text-sm cursor-pointer transition-colors',
                    isActive
                      ? 'bg-[var(--color-copper-soft)]/60 text-[var(--color-ink)]'
                      : 'text-[var(--color-ink-2)]',
                    already ? 'opacity-40 cursor-not-allowed' : '',
                  ].join(' ')}
                >
                  <span className="truncate">
                    <span className="text-[var(--color-ink)]">{s.name}</span>
                    {s.label && s.label !== s.name ? (
                      <span className="ml-1.5 text-[var(--color-ink-3)]">
                        {s.label}
                      </span>
                    ) : null}
                  </span>
                  <span className="shrink-0 text-[0.65rem] tracking-wide uppercase text-[var(--color-ink-3)]">
                    {already ? 'added' : typeBadge(s.type)}
                  </span>
                </li>
              );
            })
          )}
        </ul>
      ) : null}

      {notConfigured ? (
        <p className="mt-1 text-[0.7rem] text-[var(--color-ochre)]">
          Mapy.cz autocomplete is unavailable (server not configured).
          Type values separated by commas; press Enter to add.
        </p>
      ) : null}
    </div>
  );
}

function Chip({
  chip,
  onRemove,
}: {
  chip: LocationChip;
  onRemove: () => void;
}) {
  return (
    <span className="inline-flex items-center gap-1 pl-2 pr-1 py-0.5 text-[0.75rem] rounded-[var(--radius-xs)] bg-[var(--color-copper-soft)]/70 text-[var(--color-ink)] border border-[var(--color-copper)]/30">
      <span>{chip.name}</span>
      {chip.type ? (
        <span className="text-[0.6rem] tracking-wide uppercase text-[var(--color-ink-3)]">
          {typeBadge(chip.type)}
        </span>
      ) : null}
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onRemove();
        }}
        aria-label={`Remove ${chip.name}`}
        className="ml-0.5 w-4 h-4 inline-flex items-center justify-center rounded-full text-[var(--color-ink-3)] hover:text-[var(--color-ink)] hover:bg-[var(--color-copper)]/20 transition-colors"
      >
        ×
      </button>
    </span>
  );
}
