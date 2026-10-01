/* useOptimisticWrite — rule #22's one write policy.
 *
 * Built on the app's real MutationCache (lib/mutationCache) so the global "a
 * write failed" toast is in the loop: the property the policy exists for is
 * that a failed optimistic write snaps back AND says why, exactly once.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';

import { createMutationCache } from './mutationCache';
import {
  cachePatch,
  useOptimisticWrite,
  type OptimisticWrite,
  type OptimisticWriteResult,
} from './useOptimisticWrite';
import * as toast from '@/lib/toast';

vi.mock('@/lib/toast', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/toast')>();
  return { ...actual, pushToast: vi.fn(() => 1) };
});

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
  qc.setQueryData(['n'], 1);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  return { qc, wrapper };
}

const N = { queryKey: ['n'], exact: true };
const toTwo = () => [cachePatch<number>(N, () => 2)];
const boom = async (): Promise<string> => {
  throw new Error('boom');
};

describe('useOptimisticWrite', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows the patch while the write is in flight', async () => {
    const { qc, wrapper } = setup();
    const write = deferred<string>();
    const { result } = renderHook(
      () => useOptimisticWrite({ mutationKey: ['write', 't'], mutationFn: (_: number) => write.promise, patch: toTwo }),
      { wrapper },
    );
    act(() => result.current.mutate(1));
    await waitFor(() => expect(qc.getQueryData(['n'])).toBe(2));
    await act(async () => write.resolve('ok'));
    expect(qc.getQueryData(['n'])).toBe(2);
  });

  it('cancels an in-flight read so it cannot land over the patch', async () => {
    const { qc, wrapper } = setup();
    const read = deferred<number>();
    const write = deferred<string>();
    const { result } = renderHook(
      () => ({
        q: useQuery({ queryKey: ['n'], queryFn: () => read.promise, staleTime: Infinity }),
        w: useOptimisticWrite({ mutationKey: ['write', 't'], mutationFn: (_: number) => write.promise, patch: toTwo }),
      }),
      { wrapper },
    );
    act(() => {
      void result.current.q.refetch();
    });
    await waitFor(() => expect(qc.getQueryState(['n'])?.fetchStatus).toBe('fetching'));
    act(() => result.current.w.mutate(1));
    await waitFor(() => expect(qc.getQueryData(['n'])).toBe(2));
    await act(async () => read.resolve(99));
    expect(qc.getQueryData(['n'])).toBe(2);
  });

  it('rolls back from onSettled, never onError, and the global toast fires once', async () => {
    const { qc, wrapper } = setup();
    const write = deferred<string>();
    const { result } = renderHook(
      () => useOptimisticWrite({ mutationKey: ['write', 't'], mutationFn: (_: number) => write.promise, patch: toTwo }),
      { wrapper },
    );
    act(() => result.current.mutate(1));
    // The patch is on screen while the write is in flight…
    await waitFor(() => expect(qc.getQueryData(['n'])).toBe(2));
    expect(toast.pushToast).not.toHaveBeenCalled();
    await act(async () => write.reject(new Error('boom')));
    // …and gone once it fails.
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(qc.getMutationCache().getAll()[0].options.onError).toBeUndefined();
    expect(qc.getQueryData(['n'])).toBe(1);
    expect(toast.pushToast).toHaveBeenCalledTimes(1);
    expect(toast.pushToast).toHaveBeenCalledWith('err', 'boom');
  });

  it('rolls back BEFORE the re-read', async () => {
    const { qc, wrapper } = setup();
    const seen: unknown[] = [];
    vi.spyOn(qc, 'invalidateQueries').mockImplementation(async () => {
      seen.push(qc.getQueryData(['n']));
    });
    const { result } = renderHook(
      () =>
        useOptimisticWrite({
          mutationKey: ['write', 't'],
          mutationFn: (_: number) => boom(),
          patch: toTwo,
          revalidate: [['n']],
        }),
      { wrapper },
    );
    act(() => result.current.mutate(1));
    await waitFor(() => expect(seen).toEqual([1]));
  });

  it('re-reads after every settle, and only after it', async () => {
    const { qc, wrapper } = setup();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');
    const revalidate = vi.fn((_v: number, _failed: boolean) => [['a'], ['b']]);
    const first = deferred<string>();
    const { result } = renderHook(
      () =>
        useOptimisticWrite({
          mutationKey: ['write', 't'],
          mutationFn: (v: number) => (v === 1 ? first.promise : boom()),
          revalidate,
        }),
      { wrapper },
    );
    act(() => result.current.mutate(1));
    await act(async () => {});
    expect(revalidate).not.toHaveBeenCalled();
    expect(invalidate).not.toHaveBeenCalled();

    await act(async () => first.resolve('ok'));
    await waitFor(() => expect(revalidate).toHaveBeenCalledWith(1, false));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['a'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['b'] });

    invalidate.mockClear();
    act(() => result.current.mutate(2));
    await waitFor(() => expect(revalidate).toHaveBeenCalledWith(2, true));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['a'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['b'] });
  });

  it('lets a site own the message, never the rollback', async () => {
    const { qc, wrapper } = setup();
    const onError = vi.fn((_e: Error, _v: number) => {});
    const { result } = renderHook(
      () =>
        useOptimisticWrite({
          mutationKey: ['write', 't'],
          mutationFn: (_: number) => boom(),
          patch: toTwo,
          onError,
        }),
      { wrapper },
    );
    act(() => result.current.mutate(5));
    await waitFor(() => expect(onError).toHaveBeenCalledTimes(1));
    // (error, vars) and nothing else: no context, so no rollback can live there.
    expect(onError.mock.calls[0]).toHaveLength(2);
    expect(onError.mock.calls[0][1]).toBe(5);
    expect(qc.getMutationCache().getAll()[0].options.onError).toHaveLength(2);
    await waitFor(() => expect(qc.getQueryData(['n'])).toBe(1));
    expect(toast.pushToast).not.toHaveBeenCalled();
  });

  it('shows a function-form paint in flight, keeps it on success, undoes it on failure', async () => {
    const { wrapper } = setup();
    const writes = new Map<number, ReturnType<typeof deferred<string>>>();
    const { result } = renderHook(
      () => {
        const [painted, setPainted] = useState<number[]>([]);
        const w = useOptimisticWrite({
          mutationKey: ['write', 't'],
          mutationFn: (v: number) => {
            const d = deferred<string>();
            writes.set(v, d);
            return d.promise;
          },
          // React state the query cache cannot see: paint now, hand back the undo.
          patch: (v) => {
            setPainted((p) => [...p, v]);
            return () => setPainted((p) => p.filter((x) => x !== v));
          },
        });
        return { painted, w };
      },
      { wrapper },
    );
    act(() => result.current.w.mutate(1));
    expect(result.current.painted).toEqual([1]);
    await waitFor(() => expect(writes.has(1)).toBe(true));
    await act(async () => writes.get(1)!.resolve('ok'));
    await waitFor(() => expect(result.current.w.isSuccess).toBe(true));
    expect(result.current.painted).toEqual([1]);

    act(() => result.current.w.mutate(2));
    expect(result.current.painted).toEqual([1, 2]);
    await waitFor(() => expect(writes.has(2)).toBe(true));
    await act(async () => writes.get(2)!.reject(new Error('boom')));
    await waitFor(() => expect(result.current.w.isError).toBe(true));
    expect(result.current.painted).toEqual([1]);
    expect(toast.pushToast).toHaveBeenCalledTimes(1);
  });

  it('answers pendingFor per key, across instances, live', async () => {
    const { wrapper } = setup();
    const writes = new Map<number, ReturnType<typeof deferred<string>>>();
    const write = {
      mutationKey: ['write', 't', 'pending'],
      mutationFn: (v: number) => {
        const d = deferred<string>();
        writes.set(v, d);
        return d.promise;
      },
      pendingKey: (v: number) => v,
    };
    const { result } = renderHook(
      () => ({ a: useOptimisticWrite(write), b: useOptimisticWrite(write) }),
      { wrapper },
    );
    act(() => result.current.a.mutate(7));
    expect(result.current.b.pendingFor(7)).toBe(true);
    expect(result.current.b.pendingFor(8)).toBe(false);
    act(() => result.current.a.mutate(8));
    expect(result.current.b.pendingFor(7)).toBe(true);
    expect(result.current.b.pendingFor(8)).toBe(true);

    await waitFor(() => expect(writes.size).toBe(2));
    await act(async () => writes.get(7)!.resolve('ok'));
    await waitFor(() => expect(result.current.b.pendingFor(7)).toBe(false));
    expect(result.current.b.pendingFor(8)).toBe(true);
    await act(async () => writes.get(8)!.resolve('ok'));
    await waitFor(() => expect(result.current.a.pendingFor(8)).toBe(false));
  });

  it('restores the keys it held at mutate time, not the ones on screen at failure', async () => {
    const { qc, wrapper } = setup();
    qc.setQueryData(['rows', 1], 'a');
    qc.setQueryData(['rows', 2], 'b');
    const write = deferred<string>();
    const { result } = renderHook(
      () =>
        useOptimisticWrite({
          mutationKey: ['write', 't'],
          mutationFn: (_: { page: number }) => write.promise,
          patch: (v) => [cachePatch<string>({ queryKey: ['rows', v.page], exact: true }, () => 'patched')],
        }),
      { wrapper },
    );
    act(() => result.current.mutate({ page: 1 }));
    await waitFor(() => expect(qc.getQueryData(['rows', 1])).toBe('patched'));
    act(() => {
      qc.setQueryData(['rows', 2], 'b2');
    });
    await act(async () => write.reject(new Error('x')));
    await waitFor(() => expect(qc.getQueryData(['rows', 1])).toBe('a'));
    expect(qc.getQueryData(['rows', 2])).toBe('b2');
  });

  it('writes no cache without a patch, and a failure still says why once', async () => {
    const { qc, wrapper } = setup();
    const snapshot = () => qc.getQueryCache().getAll().map((q) => [q.queryKey, q.state.data]);
    const before = snapshot();
    const { result } = renderHook(
      () => useOptimisticWrite({ mutationKey: ['write', 't'], mutationFn: (_: number) => boom() }),
      { wrapper },
    );
    act(() => result.current.mutate(1));
    await waitFor(() => expect(toast.pushToast).toHaveBeenCalledTimes(1));
    expect(snapshot()).toEqual(before);
  });
  /* Browse mounts five of these per card. A parent re-render makes every
   * useMutation tell the MutationCache its options changed; nothing may answer
   * that by scanning the cache, or one hover costs rows × rows × retained writes. */
  describe('cost at Browse scale', () => {
    let inFlight: Promise<string> | null = null;
    const keyed: OptimisticWrite<number, string> = {
      mutationKey: ['write', 't', 'row'],
      mutationFn: () => inFlight ?? Promise.resolve('ok'),
      pendingKey: (v) => v,
    };
    const unkeyed: OptimisticWrite<number, string> = {
      mutationKey: ['write', 't', 'plain'],
      mutationFn: () => Promise.resolve('ok'),
    };

    function Row({
      id,
      renders,
      handles,
    }: {
      id: number;
      renders: Map<number, number>;
      handles: Map<number, OptimisticWriteResult<number, string>>;
    }) {
      const a = useOptimisticWrite(keyed);
      const b = useOptimisticWrite({ ...keyed, mutationKey: ['write', 't', 'row-b'] });
      const c = useOptimisticWrite({ ...keyed, mutationKey: ['write', 't', 'row-c'] });
      useOptimisticWrite(unkeyed);
      useOptimisticWrite({ ...unkeyed, mutationKey: ['write', 't', 'plain-b'] });
      handles.set(id, a);
      renders.set(id, (renders.get(id) ?? 0) + 1);
      const busy = a.pendingFor(id) || b.pendingFor(id) || c.pendingFor(id);
      return <span data-testid={`row-${id}`}>{busy ? 'busy' : 'idle'}</span>;
    }

    function mountRows(qc: QueryClient, n: number) {
      const renders = new Map<number, number>();
      const handles = new Map<number, OptimisticWriteResult<number, string>>();
      const ids = Array.from({ length: n }, (_, i) => i);
      const tree = (tick: number) => (
        <QueryClientProvider client={qc}>
          <div data-tick={tick}>
            {ids.map((id) => (
              <Row key={id} id={id} renders={renders} handles={handles} />
            ))}
          </div>
        </QueryClientProvider>
      );
      const view = render(tree(0));
      return { renders, handles, rerender: (tick: number) => view.rerender(tree(tick)) };
    }

    it('a parent re-render scans no mutations, however many keyed writes are retained', async () => {
      const { qc, wrapper } = setup();
      const { result } = renderHook(() => useOptimisticWrite(keyed), { wrapper });
      for (let i = 0; i < 20; i += 1) {
        await act(async () => {
          await result.current.mutateAsync(1000 + i);
        });
      }
      expect(qc.getMutationCache().getAll().length).toBeGreaterThanOrEqual(20);

      const rows = mountRows(qc, 50);
      const findAll = vi.spyOn(qc.getMutationCache(), 'findAll');
      const find = vi.spyOn(qc.getMutationCache(), 'find');
      act(() => rows.rerender(1));
      expect(findAll).not.toHaveBeenCalled();
      expect(find).not.toHaveBeenCalled();
      // One render per row for one parent render: nothing cascades.
      expect([...rows.renders.values()].every((n) => n === 2)).toBe(true);
    });

    it('a write re-renders only the row that wrote and the row that asked', async () => {
      const { qc } = setup();
      const rows = mountRows(qc, 30);
      const before = new Map(rows.renders);
      const woke = () =>
        [...rows.renders].filter(([id, n]) => n !== before.get(id)).map(([id]) => id);
      const write = deferred<string>();
      inFlight = write.promise;
      // Row 3's hook writes for property 7 — as a stage menu writes for its funnel.
      act(() => rows.handles.get(3)!.mutate(7));
      inFlight = null;
      expect(screen.getByTestId('row-7')).toHaveTextContent('busy');
      expect(screen.getByTestId('row-3')).toHaveTextContent('idle');
      expect(woke()).toContain(7);

      await act(async () => write.resolve('ok'));
      await waitFor(() => expect(screen.getByTestId('row-7')).toHaveTextContent('idle'));
      // The writer re-renders off its own mutation, the asker off the index —
      // and no other row at all.
      expect(woke().filter((id) => id !== 3 && id !== 7)).toEqual([]);
    });
  });
});
