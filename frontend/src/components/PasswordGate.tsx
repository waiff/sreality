import { useState, type FormEvent, type ReactNode } from 'react';
import { isUnlocked, tryUnlock, isGateConfigured } from '@/lib/auth';

interface Props {
  children: ReactNode;
}

export default function PasswordGate({ children }: Props) {
  const [unlocked, setUnlocked] = useState<boolean>(isUnlocked());
  const [pw, setPw] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (unlocked) return <>{children}</>;

  if (!isGateConfigured()) {
    return (
      <GateShell>
        <div className="space-y-3">
          <h1 className="text-2xl">Configuration missing</h1>
          <p className="text-sm text-[var(--color-ink-2)] leading-relaxed">
            The unlock-password hash is not set. Add{' '}
            <code className="font-mono text-[0.85em] px-1.5 py-0.5 rounded-[var(--radius-xs)] bg-[var(--color-inset)]">
              VITE_PASSWORD_HASH
            </code>{' '}
            to the environment (a SHA-256 hex digest, 64 chars) and rebuild.
          </p>
        </div>
      </GateShell>
    );
  }

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const ok = await tryUnlock(pw);
    setBusy(false);
    if (ok) {
      setUnlocked(true);
    } else {
      setError('That password did not match.');
      setPw('');
    }
  };

  return (
    <GateShell>
      <form onSubmit={onSubmit} className="space-y-5" noValidate>
        <div className="space-y-2">
          <h1 className="text-2xl leading-tight">sreality</h1>
          <p className="text-sm text-[var(--color-ink-3)] tracking-wide uppercase">
            database&nbsp;browser
          </p>
        </div>
        <p className="text-sm text-[var(--color-ink-2)] leading-relaxed">
          A read-only window into the rental scrape. Enter the unlock password
          to continue.
        </p>
        <div className="space-y-2">
          <label
            htmlFor="pw"
            className="block text-xs font-medium tracking-wide uppercase text-[var(--color-ink-3)]"
          >
            Password
          </label>
          <input
            id="pw"
            type="password"
            value={pw}
            onChange={(e) => setPw(e.target.value)}
            autoFocus
            autoComplete="current-password"
            disabled={busy}
            className="w-full px-3 py-2 rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)] disabled:opacity-60"
          />
          {error && (
            <p className="text-xs text-[var(--color-brick)] mt-1">{error}</p>
          )}
        </div>
        <button
          type="submit"
          disabled={busy || pw.length === 0}
          className="w-full px-3 py-2 rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white text-sm font-medium tracking-wide hover:bg-[var(--color-copper-2)] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {busy ? 'Checking…' : 'Unlock'}
        </button>
        <p className="text-[0.7rem] text-[var(--color-ink-4)] leading-relaxed pt-2 border-t border-[var(--color-rule-soft)]">
          Soft gate. The data is already on sreality.cz. Real protection is the
          read-only Postgres views.
        </p>
      </form>
    </GateShell>
  );
}

function GateShell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-dvh grid place-items-center px-6 py-12 bg-[var(--color-paper)]">
      <div className="w-full max-w-sm bg-[var(--color-paper-2)] border border-[var(--color-rule)] rounded-[var(--radius-md)] p-8">
        {children}
      </div>
    </div>
  );
}
