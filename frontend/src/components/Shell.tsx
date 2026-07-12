import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { useEffect, useRef, useState, type ReactNode } from 'react';
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

type NavItem = { to: string; label: string; disabled?: boolean; title?: string; admin?: boolean };

// `admin: true` entries only render for admin sessions (the routes themselves
// are wrapped in <RequireAdmin>, so hiding here is UX, not the security gate).
const navItems: ReadonlyArray<NavItem> = [
  { to: '/browse',      label: 'Browse' },
  { to: '/pipeline',    label: 'Pipeline' },
  { to: '/estimations', label: 'Estimations' },
  { to: '/watchdog',    label: 'Watchdogs' },
  { to: '/notifications', label: 'Notifications' },
  { to: '/brokers',     label: 'Brokers' },
  { to: '/datasets',    label: 'Datasets', admin: true },
  { to: '/outreach',    label: 'Outreach', disabled: true, admin: true,
    title: 'Outreach is paused — not available yet.' },
  { to: '/collections', label: 'Collections' },
];

// Grouped under the "Settings" dropdown trigger — all admin-only, so the
// whole group renders (or not) alongside the other admin-gated nav items.
const settingsItems: ReadonlyArray<{ to: string; label: string }> = [
  { to: '/dedup',    label: 'Dedup' },
  { to: '/health',   label: 'Health' },
  { to: '/costs',    label: 'LLM Costs' },
  { to: '/scrapers', label: 'Scrapers' },
  { to: '/settings', label: 'General Settings' },
];

function isPathActive(pathname: string, to: string): boolean {
  return pathname === to || pathname.startsWith(`${to}/`);
}

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
  const location = useLocation();
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
  const settingsActive = settingsItems.some((s) => isPathActive(location.pathname, s.to));
  return (
    <header className="border-b border-[var(--color-rule)] bg-[var(--color-paper)] sticky top-0 z-30">
      <div className="px-6 h-14 flex items-center gap-8">
        <BrandMark />
        <nav className="flex items-center gap-1">
          {items.map((item) => {
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
          {showAdmin && (
            <>
              <span className="mx-2 h-4 w-px bg-[var(--color-rule)]" aria-hidden />
              <SettingsMenu items={settingsItems} active={settingsActive} />
            </>
          )}
        </nav>
        <div className="ml-auto">
          <AccountMenu />
        </div>
      </div>
    </header>
  );
}

function SettingsMenu({
  items,
  active,
}: {
  items: ReadonlyArray<{ to: string; label: string }>;
  active: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        className={[
          'relative px-3 py-1.5 text-sm tracking-wide rounded-[var(--radius-xs)] transition-colors',
          active ? 'text-[var(--color-ink)]' : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
        ].join(' ')}
      >
        <NavLabel active={active}>
          Settings
          <CaretIcon spin={open} />
        </NavLabel>
      </button>
      {open ? (
        <div
          role="menu"
          className="absolute right-0 top-[calc(100%+4px)] z-30 min-w-[11rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] py-1 shadow-lg"
        >
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              role="menuitem"
              onClick={() => setOpen(false)}
              className={({ isActive }) =>
                [
                  'block px-3 py-1.5 text-[0.8rem]',
                  isActive
                    ? 'text-[var(--color-ink)] bg-[var(--color-paper-2)]'
                    : 'text-[var(--color-ink-2)] hover:bg-[var(--color-paper-2)] hover:text-[var(--color-ink)]',
                ].join(' ')
              }
            >
              {item.label}
            </NavLink>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function CaretIcon({ spin }: { spin?: boolean }) {
  return (
    <svg
      width="8"
      height="8"
      viewBox="0 0 8 8"
      className={`ml-1 inline-block transition-transform ${spin ? 'rotate-180' : ''}`}
      aria-hidden
    >
      <path d="M1.5 3 L6.5 3 L4 6.5 Z" fill="currentColor" />
    </svg>
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
