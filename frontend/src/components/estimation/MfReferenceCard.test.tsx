/* The MF card renders the ONE result by its shape — value, range + (i) note,
 * note, none — and holds no reason text of its own. The notes here are
 * stand-ins: the real sentences exist once, in the SQL that returns them. */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import type { ReferenceRent } from '@/lib/mfReference';
import { MfReferenceCard } from './MfReferenceCard';

/* Today's stored breakdown: no `status` key at all — must read as a value. */
const STORED: ReferenceRent = {
  territory: { ruian_code: 698903, level: 'ku', name: 'Moravské Budějovice', kraj: 'Kraj Vysočina' },
  vk: 3,
  is_novostavba: false,
  source_revision: 2,
  base_per_m2: 184,
  adjustments: [
    { attribute: 'balcony', czk_per_m2: 5 },
    { attribute: 'elevator', czk_per_m2: 47 },
  ],
  adjustments_sum_per_m2: 52,
  total_per_m2: 236,
  area_m2: 75,
  monthly_rent_czk: 17_700,
};

const NOTE_COARSE = '(the range note, from SQL)';

/* A town-level location in a town priced per katastr: no value, the range.
 * Its per-m² ends are TOTAL rates — the published 203–232 plus this flat's
 * 5 Kč balcony — so each rent is its total × area (208 × 75, 237 × 75). */
const RANGE = {
  territory: { ruian_code: 586846, level: 'obec', name: 'Jihlava', kraj: 'Kraj Vysočina' },
  vk: 3,
  is_novostavba: false,
  source_revision: 3,
  base_per_m2: null,
  adjustments: [{ attribute: 'balcony', czk_per_m2: 5 }],
  total_per_m2: null,
  area_m2: 75,
  monthly_rent_czk: null,
  status: 'territory_coarse',
  note: NOTE_COARSE,
  range: {
    per_m2_min: 208,
    per_m2_max: 237,
    rent_min_czk: 15_600,
    rent_max_czk: 17_775,
    yield_min_pct: 3.6,
    yield_max_pct: 4.1,
  },
} as unknown as ReferenceRent;

const NOTE_ONLY = {
  status: 'no_rent_cell',
  note: '(a reason note, from SQL)',
  monthly_rent_czk: null,
} as unknown as ReferenceRent;

describe('MfReferenceCard', () => {
  it('renders a stored breakdown with no status as a value, with the property yield', () => {
    const { container } = render(<MfReferenceCard refRent={STORED} yieldPct={5.25} />);
    expect(container).toHaveTextContent('17 700 Kč/měs');
    expect(container).toHaveTextContent('hrubý výnos 5,25 %');
    expect(container).toHaveTextContent('Nájemné referenčního bytu184 Kč/m²/měs');
    expect(container).toHaveTextContent('+ výtah+47 Kč/m²/měs');
    expect(container).toHaveTextContent('Celkem za m²236 Kč/m²/měs');
    expect(container).toHaveTextContent('× plocha 75 m²17 700 Kč');
    expect(container).toHaveTextContent('Moravské Budějovice, Kraj Vysočina · VK3 · Ministerstvo financí');
    expect(screen.queryByRole('img')).toBeNull();
  });

  it('renders a value without a yield when none is given (a rental flat)', () => {
    const { container } = render(<MfReferenceCard refRent={STORED} />);
    expect(container).toHaveTextContent('17 700 Kč/měs');
    expect(container).not.toHaveTextContent('hrubý výnos');
  });

  it('keeps a frozen run readable, source date included', () => {
    const run = { ...STORED, source_date: '2026-05-01' };
    const { container } = render(<MfReferenceCard refRent={run} />);
    expect(container).toHaveTextContent('Ministerstvo financí (2026-05-01)');
  });

  it('renders a range with its note behind the (i), and the range yield', () => {
    const { container } = render(<MfReferenceCard refRent={RANGE} yieldPct={9.99} />);
    expect(container).toHaveTextContent('15 600–17 775 Kč/měs');
    expect(container).toHaveTextContent('+ balkón+5 Kč/m²/měs');
    expect(container).toHaveTextContent('Celkem za m²208–237 Kč/m²/měs');
    expect(container).toHaveTextContent('× plocha 75 m²15 600–17 775 Kč');
    expect(container).toHaveTextContent('hrubý výnos 3,60–4,10 %');
    // The range carries its own yields; the value yield is not borrowed.
    expect(container).not.toHaveTextContent('9,99');
    const hint = screen.getByRole('img', { name: NOTE_COARSE });
    expect(hint).toHaveAttribute('title', NOTE_COARSE);
    // A range is not a value: no reference-flat base, no single rent.
    expect(container).not.toHaveTextContent('Nájemné referenčního bytu');
  });

  it('renders a range without yields for a rental flat', () => {
    const rental = {
      ...RANGE,
      range: { ...RANGE.range!, yield_min_pct: null, yield_max_pct: null },
    } as ReferenceRent;
    const { container } = render(<MfReferenceCard refRent={rental} />);
    expect(container).toHaveTextContent('15 600–17 775 Kč/měs');
    expect(container).not.toHaveTextContent('hrubý výnos');
  });

  it('renders a reason as the note alone', () => {
    const { container } = render(<MfReferenceCard refRent={NOTE_ONLY} yieldPct={4} />);
    expect(container).toHaveTextContent('Odhad nájmu · cenová mapa MF');
    expect(container).toHaveTextContent(NOTE_ONLY.note!);
    expect(container).not.toHaveTextContent('Kč');
    expect(container).not.toHaveTextContent('hrubý výnos');
  });

  it('renders nothing when there is no result', () => {
    expect(render(<MfReferenceCard refRent={null} />).container).toBeEmptyDOMElement();
    const empty = {} as ReferenceRent;
    expect(render(<MfReferenceCard refRent={empty} />).container).toBeEmptyDOMElement();
  });
});
