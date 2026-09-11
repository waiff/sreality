import { useCallback, useState } from 'react';

/* One boolean workspace preference, remembered per-browser in localStorage.
 *
 * These are DISPLAY preferences, never part of a shareable view: a link
 * carries what the recipient should see, not how this browser has its panels
 * and photo sizes set. Every read/write is guarded — localStorage can throw
 * (private-mode lockdown), and a preference must never take a page down.
 *
 * Lives on its own rather than inside browseLayout.ts because more than one
 * surface now keeps flags this way (Browse's map-collapsed + card image size,
 * the NEW DEDUP labeling grid's image size). Each caller owns its OWN key —
 * two surfaces sizing their photos independently is the point; sharing one key
 * would make a change on one page silently reshape the other. */

export function readFlag(key: string, fallback: boolean): boolean {
  try {
    const raw = localStorage.getItem(key);
    if (raw != null) return raw === '1';
  } catch {
    /* localStorage may be unavailable (SSR, private mode lockdown) — fall through */
  }
  return fallback;
}

function writeFlag(key: string, value: boolean): void {
  try {
    localStorage.setItem(key, value ? '1' : '0');
  } catch {
    /* ignore */
  }
}

export interface PersistedFlag {
  value: boolean;
  /* Set + commit to localStorage in one step (a toggle has no drag, so
   * unlike PersistedWidth there's no separate live-update / persist split). */
  set: (v: boolean) => void;
  toggle: () => void;
}

export function usePersistedFlag(key: string, fallback: boolean): PersistedFlag {
  const [value, setValue] = useState<boolean>(() => readFlag(key, fallback));
  const set = useCallback(
    (v: boolean) => {
      setValue(v);
      writeFlag(key, v);
    },
    [key],
  );
  const toggle = useCallback(() => {
    /* Functional update so the callback never closes over a stale value. */
    setValue((prev) => {
      const next = !prev;
      writeFlag(key, next);
      return next;
    });
  }, [key]);
  return { value, set, toggle };
}

/* The same preference, when the choice is one of N labelled steps rather than
 * on/off (Pipeline's three card sizes). Stored as the literal member so the
 * value is readable in devtools, and validated on read: a key left behind by an
 * older build — or edited by hand — falls back instead of rendering a size the
 * geometry table has no entry for. */
export function readChoice<T extends string>(
  key: string,
  allowed: readonly T[],
  fallback: T,
): T {
  try {
    const raw = localStorage.getItem(key);
    if (raw != null && (allowed as readonly string[]).includes(raw)) return raw as T;
  } catch {
    /* localStorage may be unavailable (SSR, private mode lockdown) — fall through */
  }
  return fallback;
}

export interface PersistedChoice<T extends string> {
  value: T;
  set: (v: T) => void;
}

export function usePersistedChoice<T extends string>(
  key: string,
  allowed: readonly T[],
  fallback: T,
): PersistedChoice<T> {
  const [value, setValue] = useState<T>(() => readChoice(key, allowed, fallback));
  const set = useCallback(
    (v: T) => {
      setValue(v);
      try {
        localStorage.setItem(key, v);
      } catch {
        /* ignore */
      }
    },
    [key],
  );
  return { value, set };
}
