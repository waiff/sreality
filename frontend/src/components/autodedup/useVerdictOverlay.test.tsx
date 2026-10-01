/* useVerdictOverlay — the one site that paints React state (not the query
 * cache) AND owns its failure message. Built on the app's real MutationCache
 * so the global toast is in the loop: a failed verdict must say why exactly
 * once (its own wording, the global one silent), and a split refused with 409
 * must arm the button without any toast at all. */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';

import * as api from '@/lib/api';
import { autodedupKeys } from '@/lib/autodedupKeys';
import { createMutationCache } from '@/lib/mutationCache';
import * as toast from '@/lib/toast';
import { useVerdictOverlay } from './useVerdictOverlay';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, postAutodedupVerdict: vi.fn(), postAutodedupSplitVerdict: vi.fn() };
});
vi.mock('@/lib/toast', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/toast')>();
  return { ...actual, pushToast: vi.fn(() => 1) };
});

type VerdictResponse = Awaited<ReturnType<typeof api.postAutodedupVerdict>>;
type SplitResponse = Awaited<ReturnType<typeof api.postAutodedupSplitVerdict>>;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function setup() {
  const qc = new QueryClient({
    mutationCache: createMutationCache(),
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidate = vi.spyOn(qc, 'invalidateQueries');
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  const view = renderHook(() => useVerdictOverlay(), { wrapper });
  return { ...view, invalidate };
}

const PAIR = { kind: 'pair' as const, verdict: 'same' as const, listing_lo: 1, listing_hi: 2 };
const stored = (verdict: 'same' | 'different') => ({
  id: 9, kind: 'pair' as const, listing_lo: 1, listing_hi: 2, cluster_key: null,
  verdict, note: null, decided_by: 'operator', decided_at: '2026-10-01T00:00:00Z',
});
const errToasts = () => vi.mocked(toast.pushToast).mock.calls.filter(([kind]) => kind === 'err');

describe('useVerdictOverlay', () => {
  beforeEach(() => vi.clearAllMocks());

  it('paints the row while the verdict is in flight, per key, and puts the stored row back on failure', async () => {
    const { result, invalidate } = setup();
    vi.mocked(api.postAutodedupVerdict).mockResolvedValueOnce({
      store_ready: true, data: stored('different'), must_not_link: true,
    } as VerdictResponse);
    act(() => result.current.submit('1:2', { ...PAIR, verdict: 'different' }));
    await waitFor(() => expect(result.current.overlay['1:2']?.id).toBe(9));
    vi.mocked(toast.pushToast).mockClear();

    const write = deferred<VerdictResponse>();
    vi.mocked(api.postAutodedupVerdict).mockReturnValueOnce(write.promise);
    act(() => result.current.submit('1:2', PAIR));
    // In flight: the provisional row is on screen and only this key is busy.
    expect(result.current.overlay['1:2']).toMatchObject({ id: 0, verdict: 'same', decided_by: 'ukládám…' });
    expect(result.current.isPending('1:2')).toBe(true);
    expect(result.current.isPending('3:4')).toBe(false);

    invalidate.mockClear();
    await act(async () => write.reject(new Error('server down')));
    await waitFor(() => expect(result.current.isPending('1:2')).toBe(false));
    expect(result.current.overlay['1:2']).toEqual(stored('different'));
    // Its own wording, once — the global toast stays silent.
    expect(toast.pushToast).toHaveBeenCalledTimes(1);
    expect(toast.pushToast).toHaveBeenCalledWith('err', 'Uložení se nepovedlo: server down');
    expect(invalidate).toHaveBeenCalledWith({ queryKey: autodedupKeys.validationProgress });
  });

  it('removes a provisional row that had nothing under it', async () => {
    const { result } = setup();
    const write = deferred<VerdictResponse>();
    vi.mocked(api.postAutodedupVerdict).mockReturnValueOnce(write.promise);
    act(() => result.current.submit('5:6', { ...PAIR, listing_lo: 5, listing_hi: 6 }));
    expect(result.current.overlay['5:6']?.decided_by).toBe('ukládám…');
    await act(async () => write.reject(new Error('nope')));
    await waitFor(() => expect('5:6' in result.current.overlay).toBe(false));
    expect(errToasts()).toEqual([['err', 'Uložení se nepovedlo: nope']]);
  });

  it('a split refused with 409 arms the confirm, rolls the badge back and toasts nothing', async () => {
    const { result, invalidate } = setup();
    const write = deferred<SplitResponse>();
    vi.mocked(api.postAutodedupSplitVerdict).mockReturnValueOnce(write.promise);
    const input = {
      cluster_key: 77, generation: 'g1',
      units: [{ listing_id: 1, unit: 'A' }, { listing_id: 2, unit: 'B' }],
    };
    act(() => result.current.submitSplit('77', input));
    expect(result.current.overlay['77']).toMatchObject({ verdict: 'different', decided_by: 'ukládám…' });
    expect(result.current.isPending('77')).toBe(true);

    await act(async () =>
      write.reject(new api.ApiError('a veto would be taken back', 409, null)));
    await waitFor(() => expect(result.current.splitErrors['77']?.needsConfirm).toBe(true));
    expect(result.current.splitErrors['77'].message).toBe('a veto would be taken back');
    expect('77' in result.current.overlay).toBe(false);
    expect(result.current.isPending('77')).toBe(false);
    expect(toast.pushToast).not.toHaveBeenCalled();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: autodedupKeys.validationProgress });
  });
});
