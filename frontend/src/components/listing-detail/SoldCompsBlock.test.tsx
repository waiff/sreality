/* The sold-comps block's honesty rules, which are the whole point of it.
 *
 * An empty table has three different causes and only one of them means "this
 * neighbourhood has no registered sales" — so the coverage sentence is pinned
 * here, as is the refusal to print a median off four rows, the refusal to print
 * a bare Kč/m², and the fact that the radius and the registry filters reach the
 * fetcher rather than being applied in the browser.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import type { SoldComparable, SoldCoverage } from '@/lib/types';

vi.mock('@/lib/queries', async (orig) => ({
  ...(await orig<typeof import('@/lib/queries')>()),
  fetchSoldComparables: vi.fn(),
  fetchSoldCoverage: vi.fn(),
}));

import { fetchSoldComparables, fetchSoldCoverage } from '@/lib/queries';
import SoldCompsBlock from './SoldCompsBlock';

const comps = vi.mocked(fetchSoldComparables);
const coverage = vi.mocked(fetchSoldCoverage);

const LAT = 50.081234;
const LNG = 14.428765;

const COVERAGE: SoldCoverage = {
  fetched_at: '2026-09-18T04:10:00+00:00',
  obec_kod: 554782,
  record_count: 89,
  source_total: 625,
};

const sale = (over: Partial<SoldComparable> = {}): SoldComparable => ({
  source: 'reas',
  source_record_id: `t${Math.random()}`,
  sold_at: '2026-06-14',
  price_czk: 7_450_000,
  asking_last_czk: 7_900_000,
  listed_at: '2026-03-02T00:00:00+00:00',
  published_at: '2026-07-14T00:00:00+00:00',
  category_main: 'byt',
  category_type: 'prodej',
  subtype: null,
  disposition: '2+kk',
  area_m2: 62,
  area_basis: 'usable',
  usable_area: 62,
  estate_area: null,
  lat: LAT,
  lng: LNG,
  address_text: 'Rašínovo nábřeží 12, Praha',
  obec_kod: 554782,
  ku_kod: null,
  ulice_kod: null,
  photo_urls: ['https://cdn.reas.cz/a.jpg'],
  source_url: 'https://www.reas.cz/prodane/x',
  fetched_at: '2026-09-18T04:10:00+00:00',
  distance_m: 180,
  sold_age_days: 99,
  price_per_m2: 120_161,
  price_per_m2_basis: 'sale_capital_czk_m2',
  ...over,
});

function renderBlock(categoryMain = 'byt') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SoldCompsBlock categoryMain={categoryMain} lat={LAT} lng={LNG} />
    </QueryClientProvider>,
  );
}

describe('<SoldCompsBlock> coverage states', () => {
  beforeEach(() => {
    comps.mockReset();
    coverage.mockReset();
    comps.mockResolvedValue([]);
    coverage.mockResolvedValue(null);
  });

  it('says nobody has ever looked here when there is no coverage row', async () => {
    renderBlock();

    expect(await screen.findByText(/Not checked yet/)).toBeInTheDocument();
    expect(screen.getByText(/deal pipeline has a live card/)).toBeInTheDocument();
  });

  it('says when we looked and how much of the cell we hold', async () => {
    coverage.mockResolvedValue(COVERAGE);
    comps.mockResolvedValue([sale()]);
    renderBlock();

    const line = await screen.findByText(/reas\.cz · checked/);
    expect(line).toHaveTextContent('89 of the 625 sales');
    expect(line).toHaveTextContent('about 30 days after the transfer');
  });

  /* "We looked and this cell held nothing" is a DIFFERENT answer from "nobody
     has looked", and the two used to read identically as an empty table. */
  it('keeps the checked-on line when the cohort is empty', async () => {
    coverage.mockResolvedValue({ ...COVERAGE, record_count: 0, source_total: 0 });
    comps.mockResolvedValue([]);
    renderBlock();

    expect(await screen.findByText(/reas\.cz · checked/)).toBeInTheDocument();
    expect(screen.getByText(/No registered sale within 1 km/)).toBeInTheDocument();
    expect(screen.queryByText(/Not checked yet/)).toBeNull();
  });
});

describe('<SoldCompsBlock> what it refuses to show', () => {
  beforeEach(() => {
    comps.mockReset();
    coverage.mockReset();
    coverage.mockResolvedValue(COVERAGE);
  });

  it('renders one honest line, and no query, for a listing reas does not cover', () => {
    renderBlock('pozemek');

    expect(screen.getByText(/flats and houses only/)).toBeInTheDocument();
    expect(comps).not.toHaveBeenCalled();
    expect(coverage).not.toHaveBeenCalled();
  });

  it('hides the summary under five sales and shows it at five', async () => {
    comps.mockResolvedValue([sale(), sale(), sale(), sale()]);
    const { unmount } = renderBlock();
    await screen.findByRole('table');
    expect(screen.queryByText(/median/)).toBeNull();
    unmount();

    comps.mockResolvedValue([
      sale({ price_per_m2: 100_000 }),
      sale({ price_per_m2: 110_000 }),
      sale({ price_per_m2: 120_000 }),
      sale({ price_per_m2: 130_000 }),
      sale({ price_per_m2: 140_000 }),
    ]);
    renderBlock();
    expect(await screen.findByText(/median/)).toHaveTextContent('120 000 Kč/m²');
  });

  /* A bare Kč/m² is 300x ambiguous between sale and rent, so the number is
     rendered only with the basis the server published for that row. */
  it('renders no Kč/m² for a row whose published basis is null', async () => {
    comps.mockResolvedValue([sale({ price_per_m2_basis: null })]);
    renderBlock();

    await screen.findByRole('table');
    expect(screen.queryByText(/Kč\/m²/)).toBeNull();
    expect(screen.getByText('7 450 000 Kč')).toBeInTheDocument();
  });
});

describe('<SoldCompsBlock> query plumbing', () => {
  beforeEach(() => {
    comps.mockReset();
    coverage.mockReset();
    coverage.mockResolvedValue(COVERAGE);
    comps.mockResolvedValue([sale()]);
  });

  it('asks for 1 km around the point, seeded with the subject kind', async () => {
    renderBlock('dum');

    await waitFor(() => expect(comps).toHaveBeenCalled());
    expect(comps).toHaveBeenCalledWith(LAT, LNG, 1000, { category_main_in: ['dum'] });
    expect(coverage).toHaveBeenCalledWith(LAT, LNG);
  });

  it('re-asks the server when the radius changes — no client-side narrowing', async () => {
    const user = userEvent.setup();
    renderBlock();
    await waitFor(() => expect(comps).toHaveBeenCalled());

    await user.click(screen.getByRole('button', { name: '3 km' }));

    await waitFor(() =>
      expect(comps).toHaveBeenCalledWith(LAT, LNG, 3000, { category_main_in: ['byt'] }),
    );
  });

  it('sends a registry filter to the server under its registry id', async () => {
    const user = userEvent.setup();
    renderBlock();
    await waitFor(() => expect(comps).toHaveBeenCalled());

    await user.type(screen.getByRole('textbox', { name: /Sold within/ }), '730');

    await waitFor(() =>
      expect(comps).toHaveBeenCalledWith(
        LAT,
        LNG,
        1000,
        expect.objectContaining({ max_sold_age_days: 730 }),
      ),
    );
  });
});
