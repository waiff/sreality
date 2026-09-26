import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getNotificationUnreadCount } from '@/lib/api';
import { notificationKeys } from '@/lib/queries';
import { useAuth } from '@/lib/auth';
import { isSupabaseConfigured } from '@/lib/supabase';
import { NewEstimationProvider } from './NewEstimationModal';
import { ExploreAreaProvider } from './ExploreAreaModal';
import { ExploreBrokerProvider } from './ExploreBrokerModal';
import ToastViewport from './ToastViewport';
import ErrorBoundary from './ErrorBoundary';
import AccountMenu from './AccountMenu';
import { APP_NAME } from '@/lib/brand';
import { useBuildSkew } from '@/lib/useBuildSkew';
import { MAIN_ID, useRouteFocus } from '@/lib/useRouteFocus';
import SkipLink from './SkipLink';
import { ROUTES } from '@/lib/routes';
import { PIN_AUDIT_TOTAL_KEY, fetchPinAuditTotal } from '@/lib/pinAudit';
import { fmtCount } from '@/lib/format';

type NavItem = { to: string; label: string; disabled?: boolean; title?: string; admin?: boolean; agenda?: string };

// `admin: true` entries only render for admin sessions; `agenda` keys tie a
// link to the session plan's agenda-visibility map (Settings › Tiers). Both
// are UX — the routes themselves carry the security gates.
const navItems: ReadonlyArray<NavItem> = [
  /* FIRST on purpose, and shouting on purpose. These are the listings W5 hides
   * from customers until their location is resolved (migration 514), so the
   * badge is a WORK QUEUE, not a one-off review: it counts down as the resolver
   * lane works and the entry is quiet at zero. It no longer leaves this list
   * when a ruling lands — the ruling IS what put it here. */
  { to: ROUTES.newDedupPinAudit.build(), label: '!AUDIT POLOH', admin: true },
  { to: ROUTES.browse.build(),      label: 'Browse', agenda: 'browse' },
  { to: ROUTES.pipeline.build(),    label: 'Pipeline', agenda: 'pipeline' },
  { to: ROUTES.estimations.build(), label: 'Estimations', agenda: 'estimations' },
  { to: ROUTES.watchdog.build(),    label: 'Watchdogs', agenda: 'watchdogs' },
  { to: ROUTES.notifications.build(), label: 'Notifications', agenda: 'notifications' },
  { to: ROUTES.brokers.build(),     label: 'Brokers', agenda: 'brokers' },
  { to: ROUTES.collections.build(), label: 'Collections', agenda: 'collections' },
];

type MenuItem = { to: string; label: string; end?: boolean; disabled?: boolean; title?: string };
type MenuGroup = { label: string; items: ReadonlyArray<MenuItem> };

// THE BAR OVERFLOWED. At 1440px the header measured 1555px wide and the last
// entries — Settings, and the account button after it — were clipped off the
// right edge with no way to reach them. The bar's height is load-bearing for
// three Browse panes (see the comment on the row below), so a second row was
// never an option and tightening the gaps had already been spent. What was left
// was to stop spending the row on surfaces the operator does not use hourly:
// the three admin dropdowns collapse into ONE "Admin" trigger with a section
// per program, and the three admin-only links (Broker Review, Datasets, the
// paused Outreach) move inside it. The top row is now the daily surfaces plus
// the audit queue, which fits with room to spare at 1280.
//
// Grouped under the "Settings" section of that menu — all admin-only, so the
// whole group renders (or not) with the trigger.
const settingsItems: ReadonlyArray<MenuItem> = [
  { to: ROUTES.health.build(),   label: 'Health' },
  { to: ROUTES.costs.build(),    label: 'LLM Costs' },
  { to: ROUTES.locationQuality.build(), label: 'Location Quality' },
  { to: ROUTES.scrapers.build(), label: 'Scrapers' },
  { to: ROUTES.settings.build(), label: 'General Settings' },
];

// The NEW DEDUP program's own group. Admin-only, same posture as the
// Settings group. Dashboard carries the funnel + cost table (Wave 2);
// Settings + Labeling are Wave 1; Candidates is the Wave 2 audit page.
const newDedupItems: ReadonlyArray<MenuItem> = [
  { to: ROUTES.newDedup.build(), label: 'Dashboard', end: true },
  { to: ROUTES.newDedupCandidates.build(), label: 'Candidates' },
  { to: ROUTES.newDedupSettings.build(), label: 'Settings' },
  // `end` so /new-dedup/labeling/taxonomy lights only Taxonomy — NavMenu passes
  // this straight to NavLink, whose default prefix match would light both.
  { to: ROUTES.newDedupLabeling.build(), label: 'Labeling', end: true },
  { to: ROUTES.newDedupTaxonomy.build(), label: 'Taxonomy' },
  { to: ROUTES.newDedupTrainingSet.build(), label: 'Training set' },
  { to: ROUTES.newDedupExam.build(), label: 'Exam' },
  { to: ROUTES.newDedupTaggingBakeoff.build(), label: 'Tagging bake-off' },
];

// The AUTONOMOUS dedup program's own group (docs/design/autodedup/PROGRAM.md) —
// a different program from NEW DEDUP above, with its own schema, lane and
// settings namespace, so it gets its own dropdown rather than a sub-entry that
// would read as a page of that one. Progress is its first surface: the
// iteration-by-iteration ledger that makes a long autonomous run legible while
// it runs.
const autodedupItems: ReadonlyArray<MenuItem> = [
  { to: ROUTES.autodedupProgress.build(), label: 'Progress', end: true },
  // The W5 validation views. Groups is the proposed-cluster queue, Residual the
  // pairs the engine did NOT join; the pair page is drilled into from both and
  // is deliberately not a menu entry — it has no meaning without a pair.
  { to: ROUTES.autodedupGroups.build(), label: 'Groups' },
  { to: ROUTES.autodedupResidual.build(), label: 'Residual' },
  // Decision 9: what a refit would take apart, split only by the operator.
  { to: ROUTES.autodedupProposedSplits.build(), label: 'Návrhy rozdělení' },
  // E920: every ruling the operator ever made, beside the engine's view of it.
  { to: ROUTES.autodedupRulings.build(), label: 'Rozhodnutí' },
];

// The admin-only surfaces that used to sit in the top row, now the first
// section of the one "Admin" menu. Outreach keeps its paused state here rather
// than vanishing — a surface that exists and is switched off is a different
// fact from one that was removed.
const adminItems: ReadonlyArray<MenuItem> = [
  // The merge-review queue is a real admin surface (routes.tsx wraps it in
  // AdminPage) that was only reachable from a conditional chip on /brokers —
  // invisible whenever the queue happened to be empty.
  { to: ROUTES.brokersReview.build(), label: 'Broker Review' },
  { to: ROUTES.datasets.build(), label: 'Datasets' },
  { to: ROUTES.outreach.build(), label: 'Outreach', disabled: true,
    title: 'Outreach is paused — not available yet.' },
];

// One trigger, four sections, in the order they are reached for.
const adminGroups: ReadonlyArray<MenuGroup> = [
  { label: 'Admin', items: adminItems },
  { label: 'NEW DEDUP', items: newDedupItems },
  { label: 'AUTODEDUP', items: autodedupItems },
  { label: 'Settings', items: settingsItems },
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
  // Where focus GOES on a route change: the <main> landmark below.
  useRouteFocus();
  /* Offer a reload when a newer build is deployed — see lib/buildSkew.ts. */
  useBuildSkew();
  return (
    <NewEstimationProvider>
      <ExploreAreaProvider>
        <ExploreBrokerProvider>
          <div className="min-h-dvh flex flex-col bg-[var(--color-paper)] text-[var(--color-ink)]">
            <SkipLink />
            <TopBar />
            {/* Keyed on pathname so a crashed page recovers on the next nav.
              * Scoped to the route body on purpose: a page crash must not take
              * the nav, the footer or the toast surface with it — the fallback
              * says "use the back button", which needs the nav to still exist. */}
            {/* id + tabIndex=-1: the skip link's target and where useRouteFocus
              * lands keyboard focus after every navigation. -1 keeps it out of
              * the tab order while letting it be focused programmatically. */}
            <main id={MAIN_ID} tabIndex={-1} className="flex-1 outline-none">
              <ErrorBoundary key={location.pathname} label="route">
                <Outlet />
              </ErrorBoundary>
            </main>
            <Footer />
          </div>
          <ToastViewport />
        </ExploreBrokerProvider>
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
  /* The !AUDIT POLOH count — the UNRESOLVED rows only (W7-b): the listings the
   * system finished and could not place. The ones it simply has not reached yet
   * are normal traffic, and a badge that counted them would climb every time the
   * scrapers ran. One head-count request, held for the session
   * (`staleTime: Infinity`) because the matview behind it only refreshes
   * hourly; the audit page invalidates this key on open, which is the one
   * moment a fresh number is worth a round trip. `retry: false` + reading
   * `data` (never `isError`) is what keeps a failed count from touching
   * navigation — the entry renders with no badge and still works. */
  const pinAuditQ = useQuery({
    queryKey: PIN_AUDIT_TOTAL_KEY,
    queryFn: fetchPinAuditTotal,
    enabled: isAdmin || !isSupabaseConfigured(),
    staleTime: Infinity,
    retry: false,
  });
  const pinAuditTotal = pinAuditQ.data ?? null;
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
  /* `end` is honoured here, not just on the NavLink: Dashboard's `to` is
   * `/new-dedup`, which prefix-matches every page in the program — including
   * /new-dedup/pin-audit, a top-level entry of its own. Without this the
   * dropdown and !AUDIT POLOH would both light up on that path. */
  const adminActive = adminGroups.some((group) =>
    group.items.some((s) =>
      s.end ? location.pathname === s.to : isPathActive(location.pathname, s.to),
    ),
  );
  return (
    <header className="border-b border-[var(--color-rule)] bg-[var(--color-paper)] sticky top-0 z-30">
      {/* THE BAR IS EXACTLY 3.5rem TALL AND STAYS THAT WAY. Three Browse panes
        * and the pipeline board's stage headers pin themselves against that
        * number in CSS (`top-14`, `calc(100dvh-3.5rem)` in Filters.tsx,
        * BrowseExperience.tsx and Pipeline.tsx), so a header that grows a
        * second row slides the sidebar heading and the stage labels under it
        * and overflows the map pane. Room for the nav is bought by TIGHTENING and,
        * once tightening ran out (the bar measured 1555px inside 1440 and
        * clipped Settings and the account button), by CARRYING LESS: the three
        * admin dropdowns are one "Admin" trigger and the admin-only links live
        * inside it. Never by changing the height. Making the bar
        * variable-height needs `--header-h` published here and consumed at
        * those four sites; that is a Browse + Pipeline change, not a nav change.
        *
        * Horizontal scrolling was considered and rejected: `overflow-x: auto`
        * forces `overflow-y` to auto as well, which clips the dropdown panels
        * and the active underline that hangs below each label. */}
      <div className="px-6 h-14 flex items-center gap-4">
        <BrandMark />
        {/* `min-w-0` + a non-shrinking account menu: whatever the nav's width
          * does, the account button stays on screen and reachable. */}
        <nav className="flex min-w-0 items-center gap-0.5">
          {items.map((item) => {
            if (item.disabled) {
              return (
                <span
                  key={item.to}
                  title={item.title}
                  aria-disabled="true"
                  className="relative px-2 xl:px-2.5 py-1.5 text-sm tracking-wide text-[var(--color-ink-4)] opacity-50 cursor-not-allowed select-none"
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
                    'relative px-2 xl:px-2.5 py-1.5 text-sm tracking-wide rounded-[var(--radius-xs)] transition-colors',
                    isActive
                      ? 'text-[var(--color-ink)]'
                      : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
                  ].join(' ')
                }
              >
                {({ isActive }) => (
                  <NavLabel active={isActive}>
                    {item.label}
                    {item.to === ROUTES.newDedupPinAudit.build() &&
                      pinAuditTotal !== null && (
                        <>
                          <span
                            className="ml-1.5 inline-flex items-center justify-center h-[1.05rem] px-1.5 rounded-full bg-[var(--color-brick)] text-white text-[0.6rem] font-medium tabular-nums"
                            aria-hidden="true"
                          >
                            {fmtCount(pinAuditTotal)}
                          </span>
                          <span className="sr-only">
                            {' '}
                            {pinAuditTotal} inzerátů s nevyřešenou polohou
                          </span>
                        </>
                      )}
                    {item.to === '/notifications' && unread > 0 && (
                      <>
                        {/* The badge is a glyph; the count reaches the link's
                            accessible name through the hidden suffix, so an
                            aria-label can't overwrite "Notifications". */}
                        <span
                          className="ml-1.5 inline-flex items-center justify-center min-w-[1.05rem] h-[1.05rem] px-1 rounded-full bg-[var(--color-brick)] text-white text-[0.6rem] font-medium tabular-nums"
                          aria-hidden="true"
                        >
                          {unread > 99 ? '99+' : unread}
                        </span>
                        <span className="sr-only"> {unread} unread notifications</span>
                      </>
                    )}
                  </NavLabel>
                )}
              </NavLink>
            );
          })}
          {showAdmin && (
            <>
              <span className="mx-2 h-4 w-px bg-[var(--color-rule)]" aria-hidden />
              <NavMenu label="Admin" groups={adminGroups} active={adminActive} />
            </>
          )}
        </nav>
        <div className="ml-auto shrink-0">
          <AccountMenu />
        </div>
      </div>
    </header>
  );
}

function NavMenu({
  label,
  groups,
  active,
}: {
  label: string;
  /* Sections, not one flat list: four programs under one trigger stay legible
   * only if each keeps its own heading. A single-section menu renders no
   * heading at all. */
  groups: ReadonlyArray<MenuGroup>;
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
          'relative px-2 xl:px-2.5 py-1.5 text-sm tracking-wide rounded-[var(--radius-xs)] transition-colors',
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
          className="absolute right-0 top-[calc(100%+4px)] z-30 min-w-[12rem] max-h-[calc(100dvh-5rem)] overflow-y-auto rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] py-1 shadow-lg"
        >
          {groups.map((group, gi) => (
            /* `role="group"`, not a bare div: a menu owns its menuitems directly
             * or through a group, and a plain element between the two breaks that
             * ownership for a screen reader even though every item still computes
             * as a menuitem on its own. The visual heading is the group's label,
             * so it is named here and hidden there rather than read twice. */
            <div key={group.label} role="group" aria-label={group.label}>
              {groups.length > 1 && (
                <p
                  aria-hidden="true"
                  className={`px-3 pb-0.5 text-[0.58rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)] ${
                    gi === 0 ? 'pt-1' : 'mt-1 pt-1.5 border-t border-[var(--color-rule-soft)]'
                  }`}
                >
                  {group.label}
                </p>
              )}
              {group.items.map((item) =>
                item.disabled ? (
                  <span
                    key={item.to}
                    title={item.title}
                    aria-disabled="true"
                    className="block px-3 py-1.5 text-[0.8rem] text-[var(--color-ink-4)] opacity-50 cursor-not-allowed select-none"
                  >
                    {item.label}
                  </span>
                ) : (
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
                ),
              )}
            </div>
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
        /* Hidden below xl: it is decoration, and the narrow row needs its
          * ~70px more than the wordmark needs its second half. */
        <span className="hidden xl:inline text-[0.65rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
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
