/* Session bootstrap: one read per user, and no cross-account cache bleed.
 *
 * Both properties here were live defects found by the hydration audit, and both
 * are invisible in normal use — the first only shows up in a network trace, the
 * second only when two accounts are used in one browser. So they get tests.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const h = vi.hoisted(() => ({
  /* Emitted auth-state callbacks, so a test can fire events itself. */
  listeners: [] as Array<(event: string, session: unknown) => void>,
  initialSession: null as unknown,
  reads: [] as string[],
}));

vi.mock('./supabase', () => {
  const table = (relation: string) => {
    h.reads.push(relation);
    const rows =
      relation === 'plans'
        ? [{ key: 'free', agendas: { browse: true }, is_default: true }]
        : [];
    const b: Record<string, unknown> = {
      select: () => b,
      maybeSingle: () => Promise.resolve({ data: null, error: null }),
      then: (resolve: (r: unknown) => unknown) =>
        resolve({ data: rows, error: null }),
    };
    return b;
  };
  return {
    isSupabaseConfigured: () => true,
    supabase: {
      from: table,
      auth: {
        getSession: () => Promise.resolve({ data: { session: h.initialSession } }),
        onAuthStateChange: (cb: (event: string, session: unknown) => void) => {
          h.listeners.push(cb);
          return { data: { subscription: { unsubscribe: () => {} } } };
        },
      },
    },
  };
});

import { AuthProvider, useAuth } from './auth';

/* A fresh object each call — exactly what Supabase hands out per auth event,
 * and the reason keying on the session object refetched three times. */
const sessionFor = (userId: string) => ({
  access_token: 't', user: { id: userId, app_metadata: {} },
});

function Probe() {
  const { agendas } = useAuth();
  return <span data-testid="agendas">{agendas ? Object.keys(agendas).join(',') : 'null'}</span>;
}

function renderAuth(client: QueryClient) {
  return render(
    <QueryClientProvider client={client}>
      <AuthProvider><Probe /></AuthProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  h.listeners = [];
  h.reads = [];
  h.initialSession = null;
});

describe('AuthProvider session bootstrap', () => {
  it('reads entitlements + plans ONCE across repeated auth events for one user', async () => {
    h.initialSession = sessionFor('user-1');
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { getByTestId } = renderAuth(qc);

    await waitFor(() => expect(getByTestId('agendas').textContent).toBe('browse'));
    const afterFirst = h.reads.length;

    /* Supabase fires INITIAL_SESSION, SIGNED_IN and TOKEN_REFRESHED on a normal
       start, each with a NEW session object for the SAME user. Before this was
       keyed on user.id, each one re-ran the pair — six requests per app start,
       on every route, measured at 231-433 ms each. */
    for (const event of ['SIGNED_IN', 'TOKEN_REFRESHED', 'INITIAL_SESSION']) {
      for (const cb of h.listeners) cb(event, sessionFor('user-1'));
    }
    await new Promise((r) => setTimeout(r, 20));

    expect(h.reads.length).toBe(afterFirst);
    expect(h.reads.filter((r) => r === 'entitlements')).toHaveLength(1);
    expect(h.reads.filter((r) => r === 'plans')).toHaveLength(1);
  });

  it('clears the query cache when the signed-in identity changes', async () => {
    h.initialSession = sessionFor('user-1');
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { getByTestId } = renderAuth(qc);
    await waitFor(() => expect(getByTestId('agendas').textContent).toBe('browse'));

    /* Stand in for any RLS-scoped read the previous session left behind. */
    qc.setQueryData(['collections'], [{ id: 1, name: "user-1's collection" }]);
    expect(qc.getQueryData(['collections'])).toBeDefined();

    for (const cb of h.listeners) cb('SIGNED_IN', sessionFor('user-2'));

    await waitFor(() => expect(qc.getQueryData(['collections'])).toBeUndefined());
  });

  it('does not clear the cache on the first sign-in of a session', async () => {
    h.initialSession = null;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderAuth(qc);
    qc.setQueryData(['market-data'], ['anon-readable rows']);

    for (const cb of h.listeners) cb('SIGNED_IN', sessionFor('user-1'));
    await new Promise((r) => setTimeout(r, 20));

    // null -> user is a login, not a switch: nothing cached belonged to anyone else.
    expect(qc.getQueryData(['market-data'])).toBeDefined();
  });
});
