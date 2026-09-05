/* Collections — the curated-lists page.
 *
 * Pins the new-collection form's two text fields, which used to be named by a
 * placeholder only (a placeholder is not an accessible name, and it disappears
 * the moment the operator types). The monitoring checkbox was already wrapped
 * in its own <label>, so it is asserted here as the control that must NOT have
 * changed.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import Collections from './Collections';
import * as api from '@/lib/api';

vi.mock('@/lib/api', () => ({
  listCollections: vi.fn(),
  createCollection: vi.fn(),
  deleteCollection: vi.fn(),
}));

const renderPage = () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Collections />
      </MemoryRouter>
    </QueryClientProvider>,
  );
};

describe('<Collections>', () => {
  beforeEach(() => {
    vi.mocked(api.listCollections).mockResolvedValue({ data: [], total: 0 });
  });

  it('names both new-collection fields and leaves the monitoring checkbox named', async () => {
    renderPage();
    const name = await screen.findByRole('textbox', { name: 'Name' });
    expect(name).toHaveAccessibleName('Name');
    expect(screen.getByRole('textbox', { name: 'Description' })).toBeInTheDocument();
    expect(
      screen.getByRole('checkbox', { name: /Monitor changes/ }),
    ).toBeInTheDocument();
  });

  it('submits what was typed into the named fields', async () => {
    vi.mocked(api.createCollection).mockResolvedValue({} as never);
    renderPage();
    fireEvent.change(await screen.findByRole('textbox', { name: 'Name' }), {
      target: { value: 'Shortlist' },
    });
    fireEvent.change(screen.getByRole('textbox', { name: 'Description' }), {
      target: { value: 'the good ones' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));
    await waitFor(() =>
      expect(api.createCollection).toHaveBeenCalledWith({
        name: 'Shortlist',
        description: 'the good ones',
        monitoring_enabled: false,
      }),
    );
  });
});
