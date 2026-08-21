/* THE code-splitting entry point: `lazyChunk` instead of React's `lazy`.
 *
 * Every deploy rebuilds `dist/` and renames every hashed chunk (measured: a
 * one-character source change rotates 30 of 30 JS chunk hashes, because each
 * lazy chunk hard-references the entry chunk by filename and Rollup's hash
 * cascade reaches all of them). `Caddyfile`'s `handle_path /assets/*` has no
 * SPA fallback, so a tab that was open across a deploy gets a hard 404 the
 * moment it lazy-loads a chunk it hasn't fetched yet. That is normal for any
 * hashed-asset SPA and is NOT the bug — the bug was what the app did about it.
 *
 * The incident this replaces (2026-08-19). `main.tsx` used to listen for
 * `vite:preloadError` globally and call `event.preventDefault()` before
 * scheduling `window.location.reload()`. But `preventDefault` on that event is
 * Vite's "I have handled this, do not throw" signal — its helper is
 * `baseModule().catch(handlePreloadError)`, and a prevented error makes that
 * catch return `undefined`, so the `import()` promise RESOLVES to `undefined`
 * instead of rejecting. React's `lazy` initializer then reads `.default` off
 * `undefined` and throws `TypeError: Cannot read properties of undefined
 * (reading 'default')` into the nearest error boundary — a full-page crash
 * screen, held on screen for the entire duration of the reload the handler had
 * just scheduled. The reload always worked; the handler merely let React
 * observe the corrupted module first, turning a self-healing hiccup into a
 * crash. A window-level listener structurally cannot do better: it has no
 * reference to the pending import, so it cannot keep React suspended.
 *
 * The fix is to own the failure where the import lives. On a rejected chunk
 * load this returns a promise that NEVER SETTLES and reloads the page. React
 * keeps the `Suspense` fallback mounted on a never-settling payload, and there
 * is no settled value for the lazy initializer to dereference — so no error can
 * paint while the browser navigates. The user sees the loading skeleton, then
 * the fresh page.
 *
 * The rails, and why each is shaped the way it is:
 * - RATE LIMIT, NOT A ONE-SHOT FLAG. The old boolean guard was cleared by a
 *   `load` listener on the very reload it triggered, so it bounded nothing
 *   across documents. A timestamp + minimum interval is what actually makes a
 *   reload loop impossible: a genuinely broken build (as opposed to a stale
 *   one) reloads once and then surfaces `StaleBuildError` instead of spinning.
 * - STORAGE FAILURES FAVOUR RECOVERY. Safari lockdown / private modes throw
 *   from `sessionStorage`. The old handler did its `getItem` first and outside
 *   a try, so a throw disabled recovery entirely. Here every storage access is
 *   wrapped and a failure degrades to "allow the reload" — the reload is the
 *   thing that fixes the user's page.
 * - `reloadInFlight` IS NOT REDUNDANT. React caches a lazy's payload, so a
 *   single component's constructor runs once even under StrictMode. The flag
 *   exists for CONCURRENT SIBLINGS: Listing Detail alone fires six lazy loads
 *   at once, and after a deploy all six reject. Without the flag each would
 *   call `reload()`.
 * - OFFLINE IS NOT STALENESS. `navigator.onLine === false` means a reload would
 *   replace a working app with the browser's offline page. Surface a typed
 *   error instead and let the boundary offer a manual retry.
 *
 * `StaleBuildError` is a `UserFacingError` (lib/errors.ts), so the error
 * boundary renders plain-language copy instead of a raw TypeError.
 *
 * An ESLint rule bans bare `lazy(` everywhere except this file, so the mechanism
 * stays app-wide by construction rather than by convention.
 */
import { lazy, type ComponentType, type LazyExoticComponent } from 'react';
import { UserFacingError } from '@/lib/errors';

/* A chunk failed to load and reloading is not the answer right now. `offline`:
 * the network is down, so a reload would lose the working app. `blocked`: we
 * already reloaded moments ago and the chunk still won't load, which points at
 * a broken deploy rather than a stale tab. */
export class StaleBuildError extends UserFacingError {
  constructor(
    readonly reason: 'offline' | 'blocked',
    cause: unknown,
  ) {
    super(
      reason === 'offline'
        ? 'This part of the app could not be downloaded because the connection is offline.'
        : 'A newer version of the app was released and this page could not finish loading it.',
      reason === 'offline'
        ? 'Check the connection, then reload.'
        : 'Reload to pick up the newest version.',
      { cause },
    );
    this.name = 'StaleBuildError';
  }
}

const RELOAD_STAMP_KEY = 'limen:chunkReloadAt';
const CHUNK_EVENTS_KEY = 'limen:chunkEvents';
/* One reload per minute per tab. Long enough that a rejected chunk from a
 * genuinely broken build can't spin the page, short enough that a second real
 * deploy during a long session still self-heals. */
const MIN_RELOAD_INTERVAL_MS = 60_000;
const MAX_RECORDED_EVENTS = 20;

/* A promise that never settles: React holds the Suspense fallback and never
 * gets a value to dereference or a rejection to escalate. */
const NEVER: Promise<never> = new Promise<never>(() => {});

let reloadInFlight = false;

function readStamp(): number | null {
  try {
    const raw = window.sessionStorage.getItem(RELOAD_STAMP_KEY);
    if (raw == null) return null;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function writeStamp(at: number): void {
  try {
    window.sessionStorage.setItem(RELOAD_STAMP_KEY, String(at));
  } catch {
    /* Storage unavailable — proceed with the reload anyway (see header). */
  }
}

export interface ChunkEvent {
  kind: 'reload' | 'blocked' | 'offline';
  at: string;
  path: string;
}

/* Observability tier 1: there is no client telemetry in this app and no backend
 * endpoint that accepts browser events, so a shipped fix cannot be verified
 * from server-side data. A capped ring in localStorage at least lets the
 * operator answer "is this still happening, and how often" from the console:
 *   JSON.parse(localStorage.getItem('limen:chunkEvents'))
 * Deliberately per-browser and best-effort. If this ever needs to be real
 * telemetry, this function is the single place a beacon would go. */
export function recordChunkEvent(kind: ChunkEvent['kind']): void {
  try {
    const event: ChunkEvent = {
      kind,
      at: new Date().toISOString(),
      path: window.location.pathname,
    };
    const events = [...readChunkEvents(), event].slice(-MAX_RECORDED_EVENTS);
    window.localStorage.setItem(CHUNK_EVENTS_KEY, JSON.stringify(events));
  } catch {
    /* Never let bookkeeping break recovery. */
  }
}

export function readChunkEvents(): ChunkEvent[] {
  try {
    const raw = window.localStorage.getItem(CHUNK_EVENTS_KEY);
    const parsed: unknown = raw == null ? [] : JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as ChunkEvent[]) : [];
  } catch {
    return [];
  }
}

/* Exported for tests; module state has to be resettable between cases. */
export function __resetChunkLoaderForTests(): void {
  reloadInFlight = false;
}

export async function loadChunk<T>(load: () => Promise<T>): Promise<T> {
  try {
    return await load();
  } catch (cause) {
    /* A sibling chunk already won the race and the browser is navigating —
     * stay suspended rather than reloading again. */
    if (reloadInFlight) return NEVER;

    if (typeof navigator !== 'undefined' && navigator.onLine === false) {
      recordChunkEvent('offline');
      throw new StaleBuildError('offline', cause);
    }

    const now = Date.now();
    const last = readStamp();
    if (last != null && now - last < MIN_RELOAD_INTERVAL_MS) {
      recordChunkEvent('blocked');
      throw new StaleBuildError('blocked', cause);
    }

    writeStamp(now);
    recordChunkEvent('reload');
    reloadInFlight = true;
    window.location.reload();
    return NEVER;
  }
}

/* Drop-in replacement for React.lazy at every code-splitting site. Mirrors
 * React's own signature so the swap is mechanical. */
export function lazyChunk<T extends ComponentType<any>>(
  load: () => Promise<{ default: T }>,
): LazyExoticComponent<T> {
  return lazy(() => loadChunk(load));
}
