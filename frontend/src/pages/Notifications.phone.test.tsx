/* Notifications at phone width (W5a): the SAME page inside the phone frame, no
 * phone fork. The frame's bell and the page read Shell's one unread observer
 * (one request), the page keeps every control, and its phone layout is the
 * `max-md:` pass. jsdom loads no CSS, so the layout is pinned on the classes
 * that carry it (as Health's phone pass is); the measured fit — no horizontal
 * scroll at 375 px and 320 px, prices never split — was checked in headless
 * Chromium, not here. */
import { render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import Shell from '@/components/Shell';
import * as api from '@/lib/api';
import * as auth from '@/lib/auth';
import { TitleController } from '@/lib/pageTitle';
import { PHONE_SHELL_QUERY } from '@/lib/phoneShell';
import type { WatchdogDispatch } from '@/lib/types';
import { expectNoNestedInteractive } from '@/test/a11y';

import Notifications from './Notifications';

vi.mock('@/lib/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/auth')>()),
  useAuth: vi.fn(),
}));
vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  listNotifications: vi.fn(),
  getNotificationUnreadCount: vi.fn(),
}));

const row: WatchdogDispatch = {
  id: 'd1',
  source_kind: 'collection_monitor',
  subscription_id: null,
  subscription_name: null,
  collection_id: 7,
  collection_name: 'Vinohrady investice',
  property_id: 999,
  change_kind: 'price_drop',
  actor_email: null,
  share_name: null,
  share_mode: null,
  share_kind: null,
  share_ref: null,
  dispatched_at: '2026-10-21T10:00:00+00:00',
  seen_at: null,
  trigger_price_czk: 11_900_000,
  prev_price_czk: 12_500_000,
  category_main: 'byty',
  category_type: 'prodej',
  price_czk: 11_900_000,
  price_unit: null,
  area_m2: 86,
  disposition: '3+kk',
  subtype: null,
  display_label: 'Praha 2 – Vinohrady, Korunní',
  is_active: true,
  first_seen_at: null,
  last_seen_at: null,
  mf_gross_yield_pct: null,
  source: 'sreality',
  source_url: null,
};

const share: WatchdogDispatch = {
  ...row,
  id: 'd2',
  source_kind: 'share',
  change_kind: 'shared_collection',
  share_kind: 'collection',
  share_ref: '42',
  collection_id: 42,
  collection_name: 'Vinohrady',
  property_id: null,
  display_label: null,
  price_czk: null,
  prev_price_czk: null,
  trigger_price_czk: null,
  actor_email: 'owner@example.com',
  share_name: 'Vinohrady',
  share_mode: null,
};

const originalMatchMedia = window.matchMedia;
function setNarrow(narrow: boolean) {
  window.matchMedia = ((query: string) => ({
    matches: query === PHONE_SHELL_QUERY ? narrow : false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

beforeEach(() => {
  window.scrollTo = vi.fn() as unknown as typeof window.scrollTo;
  vi.mocked(api.getNotificationUnreadCount).mockResolvedValue({ unread_count: 2 });
  vi.mocked(api.listNotifications).mockResolvedValue({
    data: [row, share],
    total: 2,
    limit: 50,
    offset: 0,
    next_cursor: null,
  });
  vi.mocked(auth.useAuth).mockReturnValue({
    isAdmin: false,
    session: null,
    user: { email: 'operator@example.cz' },
    loading: false,
    agendas: { notifications: true },
    signOut: vi.fn(),
  } as unknown as ReturnType<typeof auth.useAuth>);
  setNarrow(true);
});

afterEach(() => {
  window.matchMedia = originalMatchMedia;
  vi.clearAllMocks();
});

function renderPhone() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/notifications']}>
        <TitleController routes={[{ path: '/notifications', handle: { title: 'Notifications' } }]}>
          <Routes>
            <Route element={<Shell />}>
              <Route path="/notifications" element={<Notifications />} />
            </Route>
          </Routes>
        </TitleController>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('<Notifications> at phone width', () => {
  it('sits in the phone frame under the bell, which reads the same one count', async () => {
    renderPhone();
    /* The frame's header is the first banner; the page's own header follows. */
    const header = screen.getAllByRole('banner')[0];
    const bell = await within(header).findByRole('link', { name: 'Upozornění: 2 nepřečtená' });
    expect(bell).toHaveAttribute('aria-current', 'page');
    expect(within(header).getByRole('button', { name: 'Nové' })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Mark all read' })).toBeEnabled());
    expect(api.getNotificationUnreadCount).toHaveBeenCalledTimes(1);
    /* The bell carries the count; the header's title does not repeat it. */
    expect(within(header).getByRole('heading', { level: 1 })).toHaveTextContent(/^Notifications$/);
    expect(document.title).not.toContain('(2)');
  });

  it('keeps every control of the desktop page, tab names in English', async () => {
    const { container } = renderPhone();
    expect(await screen.findByRole('link', { name: /^Price drop Praha 2/ })).toHaveAttribute('href', '/property/999');
    for (const name of ['Monitoring', 'Watchdogs', 'Shared', 'Unread', 'Mark all read']) {
      expect(screen.getByRole('button', { name })).toBeInTheDocument();
    }
    expect(screen.getAllByRole('button', { name: 'All' })).toHaveLength(2);
    expect(screen.getByRole('link', { name: 'Open collection' })).toHaveAttribute('href', '/collection/42');
    expectNoNestedInteractive(container);
  });

  it('lays out to the phone gutters with hairlines to the edge', async () => {
    renderPhone();
    const rowLink = await screen.findByRole('link', { name: /^Price drop Praha 2/ });
    const list = rowLink.closest('ul')!;
    expect(list.className).toContain('max-md:-mx-4');
    expect(list.parentElement!.parentElement!.className).toContain('max-md:px-4');
    /* A row turns into two lines: chip and time, then what it is about. */
    expect(rowLink.className).toContain('max-md:flex-wrap');
    expect(rowLink.className).toContain('max-md:px-4');
    /* The frame's header names the page and its bell carries the count. */
    expect(screen.getByText('Notifications', { selector: 'p' }).className).toContain('max-md:hidden');
    for (const name of ['All', 'Unread']) {
      expect(screen.getAllByRole('button', { name })[0].className).toContain('max-md:h-9');
    }
  });

  it('wraps a price whole, never opening a line on its separator', async () => {
    renderPhone();
    const rowLink = await screen.findByRole('link', { name: /^Price drop Praha 2/ });
    const [price, move] = Array.from(rowLink.querySelectorAll('span.font-mono'));
    expect(price.textContent).toMatch(/^ · 11\s900\s000\sKč$/);
    expect(move.firstElementChild).toHaveClass('max-md:hidden');
    expect(price.className).toContain('whitespace-nowrap');
    expect(price.firstElementChild).toHaveClass('max-md:hidden');
    expect(price.firstElementChild!.textContent).toBe(' · ');
  });
});

describe('<Notifications> on the desktop', () => {
  it('keeps the unread count in the tab title', async () => {
    setNarrow(false);
    renderPhone();
    await waitFor(() => expect(document.title).toMatch(/Notifications \(2\)$/));
  });
});
