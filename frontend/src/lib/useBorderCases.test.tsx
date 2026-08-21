/* useBorderCases — the one store behind every "Border case" button.
 *
 * Hermetic: mock the Supabase read and the two writes. Pins the three
 * properties the review grids depend on and that a type-checker cannot see:
 * ids accumulate (a settled id is never re-requested, so a changing grid never
 * blanks a flag it already holds) and a toggle shows immediately, rolling back
 * if the write fails.
 *
 * Both were verified to fail when the property they pin is removed.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';

import { useBorderCases } from './useBorderCases';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, setBorderCase: vi.fn(), deleteBorderCase: vi.fn() };
});
vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  return { ...actual, fetchBorderCasesByImageIds: vi.fn() };
});

const read = () => vi.mocked(queries.fetchBorderCasesByImageIds);

/** A promise plus the handle to settle it later, so a test can hold a read or a
 *  write open and assert what the grid shows meanwhile. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function harness(initial: number[]) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  return renderHook(({ ids }: { ids: number[] }) => useBorderCases(ids), {
    initialProps: { ids: initial },
    wrapper,
  });
}

describe('useBorderCases', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    read().mockResolvedValue(new Set());
    vi.mocked(api.setBorderCase).mockResolvedValue({
      data: { image_id: 0, created_at: '2026-08-21T00:00:00Z' },
    });
    vi.mocked(api.deleteBorderCase).mockResolvedValue({ data: { deleted: true } });
  });

  it('reads the flags of the grid it is given', async () => {
    read().mockResolvedValue(new Set([2]));
    const { result } = harness([1, 2]);
    await waitFor(() => expect(result.current.has(2)).toBe(true));
    expect(result.current.has(1)).toBe(false);
    expect(read()).toHaveBeenCalledWith([1, 2]);
  });

  it('requests only never-seen ids, and never blanks a flag it already holds', async () => {
    read().mockResolvedValue(new Set([2]));
    const { result, rerender } = harness([1, 2]);
    await waitFor(() => expect(result.current.has(2)).toBe(true));

    // The grid moves on: image 1 is reviewed away, image 3 arrives. Image 2 is
    // still on screen and must keep its flag through the whole transition —
    // keying the read on the id list is what used to blank it.
    const next = deferred<Set<number>>();
    read().mockReturnValue(next.promise);
    rerender({ ids: [2, 3] });
    expect(result.current.has(2)).toBe(true);

    await waitFor(() => expect(read()).toHaveBeenCalledTimes(2));
    expect(read()).toHaveBeenLastCalledWith([3]);
    await act(async () => {
      next.resolve(new Set([3]));
    });
    await waitFor(() => expect(result.current.has(3)).toBe(true));
    expect(result.current.has(2)).toBe(true);
  });

  it('flags optimistically — the tile shows it before the write lands', async () => {
    const { result } = harness([5]);
    await waitFor(() => expect(read()).toHaveBeenCalled());
    const write = deferred<{ data: api.BorderCase }>();
    vi.mocked(api.setBorderCase).mockReturnValue(write.promise);

    act(() => result.current.toggle(5));
    // The write is still open below — this is the tile painting ahead of it.
    expect(result.current.has(5)).toBe(true);
    expect(result.current.isPending(5)).toBe(true);
    await waitFor(() => expect(api.setBorderCase).toHaveBeenCalledWith(5));

    await act(async () => {
      write.resolve({ data: { image_id: 5, created_at: 't' } });
    });
    expect(result.current.has(5)).toBe(true);
    expect(result.current.isPending(5)).toBe(false);
  });

  it('unflags an already-flagged image through the delete endpoint', async () => {
    read().mockResolvedValue(new Set([5]));
    const { result } = harness([5]);
    await waitFor(() => expect(result.current.has(5)).toBe(true));

    await act(async () => {
      result.current.toggle(5);
    });
    expect(api.deleteBorderCase).toHaveBeenCalledWith(5);
    expect(api.setBorderCase).not.toHaveBeenCalled();
    expect(result.current.has(5)).toBe(false);
  });

  it('rolls the flag back when the write fails', async () => {
    const { result } = harness([5]);
    await waitFor(() => expect(read()).toHaveBeenCalled());
    vi.mocked(api.setBorderCase).mockRejectedValue(new Error('nope'));

    await act(async () => {
      result.current.toggle(5);
    });
    expect(result.current.has(5)).toBe(false);
    expect(result.current.isPending(5)).toBe(false);
  });
});
