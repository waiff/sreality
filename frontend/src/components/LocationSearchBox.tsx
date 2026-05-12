import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react';
import {
  fetchSuggest,
  resolveSuggestion,
  typeBadge,
  SUGGEST_NOT_CONFIGURED,
  type LocationResolution,
  type MapySuggestion,
} from '@/lib/maps';

const DEBOUNCE_MS = 250;

interface Props {
  onResolve: (result: LocationResolution) => void;
  onUnconfigured?: () => void;
}

type Status = 'idle' | 'loading' | 'ok' | 'error' | 'unconfigured';

export default function LocationSearchBox({ onResolve, onUnconfigured }: Props) {
  const [query, setQuery] = useState('');
  const [items, setItems] = useState<MapySuggestion[]>([]);
  const [status, setStatus] = useState<Status>('idle');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [activeIdx, setActiveIdx] = useState(-1);
  const [resolving, setResolving] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  /* Debounced fetch. Aborts in-flight requests when query changes
   * (prevents out-of-order suggestion lists from overwriting newer ones). */
  useEffect(() => {
    const trimmed = query.trim();
    if (trimmed.length < 2) {
      setItems([]);
      setStatus('idle');
      return;
    }
    setStatus('loading');
    const ctrl = new AbortController();
    const handle = window.setTimeout(async () => {
      try {
        const results = await fetchSuggest(trimmed, ctrl.signal);
        if (ctrl.signal.aborted) return;
        setItems(results);
        setStatus('ok');
        setActiveIdx(results.length > 0 ? 0 : -1);
      } catch (err) {
        if (ctrl.signal.aborted) return;
        if (err instanceof Error && err.message === SUGGEST_NOT_CONFIGURED) {
          setStatus('unconfigured');
          onUnconfigured?.();
        } else {
          setErrorMsg(err instanceof Error ? err.message : 'Suggest failed');
          setStatus('error');
        }
      }
    }, DEBOUNCE_MS);
    return () => {
      window.clearTimeout(handle);
      ctrl.abort();
    };
  }, [query, onUnconfigured]);

  /* Outside-click close. */
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const pick = useCallback(
    async (item: MapySuggestion) => {
      setOpen(false);
      setResolving(true);
      try {
        const result = await resolveSuggestion(item);
        onResolve(result);
        setQuery(item.location ?? item.name);
      } catch (err) {
        setErrorMsg(err instanceof Error ? err.message : 'Resolve failed');
        setStatus('error');
      } finally {
        setResolving(false);
      }
    },
    [onResolve],
  );

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (status === 'unconfigured') return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (items.length === 0) return;
      setOpen(true);
      setActiveIdx((i) => (i + 1) % items.length);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (items.length === 0) return;
      setOpen(true);
      setActiveIdx((i) => (i <= 0 ? items.length - 1 : i - 1));
    } else if (e.key === 'Enter') {
      if (open && activeIdx >= 0 && activeIdx < items.length) {
        e.preventDefault();
        void pick(items[activeIdx]);
      }
    } else if (e.key === 'Escape') {
      setOpen(false);
    }
  };

  const showDropdown =
    open &&
    status !== 'unconfigured' &&
    query.trim().length >= 2 &&
    (items.length > 0 || status === 'loading' || status === 'error');

  const showUnconfiguredHint = status === 'unconfigured';

  const hint = useMemo(() => {
    if (showUnconfiguredHint) {
      return 'Hledání lokalit není nakonfigurováno — použijte pokročilé filtry.';
    }
    return null;
  }, [showUnconfiguredHint]);

  return (
    <div ref={wrapRef} className="relative">
      <label className="block">
        <span className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Lokalita
        </span>
        <div className="mt-1.5 relative">
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setOpen(true);
              if (status === 'unconfigured') setStatus('idle');
            }}
            onFocus={() => setOpen(true)}
            onKeyDown={onKeyDown}
            placeholder="Hledat lokalitu, ulici, adresu…"
            disabled={resolving}
            aria-autocomplete="list"
            aria-controls="location-search-listbox"
            aria-expanded={showDropdown}
            aria-activedescendant={
              activeIdx >= 0 ? `location-suggestion-${activeIdx}` : undefined
            }
            className="w-full pl-9 pr-9 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-3)] focus:outline-none focus:border-[var(--color-rule-strong)] disabled:opacity-60"
          />
          <PinIcon className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[var(--color-ink-3)]" />
          {(status === 'loading' || resolving) && (
            <Spinner className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[var(--color-ink-3)]" />
          )}
        </div>
      </label>

      {hint && (
        <p className="mt-1.5 text-[0.75rem] text-[var(--color-ochre)]">
          {hint}
        </p>
      )}

      {showDropdown && (
        <ul
          id="location-search-listbox"
          role="listbox"
          className="absolute z-30 mt-1 w-full max-h-80 overflow-y-auto rounded-[var(--radius-md)] bg-[var(--color-paper-3)] border border-[var(--color-rule-strong)] py-1"
        >
          {status === 'loading' && items.length === 0 && (
            <li className="px-3 py-2 text-sm text-[var(--color-ink-3)] italic">
              Hledám…
            </li>
          )}
          {status === 'error' && (
            <li className="px-3 py-2 text-sm text-[var(--color-brick)]">
              {errorMsg ?? 'Vyhledávání selhalo.'}
            </li>
          )}
          {status === 'ok' && items.length === 0 && (
            <li className="px-3 py-2 text-sm text-[var(--color-ink-3)] italic">
              Žádné výsledky.
            </li>
          )}
          {items.map((item, idx) => {
            const subtitle =
              item.location && item.location !== item.name ? item.location : null;
            return (
              <li key={`${item.name}-${idx}`}>
                <button
                  type="button"
                  id={`location-suggestion-${idx}`}
                  role="option"
                  aria-selected={idx === activeIdx}
                  onMouseEnter={() => setActiveIdx(idx)}
                  onClick={() => void pick(item)}
                  className={[
                    'w-full flex items-center gap-3 px-3 py-1.5 text-left text-sm',
                    idx === activeIdx
                      ? 'bg-[var(--color-copper-soft)]'
                      : 'hover:bg-[var(--color-copper-soft)]',
                  ].join(' ')}
                >
                  <PinIcon className="shrink-0 text-[var(--color-ink-3)]" />
                  <span className="flex-1 min-w-0">
                    <span className="block truncate text-[var(--color-ink)]">
                      {item.name}
                    </span>
                    {subtitle && (
                      <span className="block truncate text-[0.7rem] text-[var(--color-ink-3)]">
                        {subtitle}
                      </span>
                    )}
                  </span>
                  <span className="shrink-0 text-[0.65rem] tracking-[0.08em] uppercase text-[var(--color-ink-3)] border border-[var(--color-rule)] rounded-[var(--radius-xs)] px-1.5 py-0.5">
                    {typeBadge(item.type)}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function PinIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M8 14s5-4.6 5-8.5A5 5 0 0 0 3 5.5C3 9.4 8 14 8 14Z" />
      <circle cx="8" cy="5.5" r="1.75" />
    </svg>
  );
}

function Spinner({ className }: { className?: string }) {
  return (
    <svg
      className={`${className ?? ''} animate-spin`}
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      aria-hidden
    >
      <path d="M14 8a6 6 0 1 1-6-6" strokeLinecap="round" />
    </svg>
  );
}
