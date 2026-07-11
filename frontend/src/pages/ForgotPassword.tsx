import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { useAuth } from '../lib/auth';
import { AuthShell } from './Login';

/** Phase 1 auth — request a password-reset email. */
export default function ForgotPassword() {
  const { sendPasswordReset } = useAuth();
  const [email, setEmail] = useState('');
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await sendPasswordReset(email);
      setSent(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not send reset email');
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell>
      <h1 className="text-lg font-semibold">Reset password</h1>
      {sent ? (
        <p className="text-sm text-emerald-600">
          If an account exists for {email}, a reset link is on its way.
        </p>
      ) : (
        <form className="flex flex-col gap-3" onSubmit={onSubmit}>
          <label className="flex flex-col gap-1 text-sm">
            Email
            <input
              className="rounded border border-neutral-300 dark:border-neutral-600 bg-transparent px-3 py-2 text-sm"
              type="email" autoComplete="email" required
              value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          {error && <p className="text-sm text-red-600">{error}</p>}
          <button
            className="rounded bg-neutral-900 text-white dark:bg-white dark:text-neutral-900 px-3 py-2 text-sm font-medium disabled:opacity-50"
            type="submit" disabled={busy}>
            {busy ? '…' : 'Send reset link'}
          </button>
        </form>
      )}
      <Link className="text-sm text-blue-600 hover:underline" to="/login">Back to sign in</Link>
    </AuthShell>
  );
}
