/* NewDedupDashboard — the NEW DEDUP program's front page.
 *
 * Hermetic: the candidate overview and the settings registry are both mocked.
 *
 * Pins:
 *   * the FUNNEL renders the same five narrowing steps the Candidates page
 *     shows, off the same shared component and the same generation stats — one
 *     component so the two pages can never disagree about how many listings the
 *     program can reach;
 *   * the funnel's last step is split BY PROPERTY TYPE and BY RUNG, because the
 *     wave text asks it to end "by type and path" and the dashboard has no
 *     matrix underneath to carry the type half;
 *   * the COST TABLE reads the two spend knobs out of the settings registry, and
 *     says "not yet" — never a zero — wherever nothing has been measured or the
 *     registry has no such knob. A zero would read as "we ran it and it was
 *     free", which is the one wrong answer here;
 *   * with no finished run, the funnel says so instead of rendering an all-zero
 *     shape.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import NewDedupDashboard from './NewDedupDashboard';
import * as api from '@/lib/api';
import { LOCATION_STEPS } from '@/lib/locationSteps';
import type * as waterfall from '@/lib/locationWaterfall';
import type {
  NewDedupCandidateOverview,
  NewDedupCandidateStats,
  NewDedupSetting,
} from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    getNewDedupCandidateOverview: vi.fn(),
    listNewDedupSettings: vi.fn(),
  };
});

/* Counts under 1 000, so no Czech group separator gets into an assertion. */
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
  matrix: [],
  pairs: { C1: 120, C3: 37, total: 157 },
  listings_with_candidates: [
    { category_main: 'byt', listings: 180 },
    { category_main: 'dum', listings: 30 },
  ],
  towns_with_pairs: 12,
  top_towns: [],
  distribution: [],
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
  top_buckets: [],
  scope: 'all',
  partial: false,
};

const OVERVIEW: NewDedupCandidateOverview = {
  store_ready: true,
  paths: [],
  generation: {
    id: 41,
    simulation_run_id: 7,
    inputs_id: 3,
    path: 'C',
    fingerprint: '0ec174f0693a2c01',
    status: 'success',
    created_at: '2026-09-10T09:00:00Z',
    started_at: '2026-09-10T09:00:05Z',
    completed_at: '2026-09-10T09:12:00Z',
    inputs: {},
    progress: null,
    error_message: null,
  },
  stats: STATS,
  recent: [],
};

function setting(key: string, value: unknown, valueType: NewDedupSetting['value_type']): NewDedupSetting {
  return {
    key,
    category: 'l3_embeddings',
    value_type: valueType,
    value,
    default: value,
    is_override: false,
    decided: true,
    explanation: '',
    enum_choices: null,
    minimum: null,
    maximum: null,
  };
}

const SETTINGS: NewDedupSetting[] = [
  setting('l3_runpod_daily_cost_cap_usd', 1, 'numeric'),
  setting('l4_vision_model', 'gpt-5-mini', 'text'),
];

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <NewDedupDashboard />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('<NewDedupDashboard>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getNewDedupCandidateOverview).mockResolvedValue({ data: OVERVIEW });
    vi.mocked(api.listNewDedupSettings).mockResolvedValue({ data: SETTINGS });
  });

  it('renders the chain the lane stamped, in the shared vocabulary', async () => {
    renderPage();
    await screen.findByText(LOCATION_STEPS.all_listings.en);

    /* Every label is read from the ONE wording module — a step renamed there is
     * renamed on the audit page and here at once, and this test cannot pass with
     * a name only one page uses. The counts are the lane's, not a sum. */
    const step = (key: keyof typeof LOCATION_STEPS) =>
      screen.getByText(LOCATION_STEPS[key].en).closest('li')!;
    expect(step('all_listings')).toHaveTextContent('800');
    expect(step('with_verdict')).toHaveTextContent('700');
    expect(step('located')).toHaveTextContent('630');
    expect(step('located_town')).toHaveTextContent('600');
    expect(step('eligible')).toHaveTextContent('556');
    expect(step('paired')).toHaveTextContent('210');

    /* The loss comes from the payload, never from a subtraction here. */
    expect(step('with_verdict')).toHaveTextContent('Lost at this step: 100');
    expect(step('located_town')).toHaveTextContent('Lost at this step: 30');
  });

  it('prints abroad as an answer under "has a location", never as a loss', async () => {
    renderPage();
    const abroad = await screen.findByTestId('funnel-split-located_foreign');
    expect(abroad).toHaveTextContent(LOCATION_STEPS.located_foreign.en);
    expect(abroad).toHaveTextContent('23');
    expect(abroad).not.toHaveTextContent('Lost at this step');
    expect(screen.getByTestId('funnel-split-located_no_town')).toHaveTextContent('7');
    /* 23 + 7 = 30, which is exactly what the town step below says it lost. */
    expect(screen.getByText(LOCATION_STEPS.located_town.en).closest('li')!).toHaveTextContent(
      'Lost at this step: 30',
    );
  });

  it('says which listings the run looked at and when it counted them', async () => {
    renderPage();
    /* The two honest differences from the hourly audit page, printed rather than
     * left for the operator to discover as a contradiction. */
    const asOf = await screen.findByText(/frozen with the run/);
    expect(asOf).toHaveTextContent('every listing ever collected, active or delisted');
    /* And the moment it was counted, so "819,770 here vs 821,193 there" reads as
     * two clocks rather than as a contradiction. */
    expect(asOf).toHaveTextContent('2026');
  });

  it('renders a run that predates the shared steps as a gap, never a zero', async () => {
    const { waterfall: _drop, ...older } = STATS;
    vi.mocked(api.getNewDedupCandidateOverview).mockResolvedValue({
      data: { ...OVERVIEW, stats: older },
    });
    renderPage();
    expect(
      await screen.findByText(/predates the shared steps/),
    ).toBeInTheDocument();
    expect(screen.queryByText(LOCATION_STEPS.all_listings.en)).toBeNull();
  });

  it('still totals the pairs by rung under the chain', async () => {
    renderPage();
    expect(await screen.findByText(/Candidate pairs found/)).toHaveTextContent('157');
    expect(screen.getByText(/disposition rung/)).toHaveTextContent('120');
    expect(screen.getByText(/area rung/)).toHaveTextContent('37');
  });

  it('splits the last funnel step by property type, not only by rung', async () => {
    renderPage();
    /* "by type and path" — the dashboard has no matrix under the funnel, so the
     * type half of that requirement lives in the funnel or nowhere. */
    const paired = (await screen.findByText(LOCATION_STEPS.paired.en))
      .closest('li')!;
    const byType = within(paired).getByText('By property type:').closest('p')!;
    expect(byType).toHaveTextContent('Byt');
    expect(byType).toHaveTextContent('180');
    expect(byType).toHaveTextContent('Dům');
    expect(byType).toHaveTextContent('30');
    /* The headline stays the sum of those rows. */
    expect(paired).toHaveTextContent('210');
  });

  it('shows the two spend knobs and says "not yet" for every unmeasured cell', async () => {
    renderPage();
    /* Wait for a cell the SETTINGS query fills: the table's chrome renders
     * before that query resolves, so finding the row is not enough. */
    await screen.findByText('gpt-5-mini');
    const gpuRow = screen.getByText('L3 · Embeddings').closest('tr')!;
    const visionRow = screen.getByText('L4 · Vision').closest('tr')!;

    /* The knobs come from the settings registry, never from a constant here. */
    expect(within(gpuRow).getByText('$1.00 / day')).toBeInTheDocument();
    expect(within(visionRow).getByText('gpt-5-mini')).toBeInTheDocument();

    /* Spent so far and Projected — neither level has run. */
    expect(within(gpuRow).getAllByText('not yet')).toHaveLength(2);
    expect(within(visionRow).getAllByText('not yet')).toHaveLength(2);
  });

  it('says "not yet" for a knob the settings registry does not carry', async () => {
    vi.mocked(api.listNewDedupSettings).mockResolvedValue({ data: [] });
    renderPage();
    await waitFor(() => expect(api.listNewDedupSettings).toHaveBeenCalled());
    const gpuRow = (await screen.findByText('L3 · Embeddings')).closest('tr')!;
    /* Setting, spent and projected — three gaps, no invented cap. */
    await waitFor(() => expect(within(gpuRow).getAllByText('not yet')).toHaveLength(3));
  });

  it('says nothing has been counted when no run has finished', async () => {
    vi.mocked(api.getNewDedupCandidateOverview).mockResolvedValue({
      data: { ...OVERVIEW, generation: null, stats: null },
    });
    renderPage();
    expect(
      await screen.findByText('No candidate run has finished yet, so there is nothing to count.'),
    ).toBeInTheDocument();
    expect(screen.queryByText(LOCATION_STEPS.all_listings.en)).not.toBeInTheDocument();
  });

  it('names the wave being built', async () => {
    renderPage();
    await screen.findByText('Where the program is');
    const wave2 = screen.getByText('W2').closest('li')!;
    expect(wave2).toHaveTextContent('being built');
    expect(wave2).toHaveTextContent('Level 0 — candidate selection');
  });
});
