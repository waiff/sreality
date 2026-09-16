/* AutodedupProgress — the autonomous dedup program's ledger page.
 *
 * Hermetic: both API reads are mocked.
 *
 * Pins:
 *   * the four KPI tiles read the STATS payload, and say "not yet" — never a
 *     zero — whenever the store is missing or a figure is null. A zero on a
 *     progress page reads as a measured result;
 *   * the caps ($200 program, $25/run) and the mode (SHADOW) are program
 *     CONSTANTS and render with no backend at all — they are never "not yet";
 *   * a measured $0.00 is money, not a gap: the census and export iterations
 *     really are free, and printing them as "not yet" would lose that;
 *   * the current wave is the wave of the NEWEST iteration, not a constant;
 *   * an iteration card carries its wave, title, status and tools;
 *   * "Load more" pages by the cursor the previous page returned, never by an
 *     offset;
 *   * an un-migrated store renders the empty state instead of failing;
 *   * a FAILED read shows the error and NOT "nothing has been recorded" — the
 *     page does not know, so it must not claim;
 *   * a fractional metric keeps its magnitude: 0.0004 is not "0";
 *   * an artifact link is labelled by its DESTINATION ("Actions run"), not by
 *     the lane's jsonb key, and a metrics blob that only repeats the sample is
 *     hidden rather than printed twice as if it were a second finding;
 *   * no interactive control is nested inside another (the collapse buttons and
 *     the artifact links live in separate subtrees).
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import AutodedupProgress from './AutodedupProgress';
import * as api from '@/lib/api';
import { expectNoNestedInteractive } from '@/test/a11y';
import type { AutodedupIteration, AutodedupStats } from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    getAutodedupIterations: vi.fn(),
    getAutodedupStats: vi.fn(),
  };
});

/* The page's own page size — the keyset hook stops on a short page, so a test
 * that wants a second page has to hand it a full first one. */
const PAGE_SIZE = 25;

function iteration(over: Partial<AutodedupIteration> & { id: number }): AutodedupIteration {
  return {
    wave: 'W0',
    title: 'An iteration',
    status: 'done',
    approach: null,
    tools: [],
    sample_stats: {},
    metrics: {},
    cost_usd: 0,
    artifacts: {},
    run_id: null,
    notes: null,
    started_at: '2026-09-16T08:00:00Z',
    finished_at: '2026-09-16T08:20:00Z',
    created_at: '2026-09-16T08:00:00Z',
    ...over,
  };
}

const NEWEST = iteration({
  id: 12,
  wave: 'W1',
  title: 'Cohort export',
  status: 'running',
  approach: 'Ship the whole cohort as one gzipped artifact so the rest runs locally at zero cost.',
  tools: ['autodedup.lane', 'psycopg', 'GitHub Actions'],
  sample_stats: { cohort: 'jablonec', listings: 3120, blocks: { primary: 2, dense: 1 } },
  metrics: { blocking_recall: 0.94, gate: '>= 0.90' },
  cost_usd: null,
  artifacts: { 'cohort.tar.gz': 'https://example.invalid/cohort.tar.gz' },
  run_id: 55501,
  notes: 'Still running — the row is the signal.',
});

const OLDER = iteration({ id: 11, wave: 'W0', title: 'Census', tools: ['autodedup.census'] });

const STATS: AutodedupStats = {
  n_iterations: 12,
  total_cost_usd: 0,
  last_iteration_at: '2026-09-16T08:20:00Z',
  /* Ordered by each wave's latest pass, the way the rollup query returns it. */
  waves: [
    { wave: 'W0', n: 11, last_status: 'done', cost_usd: 0 },
    { wave: 'W1', n: 1, last_status: 'running', cost_usd: 0 },
  ],
};

function page(rows: AutodedupIteration[], nextAfterId: number | null = null) {
  return {
    store_ready: true,
    data: { items: rows, has_more: nextAfterId != null, next_after_id: nextAfterId },
  };
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AutodedupProgress />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function tile(label: string): HTMLElement {
  return screen.getByText(label).closest('div')!.parentElement!;
}

describe('<AutodedupProgress>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAutodedupStats).mockResolvedValue({ store_ready: true, data: STATS });
    vi.mocked(api.getAutodedupIterations).mockResolvedValue(page([NEWEST, OLDER]));
  });

  /* The block names live in the cohort module and in each iteration's
   * sample_stats, never in the prose — so nothing here pins them. */
  it('says the trial merges nothing, and shows the mode as a constant', async () => {
    renderPage();
    const lede = (await screen.findByText(/shadow mode/)).closest('p')!;
    expect(lede).toHaveTextContent('merges nothing');
    expect(lede).toHaveTextContent('negative-control');
    /* SHADOW is a program constant (D4/E39): present regardless of any read. */
    expect(screen.getByText('SHADOW')).toBeInTheDocument();
  });

  it('prints the caps even before the store answers', async () => {
    vi.mocked(api.getAutodedupStats).mockResolvedValue({ store_ready: false, data: null });
    vi.mocked(api.getAutodedupIterations).mockResolvedValue({ store_ready: false, data: null });
    renderPage();
    expect(await screen.findByText(/Schema not migrated yet/)).toBeInTheDocument();
    expect(tile('Spent so far')).toHaveTextContent('$25.00 is the hard cap');
  });

  it('fills the four tiles, taking the current wave from the newest iteration', async () => {
    renderPage();
    await screen.findByText('Cohort export');
    expect(within(tile('Iterations')).getByText('12')).toBeInTheDocument();
    /* A measured zero IS money here — two iterations of this program really are
     * free — so it prints as $0.00 and not as a gap; and it is always shown
     * against the $200 program cap, because 6% and 60% are not the same news. */
    expect(tile('Spent so far')).toHaveTextContent('$0.00 of $200.00');
    /* W0's newest iteration is done, W1's is running: one closed of two. */
    expect(tile('Waves closed')).toHaveTextContent('1 of 2');
    /* W1, from the newest ROW — not the rollup's first wave ('W0'), which is
     * only the fallback for the moment before the list lands. */
    expect(within(tile('Current wave')).getByText('W1')).toBeInTheDocument();
  });

  it('says "not yet" in every tile when the store is not migrated', async () => {
    vi.mocked(api.getAutodedupStats).mockResolvedValue({ store_ready: false, data: null });
    vi.mocked(api.getAutodedupIterations).mockResolvedValue({ store_ready: false, data: null });
    renderPage();
    expect(await screen.findByText(/Schema not migrated yet/)).toBeInTheDocument();
    /* Four tiles, four gaps — never a fabricated zero. */
    await waitFor(() => expect(screen.getAllByText('not yet')).toHaveLength(4));
  });

  it('says nothing has been recorded when the store is empty', async () => {
    vi.mocked(api.getAutodedupIterations).mockResolvedValue(page([]));
    renderPage();
    expect(await screen.findByText(/No iteration has been recorded yet/)).toBeInTheDocument();
    expect(screen.queryByText(/Schema not migrated yet/)).toBeNull();
  });

  it('renders each iteration with its wave, title, status and cost', async () => {
    renderPage();
    const card = (await screen.findByText('Cohort export')).closest('li')!;
    expect(within(card).getByText('W1')).toBeInTheDocument();
    expect(within(card).getByText('running')).toBeInTheDocument();
    /* The lane has not read this iteration's bill back from the model calls
     * yet, so the card says so rather than showing $0.00. */
    expect(within(card).getByText('not yet')).toBeInTheDocument();

    const older = screen.getByText('Census').closest('li')!;
    expect(within(older).getByText('W0')).toBeInTheDocument();
    expect(within(older).getByText('$0.00')).toBeInTheDocument();
  });

  it('opens a card onto its approach, tools, sample, metrics and artifacts', async () => {
    const user = userEvent.setup();
    renderPage();
    const card = (await screen.findByText('Cohort export')).closest('li')!;
    const toggle = within(card).getByRole('button', { name: /Cohort export/ });
    /* `hidden` keeps the body in the DOM, so aria-expanded alone would pass
     * against a card that never actually folds. Assert the element. */
    const body = card.querySelector('#autodedup-iteration-12')!;
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(body).not.toBeVisible();
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(body).toBeVisible();

    expect(within(card).getByText(/gzipped artifact/)).toBeInTheDocument();
    expect(within(card).getByText('autodedup.lane')).toBeInTheDocument();
    expect(within(card).getByText('psycopg')).toBeInTheDocument();

    /* Both jsonb blobs are rendered as they were written — numbers through the
     * shared formatter, a nested object printed rather than guessed at. */
    const cohort = within(card).getByText('cohort').closest('tr')!;
    expect(cohort).toHaveTextContent('jablonec');
    expect(within(card).getByText('blocks').closest('tr')!).toHaveTextContent('"primary": 2');
    expect(within(card).getByText('blocking_recall').closest('tr')!).toHaveTextContent('0,94');

    const artifact = within(card).getByRole('link', { name: 'cohort.tar.gz' });
    expect(artifact).toHaveAttribute('href', 'https://example.invalid/cohort.tar.gz');
    expect(artifact.getAttribute('rel')).toContain('noopener');
    expect(within(card).getByText(/55501/)).toBeInTheDocument();
    expect(within(card).getByText(/the row is the signal/)).toBeInTheDocument();

    /* The header toggle and the artifact link are siblings, never nested. */
    expectNoNestedInteractive(card);
  });

  it('loads the next page by the cursor the previous page returned', async () => {
    const user = userEvent.setup();
    const first = Array.from({ length: PAGE_SIZE }, (_, i) =>
      iteration({ id: 100 - i, title: `Iteration ${100 - i}` }),
    );
    vi.mocked(api.getAutodedupIterations)
      .mockResolvedValueOnce(page(first, 76))
      .mockResolvedValueOnce(page([iteration({ id: 75, title: 'Iteration 75' })]));

    renderPage();
    await screen.findByText('Iteration 100');
    expect(api.getAutodedupIterations).toHaveBeenCalledWith({ limit: PAGE_SIZE, after: null });

    await user.click(screen.getByRole('button', { name: 'Load more' }));
    await waitFor(() =>
      expect(api.getAutodedupIterations).toHaveBeenLastCalledWith({
        limit: PAGE_SIZE,
        after: 76,
      }),
    );
    expect(await screen.findByText('Iteration 75')).toBeInTheDocument();
    /* The last page is short, so the button retires rather than looping. */
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull());
  });

  it('surfaces a failed read instead of an empty page', async () => {
    vi.mocked(api.getAutodedupIterations).mockRejectedValue(new Error('boom'));
    renderPage();
    expect(await screen.findByText(/boom/)).toBeInTheDocument();
    /* The read never landed, so the page knows nothing about the store — an
     * "it is empty" next to the error would be a fabricated fact. */
    expect(screen.queryByText(/No iteration has been recorded yet/)).toBeNull();
  });

  it('keeps a sub-thousandth metric off zero', async () => {
    vi.mocked(api.getAutodedupIterations).mockResolvedValue(
      page([iteration({ id: 9, title: 'Calibration', metrics: { ece: 0.0004, bins: 10 } })]),
    );
    const user = userEvent.setup();
    renderPage();
    const card = (await screen.findByText('Calibration')).closest('li')!;
    await user.click(within(card).getByRole('button', { name: /Calibration/ }));
    /* The cs-CZ count formatter would render this "0" — a measured zero is the
     * one answer this page must not invent. */
    expect(within(card).getByText('ece').closest('tr')!).toHaveTextContent('0,0004');
    expect(within(card).getByText('bins').closest('tr')!).toHaveTextContent('10');
  });

  it('labels an Actions-run artifact by what it is, not by its jsonb key', async () => {
    vi.mocked(api.getAutodedupIterations).mockResolvedValue(
      page([
        iteration({
          id: 7,
          title: 'Labelled artifact',
          artifacts: {
            /* The lane's own key is a machine name; the destination is what
             * the reader needs. */
            run: 'https://github.com/acme/repo/actions/runs/55501',
            'cohort.tar.gz': 'https://example.invalid/cohort.tar.gz',
          },
        }),
      ]),
    );
    const user = userEvent.setup();
    renderPage();
    const card = (await screen.findByText('Labelled artifact')).closest('li')!;
    await user.click(within(card).getByRole('button', { name: /Labelled artifact/ }));
    expect(within(card).getByRole('link', { name: 'Actions run' })).toHaveAttribute(
      'href',
      'https://github.com/acme/repo/actions/runs/55501',
    );
    /* A key that already reads as a name is left alone. */
    expect(within(card).getByRole('link', { name: 'cohort.tar.gz' })).toBeInTheDocument();
  });

  it('hides the metrics table when it only repeats the sample', async () => {
    const blob = { cohort: 'jablonec', listings: 3120 };
    vi.mocked(api.getAutodedupIterations).mockResolvedValue(
      page([
        iteration({ id: 6, title: 'Census', sample_stats: blob, metrics: { ...blob } }),
        iteration({
          id: 5,
          title: 'Calibration',
          sample_stats: blob,
          metrics: { ece: 0.0004 },
        }),
      ]),
    );
    const user = userEvent.setup();
    renderPage();
    const same = (await screen.findByText('Census')).closest('li')!;
    await user.click(within(same).getByRole('button', { name: /Census/ }));
    expect(within(same).queryByText('Metrics')).toBeNull();
    expect(within(same).getByText(/identical to the sample/)).toBeInTheDocument();

    /* A real measurement still gets its own table. */
    const other = screen.getByText('Calibration').closest('li')!;
    await user.click(within(other).getByRole('button', { name: /Calibration/ }));
    expect(within(other).getByText('Metrics')).toBeInTheDocument();
  });

  it('drops an artifact value that is not an http(s) link', async () => {
    vi.mocked(api.getAutodedupIterations).mockResolvedValue(
      page([
        iteration({
          id: 8,
          title: 'Bad artifact',
          artifacts: { nested: { url: 'x' }, ok: 'https://example.invalid/a.gz' },
        }),
      ]),
    );
    const user = userEvent.setup();
    renderPage();
    const card = (await screen.findByText('Bad artifact')).closest('li')!;
    await user.click(within(card).getByRole('button', { name: /Bad artifact/ }));
    expect(within(card).getByRole('link', { name: 'ok' })).toBeInTheDocument();
    expect(within(card).queryByRole('link', { name: 'nested' })).toBeNull();
  });
});
