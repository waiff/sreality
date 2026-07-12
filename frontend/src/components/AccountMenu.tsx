import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '@/lib/auth';

/* Additive UI only — does not gate anything. Anonymous users see a "Sign in"
 * link; logged-in users see who they are + a sign-out affordance. */
export default function AccountMenu() {
  const { user, loading, signOut } = useAuth();
  const navigate = useNavigate();
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

  if (loading) return null;

  if (!user) {
    return (
      <button
        type="button"
        onClick={() => navigate('/login')}
        className="px-3 py-1.5 text-sm tracking-wide rounded-[var(--radius-xs)] border border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] hover:bg-[var(--color-paper-2)]"
      >
        Sign in
      </button>
    );
  }

  const label = user.email ?? 'Account';

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        title={label}
        className={[
          'flex items-center justify-center w-8 h-8 rounded-full transition-colors',
          open
            ? 'bg-[var(--color-copper)] text-white'
            : 'bg-[var(--color-copper-soft)] text-[var(--color-copper)] hover:bg-[var(--color-copper)] hover:text-white',
        ].join(' ')}
      >
        <UserIcon />
      </button>
      {open ? (
        <div
          role="menu"
          className="absolute right-0 top-[calc(100%+4px)] z-30 min-w-[12rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] py-1 shadow-lg"
        >
          <div className="px-3 py-1.5 text-[0.7rem] tracking-wide text-[var(--color-ink-4)] truncate">
            Signed in as {label}
          </div>
          <div className="my-1 h-px bg-[var(--color-rule)]" aria-hidden />
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              void signOut();
            }}
            className="block w-full px-3 py-1.5 text-left text-[0.8rem] text-[var(--color-ink-2)] hover:bg-[var(--color-paper-2)] hover:text-[var(--color-brick)]"
          >
            Sign out
          </button>
        </div>
      ) : null}
    </div>
  );
}

function UserIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 14 14" fill="currentColor" aria-hidden>
      <circle cx="7" cy="4.2" r="2.6" />
      <path d="M1 13c0-3 2.7-5 6-5s6 2 6 5" />
    </svg>
  );
}
