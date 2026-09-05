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
import ErrorBoundary from './ErrorBoundary';
import AccountMenu from './AccountMenu';
import { APP_NAME } from '@/lib/brand';
import { useBuildSkew } from '@/lib/useBuildSkew';
import { ROUTES } from '@/lib/routes';

type NavItem = { to: string; label: string; disabled?: boolean; title?: string; admin?: boolean; agenda?: string };

// `admin: true` entries only render for admin sessions; `agenda` keys tie a
// link to the session plan's agenda-visibility map (Settings › Tiers). Both
// are UX — the routes themselves carry the security gates.
const navItems: ReadonlyArray<NavItem> = [
  { to: ROUTES.browse.build(),      label: 'Browse', agenda: 'browse' },
  { to: ROUTES.pipeline.build(),    label: 'Pipeline', agenda: 'pipeline' },
  { to: ROUTES.estimations.build(), label: 'Estimations', agenda: 'estimations' },
  { to: ROUTES.watchdog.build(),    label: 'Watchdogs', agenda: 'watchdogs' },
  { to: ROUTES.notifications.build(), label: 'Notifications', agenda: 'notifications' },
  { to: ROUTES.brokers.build(),     label: 'Brokers', agenda: 'brokers' },
  // The merge-review queue is a real admin surface (routes.tsx wraps it in
  // AdminPage) that was only reachable from a conditional chip on /brokers —
  // invisible whenever the queue happened to be empty.
  { to: ROUTES.brokersReview.build(), label: 'Broker Review', admin: true },
  { to: ROUTES.datasets.build(),    label: 'Datasets', admin: true },
  { to: ROUTES.outreach.build(),    label: 'Outreach', disabled: true, admin: true,
    title: 'Outreach is paused — not available yet.' },
  { to: ROUTES.collections.build(), label: 'Collections', agenda: 'collections' },
];

type MenuItem = { to: string; label: string; end?: boolean };

// Grouped under the "Settings" dropdown trigger — all admin-only, so the
// whole group renders (or not) alongside the other admin-gated nav items.
const settingsItems: ReadonlyArray<MenuItem> = [
  { to: ROUTES.health.build(),   label: 'Health' },
  { to: ROUTES.costs.build(),    label: 'LLM Costs' },
  { to: ROUTES.locationQuality.build(), label: 'Location Quality' },
  { to: ROUTES.scrapers.build(), label: 'Scrapers' },
  { to: ROUTES.settings.build(), label: 'General Settings' },
];

// The NEW DEDUP program's own group. Admin-only, same posture as the
// Settings group. Dashboard is still a Wave 0 placeholder (real content
// lands with the funnel/cost work); Settings + Labeling are real (Wave 1).
const newDedupItems: ReadonlyArray<MenuItem> = [
  { to: ROUTES.newDedup.build(), label: 'Dashboard', end: true },
  { to: ROUTES.newDedupSettings.build(), label: 'Settings' },
  // `end` so /new-dedup/labeling/taxonomy lights only Taxonomy — NavMenu passes
  // this straight to NavLink, whose default prefix match would light both.
  { to: ROUTES.newDedupLabeling.build(), label: 'Labeling', end: true },
  { to: ROUTES.newDedupTaxonomy.build(), label: 'Taxonomy' },
  { to: ROUTES.newDedupExam.build(), label: 'Exam' },
];

function isPathActive(pathname: string, to: string): boolean {
  return pathname === to || pathname.startsWith(`${to}/`);
}

/* Which nav entry owns the current path when one nests inside another
 * (`/brokers/review` under `/brokers`). NavLink's default prefix matching would
 * light both at once; the longest match wins instead, so `/brokers/123` still
 * highlights Brokers while `/brokers/review` highlights only itself. */
export function activeNavTo(pathname: string, tos: ReadonlyArray<string>): string | null {
  return tos.reduce<string | null>(
    (best, to) =>
      isPathActive(pathname, to) && (best === null || to.length > best.length) ? to : best,
    null,
  );
}

export default function Shell() {
  const location = useLocation();
  /* Offer a reload when a newer build is deployed — see lib/buildSkew.ts. */
  useBuildSkew();
  return (
    <NewEstimationProvider>
      <ExploreAreaProvider>
        <div className="min-h-dvh flex flex-col bg-[var(--color-paper)] text-[var(--color-ink)]">
          <TopBar />
          {/* Keyed on pathname so a crashed page recovers on the next nav.
            * Scoped to the route body on purpose: a page crash must not take
            * the nav, the footer or the toast surface with it — the fallback
            * says "use the back button", which needs the nav to still exist. */}
          <main className="flex-1">
            <ErrorBoundary key={location.pathname} label="route">
              <Outlet />
            </ErrorBoundary>
          </main>
          <Footer />
        </div>
        <ToastViewport />
      </ExploreAreaProvider>
    </NewEstimationProvider>
  );
}

function TopBar() {
  const { isAdmin, agendas } = useAuth();
  const location = useLocation();
  /* The badge polls a Railway route that costs ~300 ms of unpooled connection
   * setup for ~15 ms of server work, on every page load and then every 30 s.
   * Two changes: it only runs when the Notifications nav item is actually
   * visible to this session (the agenda gate below already decides that, and
   * polling a count for a hidden entry is pure cost), and the cadence backs off
   * to 60 s — an unread badge is not a real-time surface, and the matcher that
   * feeds it does not run faster than that either. */
  const notificationsVisible =
    isAdmin || !isSupabaseConfigured() || agendas === null ||
    agendas['notifications'] === true;
  const unreadQ = useQuery({
    queryKey: notificationKeys.unreadCount,
    queryFn: () => getNotificationUnreadCount(),
    enabled: notificationsVisible,
    staleTime: 60_000,
    refetchInterval: 60_000,
  });
  const unread = unreadQ.data?.unread_count ?? 0;
  // Unconfigured local dev has no session (so no is_admin claim) — show the
  // full nav there, mirroring the guards' allow-through posture.
  const showAdmin = isAdmin || !isSupabaseConfigured();
  const items = navItems.filter((item) => {
    if (item.admin && !showAdmin) return false;
    // Plan agenda gating (non-admins only). agendas === null means the
    // billing read hasn't resolved / failed — show everything rather than
    // blank the nav over a read hiccup; admins always bypass.
    if (!showAdmin && agendas !== null && item.agenda && agendas[item.agenda] !== true) {
      return false;
    }
    return true;
  });
  const ownerTo = activeNavTo(location.pathname, items.map((i) => i.to));
  const settingsActive = settingsItems.some((s) => isPathActive(location.pathname, s.to));
  const newDedupActive = newDedupItems.some((s) => isPathActive(location.pathname, s.to));
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
                /* Exact matching for every entry that is NOT the current path's
                   owner, so a parent (/brokers) can't stay lit on a nested
                   sibling (/brokers/review). aria-current follows the same rule. */
                end={item.to !== ownerTo}
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
              <NavMenu label="NEW DEDUP" items={newDedupItems} active={newDedupActive} />
              <NavMenu label="Settings" items={settingsItems} active={settingsActive} />
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

function NavMenu({
  label,
  items,
  active,
}: {
  label: string;
  items: ReadonlyArray<MenuItem>;
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
          {label}
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
              end={item.end}
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
