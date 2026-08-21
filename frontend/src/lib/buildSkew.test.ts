/* buildSkew — the check must be silent on every uncertainty.
 *
 * A false positive here is a toast telling the operator to reload a tab that is
 * already current; a flaky network must therefore read as "no news", never as
 * "new version". These cases pin that asymmetry.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetBuildSkewForTests, isBuildStale, runningBuildId } from './buildSkew';

const RUNNING = '/assets/index-AAAA1111.js';

function setRunningEntry(src: string) {
  document.head.innerHTML = `<script type="module" src="${src}"></script>`;
}

function servedHtml(entry: string) {
  return {
    ok: true,
    text: async () => `<!doctype html><html><head><script type="module" src="${entry}"></script></head><body></body></html>`,
  } as unknown as Response;
}

beforeEach(() => {
  __resetBuildSkewForTests();
  setRunningEntry(RUNNING);
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.head.innerHTML = '';
});

describe('runningBuildId', () => {
  it('reads the entry script the document was loaded with', () => {
    expect(runningBuildId()).toBe(RUNNING);
  });
});

describe('isBuildStale', () => {
  it('is true when the server serves a different entry chunk', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(servedHtml('/assets/index-BBBB2222.js')),
    );
    await expect(isBuildStale()).resolves.toBe(true);
  });

  it('is false when the server serves the same entry chunk', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(servedHtml(RUNNING)));
    await expect(isBuildStale()).resolves.toBe(false);
  });

  it('is false when the probe fails — a flaky network is not news', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    await expect(isBuildStale()).resolves.toBe(false);
  });

  it('is false on a non-OK response mid-deploy', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false } as Response));
    await expect(isBuildStale()).resolves.toBe(false);
  });

  it('is inert in dev, where the entry is not hashed', async () => {
    setRunningEntry('/src/main.tsx');
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    await expect(isBuildStale()).resolves.toBe(false);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('throttles: a second check within the interval does not probe again', async () => {
    const fetchSpy = vi.fn().mockResolvedValue(servedHtml('/assets/index-BBBB2222.js'));
    vi.stubGlobal('fetch', fetchSpy);
    const now = 1_000_000;
    await expect(isBuildStale(now)).resolves.toBe(true);
    await expect(isBuildStale(now + 60_000)).resolves.toBe(false);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    await expect(isBuildStale(now + 5 * 60_000 + 1)).resolves.toBe(true);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });
});
