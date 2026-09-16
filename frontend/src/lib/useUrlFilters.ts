/* A filter bar whose state lives in the URL, not in the component.
 *
 * WHY. A review queue is worked across days, in several tabs, and handed to
 * someone else with a link ("look at block 563510, verdict unreviewed"). With
 * the filters in `useState` none of that survives: a reload restores the
 * default queue, the back button leaves the filters where they were, and a
 * pasted link shows a different list to the two people reading it. The URL is
 * the one place a filter set can be all three of bookmarkable, shareable and
 * reload-proof.
 *
 * THE CONTRACT, in one sentence: a key PRESENT in the query string wins; an
 * absent key means the default. So the writer only spells out what actually
 * differs from the default — including a value the operator CLEARED whose
 * default is non-empty (`?min_score=`), which is a real state and not the same
 * thing as "never set". That round-trips exactly: read(write(x)) === x.
 *
 * `replace: true` on every write, because a filter is not a destination: ten
 * keystrokes in a number field must not put ten entries in the history stack
 * between the operator and the page they came from.
 *
 * The cursor is deliberately NOT here. Keyset pages are ephemeral — a shared
 * link means "this filter", never "this page of it", and a stale cursor pasted
 * into a rebuilt generation is a page of rows nobody can explain. Changing a
 * filter changes the react-query key, which restarts the list at page one.
 */

import { useCallback, useMemo, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';

/* "Every property of T is a string" — spelled as a mapped constraint rather
 * than `Record<string, string>`, which an interface (no index signature) can
 * never satisfy, and the filter states ARE interfaces. */
export type UrlFilterState<T> = { [K in keyof T]: string };

export function readUrlFilters<T extends UrlFilterState<T>>(
  params: URLSearchParams,
  defaults: T,
): T {
  const out = { ...defaults };
  for (const key of Object.keys(defaults)) {
    const raw = params.get(key);
    if (raw !== null) out[key as keyof T] = raw as T[keyof T];
  }
  return out;
}

/* Every key that differs from its default, and nothing else — a short URL that
 * still says everything. Keys the caller does not own are carried through
 * untouched, so a page can keep an unrelated param (a drilled-in id, a
 * generation a link arrived with) beside its filters. */
export function writeUrlFilters<T extends UrlFilterState<T>>(
  previous: URLSearchParams,
  next: T,
  defaults: T,
): URLSearchParams {
  const merged = new URLSearchParams(previous);
  for (const key of Object.keys(next) as Array<keyof T & string>) {
    const value = next[key];
    if (value === defaults[key]) merged.delete(key);
    else merged.set(key, value);
  }
  return merged;
}

export function useUrlFilters<T extends UrlFilterState<T>>(
  defaults: T,
): [T, (next: T) => void] {
  const [params, setParams] = useSearchParams();
  /* The defaults object is usually a literal, so a new identity every render.
   * Held in a ref, the memo below keys on the URL alone and the returned state
   * is referentially stable — which matters, because it is part of the list's
   * react-query key and a new identity every render would refetch forever. */
  const defaultsRef = useRef(defaults);
  const value = useMemo(
    () => readUrlFilters(params, defaultsRef.current),
    [params],
  );
  const set = useCallback(
    (next: T) => {
      setParams((current) => writeUrlFilters(current, next, defaultsRef.current), {
        replace: true,
      });
    },
    [setParams],
  );
  return [value, set];
}
