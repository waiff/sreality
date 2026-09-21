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

import {
  fetchSoldComparables,
  fetchSoldCoverage,
  SOLD_COMPS_LIMIT,
} from '@/lib/queries';
import SoldCompsBlock from './SoldCompsBlock';

const comps = vi.mocked(fetchSoldComparables);
const coverage = vi.mocked(fetchSoldCoverage);

const LAT = 50.081234;
const LNG = 14.428765;

const COVERAGE: SoldCoverage = {
  obec_kod: 554782,
  obec_name: 'Praha',
  fetched_at: '2026-09-18T04:10:00+00:00',
  record_count: 89,
  source_total: 625,
  last_attempt_at: '2026-09-18T04:10:00+00:00',
  last_attempt_status: 'ok',
};

const sale = (over: Partial<SoldComparable> = {}): SoldComparable => ({
  source: 'reas',
  source_record_id: `t${Math.random()}`,
  sold_at: '2026-06-14',
  price_czk: 7_450_000,
  asking_last_czk: 7_900_000,
  listed_at: '2026-03-02T00:00:00+00:00',
  category_main: 'byt',
  category_type: 'prodej',
  subtype: null,
  disposition: '2+kk',
  area_m2: 62,
  area_basis: 'usable',
  address_text: 'Rašínovo nábřeží 12, Praha',
  photo_urls: ['https://cdn.reas.cz/a.jpg'],
  source_url: 'https://www.reas.cz/prodane/x',
  fetched_at: '2026-09-18T04:10:00+00:00',
  distance_m: 180,
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

    expect(await screen.findByText(/has not been checked yet/)).toBeInTheDocument();
    expect(screen.getByText(/deal pipeline has a live card/)).toBeInTheDocument();
  });

  /* The two numbers are different POPULATIONS — what reas publishes inside its
     24-month window, and what it says has ever been registered — so the line
     must never read as an ingest shortfall ("89 of 625"). */
  it('names the town, the window and the source ceiling', async () => {
    coverage.mockResolvedValue(COVERAGE);
    comps.mockResolvedValue([sale()]);
    renderBlock();

    const line = await screen.findByText(/reas\.cz · checked/);
    expect(line).toHaveTextContent('Praha');
    expect(line).toHaveTextContent('all 89 sales it publishes here from the last 24 months');
    expect(line).toHaveTextContent('625 have ever been registered here');
    expect(line).toHaveTextContent('about 30 days after the transfer');
    expect(line).toHaveTextContent(/minority of registered transfers/);
  });

  /* "We looked and this cell held nothing" is a DIFFERENT answer from "nobody
     has looked", and the two used to read identically as an empty table. */
  it('keeps the checked-on line when the cohort is empty', async () => {
    coverage.mockResolvedValue({ ...COVERAGE, record_count: 0, source_total: 0 });
    comps.mockResolvedValue([]);
    renderBlock();

    expect(await screen.findByText(/reas\.cz · checked/)).toBeInTheDocument();
    expect(screen.getByText(/No registered sale within 1 km/)).toBeInTheDocument();
    expect(screen.queryByText(/has not been checked yet/)).toBeNull();
  });

  /* A lane that only ever FAILED here is our outage. Telling the operator to
     add a pipeline card they may already hold would hide it. */
  it('says the fetch failed rather than blaming the pipeline', async () => {
    coverage.mockResolvedValue({
      ...COVERAGE,
      fetched_at: null,
      record_count: null,
      source_total: null,
      last_attempt_status: 'failed',
    });
    renderBlock();

    expect(await screen.findByText(/the fetch failed/)).toBeInTheDocument();
    expect(screen.queryByText(/has not been checked yet/)).toBeNull();
  });

  /* An unanswered coverage read is not "nobody has looked here" — that sentence
     is the one claim about the world this block makes from an absence. */
  it('claims nothing about coverage when the coverage read fails', async () => {
    coverage.mockRejectedValue(new Error('function sold_coverage does not exist'));
    comps.mockResolvedValue([]);
    renderBlock();

    expect(await screen.findByText(/Coverage unavailable/)).toBeInTheDocument();
    expect(screen.queryByText(/has not been checked yet/)).toBeNull();
  });

  /* A failed cohort read must never render as "there are no sales here", and
     the count beside the heading must not assert a 0 it does not have. */
  it('says the sales read failed, and shows no count for it', async () => {
    comps.mockRejectedValue(new Error('function sold_comparables does not exist'));
    coverage.mockResolvedValue(COVERAGE);
    renderBlock();

    expect(await screen.findByText(/Failed to load/)).toBeInTheDocument();
    expect(screen.queryByText(/No registered sale within/)).toBeNull();
    expect(screen.queryByText('(0)')).toBeNull();
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

  /* A 0–30 m² transfer's Kč/m² is a denominator defect, not a market fact
     (Prague median 239,600 against 144,506 at 60–80 m²). The ROW is still a
     fact and stays in the table; the aggregate is the claim, so it is the
     aggregate that holds them out — and says so. */
  it('holds sales under 30 m² out of the median, and keeps them in the table', async () => {
    comps.mockResolvedValue([
      sale({ area_m2: 18, price_per_m2: 353_778 }),
      sale({ area_m2: 62, price_per_m2: 100_000 }),
      sale({ area_m2: 64, price_per_m2: 110_000 }),
      sale({ area_m2: 66, price_per_m2: 120_000 }),
      sale({ area_m2: 68, price_per_m2: 130_000 }),
      sale({ area_m2: 70, price_per_m2: 140_000 }),
    ]);
    renderBlock();

    const line = await screen.findByText(/median/);
    expect(line).toHaveTextContent('120 000 Kč/m²');
    expect(line).toHaveTextContent('over 5 sales');
    expect(line).toHaveTextContent('1 under 30 m² held out');
    expect(await screen.findAllByRole('row')).toHaveLength(7);
  });

  /* Flats and houses trade at different Kč/m² (114,519 against 50,000), so one
     median over both is a statistic about neither. */
  it('refuses a median when the cohort mixes flats and houses', async () => {
    comps.mockResolvedValue([
      sale({ category_main: 'byt' }),
      sale({ category_main: 'byt' }),
      sale({ category_main: 'byt' }),
      sale({ category_main: 'dum' }),
      sale({ category_main: 'dum' }),
    ]);
    renderBlock();

    await screen.findByRole('table');
    expect(screen.getByText(/No median/)).toBeInTheDocument();
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

  /* The fetcher asks for one row past the cap, so a full page PROVES there is
     more. The extra row is never part of the cohort, and neither the count nor
     the median may present a page as the whole. */
  it('renders a full page as 200+, and says the median is of the nearest', async () => {
    comps.mockResolvedValue(
      Array.from({ length: SOLD_COMPS_LIMIT + 1 }, () => sale()),
    );
    renderBlock();

    await screen.findByRole('table');
    expect(screen.getByText(`(${SOLD_COMPS_LIMIT}+)`)).toBeInTheDocument();
    expect(screen.getByText(/median/)).toHaveTextContent(
      `the ${SOLD_COMPS_LIMIT} nearest, not the whole cohort`,
    );
    expect(screen.getAllByRole('row')).toHaveLength(SOLD_COMPS_LIMIT + 1);
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
