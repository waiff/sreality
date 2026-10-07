/* useMergeProperties — Browse's merge, through the one transport and the app's
 * MutationCache (lib/mutationCache): a success is the one receipt, the refresh
 * (the note marks' counts included, MS16) and `onMerged` (merge mode closes); a
 * refusal is the route's Czech sentence toasted once, merge mode left open.
 * lib/api reads its base URL at module evaluation, hence the dynamic imports
 * after the env stub. */

import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';

import { supabase } from './supabase';
import * as toast from './toast';

vi.mock('./toast', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./toast')>()),
  pushToast: vi.fn(() => 1),
}));

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubEnv('VITE_API_BASE_URL', 'https://api.test.invalid');
  vi.spyOn(supabase.auth, 'getSession').mockResolvedValue({
    data: { session: { access_token: 'USER-JWT' } },
    error: null,
  } as unknown as Awaited<ReturnType<typeof supabase.auth.getSession>>);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

async function mergeAnswered(status: number, body: unknown) {
  vi.stubGlobal('fetch', async () =>
    ({ ok: status < 400, status, text: async () => JSON.stringify(body) }) as Response);
  const { useMergeProperties } = await import('./useMergeProperties');
  const { createMutationCache } = await import('./mutationCache');
  const { curationKeys } = await import('./queries');
  const qc = new QueryClient({ mutationCache: createMutationCache() });
  qc.setQueryData(curationKeys.noteCounts, {});
  const invalidate = vi.spyOn(qc, 'invalidateQueries');
  const noteCountsStale = () => qc.getQueryState(curationKeys.noteCounts)?.isInvalidated;
  const onMerged = vi.fn();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MemoryRouter>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </MemoryRouter>
  );
  const { result } = renderHook(() => useMergeProperties(onMerged), { wrapper });
  const merge = () => result.current.mutateAsync([10, 20]);
  return { merge, invalidate, noteCountsStale, onMerged };
}

it('toasts the one receipt, refreshes what the merge moved and closes merge mode', async () => {
  const { merge, invalidate, noteCountsStale, onMerged } = await mergeAnswered(200, {
    merge_group_id: 'g', survivor_id: 10, retired_ids: [20], listings_moved: 1,
    pairs_ruled_same: 1, rulings_taken_back: 0, hidden_for_you: false,
    carried: { notes: 2, pipeline: null, collections: [], tags: [] },
  });
  await act(merge);
  const [[kind, message, ttl, action]] = vi.mocked(toast.pushToast).mock.calls;
  expect([kind, message, ttl, action?.label]).toEqual(
    ['ok', 'Sloučeno do nemovitosti #10. Přesunuto: 2 poznámky.', 0, 'Otevřít #10']);
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['curation'] });
  expect(noteCountsStale()).toBe(true);
  expect(onMerged).toHaveBeenCalledTimes(1);
});

it('toasts the route’s Czech refusal once and leaves merge mode open', async () => {
  const clash = 'Inzerát v kategorii Byty a inzerát v kategorii Domy …, proto je nelze sloučit.';
  const { merge, onMerged } = await mergeAnswered(409, {
    detail: { code: 'refused', message: clash, ids: [10, 20] },
  });
  await act(() => expect(merge()).rejects.toThrow(clash));
  expect(vi.mocked(toast.pushToast).mock.calls).toEqual([['err', clash]]);
  expect(onMerged).not.toHaveBeenCalled();
});
