import { NavLink, Outlet } from 'react-router-dom';
import type { ReactNode } from 'react';
import {
  NewEstimationProvider,
  useNewEstimationModal,
} from './NewEstimationModal';

const navLinks: ReadonlyArray<{ to: string; label: string }> = [
  { to: '/browse',      label: 'Browse' },
  { to: '/listing',     label: 'Listing' },
  { to: '/estimations', label: 'Estimations' },
  { to: '/collections', label: 'Collections' },
  { to: '/health',      label: 'Health' },
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
          {navLinks.map((link) => (
            <NavLink
              key={link.to}
              to={link.to}
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
                <NavLabel active={isActive}>{link.label}</NavLabel>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-3">
          <NewEstimationCta />
          <SettingsGear />
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

function NewEstimationCta() {
  const { open } = useNewEstimationModal();
  return (
    <button
      type="button"
      onClick={open}
      className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors"
    >
      <PlusGlyph />
      <span>New estimation</span>
    </button>
  );
}

function SettingsGear() {
  return (
    <NavLink
      to="/settings"
      aria-label="Settings"
      title="Settings"
      className={({ isActive }) =>
        [
          'inline-flex items-center justify-center w-9 h-9 rounded-[var(--radius-sm)] transition-colors',
          isActive
            ? 'text-[var(--color-ink)] bg-[var(--color-paper-2)] border border-[var(--color-rule)]'
            : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] hover:bg-[var(--color-paper-2)] border border-transparent',
        ].join(' ')
      }
    >
      <GearGlyph />
    </NavLink>
  );
}

function PlusGlyph() {
  return (
    <svg width="11" height="11" viewBox="0 0 11 11" aria-hidden>
      <path
        d="M5.5 1.5 L5.5 9.5 M1.5 5.5 L9.5 5.5"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  );
}

function GearGlyph() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="2.2" />
      <path d="M8 1.4v1.7M8 12.9v1.7M14.6 8h-1.7M3.1 8H1.4M12.66 3.34l-1.2 1.2M4.54 11.46l-1.2 1.2M12.66 12.66l-1.2-1.2M4.54 4.54l-1.2-1.2" />
    </svg>
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
