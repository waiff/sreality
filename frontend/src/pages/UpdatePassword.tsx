import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../lib/auth';
import { AuthShell } from './Login';

/**
 * Phase 1 auth — set a new password. Reached from the reset-email link; Supabase
 * detects the recovery session in the URL (detectSessionInUrl), so updateUser
 * applies to the recovering user.
 */
export default function UpdatePassword() {
  const { updatePassword } = useAuth();
  const navigate = useNavigate();
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await updatePassword(password);
      setDone(true);
      setTimeout(() => navigate('/browse'), 1200);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not update password');
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell>
      <h1 className="text-lg font-semibold">Set a new password</h1>
      {done ? (
        <p className="text-sm text-emerald-600">Password updated. Taking you in…</p>
      ) : (
        <form className="flex flex-col gap-3" onSubmit={onSubmit}>
          <label className="flex flex-col gap-1 text-sm">
            New password
            <input
              className="rounded border border-neutral-300 dark:border-neutral-600 bg-transparent px-3 py-2 text-sm"
              type="password" autoComplete="new-password" required minLength={8}
              value={password} onChange={(e) => setPassword(e.target.value)} />
          </label>
          {error && <p className="text-sm text-red-600">{error}</p>}
          <button
            className="rounded bg-neutral-900 text-white dark:bg-white dark:text-neutral-900 px-3 py-2 text-sm font-medium disabled:opacity-50"
            type="submit" disabled={busy}>
            {busy ? '…' : 'Update password'}
          </button>
        </form>
      )}
    </AuthShell>
  );
}
