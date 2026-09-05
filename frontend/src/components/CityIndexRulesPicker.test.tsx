/* CityIndexRulesPicker — accessible names for the three controls of a rule row.
 *
 * The row reads "<index> ≥ 7" on screen with no captions at all; only the
 * middle operator select carried a name before. All three are asserted here.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import type { CityIndexDefinition } from '@/lib/queries';

const DEFS: CityIndexDefinition[] = [
  {
    index_name: 'celkove_hodnoceni',
    label_cs: 'Celkové hodnocení',
    label_en: null,
    category: 'overall',
    scale_min: 0,
    scale_max: 10,
    higher_is_better: true,
    sort_order: 0,
  } as CityIndexDefinition,
  {
    index_name: 'pracovni_mista',
    label_cs: 'Index nabídky pracovních míst',
    label_en: null,
    category: 'material_edu',
    scale_min: 0,
    scale_max: 10,
    higher_is_better: true,
    sort_order: 1,
  } as CityIndexDefinition,
];

vi.mock('@/lib/queries', async (orig) => ({
  ...(await orig<typeof import('@/lib/queries')>()),
  fetchCityIndexDefinitions: vi.fn(),
}));

import { fetchCityIndexDefinitions } from '@/lib/queries';
import CityIndexRulesPicker from './CityIndexRulesPicker';

describe('<CityIndexRulesPicker> rule-row names', () => {
  it('names the index select, the operator select and the threshold input', async () => {
    vi.mocked(fetchCityIndexDefinitions).mockResolvedValue(DEFS);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <CityIndexRulesPicker
          value={[{ index_name: 'celkove_hodnoceni', op: '>=', value: 7 }]}
          onChange={() => {}}
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByRole('combobox', { name: 'City index' }))
      .toHaveAccessibleName('City index');
    expect(screen.getByRole('combobox', { name: 'Comparison operator' }))
      .toBeInTheDocument();
    expect(screen.getByRole('spinbutton', { name: 'Threshold value' }))
      .toHaveAccessibleName('Threshold value');
  });
});
