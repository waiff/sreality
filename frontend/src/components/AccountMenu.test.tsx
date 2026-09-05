/* The "Sign in" affordance is a destination, and the component's own comment
 * always called it a link — it was a <button> calling navigate('/login'). */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import AccountMenu from './AccountMenu';
import * as auth from '@/lib/auth';
import { ROUTES } from '@/lib/routes';

vi.mock('@/lib/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/auth')>()),
  useAuth: vi.fn(),
}));

function renderAs(user: unknown) {
  vi.mocked(auth.useAuth).mockReturnValue({
    user,
    loading: false,
    isAdmin: false,
    session: null,
    signOut: vi.fn(),
  } as unknown as ReturnType<typeof auth.useAuth>);
  return render(
    <MemoryRouter>
      <AccountMenu />
    </MemoryRouter>,
  );
}

describe('<AccountMenu>', () => {
  it('renders Sign in as an anchor to the login route', () => {
    renderAs(null);
    const link = screen.getByRole('link', { name: 'Sign in' });
    expect(link).toHaveAttribute('href', ROUTES.login.build());
  });

  it('does not render Sign in as a button', () => {
    renderAs(null);
    expect(screen.queryByRole('button', { name: 'Sign in' })).toBeNull();
  });
});
