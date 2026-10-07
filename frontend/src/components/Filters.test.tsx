/* The Browse sidebar's two curation pickers (MS16): a failed read says so and
 * reads again, never "No collections yet" or "No tags yet". */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { CollectionsPicker } from './Filters';
import { TagPicker } from './filter-controls/TagPicker';
import * as api from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  listCollections: vi.fn(),
  listTags: vi.fn(),
}));

const renderPicker = (ui: React.ReactElement) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      {ui}
    </QueryClientProvider>,
  );

describe('Browse sidebar curation pickers', () => {
  it('says the collections failed instead of "none yet", and reads them again', async () => {
    vi.mocked(api.listCollections)
      .mockRejectedValueOnce(new Error('HTTP 500'))
      .mockResolvedValue({ data: [{ id: 7, name: 'Šortlist' }], total: 1 } as never);
    renderPicker(<CollectionsPicker value={null} onChange={() => {}} />);
    const retry = await screen.findByRole('button', { name: /Kolekce se nepodařilo načíst/ });
    expect(screen.queryByText(/No collections yet/)).toBeNull();
    fireEvent.click(retry);
    expect(await screen.findByRole('button', { name: 'Šortlist' })).toBeInTheDocument();
  });

  it('says the tags failed instead of "none yet", and reads them again', async () => {
    vi.mocked(api.listTags)
      .mockRejectedValueOnce(new Error('HTTP 500'))
      .mockResolvedValue({ data: [{ id: 3, name: 'k prohlídce', color: 'copper' }] } as never);
    renderPicker(<TagPicker value={null} onChange={() => {}} />);
    const retry = await screen.findByRole('button', { name: /Štítky se nepodařilo načíst/ });
    expect(screen.queryByText(/No tags yet/)).toBeNull();
    fireEvent.click(retry);
    expect(await screen.findByText('k prohlídce')).toBeInTheDocument();
  });
});
