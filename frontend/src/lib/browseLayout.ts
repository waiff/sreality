import { useCallback, useRef, useState } from 'react';

/* Persisted column widths for the Browse page. The three columns are the
 * filter sidebar (column 1) and, on the Listings/map tab, the cards list
 * (column 2) and the map (column 3). The operator can drag the two
 * dividers between them; we remember the result per-browser in
 * localStorage so the layout survives a reload.
 *
 * The sidebar is stored as a pixel width (sidebars are conventionally
 * fixed-width). The cards|map split is stored as the map column's
 * fraction of the inner area, so it stays proportional when the window
 * resizes. Both values are clamped on read AND on write so a hand-edited
 * or stale storage value can never push a column off-screen. */

const SIDEBAR_KEY = 'sreality.browse.sidebarWidth';
const MAP_SPLIT_KEY = 'sreality.browse.mapSplitFraction';

export const SIDEBAR_DEFAULT = 320;
export const SIDEBAR_MIN = 240;
export const SIDEBAR_MAX = 560;

export const MAP_SPLIT_DEFAULT = 0.42;
export const MAP_SPLIT_MIN = 0.25;
export const MAP_SPLIT_MAX = 0.7;

const clamp = (n: number, min: number, max: number): number =>
  Math.min(max, Math.max(min, n));

function readNumber(key: string, fallback: number, min: number, max: number): number {
  try {
    const raw = localStorage.getItem(key);
    if (raw != null) {
      const n = Number(raw);
      if (Number.isFinite(n)) return clamp(n, min, max);
    }
  } catch {
    /* localStorage may be unavailable (SSR, private mode lockdown) — fall through */
  }
  return fallback;
}

function writeNumber(key: string, value: number): void {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    /* ignore */
  }
}

export interface PersistedWidth {
  value: number;
  /* Live update during a drag — clamps and updates state, no storage write. */
  set: (n: number) => void;
  /* Commit the current value to localStorage (called once on drag end). */
  persist: () => void;
  /* Restore the default and persist it (double-click a divider to reset). */
  reset: () => void;
}

function usePersistedWidth(
  key: string,
  fallback: number,
  min: number,
  max: number,
): PersistedWidth {
  const [value, setValue] = useState<number>(() => readNumber(key, fallback, min, max));
  /* Mirror the latest value in a ref so `persist` can read it without
   * being recreated on every state change (and without a closure over a
   * stale `value`). */
  const latest = useRef(value);

  const set = useCallback(
    (n: number) => {
      const next = clamp(n, min, max);
      latest.current = next;
      setValue(next);
    },
    [min, max],
  );

  const persist = useCallback(() => {
    writeNumber(key, latest.current);
  }, [key]);

  const reset = useCallback(() => {
    latest.current = fallback;
    setValue(fallback);
    writeNumber(key, fallback);
  }, [key, fallback]);

  return { value, set, persist, reset };
}

export const useSidebarWidth = (): PersistedWidth =>
  usePersistedWidth(SIDEBAR_KEY, SIDEBAR_DEFAULT, SIDEBAR_MIN, SIDEBAR_MAX);

export const useMapSplitFraction = (): PersistedWidth =>
  usePersistedWidth(MAP_SPLIT_KEY, MAP_SPLIT_DEFAULT, MAP_SPLIT_MIN, MAP_SPLIT_MAX);
