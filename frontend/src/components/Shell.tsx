import { NavLink, Outlet } from 'react-router-dom';
import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getNotificationUnreadCount } from '@/lib/api';
import { notificationKeys } from '@/lib/queries';
import { useAuth } from '@/lib/auth';
import { isSupabaseConfigured } from '@/lib/supabase';
import { NewEstimationProvider } from './NewEstimationModal';
import { ExploreAreaProvider } from './ExploreAreaModal';
import ToastViewport from './ToastViewport';
import AccountMenu from './AccountMenu';
import { APP_NAME } from '@/lib/brand';

type NavItem =
  | { kind: 'link'; to: string; label: string; disabled?: boolean; title?: string; admin?: boolean }
  | { kind: 'divider'; admin?: boolean }
  | { kind: 'section'; label: string; admin?: boolean };

// `admin: true` entries only render for admin sessions (the routes themselves
// are wrapped in <RequireAdmin>, so hiding here is UX, not the security gate).
const navItems: ReadonlyArray<NavItem> = [
  { kind: 'link', to: '/browse',      label: 'Browse' },
  { kind: 'link', to: '/pipeline',    label: 'Pipeline' },
  { kind: 'link', to: '/estimations', label: 'Estimations' },
  { kind: 'link', to: '/watchdog',    label: 'Watchdogs' },
  { kind: 'link', to: '/notifications', label: 'Notifications' },
  { kind: 'link', to: '/brokers',     label: 'Brokers' },
  { kind: 'link', to: '/datasets',    label: 'Datasets', admin: true },
  { kind: 'link', to: '/outreach',    label: 'Outreach', disabled: true, admin: true,
    title: 'Outreach is paused — not available yet.' },
  { kind: 'link', to: '/collections', label: 'Collections' },
  { kind: 'divider', admin: true },
  // Everything past this divider lives under Settings.
  { kind: 'section', label: 'Settings', admin: true },
  { kind: 'link', to: '/dedup',       label: 'Dedup', admin: true },
  { kind: 'link', to: '/health',      label: 'Health', admin: true },
  { kind: 'link', to: '/costs',       label: 'LLM Costs', admin: true },
  { kind: 'link', to: '/scrapers',    label: 'Scrapers', admin: true },
  { kind: 'link', to: '/settings',    label: 'General Settings', admin: true },
];

export default function Shell() {
  return (
    <NewEstimationProvider>
      <ExploreAreaProvider>
        <div className="min-h-dvh flex flex-col bg-[var(--color-paper)] text-[var(--color-ink)]">
          <TopBar />
          <main className="flex-1">
            <Outlet />
          </main>
          <Footer />
        </div>
        <ToastViewport />
      </ExploreAreaProvider>
    </NewEstimationProvider>
  );
}

function TopBar() {
  const { isAdmin } = useAuth();
  const unreadQ = useQuery({
    queryKey: notificationKeys.unreadCount,
    queryFn: () => getNotificationUnreadCount(),
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
  const unread = unreadQ.data?.unread_count ?? 0;
  // Unconfigured local dev has no session (so no is_admin claim) — show the
  // full nav there, mirroring the guards' allow-through posture.
  const showAdmin = isAdmin || !isSupabaseConfigured();
  const items = navItems.filter((item) => showAdmin || !item.admin);
  return (
    <header className="border-b border-[var(--color-rule)] bg-[var(--color-paper)] sticky top-0 z-30">
      <div className="px-6 h-14 flex items-center gap-8">
        <BrandMark />
        <nav className="flex items-center gap-1">
          {items.map((item, i) => {
            if (item.kind === 'divider') {
              return (
                <span
                  key={`divider-${i}`}
                  className="mx-2 h-4 w-px bg-[var(--color-rule)]"
                  aria-hidden
                />
              );
            }
            if (item.kind === 'section') {
              return (
                <span
                  key={`section-${i}`}
                  className="mr-1 pl-1 text-[0.6rem] tracking-[0.18em] uppercase text-[var(--color-ink-4)] select-none"
                >
                  {item.label}
                </span>
              );
            }
            if (item.disabled) {
              return (
                <span
                  key={item.to}
                  title={item.title}
                  aria-disabled="true"
                  className="relative px-3 py-1.5 text-sm tracking-wide text-[var(--color-ink-4)] opacity-50 cursor-not-allowed select-none"
                >
                  {item.label}
                </span>
              );
            }
            return (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  [
                    'relative px-3 py-1.5 text-sm tracking-wide rounded-[var(--radius-xs)] transition-colors',
                    isActive
                      ? 'text-[var(--color-ink)]'
                      : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
                  ].join(' ')
                }
              >
                {({ isActive }) => (
                  <NavLabel active={isActive}>
                    {item.label}
                    {item.to === '/notifications' && unread > 0 && (
                      <span
                        className="ml-1.5 inline-flex items-center justify-center min-w-[1.05rem] h-[1.05rem] px-1 rounded-full bg-[var(--color-brick)] text-white text-[0.6rem] font-medium tabular-nums"
                        aria-label={`${unread} unread notifications`}
                      >
                        {unread > 99 ? '99+' : unread}
                      </span>
                    )}
                  </NavLabel>
                )}
              </NavLink>
            );
          })}
        </nav>
        <div className="ml-auto">
          <AccountMenu />
        </div>
      </div>
    </header>
  );
}

function NavLabel({ active, children }: { active: boolean; children: ReactNode }) {
  return (
    <span className="relative inline-flex items-center">
      {children}
      <span
        className="absolute -bottom-[15px] left-0 right-0 h-px transition-colors"
        style={{
          background: active ? 'var(--color-copper)' : 'transparent',
        }}
      />
    </span>
  );
}

function BrandMark() {
  // Two-part wordmark derived from the shared brand name: first word as the
  // display wordmark, the rest as the spaced uppercase descriptor.
  const [wordmark, ...rest] = APP_NAME.split(' ');
  const descriptor = rest.join(' ');
  return (
    <div className="flex items-baseline gap-2 select-none" title={APP_NAME}>
      <span
        className="font-display text-[1.05rem] leading-none"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        {wordmark}
      </span>
      {descriptor && (
        <span className="text-[0.65rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          {descriptor}
        </span>
      )}
    </div>
  );
}

function Footer() {
  return (
    <footer className="border-t border-[var(--color-rule-soft)] px-6 py-3 text-[0.7rem] text-[var(--color-ink-4)] tracking-wide flex items-center justify-between">
      <span>U1a · read-only · scrape data via Supabase</span>
      <span>
        map tiles ©{' '}
        <a
          href="https://openfreemap.org"
          target="_blank"
          rel="noopener noreferrer"
          className="hover:text-[var(--color-ink-3)] underline-offset-2 hover:underline"
        >
          OpenFreeMap
        </a>{' '}
        · ©{' '}
        <a
          href="https://www.openstreetmap.org/copyright"
          target="_blank"
          rel="noopener noreferrer"
          className="hover:text-[var(--color-ink-3)] underline-offset-2 hover:underline"
        >
          OpenStreetMap
        </a>{' '}
        contributors
      </span>
    </footer>
  );
}
