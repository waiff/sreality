/* PresetBar — chip names.
 *
 * The "edited since loaded" bullet used to carry aria-label="edited", which is
 * injected into the enclosing load button's name ("Praha 2+kk edited"); the
 * kebab was named by its glyph "⋯" alone. This pins the chip's load button to
 * the preset NAME (state moved to the description) and the kebab to a
 * name-scoped label, so N chips no longer expose N identical "⋯" buttons.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { filterPresetKeys, DEFAULT_SORT } from '@/lib/queries';
import type { ListingFilters } from '@/lib/filters';
import type { FilterPreset } from '@/lib/types';

const preset: FilterPreset = {
  id: 'p1',
  name: 'Praha 2+kk',
  // A sort that can never equal the current one → the chip renders dirty.
  filter_spec: { filters: {} as ListingFilters, sort: 'not-the-current-sort' },
  created_at: '2026-05-12T10:00:00+00:00',
  updated_at: '2026-05-12T10:00:00+00:00',
  position: 0,
  color: null,
};

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  isApiConfigured: () => true,
  listFilterPresets: vi.fn(),
}));

import PresetBar from './PresetBar';

function renderBar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(filterPresetKeys.all, { data: [preset], total: 1 });
  return render(
    <QueryClientProvider client={qc}>
      <PresetBar
        filters={{} as ListingFilters}
        sort={DEFAULT_SORT}
        activePresetId="p1"
        onLoad={vi.fn()}
        onActivePresetIdChange={vi.fn()}
        onFiltersChange={vi.fn()}
        onLoadPipelineView={vi.fn()}
      />
    </QueryClientProvider>,
  );
}

describe('<PresetBar> chip names', () => {
  it('names the load button by the preset name, with "edited" as a description', () => {
    renderBar();
    const load = screen.getByRole('button', { name: 'Praha 2+kk' });
    expect(load).toHaveAccessibleName('Praha 2+kk');
    expect(load).toHaveAccessibleDescription('Edited since loaded');
  });

  it('names the kebab by what it opens, scoped to its preset', () => {
    renderBar();
    const kebab = screen.getByRole('button', { name: 'Preset options: Praha 2+kk' });
    expect(kebab).toHaveAttribute('aria-expanded', 'false');
  });
});
