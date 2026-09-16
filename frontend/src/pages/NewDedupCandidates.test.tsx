/* NewDedupCandidates — the Wave 2 candidate audit page.
 *
 * Hermetic: only `getNewDedupCandidateOverview` is mocked; every number the page
 * shows is derived from the fixture below, so a change to how a figure is summed
 * shows up here as a changed expectation rather than silently.
 *
 * Pins, in the order the page can fail:
 *   * the THREE EMPTY STATES — no store, no run, a run with no statistics — are
 *     three different sentences, because they call for three different actions
 *     (apply the migration / dispatch the lane / read the run's error).
 *   * a full run renders the type × path matrix INCLUDING the unbuilt path
 *     columns as em dashes (an omitted column would hide the gap), the
 *     missing-data rows with count and share, and the top towns.
 *   * a null in the data renders as an em dash and never as a fabricated value.
 *   * the run picker round-trips through `?generation_id=` — the URL is what
 *     makes a run a link the operator can keep.
 * The backend contract is covered by tests/api/test_new_dedup_candidates.py;
 * this only checks the wiring and the arithmetic done in the browser.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import NewDedupCandidates from './NewDedupCandidates';
import * as api from '@/lib/api';
import { LOCATION_STEPS } from '@/lib/locationSteps';
import type * as waterfall from '@/lib/locationWaterfall';
import type {
  NewDedupCandidateGeneration,
  NewDedupCandidateOverview,
  NewDedupCandidatePath,
  NewDedupCandidateStats,
} from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    getNewDedupCandidateOverview: vi.fn(),
    getNewDedupCandidateListings: vi.fn(),
  };
});

/* The path registry as the backend hands it over: A and B present and unbuilt,
 * C built with its two rungs. */
const PATHS: NewDedupCandidatePath[] = [
  {
    code: 'A',
    label: 'street / geo / radius',
    built: false,
    block_key: null,
    explanation: 'Path A would pair listings that share a street or sit within so many metres.',
    rungs: [],
  },
  {
    code: 'B',
    label: 'image similarity',
    built: false,
    block_key: null,
    explanation: 'Path B would pair listings whose photographs look like the same rooms.',
    rungs: [],
  },
  {
    code: 'C',
    label: 'town + attributes',
    built: true,
    block_key: 'obec_kod',
    explanation: 'Path C blocks on the town and compares attributes.',
    rungs: [
      {
        code: 'C1',
        label: 'town + disposition',
        needs: ['disposition'],
        explanation: 'Same town and the same disposition.',
      },
      {
        code: 'C3',
        label: 'town + area',
        needs: ['area'],
        explanation: 'Same town and areas within the tolerance.',
      },
    ],
  },
];

const GENERATION: NewDedupCandidateGeneration = {
  id: 41,
  simulation_run_id: 7,
  inputs_id: 3,
  path: 'C',
  fingerprint: '0ec174f0693a2c01',
  status: 'success',
  created_at: '2026-09-10T09:00:00Z',
  started_at: '2026-09-10T09:00:05Z',
  completed_at: '2026-09-10T09:12:00Z',
  inputs: { path: 'C', l0_floor_tolerance: 2, l0_candidate_scope: 'all' },
  progress: null,
  error_message: null,
};

/* Deliberately small counts: Czech grouping inserts a non-breaking space above
 * 999, and a test that asserts on "1 500" is really asserting on ICU. */
/* The chain the LANE stamps (W16). The browser sums and subtracts nothing, so
 * these rows are the assertion: 800 listings → 700 judged → 630 with a location,
 * of which 600 are in a named town, 23 are ABROAD (an answer, printed as a split
 * and never as a loss) and 7 are a Czech point with no town. The town step's loss
 * is exactly 23 + 7 = 30. `located_town` is a CHAIN step here and a split on the
 * audit page — same key, same predicate, different role, declared in
 * location_data/location_steps.py. */
const WATERFALL: waterfall.WaterfallRow[] = [
  { step_key: 'all_listings', step_no: 1, sub_no: 0, kind: 'chain', parent_key: null, n: 800, lost: 0, share_pct: 100 },
  { step_key: 'with_verdict', step_no: 2, sub_no: 0, kind: 'chain', parent_key: null, n: 700, lost: 100, share_pct: 87.5 },
  { step_key: 'located', step_no: 3, sub_no: 0, kind: 'chain', parent_key: null, n: 630, lost: 70, share_pct: 78.75 },
  { step_key: 'located_foreign', step_no: 3, sub_no: 1, kind: 'split', parent_key: 'located', n: 23, lost: null, share_pct: 2.875 },
  { step_key: 'located_no_town', step_no: 3, sub_no: 2, kind: 'split', parent_key: 'located', n: 7, lost: null, share_pct: 0.875 },
  { step_key: 'located_town', step_no: 4, sub_no: 0, kind: 'chain', parent_key: null, n: 600, lost: 30, share_pct: 75 },
  { step_key: 'eligible', step_no: 5, sub_no: 0, kind: 'chain', parent_key: null, n: 556, lost: 44, share_pct: 69.5 },
  { step_key: 'paired', step_no: 6, sub_no: 0, kind: 'chain', parent_key: null, n: 210, lost: 346, share_pct: 26.25 },
];

const STATS: NewDedupCandidateStats = {
  matrix: [
    { rung: 'C1', category_main_lo: 'byt', category_main_hi: 'byt', category_type: 'prodej', pairs: 120, floor_checked: 90 },
    { rung: 'C3', category_main_lo: 'byt', category_main_hi: 'byt', category_type: 'prodej', pairs: 30, floor_checked: 10 },
    { rung: 'C3', category_main_lo: 'dum', category_main_hi: 'komercni', category_type: 'prodej', pairs: 7, floor_checked: 0 },
  ],
  pairs: { C1: 120, C3: 37, total: 157 },
  listings_with_candidates: [
    { category_main: 'byt', listings: 210 },
    { category_main: null, listings: 5 },
  ],
  towns_with_pairs: 12,
  top_towns: [
    { block_key: '554782', obec_name: 'Praha', C1: 100, C3: 20 },
    /* No name on the projection — the page must show a gap, not the code twice. */
    { block_key: '999999', obec_name: null, C1: 5, C3: 2 },
  ],
  /* `_distribution` walks every band edge, so the 0 band is always PRESENT — and on
   * a generation it is always 0, because the histogram is built from pair rows and a
   * town with no pair never enters them. The page must show that band as a gap. */
  distribution: [
    { pairs_from: 0, pairs_to: 0, towns: 0 },
    { pairs_from: 1, pairs_to: 10, towns: 8 },
    { pairs_from: 1000001, pairs_to: null, towns: 0 },
  ],
  funnel: [
    {
      source: 'sreality', category_main: 'byt', category_type: 'prodej',
      listings: 600, active: 500, with_verdict: 540, located: 500,
      located_town: 480, located_foreign: 15, located_no_town: 5,
      with_disposition: 420, with_area: 450, byt: 600, byt_with_floor: 360,
      c1_eligible: 420, c3_eligible: 36, town_no_attribute: 24,
    },
    {
      source: 'bazos', category_main: 'pozemek', category_type: 'prodej',
      listings: 200, active: 160, with_verdict: 160, located: 130,
      located_town: 120, located_foreign: 8, located_no_town: 2,
      with_disposition: 0, with_area: 100, byt: 0, byt_with_floor: 0,
      c1_eligible: 0, c3_eligible: 100, town_no_attribute: 20,
    },
  ],
  waterfall: WATERFALL,
  computed_at: '2026-09-15T06:30:00Z',
  top_buckets: [
    { obec_kod: '554782', obec_name: 'Praha', disposition: '2+kk', listings: 400, active: 300 },
    /* A bucket of listings that state no disposition at all. */
    { obec_kod: '582786', obec_name: 'Brno', disposition: null, listings: 120, active: 90 },
  ],
  partial: false,
  scope: 'all',
  pairs_upserted: 157,
  stale_deleted: 0,
  chunks_done: 4,
  seconds: 12.5,
};

const RECENT = [
  {
    id: 41, status: 'success', created_at: '2026-09-10T09:00:00Z',
    completed_at: '2026-09-10T09:12:00Z', fingerprint: '0ec174f0693a2c01',
    scope: 'all', partial: false, pairs_total: 157,
  },
  {
    id: 42, status: 'failed', created_at: '2026-09-10T11:00:00Z',
    completed_at: null, fingerprint: '0ec174f0693a2c01',
    scope: null, partial: null, pairs_total: null,
  },
];

function overview(patch: Partial<NewDedupCandidateOverview> = {}): NewDedupCandidateOverview {
  return {
    store_ready: true,
    paths: PATHS,
    generation: GENERATION,
    stats: STATS,
    recent: RECENT,
    ...patch,
  };
}

function renderPage(url = '/new-dedup/candidates') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <NewDedupCandidates />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('<NewDedupCandidates>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    /* The collapse state is deliberately persistent (a folded section stays
     * folded on the operator's next visit), so it leaks between cases unless
     * each one starts from a clean slate. */
    try {
      localStorage.clear();
    } catch {
      /* jsdom always has it; a browser with storage disabled does not */
    }
    vi.mocked(api.getNewDedupCandidateOverview).mockResolvedValue({ data: overview() });
    vi.mocked(api.getNewDedupCandidateListings).mockResolvedValue({
      store_ready: true,
      data: [
        {
          listing_id: 8801, property_id: 5, sreality_id: -8801, source: 'bazos',
          source_id_native: 'bz-8801', source_url: null, category_main: 'pozemek',
          category_type: 'prodej', disposition: null, area_m2: null, floor: null,
          price_czk: 1_200_000, is_active: true, first_seen_at: null, last_seen_at: null,
          display_label: null, obec_kod: null, granularity: 'obec', country_status: 'cz',
        },
      ],
      has_more: false,
      next_after_id: null,
    });
  });

  /* ------------------------------------------------------- empty states */

  it('says the store does not exist yet when the migration has not been applied', async () => {
    vi.mocked(api.getNewDedupCandidateOverview).mockResolvedValue({
      data: overview({ store_ready: false, generation: null, stats: null, recent: [] }),
    });
    renderPage();
    expect(
      await screen.findByText('The candidate store has not been created yet.'),
    ).toBeInTheDocument();
    /* The vocabulary is code, not data, so it still renders. */
    expect(screen.getByText('town + attributes')).toBeInTheDocument();
    /* And no funnel, no matrix — there is nothing to count. */
    expect(screen.queryByText('Property type × path')).not.toBeInTheDocument();
  });

  it('says no run has finished when the store is empty', async () => {
    vi.mocked(api.getNewDedupCandidateOverview).mockResolvedValue({
      data: overview({ generation: null, stats: null, recent: [] }),
    });
    renderPage();
    expect(await screen.findByText('No candidate run has finished yet.')).toBeInTheDocument();
    expect(screen.getByText('no runs to pick from yet')).toBeInTheDocument();
  });

  it('says a run produced no numbers, and shows why, when stats are missing', async () => {
    vi.mocked(api.getNewDedupCandidateOverview).mockResolvedValue({
      data: overview({
        generation: {
          ...GENERATION,
          id: 42,
          status: 'failed',
          error_message: 'statement timeout on town 554782',
        },
        stats: null,
      }),
    });
    renderPage();
    expect(await screen.findByText('This run produced no audit numbers.')).toBeInTheDocument();
    expect(screen.getByText('statement timeout on town 554782')).toBeInTheDocument();
    expect(screen.getByText('failed')).toBeInTheDocument();
  });

  /* --------------------------------------------------------- full render */

  it('renders the matrix with the unbuilt path columns present but empty', async () => {
    renderPage();
    await screen.findByText('Property type × path');

    /* Path A's and path B's columns are here from day one. */
    for (const code of ['A1', 'A2', 'A3', 'B', 'C1', 'C3']) {
      expect(screen.getByRole('columnheader', { name: code })).toBeInTheDocument();
    }

    /* The byt row (the biggest, so first): 120 pairs on C1, 30 on C3, four em
     * dashes for the unbuilt columns, 150 in total. */
    const matrix = screen.getByRole('columnheader', { name: 'A1' }).closest('table')!;
    const bytRow = within(matrix).getAllByRole('row')[1];
    const cells = within(bytRow).getAllByRole('cell').map((c) => c.textContent);
    expect(cells).toEqual(['Byt', 'Prodej', '—', '—', '—', '—', '120', '30', '150']);

    /* The one sanctioned cross-type pair renders both sides. */
    expect(screen.getByText(/Komerční prostor/)).toBeInTheDocument();

    /* The floor-rule footnote counts what could be checked, out of what there
     * was: 100 of 157. */
    expect(screen.getByText(/could be applied to/)).toHaveTextContent('100');
    expect(screen.getByText(/could be applied to/)).toHaveTextContent('157');
  });

  it('renders the overall missing-data rows with their count and share', async () => {
    renderPage();
    await screen.findByText('Missing data — overall');

    /* The floor row's denominator is apartments (600), not all listings — the
     * share column says so, and 240 of 600 is 40 %. The page emits the Czech
     * non-breaking space before the sign; Testing Library collapses whitespace
     * when it reads an element but not in the matcher, so the expectation is
     * written with a plain space. */
    const floors = screen.getByText('Apartments with no floor stated').closest('tr')!;
    expect(within(floors).getByText('240')).toBeInTheDocument();
    expect(within(floors).getByText('40,0 %')).toBeInTheDocument();
    expect(within(floors).getByText(/of apartments/)).toBeInTheDocument();
  });

  it('leaves the chain\'s own losses to the chain', async () => {
    renderPage();
    await screen.findByText('Missing data — overall');
    /* W16: these three rows restated the funnel's losses in different English,
     * and the middle one merged judged-but-not-located with ABROAD and with a
     * Czech point that has no town. The chain owns them now, split honestly. */
    expect(screen.queryByText('No answer from the location engine at all')).toBeNull();
    expect(screen.queryByText('An answer, but too coarse to name a town')).toBeNull();
    expect(screen.queryByText('A town, but neither a disposition nor an area')).toBeNull();
  });

  it('shows abroad as a split of "has a location", not as a loss', async () => {
    renderPage();
    const abroad = await screen.findByTestId('funnel-split-located_foreign');
    expect(abroad).toHaveTextContent(LOCATION_STEPS.located_foreign.en);
    expect(abroad).toHaveTextContent('23');
    expect(abroad).not.toHaveTextContent('Lost at this step');
    /* And the town step's loss is exactly abroad (23) plus the point without a
     * town (7) — one number the operator can now read as two answers. */
    const town = screen.getAllByText(LOCATION_STEPS.located_town.en)[0].closest('li')!;
    expect(town).toHaveTextContent('Lost at this step: 30');
  });

  it('sorts the per-portal table when a column heading is clicked', async () => {
    renderPage();
    const heading = await screen.findByText('Missing data — per portal, per property type');

    /* Scoped to THIS table's own <section>: the eligibility breakdown above it
     * also prints a portal column, and an unscoped row sweep would read both
     * tables as one and see every portal twice. */
    const card = heading.closest('section')!;
    const portalCells = () =>
      Array.from(card.querySelectorAll('tbody tr'))
        .map((r) => r.querySelector('td')?.textContent)
        .filter((t): t is string => t === 'sreality' || t === 'bazos');

    /* Default: most listings first. */
    expect(portalCells()).toEqual(['sreality', 'bazos']);
    fireEvent.click(screen.getByRole('button', { name: /Listings/ }));
    expect(portalCells()).toEqual(['bazos', 'sreality']);
  });

  /* The eligibility step's loss, split by property type and by portal. The two
   * tables are the SAME 44 listings counted twice — 24 sreality apartments and 20
   * bazos plots — so the assertions below pin both that each split sums to the
   * step's own loss and that the per-type note names the attribute that type
   * actually depends on. */
  it('breaks the eligibility loss down by property type, largest first', async () => {
    renderPage();
    await screen.findByText(/where “.*” lost its listings/);

    const byt = screen.getByTestId('eligibility-type-byt');
    const pozemek = screen.getByTestId('eligibility-type-pozemek');

    /* 24 of the 44 lost, and 24 of the 480 apartments that reached a town. */
    expect(byt).toHaveTextContent('24');
    expect(byt).toHaveTextContent('54,5 %');
    expect(byt).toHaveTextContent('5,0 %');
    /* Both halves of the rule exist for an apartment. */
    expect(byt).toHaveTextContent('Disposition, or floor area');
    /* Read from the run, not asserted: 420/600 state one, 450/600 state an area. */
    expect(byt).toHaveTextContent('70,0 %');
    expect(byt).toHaveTextContent('75,0 %');

    /* Land: 20 of 44, and 20 of the 120 plots that reached a town. */
    expect(pozemek).toHaveTextContent('20');
    expect(pozemek).toHaveTextContent('45,5 %');
    expect(pozemek).toHaveTextContent('16,7 %');
    /* The note that makes this table worth having: for land the area is not a
     * fallback, and the 0,0 % beside it is why. */
    expect(pozemek).toHaveTextContent('Plot area — the only route');
    expect(pozemek).toHaveTextContent('0,0 %');

    /* Largest loss first, so the row that costs most is read first. */
    const order = screen
      .getAllByTestId(/^eligibility-type-/)
      .map((el) => el.getAttribute('data-testid'));
    expect(order).toEqual(['eligibility-type-byt', 'eligibility-type-pozemek']);
  });

  it('breaks the same eligibility loss down by portal', async () => {
    renderPage();
    await screen.findByText(/where “.*” lost its listings/);

    expect(screen.getByTestId('eligibility-portal-sreality')).toHaveTextContent('24');
    expect(screen.getByTestId('eligibility-portal-bazos')).toHaveTextContent('20');

    /* The two splits are the same listings: each must sum to the step's own loss
     * of 44, which is the one number the lane measured. */
    const lostCell = (id: string) =>
      Number(
        screen
          .getByTestId(id)
          .querySelectorAll('td')
          [id.includes('-type-') ? 2 : 1].textContent!.replace(/\D/g, ''),
      );
    expect(lostCell('eligibility-type-byt') + lostCell('eligibility-type-pozemek')).toBe(44);
    expect(
      lostCell('eligibility-portal-sreality') + lostCell('eligibility-portal-bazos'),
    ).toBe(44);
  });

  /* THE DRILL-DOWN. A figure is a link to its own rows; the request carries the
   * BUCKET plus the slice the figure was counted over, which is the whole
   * contract between the page and the route. */
  it('asks for the listings behind the figure that was clicked, scoped to its row', async () => {
    renderPage();
    await screen.findByText(/where “.*” lost its listings/);

    const pozemek = screen.getByTestId('eligibility-type-pozemek');
    fireEvent.click(within(pozemek).getByRole('button', { name: '20' }));

    await waitFor(() =>
      expect(api.getNewDedupCandidateListings).toHaveBeenCalledWith(
        expect.objectContaining({ bucket: 'town_no_attribute', category_main: 'pozemek' }),
      ),
    );
    /* The row itself renders, and its listing links out by the natural key. */
    expect(await screen.findByTestId('drill-row-8801')).toHaveTextContent('bz-8801');
  });

  it('scopes a per-portal figure to that portal, type and deal at once', async () => {
    renderPage();
    await screen.findByText('Missing data — per portal, per property type');

    const card = screen.getByText('Missing data — per portal, per property type').closest('section')!;
    const row = within(card).getByText('bazos').closest('tr')!;
    /* bazos/pozemek: town_no_attribute is 20 in the fixture. */
    fireEvent.click(within(row).getByRole('button', { name: '20' }));

    await waitFor(() =>
      expect(api.getNewDedupCandidateListings).toHaveBeenCalledWith(
        expect.objectContaining({
          bucket: 'town_no_attribute',
          source: 'bazos',
          category_main: 'pozemek',
          category_type: 'prodej',
        }),
      ),
    );
  });

  it('never makes a zero clickable — there is nothing behind it', async () => {
    renderPage();
    await screen.findByText('Missing data — per portal, per property type');
    const card = screen.getByText('Missing data — per portal, per property type').closest('section')!;
    const row = within(card).getByText('bazos').closest('tr')!;
    /* bazos/pozemek states no disposition at all: c1_eligible is 0. */
    expect(within(row).queryByRole('button', { name: '0' })).toBeNull();
  });

  it('says the list is live while the figures are frozen, so the two need not tally', async () => {
    renderPage();
    const card = (await screen.findByText('The listings behind a figure')).closest('section')!;
    expect(card).toHaveTextContent(/frozen when the run executed/);
    expect(card).toHaveTextContent(/reads the database as it is now/);
  });

  /* COLLAPSIBLE, with the shared mechanism. The lede stays readable when the
   * body is folded — a section that hid its own explanation would be worse
   * closed than absent. */
  it('folds a section away while keeping its explanation on screen', async () => {
    renderPage();
    const heading = await screen.findByRole('button', { name: /Town statistics/ });
    const card = heading.closest('section')!;
    const body = card.querySelector('#card-body-town-stats') as HTMLElement;

    expect(heading).toHaveAttribute('aria-expanded', 'true');
    expect(body.hidden).toBe(false);

    fireEvent.click(heading);
    expect(heading).toHaveAttribute('aria-expanded', 'false');
    expect(body.hidden).toBe(true);
    /* The explanation is OUTSIDE the folded body, so it survives the fold.
     * Asserted structurally: `toHaveTextContent` reads hidden nodes too, so a
     * text assertion here would pass even if the paragraph were inside. */
    const lede = within(card).getByText(/Path C compares two listings only when they sit/i);
    expect(body.contains(lede)).toBe(false);
  });

  it('renders the town statistics, showing an em dash where a name is missing', async () => {
    renderPage();
    await screen.findByText('Town statistics');

    const topTowns = screen.getByRole('columnheader', { name: 'C1 pairs' }).closest('table')!;
    expect(within(topTowns).getByText('Praha')).toBeInTheDocument();
    /* The unnamed town: its code is shown, its name is a gap. */
    const unnamed = within(topTowns).getByText('999999').closest('tr')!;
    expect(within(unnamed).getAllByRole('cell')[0]).toHaveTextContent('—');
    /* …and a generation row carries no per-town listing count, so that cell is a
     * gap too — never a zero, which would claim the town holds nothing. */
    expect(within(unnamed).getAllByRole('cell')[2]).toHaveTextContent('—');

    /* The histogram's open-ended top band (Czech grouping uses a non-breaking
     * space, so the assertion matches on any whitespace). */
    expect(screen.getByText(/1\s000\s001\+/)).toBeInTheDocument();

    /* The 0 band is not a measurement — towns that produced no pair never reach
     * this histogram — so its count is a gap, while a real band prints its count. */
    const histogram = screen
      .getByText('How many towns produced how many pairs')
      .closest('div')!;
    const bands = within(histogram).getAllByRole('listitem');
    /* Band 0: the label is "0", the count beside it is an em dash. */
    expect(within(bands[0]).getByText('—')).toBeInTheDocument();
    /* Band 1–10: a real measurement, printed. */
    expect(bands[1]).toHaveTextContent('8');
    expect(within(bands[1]).queryByText('—')).toBeNull();
    /* …and the lede says why the 0 band is blank. */
    expect(
      within(histogram).getByText(/Towns that produced no pair at all are not\s+counted here/),
    ).toBeInTheDocument();

    /* A bucket whose listings state no disposition at all. */
    const buckets = screen.getByRole('columnheader', { name: 'Disposition' }).closest('table')!;
    const brno = within(buckets).getByText('Brno').closest('tr')!;
    expect(within(brno).getAllByRole('cell')[1]).toHaveTextContent('—');

    /* The town-assignment panel went with W2-b: it keyed on
     * `admin_assignment_method`, which the answer table does not carry. */
    expect(screen.queryByRole('columnheader', { name: 'Method' })).toBeNull();
  });

  /* ------------------------------------------------------- the run picker */

  it('reads the run out of the URL and writes the operator’s pick back into it', async () => {
    renderPage('/new-dedup/candidates?generation_id=41');
    await waitFor(() =>
      expect(api.getNewDedupCandidateOverview).toHaveBeenCalledWith(41),
    );

    fireEvent.change(await screen.findByRole('combobox', { name: 'Run' }), {
      target: { value: '42' },
    });
    await waitFor(() =>
      expect(api.getNewDedupCandidateOverview).toHaveBeenCalledWith(42),
    );

    /* Clearing it goes back to "newest finished run" — null, not a number. */
    fireEvent.change(screen.getByRole('combobox', { name: 'Run' }), {
      target: { value: '' },
    });
    await waitFor(() =>
      expect(api.getNewDedupCandidateOverview).toHaveBeenCalledWith(null),
    );
  });

  it('ignores a URL that does not name an integer run', async () => {
    renderPage('/new-dedup/candidates?generation_id=latest');
    await waitFor(() =>
      expect(api.getNewDedupCandidateOverview).toHaveBeenCalledWith(null),
    );
  });

  it('warns that a partial run\'s funnel is not comparable with its pair count', async () => {
    vi.mocked(api.getNewDedupCandidateOverview).mockResolvedValue({
      data: overview({ stats: { ...STATS, partial: true, only: ['554499', '599051'] } }),
    });
    renderPage();
    expect(
      await screen.findByText(/covered only part of the country/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/2 towns/)).toBeInTheDocument();
  });

  it('says nothing about partial runs when the run covered everything', async () => {
    renderPage();
    await screen.findByText(/Candidate pairs found/i);
    expect(screen.queryByText(/covered only part of the country/i)).toBeNull();
  });
});
