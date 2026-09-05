/* Notifications — the feed row's accessible NAME must be the row's own text.
 *
 * The unread dot used to carry aria-label="unread", which is injected as the
 * first token of the enclosing <Link> / <button> name — so a row renamed itself
 * the moment it was marked seen. The dot is now decorative and the state rides
 * on the control's DESCRIPTION, which is what these tests pin.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
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
  sreality_id: 12345,
  property_id: 999,
  change_kind: 'new',
  message: null,
  dispatched_at: '2026-05-12T10:00:00+00:00',
  seen_at: null,
  trigger_price_czk: null,
  prev_price_czk: null,
  trigger_snapshot_id: null,
  target_channels: [],
  estimation_run_id: null,
  estimation_status: null,
  estimation_kind: null,
  estimated_monthly_rent_czk: null,
  estimated_sale_price_czk: null,
  gross_yield_pct: null,
  confidence: null,
  category_main: 'byty',
  category_type: 'prodej',
  price_czk: 8_900_000,
  price_unit: null,
  area_m2: 55,
  disposition: '2+kk',
  subtype: null,
  locality: 'Praha 5',
  district: 'Praha 5',
  is_active: true,
  first_seen_at: null,
  last_seen_at: null,
  mf_gross_yield_pct: null,
  source: 'sreality',
  source_url: null,
};

const feed: WatchdogDispatchesResponse = {
  data: [
    baseDispatch,
    {
      ...baseDispatch,
      id: 'd2',
      sreality_id: 22222,
      locality: 'Brno-střed',
      district: 'Brno-město',
      seen_at: '2026-05-12T11:00:00+00:00',
    },
    {
      ...baseDispatch,
      id: 'd3',
      source_kind: 'system_health',
      change_kind: 'system_alert',
      message: 'Ingest stalled',
      subscription_id: null,
      subscription_name: null,
    },
  ],
  total: 3,
  limit: 100,
  offset: 0,
  next_cursor: null,
};

const counts: NotificationUnreadCount = {
  watchdog: 2,
  collection_monitor: 0,
  system_health: 0,
  total: 2,
  unread_count: 2,
};

// The cache is seeded below, so nothing here fires during the assertions; the
// mocks just guard against an accidental network call. Factory is hoisted —
// keep it free of outer-scope references.
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  listNotifications: vi.fn(),
  getNotificationUnreadCount: vi.fn(),
  markWatchdogDispatchSeen: vi.fn(),
}));

import Notifications from './Notifications';

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(notificationKeys.feed({ source: 'all', seen: 'all' }), feed);
  qc.setQueryData(notificationKeys.unreadCount, counts);
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Notifications />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

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

  it('names the system-alert row by its message, unread state in the description', () => {
    renderPage();
    const alert = screen.getByRole('button', { name: /^System Ingest stalled/ });
    expect(alert).toHaveAccessibleDescription('unread');
  });
});
