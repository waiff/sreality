import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import type { Session, User } from '@supabase/supabase-js';
import { supabase } from './supabase';

/**
 * Phase 1 auth context. Tracks the Supabase session and exposes sign-in/out.
 * The login gate is wired via <RequireAuth> / <RequireAdmin> (components/guards);
 * `isAdmin` mirrors the JWT's app_metadata.is_admin claim (stamped server-side
 * from the admins table — never client-writable).
 */
type AuthState = {
  session: Session | null;
  user: User | null;
  loading: boolean;
  isAdmin: boolean;
  /** The session's plan agenda-visibility map (RLS reads of entitlements +
   *  plans); null while loading / on error / logged out — treated as
   *  "show everything" by consumers so a billing hiccup can't blank the nav. */
  agendas: Record<string, boolean> | null;
  signInWithPassword: (email: string, password: string) => Promise<void>;
  signUpWithPassword: (email: string, password: string) => Promise<void>;
  signInWithGoogle: () => Promise<void>;
  sendPasswordReset: (email: string) => Promise<void>;
  updatePassword: (password: string) => Promise<void>;
  signOut: () => Promise<void>;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [agendas, setAgendas] = useState<Record<string, boolean> | null>(null);

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session);
      setLoading(false);
    });
    const { data: sub } = supabase.auth.onAuthStateChange((_event, next) => {
      setSession(next);
    });
    return () => sub.subscription.unsubscribe();
  }, []);

  useEffect(() => {
    // Resolve the session's plan agendas once per session: the explicit
    // entitlements row's plan, else the default plan. RLS returns only the
    // caller's own entitlement. Admins never consult this (nav bypass).
    if (!session) {
      setAgendas(null);
      return;
    }
    let cancelled = false;
    (async () => {
      const [entRes, plansRes] = await Promise.all([
        supabase.from('entitlements').select('plan,status').maybeSingle(),
        supabase.from('plans').select('key,agendas,is_default'),
      ]);
      if (cancelled) return;
      if (plansRes.error || !plansRes.data) {
        setAgendas(null);
        return;
      }
      const planKey = entRes.data?.plan
        ?? plansRes.data.find((p) => p.is_default)?.key;
      const plan = plansRes.data.find((p) => p.key === planKey);
      setAgendas((plan?.agendas as Record<string, boolean> | undefined) ?? null);
    })().catch(() => {
      if (!cancelled) setAgendas(null);
    });
    return () => { cancelled = true; };
  }, [session]);

  const value = useMemo<AuthState>(
    () => ({
      session,
      user: session?.user ?? null,
      loading,
      isAdmin: session?.user?.app_metadata?.is_admin === true,
      agendas,
      async signInWithPassword(email, password) {
        const { error } = await supabase.auth.signInWithPassword({ email, password });
        if (error) throw error;
      },
      async signUpWithPassword(email, password) {
        const { error } = await supabase.auth.signUp({ email, password });
        if (error) throw error;
      },
      async signInWithGoogle() {
        const { error } = await supabase.auth.signInWithOAuth({
          provider: 'google',
          options: { redirectTo: `${window.location.origin}/browse` },
        });
        if (error) throw error;
      },
      async sendPasswordReset(email) {
        const { error } = await supabase.auth.resetPasswordForEmail(email, {
          redirectTo: `${window.location.origin}/reset-password`,
        });
        if (error) throw error;
      },
      async updatePassword(password) {
        const { error } = await supabase.auth.updateUser({ password });
        if (error) throw error;
      },
      async signOut() {
        const { error } = await supabase.auth.signOut();
        if (error) throw error;
      },
    }),
    [session, loading, agendas],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within <AuthProvider>');
  return ctx;
}
