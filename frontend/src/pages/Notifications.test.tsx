/* Notifications — the feed row's accessible NAME must be the row's own text.
 *
 * The unread dot used to carry aria-label="unread", which is injected as the
 * first token of the enclosing <Link> / <button> name — so a row renamed itself
 * the moment it was marked seen. The dot is now decorative and the state rides
 * on the control's DESCRIPTION, which is what these tests pin.
 *
 * Every row opens what lib/dispatchTarget names (the property by property_id,
 * a share by its kind), and older rows page in on "Load more" (keyset cursor).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import { notificationKeys } from '@/lib/queries';
import type {
  NotificationUnreadCount,
  WatchdogDispatch,
  WatchdogDispatchesResponse,
} from '@/lib/types';

const baseDispatch: WatchdogDispatch = {
  id: 'd1',
  source_kind: 'watchdog',
  subscription_id: 's1',
  subscription_name: 'Praha 2+kk',
  collection_id: null,
  collection_name: null,
  property_id: 999,
  change_kind: 'new',
  share_kind: null,
  share_ref: null,
  actor_email: null,
  share_name: null,
  share_mode: null,
  dispatched_at: '2026-05-12T10:00:00+00:00',
  seen_at: null,
  trigger_price_czk: null,
  prev_price_czk: null,
  category_main: 'byty',
  category_type: 'prodej',
  price_czk: 8_900_000,
  price_unit: null,
  area_m2: 55,
  disposition: '2+kk',
  subtype: null,
  display_label: 'Praha 5',
  is_active: true,
  first_seen_at: null,
  last_seen_at: null,
  mf_gross_yield_pct: null,
  source: 'sreality',
  source_url: null,
};

const share: WatchdogDispatch = {
  ...baseDispatch,
  id: 'd4',
  source_kind: 'share',
  change_kind: 'shared_collection',
  actor_email: 'owner@example.com',
  share_name: 'Vinohrady',
  subscription_id: null,
  subscription_name: null,
  collection_id: 42,
  collection_name: 'Vinohrady',
  share_kind: 'collection',
  share_ref: '42',
  property_id: null,
  display_label: null,
  price_czk: null,
};

const feed: WatchdogDispatchesResponse = {
  data: [
    baseDispatch,
    {
      ...baseDispatch,
      id: 'd2',
      // A bazos ad: no sreality id anywhere, and the row still opens its property.
      property_id: 4321,
      source: 'bazos',
      display_label: 'Brno-střed',
      seen_at: '2026-05-12T11:00:00+00:00',
    },
    share,
  ],
  total: 3,
  limit: 50,
  offset: 0,
  next_cursor: null,
};

const counts: NotificationUnreadCount = { unread_count: 2 };

// Factory is hoisted — keep it free of outer-scope references.
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  listNotifications: vi.fn(),
  getNotificationUnreadCount: vi.fn(),
  markWatchdogDispatchSeen: vi.fn(),
}));

import { getNotificationUnreadCount, listNotifications, markWatchdogDispatchSeen } from '@/lib/api';
import { expectNoNestedInteractive } from '@/test/a11y';
import Notifications from './Notifications';

/* The cache holds the first page as the infinite list stores it, and the API
 * answers the mount's refetch with that same page. */
function renderPage(page: WatchdogDispatchesResponse = feed) {
  vi.mocked(listNotifications).mockResolvedValue(page);
  vi.mocked(getNotificationUnreadCount).mockResolvedValue(counts);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(notificationKeys.feed({ source: 'all', seen: 'all' }), {
    pages: [{ rows: page.data, nextCursor: page.next_cursor ?? undefined }],
    pageParams: [null],
  });
  qc.setQueryData(notificationKeys.unreadCount, counts);
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Notifications />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(listNotifications).mockReset();
  vi.mocked(markWatchdogDispatchSeen).mockReset();
});

describe('<Notifications> row names', () => {
  it('names an unread listing row by its own text and describes it as unread', () => {
    renderPage();
    const row = screen.getByRole('link', { name: /^New match Praha 5/ });
    expect(row).toHaveAccessibleDescription('unread');
  });

  it('names a read listing row identically — the name never flips on mark-seen', () => {
    renderPage();
    const read = screen.getByRole('link', { name: /^New match Brno-střed/ });
    expect(read).toHaveAccessibleDescription('');
  });
});

describe('<Notifications> listing rows', () => {
  it('open the property page by property_id, whatever portal the ad is on', () => {
    renderPage();
    expect(screen.getByRole('link', { name: /^New match Praha 5/ })).toHaveAttribute('href', '/property/999');
    expect(screen.getByRole('link', { name: /^New match Brno-střed/ })).toHaveAttribute('href', '/property/4321');
  });

  it('render a row with nothing to open as a button that only marks it read', async () => {
    vi.mocked(markWatchdogDispatchSeen).mockResolvedValue({ ...baseDispatch, property_id: null });
    renderPage({ ...feed, data: [{ ...baseDispatch, property_id: null }], total: 1 });
    expect(screen.queryByRole('link', { name: /^New match Praha 5/ })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /^New match Praha 5/ }));
    await waitFor(() => expect(markWatchdogDispatchSeen).toHaveBeenCalledWith('d1'));
  });
});

describe('<Notifications> source tabs', () => {
  it('offers All, Monitoring, Watchdogs and Shared, and no System tab', () => {
    renderPage();
    for (const name of ['Monitoring', 'Watchdogs', 'Shared']) {
      expect(screen.getByRole('button', { name })).toBeInTheDocument();
    }
    expect(screen.queryByRole('button', { name: 'System' })).toBeNull();
  });

  it('asks for the share kind on the Shared tab', async () => {
    renderPage();
    vi.mocked(listNotifications).mockResolvedValue({ ...feed, data: [], total: 0 });
    fireEvent.click(screen.getByRole('button', { name: 'Shared' }));
    await waitFor(() =>
      expect(listNotifications).toHaveBeenLastCalledWith(
        expect.objectContaining({ source_kind: 'share' }),
      ));
  });
});

describe('<Notifications> share rows', () => {
  it('shows the sentence and links to the shared collection', () => {
    renderPage();
    const text = screen.getByRole('button', { name: /^Shared collection owner@example\.com shared the collection "Vinohrady"/ });
    expect(text).toHaveAccessibleDescription('unread');
    expect(screen.getByRole('link', { name: 'Open collection' })).toHaveAttribute('href', '/collection/42');
  });

  it('marks the share read when the collection is opened', async () => {
    vi.mocked(markWatchdogDispatchSeen).mockResolvedValue({ ...share, seen_at: '2026-05-12T12:00:00+00:00' });
    renderPage();
    fireEvent.click(screen.getByRole('link', { name: 'Open collection' }));
    await waitFor(() => expect(markWatchdogDispatchSeen).toHaveBeenCalledWith('d4'));
  });

  /* The same kind and the same renderer for a pipeline share (migration 641). */
  it('renders a pipeline share with the one renderer, linking to the pipeline', () => {
    const pipelineShare = {
      ...share,
      id: 'd5',
      share_name: null,
      share_mode: 'edit' as const,
      collection_id: null,
      collection_name: null,
      change_kind: 'shared_pipeline' as const,
      share_kind: 'pipeline' as const,
      share_ref: '0b0b0b0b-0000-4000-8000-000000000002',
    };
    renderPage({ ...feed, data: [pipelineShare], total: 1 });
    expect(
      screen.getByRole('button', { name: /^Shared pipeline owner@example\.com shared their pipeline with you \(edit\)/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open pipeline' })).toHaveAttribute('href', '/pipeline');
    expect(screen.queryByRole('link', { name: 'Open collection' })).toBeNull();
  });

  /* A dashboard share (migration 647) names its kind and the dashboard's uuid;
   * the link opens Browse › Stats, which loads the dashboard from the URL. */
  it('renders a dashboard share with the one renderer, linking to the Stats tab', async () => {
    vi.mocked(markWatchdogDispatchSeen).mockResolvedValue({ ...share, id: 'd6', seen_at: '2026-05-12T12:00:00+00:00' });
    const dashboardShare = {
      ...share,
      id: 'd6',
      share_name: 'Ceny Praha',
      collection_id: null,
      collection_name: null,
      change_kind: 'shared_dashboard' as const,
      share_kind: 'dashboard' as const,
      share_ref: '6f1c2d3e-0000-4000-8000-000000000001',
    };
    renderPage({ ...feed, data: [dashboardShare], total: 1 });
    expect(
      screen.getByRole('button', { name: /^Shared dashboard owner@example\.com shared the dashboard "Ceny Praha" with you/ }),
    ).toBeInTheDocument();
    const link = screen.getByRole('link', { name: 'Open dashboard' });
    expect(link).toHaveAttribute('href', '/browse?tab=stats&dashboard=6f1c2d3e-0000-4000-8000-000000000001');
    expect(screen.queryByRole('link', { name: 'Open pipeline' })).toBeNull();
    fireEvent.click(link);
    await waitFor(() => expect(markWatchdogDispatchSeen).toHaveBeenCalledWith('d6'));
  });

  it('renders a share it cannot open as text alone, with no link', () => {
    renderPage({ ...feed, data: [{ ...share, share_kind: null, share_ref: null }], total: 1 });
    expect(screen.getByRole('button', { name: /^Shared collection Something was shared with you\./ })).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
  });

  /* R11: no text is stored (migration 656), so once the share ends the API sends
   * neither the sharer's address nor the collection's name, and the row says so. */
  it('names neither the sharer nor the collection once the share has ended', () => {
    renderPage({ ...feed, data: [{ ...share, actor_email: null, share_name: null }], total: 1 });
    expect(screen.getByRole('button', { name: /^Shared collection Someone shared a collection with you/ })).toBeInTheDocument();
    expect(screen.queryByText(/owner@example\.com/)).toBeNull();
  });

  it('nests no control inside another', () => {
    const { container } = renderPage();
    expectNoNestedInteractive(container);
  });
});

describe('<Notifications> Load more', () => {
  const fullPage: WatchdogDispatchesResponse = {
    ...feed,
    data: Array.from({ length: 50 }, (_, i) => ({ ...baseDispatch, id: `p1-${i}`, display_label: `Řádek ${i}` })),
    total: 51,
    next_cursor: 'cursor-1',
  };
  const older: WatchdogDispatchesResponse = {
    ...feed,
    data: [{ ...baseDispatch, id: 'p2-0', display_label: 'Nejstarší' }],
    total: null,
    next_cursor: null,
  };

  it('asks for the page after next_cursor and appends it', async () => {
    renderPage(fullPage);
    vi.mocked(listNotifications).mockImplementation(async (params = {}) =>
      params.cursor === 'cursor-1' ? older : fullPage,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await waitFor(() =>
      expect(listNotifications).toHaveBeenCalledWith(expect.objectContaining({ cursor: 'cursor-1', limit: 50 })),
    );
    expect(await screen.findByRole('link', { name: /^New match Nejstarší/ })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /^New match Řádek 0/ })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull());
  });

  it('offers no Load more when the API names no next page', () => {
    renderPage();
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
  });
});

/* The count is Shell's one observer (lib/unreadCount); the page polls none of
 * its own, so outside Shell it reads zero and has nothing to mark. */
describe('<Notifications> unread count', () => {
  it('reads the shared observer and polls no count of its own', () => {
    vi.mocked(getNotificationUnreadCount).mockClear();
    renderPage();
    expect(getNotificationUnreadCount).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Mark all read' })).toBeDisabled();
  });
});
