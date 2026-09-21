/* DismissButton + useDismissal — the one dismiss control (migration 536).
 *
 * The behaviours this file exists for: one click hides, the property leaves
 * every cached list that hides dismissed properties at once (and is restored
 * if the write fails), a revealed list keeps it, the undo offer works, and the
 * control is absent while the property is a LIVE deal — but present for a deal
 * closed into a terminal stage, which is exactly what dismissing is for.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider, type InfiniteData } from '@tanstack/react-query';

import DismissButton from './DismissButton';
import { DEFAULT_FILTERS } from '@/lib/filters';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';
import * as toast from '@/lib/toast';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, dismissProperty: vi.fn(), undismissProperty: vi.fn() };
});

vi.mock('@/lib/toast', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/toast')>();
  return { ...actual, pushToast: vi.fn(() => 7), dismissToast: vi.fn() };
});

vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  return { ...actual, fetchPipelineMembers: vi.fn(), fetchIsDismissed: vi.fn() };
});

type Page = { rows: { property_id: number }[]; nextCursor: null };
const page = (...ids: number[]): InfiniteData<Page> => ({
  pages: [{ rows: ids.map((property_id) => ({ property_id })), nextCursor: null }],
  pageParams: [null],
});
const SORT = { field: 'first_seen_at', direction: 'desc' };
const HIDING = ['cards', DEFAULT_FILTERS, SORT];
const REVEALED = ['cards', { ...DEFAULT_FILTERS, showDismissed: true }, SORT];
const TABLE = ['table', DEFAULT_FILTERS, SORT];

function setup(variant?: 'overlay' | 'inline' | 'header') {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  qc.setQueryData(HIDING, page(42, 43));
  qc.setQueryData(REVEALED, page(42, 43));
  qc.setQueryData(TABLE, page(42, 43));
  const view = render(
    <QueryClientProvider client={qc}>
      <DismissButton property_id={42} variant={variant} />
    </QueryClientProvider>,
  );
  const ids = (key: unknown[]) =>
    qc.getQueryData<InfiniteData<Page>>(key)!.pages.flatMap((p) => p.rows.map((r) => r.property_id));
  return { qc, ids, view };
}

/* The control is inert until its state has loaded — click only once it is live. */
async function live(name: string | RegExp): Promise<HTMLButtonElement> {
  const btn = (await screen.findByRole('button', { name })) as HTMLButtonElement;
  await waitFor(() => expect(btn.disabled).toBe(false));
  return btn;
}

describe('<DismissButton>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(queries.fetchPipelineMembers).mockResolvedValue(new Map());
    vi.mocked(queries.fetchIsDismissed).mockResolvedValue(false);
    vi.mocked(api.dismissProperty).mockResolvedValue({ property_id: 42, added: true });
    vi.mocked(api.undismissProperty).mockResolvedValue({ property_id: 42, removed: true });
  });

  it('hides in one click and drops the row from every hiding list, not the revealed one', async () => {
    const { ids } = setup();
    fireEvent.click(await live('Skrýt nemovitost'));
    await waitFor(() => expect(api.dismissProperty).toHaveBeenCalledWith(42));
    expect(ids(HIDING)).toEqual([43]);
    expect(ids(TABLE)).toEqual([43]);
    expect(ids(REVEALED)).toEqual([42, 43]);
  });

  it('puts the row back when the write fails', async () => {
    vi.mocked(api.dismissProperty).mockRejectedValue(new Error('boom'));
    const { ids } = setup();
    fireEvent.click(await live('Skrýt nemovitost'));
    await waitFor(() => expect(api.dismissProperty).toHaveBeenCalledWith(42));
    await waitFor(() => expect(ids(HIDING)).toEqual([42, 43]));
    expect(toast.pushToast).not.toHaveBeenCalled();
  });

  it('offers an undo that still works after the dismissed card has gone', async () => {
    const { view } = setup();
    fireEvent.click(await live('Skrýt nemovitost'));
    await waitFor(() => expect(toast.pushToast).toHaveBeenCalledTimes(1));
    const [kind, , ttl, action] = vi.mocked(toast.pushToast).mock.calls[0];
    expect([kind, ttl, action?.label]).toEqual(['info', 0, 'Vrátit']);
    // The card leaves the list — its button unmounts before the operator clicks.
    view.unmount();
    await act(async () => action!.onClick());
    await waitFor(() => expect(api.undismissProperty).toHaveBeenCalledWith(42));
    expect(toast.dismissToast).toHaveBeenCalledWith(7);
  });

  it('keeps one undo offer on screen through a run of dismissals', async () => {
    setup();
    fireEvent.click(await live('Skrýt nemovitost'));
    await waitFor(() => expect(toast.pushToast).toHaveBeenCalledTimes(1));
    // The state re-reads as not dismissed (the mock), so the same button offers again.
    fireEvent.click(await live('Skrýt nemovitost'));
    await waitFor(() => expect(toast.pushToast).toHaveBeenCalledTimes(2));
    expect(toast.dismissToast).toHaveBeenCalledWith(7);
  });

  it('restores a dismissed property in one click', async () => {
    vi.mocked(queries.fetchIsDismissed).mockResolvedValue(true);
    setup('header');
    const btn = await live(/Skryto/);
    expect(btn.getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(btn);
    await waitFor(() => expect(api.undismissProperty).toHaveBeenCalledWith(42));
    expect(api.dismissProperty).not.toHaveBeenCalled();
  });

  it('is absent while the property is a live deal', async () => {
    vi.mocked(queries.fetchPipelineMembers).mockResolvedValue(
      new Map([[42, { property_id: 42, is_terminal: false }]]) as never,
    );
    setup();
    await waitFor(() => expect(queries.fetchPipelineMembers).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Skrýt nemovitost' })).toBeNull(),
    );
  });

  it('stays for a deal closed into a terminal stage, and dismisses it', async () => {
    // "Passed" is "reviewed it, didn't like it": the closed card must not hide
    // the control, or the operator can never get a rejected deal out of Browse.
    vi.mocked(queries.fetchPipelineMembers).mockResolvedValue(
      new Map([[42, { property_id: 42, is_terminal: true }]]) as never,
    );
    setup();
    await waitFor(() => expect(queries.fetchPipelineMembers).toHaveBeenCalled());
    const button = await live('Skrýt nemovitost');
    fireEvent.click(button);
    await waitFor(() => expect(api.dismissProperty).toHaveBeenCalledWith(42));
  });

  it('does nothing until it knows the state', async () => {
    vi.mocked(queries.fetchIsDismissed).mockReturnValue(new Promise(() => {}));
    setup();
    const btn = await screen.findByRole('button', { name: 'Skrýt nemovitost' });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
  });
});
