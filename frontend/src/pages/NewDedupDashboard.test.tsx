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
      listings: 600, active: 500, with_projection: 540, with_town: 480,
      with_disposition: 420, with_area: 450, byt: 600, byt_with_floor: 360,
      c1_eligible: 420, c3_eligible: 36, town_no_attribute: 24,
    },
    {
      source: 'bazos', category_main: 'pozemek', category_type: 'prodej',
      listings: 200, active: 160, with_projection: 160, with_town: 120,
      with_disposition: 0, with_area: 100, byt: 0, byt_with_floor: 0,
      c1_eligible: 0, c3_eligible: 100, town_no_attribute: 20,
    },
  ],
  top_buckets: [],
  town_assignment: [],
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

  it('renders the funnel over the newest finished run', async () => {
    renderPage();
    await screen.findByText('Listings in the database');

    /* 600 + 200 listings narrow to 540 + 160 with a location answer, then to
     * 480 + 120 with a town, then to 420 + 36 + 100 with something to compare,
     * and 210 of them end up in a pair. */
    const step = (label: string) => screen.getByText(label).closest('li')!;
    expect(step('Listings in the database')).toHaveTextContent('800');
    expect(step('Known to the location engine')).toHaveTextContent('700');
    expect(step('Placed precisely enough to name a town')).toHaveTextContent('600');
    expect(step('Has an attribute the rule can compare')).toHaveTextContent('556');
    expect(step('Ended up in at least one candidate pair')).toHaveTextContent('210');

    /* The pair totals, split by rung. */
    expect(screen.getByText(/Candidate pairs found/)).toHaveTextContent('157');
    expect(screen.getByText(/disposition rung/)).toHaveTextContent('120');
    expect(screen.getByText(/area rung/)).toHaveTextContent('37');
  });

  it('splits the last funnel step by property type, not only by rung', async () => {
    renderPage();
    /* "by type and path" — the dashboard has no matrix under the funnel, so the
     * type half of that requirement lives in the funnel or nowhere. */
    const paired = (await screen.findByText('Ended up in at least one candidate pair'))
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
    expect(screen.queryByText('Listings in the database')).not.toBeInTheDocument();
  });

  it('names the wave being built', async () => {
    renderPage();
    await screen.findByText('Where the program is');
    const wave2 = screen.getByText('W2').closest('li')!;
    expect(wave2).toHaveTextContent('being built');
    expect(wave2).toHaveTextContent('Level 0 — candidate selection');
  });
});
