import { NavLink, Outlet } from 'react-router-dom';
import type { ReactNode } from 'react';
import { NewEstimationProvider } from './NewEstimationModal';

type NavItem =
  | { kind: 'link'; to: string; label: string; disabled?: boolean }
  | { kind: 'divider' };

const navItems: ReadonlyArray<NavItem> = [
  { kind: 'link', to: '/browse',      label: 'Browse' },
  { kind: 'link', to: '/datasets',    label: 'Datasets' },
  { kind: 'link', to: '/watchdog',    label: 'Watchdog' },
  { kind: 'link', to: '/estimations', label: 'Estimations' },
  { kind: 'link', to: '/collections', label: 'Collections', disabled: true },
  { kind: 'divider' },
  { kind: 'link', to: '/dedup',       label: 'Dedup' },
  { kind: 'link', to: '/health',      label: 'Health' },
  { kind: 'link', to: '/scrapers',    label: 'Scrapers' },
  { kind: 'link', to: '/settings',    label: 'General Settings' },
];

export default function Shell() {
  return (
    <NewEstimationProvider>
      <div className="min-h-dvh flex flex-col bg-[var(--color-paper)] text-[var(--color-ink)]">
        <TopBar />
        <main className="flex-1">
          <Outlet />
        </main>
        <Footer />
      </div>
    </NewEstimationProvider>
  );
}

function TopBar() {
  return (
    <header className="border-b border-[var(--color-rule)] bg-[var(--color-paper)] sticky top-0 z-30">
      <div className="px-6 h-14 flex items-center gap-8">
        <BrandMark />
        <nav className="flex items-center gap-1">
          {navItems.map((item, i) => {
            if (item.kind === 'divider') {
              return (
                <span
                  key={`divider-${i}`}
                  className="mx-2 h-4 w-px bg-[var(--color-rule)]"
                  aria-hidden
                />
              );
            }
            if (item.disabled) {
              return (
                <span
                  key={item.to}
                  title="Collections is being reworked — not available yet."
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
                  <NavLabel active={isActive}>{item.label}</NavLabel>
                )}
              </NavLink>
            );
          })}
        </nav>
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
  return (
    <div className="flex items-baseline gap-2 select-none">
      <span
        className="font-display text-[1.05rem] leading-none"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        sreality
      </span>
      <span className="text-[0.65rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        database&nbsp;browser
      </span>
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
