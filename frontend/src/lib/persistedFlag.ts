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
