/* Shell — error containment.
 *
 * The boundary used to sit at App level, ABOVE Shell, so any page crash also
 * removed the nav, the footer and the toast surface — while the fallback told
 * the user to "use the back button", which needs the nav to exist. The boundary
 * now wraps the route body only; App keeps a keyed last-resort net for what
 * renders outside it (TopBar, Footer, ToastViewport, the Explore-area modal).
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
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

/* The pin-loss audit was buried in the NEW DEDUP dropdown while it is the one
 * decision waiting on the operator. It now leads the nav, and the count of
 * rows in the audit set rides on it — but navigation can never depend on that
 * number arriving. */
describe('<Shell> !AUDIT POLOH', () => {
  it('is the first nav entry and carries the audit count', async () => {
    vi.mocked(pinAudit.fetchPinAuditTotal).mockResolvedValue(37052);
    renderShellWith(<p>page content</p>);
    const link = await screen.findByRole('link', { name: /^!AUDIT POLOH/ });
    expect(link).toHaveAttribute('href', '/new-dedup/pin-audit');
    await waitFor(() =>
      expect(link).toHaveAccessibleName('!AUDIT POLOH 37052 inzerátů k auditu'),
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
