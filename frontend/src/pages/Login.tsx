import { useState, type FormEvent, type ReactNode } from 'react';
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../lib/auth';

/**
 * Phase 1 auth — sign-in screen (email/password + Google). Full-page, outside
 * the app Shell; every Shell page sits behind <RequireAuth>, which redirects
 * here with the original location in `state.from`.
 */
export default function Login() {
  const { signInWithPassword, signUpWithPassword, signInWithGoogle, session, loading } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [mode, setMode] = useState<'signin' | 'signup'>('signin');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const from =
    (location.state as { from?: { pathname?: string } } | null)?.from?.pathname ?? '/browse';

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (mode === 'signin') {
        await signInWithPassword(email, password);
        navigate(from, { replace: true });
      } else {
        await signUpWithPassword(email, password);
        setNotice('Check your email to confirm your account, then sign in.');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign-in failed');
    } finally {
      setBusy(false);
    }
  }

  // Already signed in (e.g. deep-linked to /login) — straight into the app.
  if (!loading && session) {
    return <Navigate to={from} replace />;
  }

  return (
    <AuthShell>
      <h1 className="text-lg font-semibold">
        {mode === 'signin' ? 'Sign in' : 'Create account'}
      </h1>
      <form className="flex flex-col gap-3" onSubmit={onSubmit}>
        <label className="flex flex-col gap-1 text-sm">
          Email
          <input className={input} type="email" autoComplete="email" required
            value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Password
          <input className={input} type="password"
            autoComplete={mode === 'signin' ? 'current-password' : 'new-password'}
            required value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && <p className="text-sm text-red-600">{error}</p>}
        {notice && <p className="text-sm text-emerald-600">{notice}</p>}
        <button className={btn} type="submit" disabled={busy}>
          {busy ? '…' : mode === 'signin' ? 'Sign in' : 'Create account'}
        </button>
      </form>
      <button className={btnGhost} onClick={() => void signInWithGoogle()} disabled={busy}>
        Continue with Google
      </button>
      <div className="flex justify-between text-sm">
        <button className={link} type="button"
          onClick={() => setMode(mode === 'signin' ? 'signup' : 'signin')}>
          {mode === 'signin' ? 'Create an account' : 'Have an account? Sign in'}
        </button>
        <Link className={link} to="/forgot-password">Forgot password?</Link>
      </div>
    </AuthShell>
  );
}

export function AuthShell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <div className="w-full max-w-sm flex flex-col gap-4 rounded-lg border border-neutral-200 dark:border-neutral-700 p-6">
        {children}
      </div>
    </div>
  );
}

const input =
  'rounded border border-neutral-300 dark:border-neutral-600 bg-transparent px-3 py-2 text-sm';
const btn =
  'rounded bg-neutral-900 text-white dark:bg-white dark:text-neutral-900 px-3 py-2 text-sm font-medium disabled:opacity-50';
const btnGhost =
  'rounded border border-neutral-300 dark:border-neutral-600 px-3 py-2 text-sm font-medium disabled:opacity-50';
const link = 'text-sm text-blue-600 hover:underline';
