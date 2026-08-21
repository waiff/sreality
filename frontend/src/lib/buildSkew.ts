/* Notice a new deploy before a chunk 404 does.
 *
 * `lazyChunk` already makes staleness harmless: a missing chunk reloads the
 * page behind its own Suspense fallback. But it only fires when the user
 * happens to open a section whose chunk was never fetched, and the reload then
 * lands at whatever moment they chose to click. Until that happens the tab goes
 * on running old code against a moving backend.
 *
 * So: check whether the server is serving a different build, and if so offer a
 * reload rather than imposing one. This is what Remix's manifest-version check
 * and Next's hard-navigation-on-skew do; the difference here is that we never
 * navigate on the user's behalf — the toast has a button.
 *
 * BUILD IDENTITY WITHOUT BUILD PLUMBING. The running build is identified by its
 * own entry script's hashed URL, read from the DOM. Vite rewrites
 * `<script type="module" src="/src/main.tsx">` to the hashed
 * `/assets/index-<hash>.js` at build time, and that hash changes on every
 * deploy that changes anything (measured: 30 of 30 chunks rotate on a
 * one-character source change). No `define:` block, no Dockerfile ARG, no
 * version.json, and nothing that could accidentally become a build-time secret.
 * The deployed build is the same attribute read out of a fresh `index.html`,
 * which Caddy serves `max-age=0, must-revalidate` — so the probe sees the
 * current deploy, not a cached copy.
 *
 * In dev there is no hashed entry (`/src/main.tsx` is served as-is), so the two
 * ids always agree and the check is inert.
 *
 * WHEN IT RUNS. On tab focus, throttled to once every 5 minutes. A background
 * tab is exactly where a stale build accumulates, and focus is the moment the
 * user is about to act on it. No polling timer: an idle tab should cost
 * nothing, and a deploy is not urgent enough to interrupt a tab nobody is
 * looking at. */

const ENTRY_SELECTOR = 'script[type="module"][src]';
const MIN_PROBE_INTERVAL_MS = 5 * 60_000;

export function runningBuildId(doc: Document = document): string | null {
  return doc.querySelector<HTMLScriptElement>(ENTRY_SELECTOR)?.getAttribute('src') ?? null;
}

/* The entry script the server would hand a fresh visitor right now. Returns
 * null when it can't be determined — offline, a 5xx mid-deploy, an unexpected
 * document. Never throws: a failed probe must be indistinguishable from "no
 * news", or a flaky network turns into a nagging toast. */
export async function probeDeployedBuildId(): Promise<string | null> {
  try {
    const res = await fetch('/index.html', { cache: 'no-store' });
    if (!res.ok) return null;
    const html = await res.text();
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    return runningBuildId(parsed);
  } catch {
    return null;
  }
}

let lastProbeAt = 0;

/* Exported for tests; module state has to be resettable between cases. */
export function __resetBuildSkewForTests(): void {
  lastProbeAt = 0;
}

/* True when the server is serving a different build than this tab is running.
 * False on every uncertainty — unknown ids, a failed probe, or a probe that
 * ran too recently. */
export async function isBuildStale(now: number = Date.now()): Promise<boolean> {
  if (now - lastProbeAt < MIN_PROBE_INTERVAL_MS) return false;
  lastProbeAt = now;
  const running = runningBuildId();
  /* Dev serves the unhashed entry; nothing to compare. */
  if (running == null || !running.includes('/assets/')) return false;
  const deployed = await probeDeployedBuildId();
  if (deployed == null) return false;
  return deployed !== running;
}
