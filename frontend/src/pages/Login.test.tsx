/* The post-login round trip must hand back the WHOLE destination.
 *
 * RequireAuth (components/guards.tsx) stashes `state={{ from: location }}` —
 * the full Location — but Login used to read only `.pathname`. A logged-out
 * session opening a shared Browse cohort (filters live entirely in the query
 * string) or a run deep-link (#estimations) authenticated successfully and then
 * landed on a bare /browse with no error. Both consumers dropped it: the
 * post-sign-in navigate() and the already-authed <Navigate>. */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, createPath, useLocation } from 'react-router-dom';

import Login from './Login';
import * as auth from '@/lib/auth';
import { ROUTES } from '@/lib/routes';

vi.mock('@/lib/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/auth')>()),
  useAuth: vi.fn(),
}));

const signInWithPassword = vi.fn();

function asAuth(session: unknown) {
  vi.mocked(auth.useAuth).mockReturnValue({
    session,
    loading: false,
    signInWithPassword,
    signUpWithPassword: vi.fn(),
    signInWithGoogle: vi.fn(),
  } as unknown as ReturnType<typeof auth.useAuth>);
}

/* Reports wherever the app ended up, in full — pathname + search + hash. */
function Sink() {
  return <div data-testid="landed">{createPath(useLocation())}</div>;
}

function renderLogin(fromState: unknown) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: '/login', state: fromState }]}>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="*" element={<Sink />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  signInWithPassword.mockReset().mockResolvedValue(undefined);
});

const COHORT = { pathname: '/browse', search: '?deal=prodej&city=554782', hash: '' };
const RUN_LINK = { pathname: '/listing/-284913', search: '?run=99', hash: '#estimations' };

describe('<Login> already-authed deep link', () => {
  it('restores the full query string, not just the pathname', async () => {
    asAuth({ user: { id: 'u1' } });
    renderLogin({ from: COHORT });
    expect(await screen.findByTestId('landed')).toHaveTextContent(
      '/browse?deal=prodej&city=554782',
    );
  });

  it('restores the hash of a run deep-link', async () => {
    asAuth({ user: { id: 'u1' } });
    renderLogin({ from: RUN_LINK });
    expect(await screen.findByTestId('landed')).toHaveTextContent(
      '/listing/-284913?run=99#estimations',
    );
  });

  it('falls back to the registry Browse route when nothing was stashed', async () => {
    asAuth({ user: { id: 'u1' } });
    renderLogin(null);
    expect(await screen.findByTestId('landed')).toHaveTextContent(ROUTES.browse.build());
  });
});

describe('<Login> post-sign-in redirect', () => {
  async function signIn() {
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'a@b.cz' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'pw' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await waitFor(() => expect(signInWithPassword).toHaveBeenCalled());
  }

  it('carries the whole stashed location through the sign-in', async () => {
    asAuth(null);
    renderLogin({ from: RUN_LINK });
    await signIn();
    // The mocked auth never flips `session`, so the component stays mounted and
    // the imperative navigate() is what moves the router — which is exactly the
    // consumer under test (Login's navigate(from, { replace: true })).
    expect(await screen.findByTestId('landed')).toHaveTextContent(
      '/listing/-284913?run=99#estimations',
    );
  });

  it('still lands on Browse when there was no stashed location', async () => {
    asAuth(null);
    renderLogin(null);
    await signIn();
    expect(await screen.findByTestId('landed')).toHaveTextContent(ROUTES.browse.build());
  });
});
