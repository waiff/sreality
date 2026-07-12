import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '@/lib/auth';
import { isSupabaseConfigured } from '@/lib/supabase';

/**
 * Phase 1 route guards. <RequireAuth> wraps the whole Shell (every app page
 * needs a session); <RequireAdmin> additionally bounces non-admins off the
 * admin surfaces. Both render nothing while the session is still loading so
 * a signed-in user never flashes through /login on a hard refresh.
 */

export function RequireAuth({ children }: { children: ReactNode }) {
  const { session, loading } = useAuth();
  const location = useLocation();
  // Local dev without supabase env vars has no way to sign in — don't lock
  // the app out; the gate only means something where auth is configured.
  if (!isSupabaseConfigured()) {
    console.warn('Supabase not configured — login gate disabled (local dev).');
    return <>{children}</>;
  }
  if (loading) return null;
  if (!session) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return <>{children}</>;
}

export function RequireAdmin({ children }: { children: ReactNode }) {
  const { loading, isAdmin } = useAuth();
  if (!isSupabaseConfigured()) return <>{children}</>;
  if (loading) return null;
  if (!isAdmin) return <Navigate to="/browse" replace />;
  return <>{children}</>;
}
