/* BuildingUnitEditor — accessible names for the per-unit table cells.
 *
 * Every editor in the table repeats once per unit row, so the name has to
 * carry the row's unit id as well as the column word — otherwise N rows all
 * announce "Notes". The column <th> is NOT used by browsers to name a control,
 * which is why these are asserted against the rendered DOM.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import type { BuildingRun } from '@/lib/types';

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  confirmBuildingUnits: vi.fn(),
}));

import BuildingUnitEditor from './BuildingUnitEditor';

const building = {
  id: 7,
  status: 'awaiting_input',
  units: [
    {
      unit_id: 'u1', label: 'flat 1', floor: '1', area_m2: 60,
      disposition: '2+kk', condition: 'dobry', is_potential: false,
      source: 'both', notes: null,
    },
    {
      unit_id: 'u2', label: 'flat 2', floor: '2', area_m2: 80,
      disposition: '3+kk', condition: null, is_potential: false,
      source: 'both', notes: null,
    },
  ],
  units_proposal: null,
} as unknown as BuildingRun;

function renderEditor() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <BuildingUnitEditor building={building} onConfirmed={() => {}} />
    </QueryClientProvider>,
  );
}

describe('<BuildingUnitEditor> cell names', () => {
  it('names every text cell by its column and unit id', () => {
    renderEditor();
    for (const unit of ['u1', 'u2']) {
      for (const col of ['Label', 'Floor', 'Disposition', 'Notes']) {
        expect(
          screen.getByRole('textbox', { name: `${col} — ${unit}` }),
        ).toBeInTheDocument();
      }
    }
  });

  it('names the area cell and the condition select per unit', () => {
    renderEditor();
    expect(screen.getByRole('spinbutton', { name: 'Area m² — u1' }))
      .toHaveAccessibleName('Area m² — u1');
    expect(screen.getByRole('spinbutton', { name: 'Area m² — u2' }))
      .toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Condition — u1' }))
      .toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Condition — u2' }))
      .toBeInTheDocument();
  });
});
