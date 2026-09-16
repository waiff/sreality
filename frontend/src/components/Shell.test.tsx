/* Shell — error containment.
 *
 * The boundary used to sit at App level, ABOVE Shell, so any page crash also
 * removed the nav, the footer and the toast surface — while the fallback told
 * the user to "use the back button", which needs the nav to exist. The boundary
 * now wraps the route body only; App keeps a keyed last-resort net for what
 * renders outside it (TopBar, Footer, ToastViewport, the Explore-area modal).
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import Shell from './Shell';
import * as auth from '@/lib/auth';
import * as api from '@/lib/api';
import * as pinAudit from '@/lib/pinAudit';

vi.mock('@/lib/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/auth')>()),
  useAuth: vi.fn(),
}));
vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  getNotificationUnreadCount: vi.fn(),
}));
vi.mock('@/lib/pinAudit', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/pinAudit')>()),
  fetchPinAuditTotal: vi.fn(),
}));
vi.mock('@/lib/supabase', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/supabase')>()),
  isSupabaseConfigured: () => true,
}));

function Boom(): never {
  throw new Error('page exploded');
}

beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
  vi.mocked(api.getNotificationUnreadCount).mockResolvedValue(0);
  vi.mocked(pinAudit.fetchPinAuditTotal).mockResolvedValue(0);
  vi.mocked(auth.useAuth).mockReturnValue({
    isAdmin: true,
    session: null,
    user: null,
    loading: false,
    agendas: [],
  } as unknown as ReturnType<typeof auth.useAuth>);
});

afterEach(() => {
  vi.restoreAllMocks();
});

function renderShellWith(element: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/browse']}>
        <Routes>
          <Route element={<Shell />}>
            <Route path="/browse" element={element} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('<Shell> error containment', () => {
  it('keeps the nav when the route body crashes', () => {
    renderShellWith(<Boom />);
    expect(screen.getByText('This page hit an error')).toBeInTheDocument();
    /* The nav is what the fallback's advice depends on. */
    expect(screen.getByRole('navigation')).toBeInTheDocument();
  });

  it('renders the route body normally when nothing throws', () => {
    renderShellWith(<p>page content</p>);
    expect(screen.getByText('page content')).toBeInTheDocument();
    expect(screen.queryByText('This page hit an error')).toBeNull();
  });
});

/* The unread badge sits INSIDE the Notifications link, so an aria-label on it
 * replaces the link's own name. The nav item has to stay findable by the word
 * the operator reads. */
describe('<Shell> notifications badge', () => {
  it('keeps the nav item named "Notifications" and adds the count to it', async () => {
    vi.mocked(api.getNotificationUnreadCount).mockResolvedValue({
      unread_count: 5,
    } as unknown as Awaited<ReturnType<typeof api.getNotificationUnreadCount>>);
    renderShellWith(<p>page content</p>);
    const link = await screen.findByRole('link', { name: /^Notifications/ });
    await waitFor(() =>
      expect(link).toHaveAccessibleName('Notifications 5 unread notifications'),
    );
  });
});

/* The hidden set leads the nav, and the count of rows in it rides on the entry
 * — but navigation can never depend on that number arriving. The number is the
 * ISSUE count (W7-b): `fetchPinAuditTotal` asks for `state=unresolved` only,
 * which `lib/pinAudit.test.ts` pins on the read itself. */
describe('<Shell> !AUDIT POLOH', () => {
  it('is the first nav entry and carries the audit count', async () => {
    vi.mocked(pinAudit.fetchPinAuditTotal).mockResolvedValue(37052);
    renderShellWith(<p>page content</p>);
    const link = await screen.findByRole('link', { name: /^!AUDIT POLOH/ });
    expect(link).toHaveAttribute('href', '/new-dedup/pin-audit');
    await waitFor(() =>
      expect(link).toHaveAccessibleName(
        '!AUDIT POLOH 37052 inzerátů s nevyřešenou polohou',
      ),
    );
    const nav = screen.getByRole('navigation');
    expect(nav.querySelectorAll('a')[0]).toBe(link);
  });

  it('still navigates when the count read fails', async () => {
    vi.mocked(pinAudit.fetchPinAuditTotal).mockRejectedValue(new Error('nope'));
    renderShellWith(<p>page content</p>);
    const link = await screen.findByRole('link', { name: '!AUDIT POLOH' });
    await waitFor(() => expect(link).toHaveAccessibleName('!AUDIT POLOH'));
    expect(link).toHaveAttribute('href', '/new-dedup/pin-audit');
  });
});

/* THE BAR OVERFLOWED ITS OWN ROW. At 1440px the header measured 1555px and the
 * last entries — Settings, and the account button after it — were clipped off
 * the right edge. The height is load-bearing (three Browse panes pin themselves
 * to 3.5rem), so the fix is to carry less: one "Admin" trigger with a section
 * per program, and the admin-only links inside it.
 *
 * jsdom measures no widths, so what is pinned here is the STRUCTURE the fix
 * rests on — one admin trigger, not four, and nothing lost on the way in.
 */
describe('<Shell> admin menu', () => {
  it('carries the admin surfaces under one trigger instead of four', async () => {
    const user = userEvent.setup();
    renderShellWith(<p>page content</p>);
    const nav = screen.getByRole('navigation');
    for (const gone of ['NEW DEDUP', 'AUTODEDUP', 'Settings']) {
      expect(within(nav).queryByRole('button', { name: new RegExp(`^${gone}`) })).toBeNull();
    }
    const trigger = within(nav).getByRole('button', { name: /^Admin/ });
    await user.click(trigger);
    /* Every section that used to be its own trigger, still reachable — and the
     * three admin-only links that used to spend a slot in the row. */
    for (const label of ['Broker Review', 'Datasets', 'Groups', 'Residual', 'Health']) {
      expect(within(nav).getByRole('menuitem', { name: label })).toBeInTheDocument();
    }
    /* Paused, not removed: a surface switched off is a different fact from one
     * that was deleted, so it stays visible and stays inert. */
    expect(within(nav).getByText('Outreach')).toHaveAttribute('aria-disabled', 'true');
  });

  it('lets the menu own its items through a labelled group, not a bare div', async () => {
    const user = userEvent.setup();
    renderShellWith(<p>page content</p>);
    const nav = screen.getByRole('navigation');
    await user.click(within(nav).getByRole('button', { name: /^Admin/ }));
    const menu = within(nav).getByRole('menu');
    /* A menu owns its menuitems directly or through a group. Sections went in to
     * keep four programs legible under one trigger; a plain wrapper between the
     * two would have bought that legibility with the menu's own structure. */
    const sections = within(menu).getAllByRole('group');
    expect(sections.length).toBeGreaterThan(1);
    expect(within(sections[0]).getAllByRole('menuitem').length).toBeGreaterThan(0);
    /* Each section is NAMED — and its visible heading is not read a second time. */
    for (const section of sections) {
      expect(section).toHaveAttribute('aria-label');
    }
  });

  it('keeps the daily surfaces in the row itself', () => {
    renderShellWith(<p>page content</p>);
    const nav = screen.getByRole('navigation');
    for (const label of ['Browse', 'Pipeline', 'Estimations', 'Collections']) {
      expect(within(nav).getByRole('link', { name: label })).toBeInTheDocument();
    }
  });
});
