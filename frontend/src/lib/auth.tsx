import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { Session, User } from '@supabase/supabase-js';
import { supabase } from './supabase';
import { ROUTES } from './routes';

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

/* Cache key for the session's plan agendas. Deliberately NOT under any
 * feature's namespace: it is session state, read once per user. */
export const sessionAgendasKey = (userId: string | null) =>
  ['session', 'agendas', userId] as const;

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const queryClient = useQueryClient();

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

  /* The session's plan agendas: the explicit entitlements row's plan, else the
   * default plan. RLS returns only the caller's own entitlement. Admins never
   * consult this (nav bypass).
   *
   * Keyed on the USER ID, not the session object. This used to be a bare
   * useEffect with `[session]` in its dependency array, and Supabase's
   * onAuthStateChange hands out a fresh session object per event (initial
   * session, signed-in, token-refreshed) — so the pair of reads ran three times
   * on every single app start, on every route, outside React Query where
   * nothing could dedupe them. Measured in production: six requests at
   * 231-433 ms each before any page data moved. A user id is a stable string,
   * so the same user's three events now collapse into one cache entry.
   *
   * staleTime Infinity because a plan does not change under a live session; a
   * real change arrives with a new sign-in, which is a new key. */
  const userId = session?.user?.id ?? null;
  const agendasQ = useQuery({
    queryKey: sessionAgendasKey(userId),
    queryFn: async (): Promise<Record<string, boolean> | null> => {
      const [entRes, plansRes] = await Promise.all([
        supabase.from('entitlements').select('plan,status').maybeSingle(),
        supabase.from('plans').select('key,agendas,is_default'),
      ]);
      // A billing read that fails or returns nothing must not blank the nav —
      // null means "no constraint" to every consumer. Same posture as before.
      if (plansRes.error || !plansRes.data) return null;
      const planKey =
        entRes.data?.plan ?? plansRes.data.find((p) => p.is_default)?.key;
      const plan = plansRes.data.find((p) => p.key === planKey);
      return (plan?.agendas as Record<string, boolean> | undefined) ?? null;
    },
    enabled: userId != null,
    staleTime: Infinity,
    gcTime: Infinity,
    retry: false,
  });
  const agendas = userId == null ? null : agendasQ.data ?? null;

  /* Drop every cached read when the signed-in identity changes.
   *
   * The cache is keyed by query, not by account, and almost everything in it is
   * RLS-scoped rows belonging to whoever was signed in when it was fetched. On
   * a sign-out or an account switch those rows stayed resident for up to gcTime
   * (5 min), so the next session could paint the previous account's collections,
   * pipeline and notifications before its own reads landed. Clearing on the
   * TRANSITION (rather than only inside signOut) also covers a switch that
   * happens through a token change rather than an explicit sign-out. */
  const prevUserId = useRef<string | null>(null);
  useEffect(() => {
    if (prevUserId.current !== null && prevUserId.current !== userId) {
      queryClient.clear();
    }
    prevUserId.current = userId;
  }, [userId, queryClient]);

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
          options: { redirectTo: `${window.location.origin}${ROUTES.browse.build()}` },
        });
        if (error) throw error;
      },
      async sendPasswordReset(email) {
        const { error } = await supabase.auth.resetPasswordForEmail(email, {
          redirectTo: `${window.location.origin}${ROUTES.resetPassword.build()}`,
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
