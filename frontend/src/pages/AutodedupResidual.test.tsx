/* AutodedupResidual — the pairs the engine did NOT join.
 *
 * Hermetic: the list read and the verdict write are mocked.
 *
 * Pins:
 *   * the display floor is 0.20 on the first read — an operator control, not a
 *     claim about the engine;
 *   * a row shows both adverts, the attribute diff, WHY it wasn't merged, the
 *     model's top contributions and the judge's verdict;
 *   * a disagreeing attribute is marked and an ABSENT one is not — a gap is
 *     never evidence against a duplicate;
 *   * a negative PAIR verdict takes two clicks, because it writes a permanent
 *     must-not-link; the positive one does not;
 *   * a zone filter sends a key and restarts the keyset;
 *   * no interactive control is nested inside another.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';

import AutodedupResidual from './AutodedupResidual';
import * as api from '@/lib/api';
import { expectNoNestedInteractive } from '@/test/a11y';
import type { AutodedupMember, AutodedupResidualRow, AutodedupVerdictRow } from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    getAutodedupResidual: vi.fn(),
    getAutodedupBlocks: vi.fn(),
    postAutodedupVerdict: vi.fn(),
  };
});

function member(over: Partial<AutodedupMember> & { listing_id: number }): AutodedupMember {
  return {
    source: 'sreality',
    source_url: 'https://www.sreality.cz/detail/1',
    category_main: 'byt',
    category_type: 'prodej',
    disposition: '2+kk',
    area_m2: 54,
    floor: 3,
    price_czk: 5_900_000,
    first_seen_at: '2026-01-04T00:00:00Z',
    last_seen_at: '2026-03-01T00:00:00Z',
    is_active: true,
    cover: { storage_path: null, sreality_url: 'https://img.example.invalid/a.jpg' },
    n_images: 12,
    ...over,
  };
}

const ROW: AutodedupResidualRow = {
  listing_lo: 101,
  listing_hi: 202,
  score: 0.44,
  zone: 'band',
  decision: 'evidence_gate',
  guard_veto: null,
  certificate: null,
  families: 1,
  family_names: ['ATTR'],
  probes: ['k1'],
  cluster_key: null,
  lo: member({ listing_id: 101 }),
  /* Different floor, and no price on this side: one disagreement, one gap. */
  /* `category_type` null on one side is a GAP the portal never published, not a
   * contradiction — bazos rows routinely arrive without it. */
  hi: member({
    listing_id: 202,
    source: 'bazos',
    floor: 5,
    price_czk: null,
    category_type: null,
  }),
  why_not_merged: 'Only one evidence family was present — the images agreed, nothing else did.',
  contributions: [
    { name: 'img_best_hamming', value: 0.9, present: true, contribution: 1.2 },
    { name: 'street_equal', value: null, present: false, contribution: 0 },
  ],
  judgement: {
    listing_lo: 101,
    listing_hi: 202,
    judge_version: 'j1',
    tier: 'vision',
    model: 'gpt-5-mini',
    verdict: 'insufficient_evidence',
    confidence: 0.5,
    unit_discriminator: null,
    key_evidence: ['same kitchen'],
    contradicting_evidence: ['different floor'],
    developer_project_suspected: false,
    created_at: '2026-09-15T00:00:00Z',
  },
  verdict: null,
};

const STORED: AutodedupVerdictRow = {
  id: 3,
  kind: 'pair',
  cluster_key: null,
  listing_lo: 101,
  listing_hi: 202,
  verdict: 'different',
  note: null,
  decided_by: 'operator@example.invalid',
  decided_at: '2026-09-16T10:00:00Z',
};

function page(
  items: AutodedupResidualRow[],
  nextAfter: string | null = null,
  total: number | null = null,
) {
  return {
    store_ready: true,
    data: { items, has_more: nextAfter != null, next_after: nextAfter, total },
  };
}

const BLOCKS = {
  store_ready: true,
  data: {
    generation: 'g1',
    items: [
      {
        block_key: 563510,
        block_grain: 'o',
        name: 'Jablonec nad Nisou',
        n_clusters: 412,
        n_listings: 900,
      },
    ],
  },
};

function LocationProbe() {
  return <i data-testid="search">{useLocation().search}</i>;
}

function renderPage(entry = '/autodedup/residual') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <AutodedupResidual />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('<AutodedupResidual>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAutodedupResidual).mockResolvedValue(page([ROW]));
    vi.mocked(api.getAutodedupBlocks).mockResolvedValue(BLOCKS);
    vi.mocked(api.postAutodedupVerdict).mockResolvedValue({ store_ready: true, data: STORED, must_not_link: false });
  });

  it('asks for the display floor of 0.20 on the first read', async () => {
    renderPage();
    await screen.findByText(/Why it wasn't merged/);
    expect(api.getAutodedupResidual).toHaveBeenCalledWith(
      expect.objectContaining({ min_score: 0.2, after: null, sort: 'score_desc' }),
    );
  });

  it('shows both adverts, the reason, the contributions and the judge', async () => {
    renderPage();
    const row = (await screen.findByText(/Why it wasn't merged/)).closest('li')!;
    /* Each id appears on its own card and again as a diff-table caption. */
    expect(within(row).getAllByText('#101').length).toBeGreaterThan(0);
    expect(within(row).getAllByText('#202').length).toBeGreaterThan(0);
    expect(row).toHaveTextContent('Only one evidence family was present');
    expect(within(row).getByText('img_best_hamming')).toBeInTheDocument();
    expect(within(row).getByText(/judge: not enough evidence/)).toBeInTheDocument();
    expect(within(row).getByText(/different floor/)).toBeInTheDocument();
    expectNoNestedInteractive(row);
  });

  it('marks a disagreeing attribute and leaves a gap unmarked', async () => {
    renderPage();
    const row = (await screen.findByText(/Why it wasn't merged/)).closest('li')!;
    /* The diff table is the only table on the row; the cards use <dl>. */
    const diff = within(row).getByRole('table');
    /* Floor 3 vs 5 — both known and different. */
    const floor = within(diff).getByText('Patro').closest('tr')!;
    expect(floor.className).toContain('brick-soft');
    /* Price known on one side only: a gap, not a disagreement. */
    const price = within(diff).getByText('Cena').closest('tr')!;
    expect(price.className).not.toContain('brick-soft');
    expect(price).toHaveTextContent('chybí');
    /* THE FORMATTER TRAP. `categoryTypeLabel(null)` is "—" and
     * `categoryMainLabel(null)` is "Nemovitost" — formatting before the null
     * check turns a missing field into two different strings and paints it
     * brick-red on the one screen whose job is weighing contradictions. */
    const deal = within(diff).getByText('Nabídka').closest('tr')!;
    expect(deal.className).not.toContain('brick-soft');
    expect(deal).toHaveTextContent('chybí');
  });

  it('renders no control the residual route does not accept', async () => {
    renderPage();
    await screen.findByText(/Why it wasn't merged/);
    /* `RESIDUAL_FILTER_KEYS` carries no category keys, and `toResidualQuery`
     * sends none — an inert select that silently returns the same list is worse
     * than no select at all. */
    const controls = screen.getAllByRole('combobox').map((el) => el.closest('label')?.textContent);
    expect(controls).not.toContain(expect.stringContaining('Druh'));
    expect(controls).not.toContain(expect.stringContaining('Nabídka'));
    expect(controls).not.toContain(expect.stringContaining('Portal'));
    /* The ones it DOES send are still there. */
    expect(controls.some((t) => t?.includes('Zone'))).toBe(true);
  });

  it('takes two clicks for a negative verdict and one for a positive one', async () => {
    const user = userEvent.setup();
    renderPage();
    const row = (await screen.findByText(/Why it wasn't merged/)).closest('li')!;
    const separate = within(row).getByRole('button', { name: 'Correctly separate' });
    await user.click(separate);
    /* Armed, not written: this verdict outlives every recalibration. */
    expect(api.postAutodedupVerdict).not.toHaveBeenCalled();
    const armed = within(row).getByRole('button', { name: 'Click again to confirm' });
    await user.click(armed);
    expect(api.postAutodedupVerdict).toHaveBeenCalledWith({
      kind: 'pair',
      reasons: [],
      note: null,
      listing_lo: 101,
      listing_hi: 202,
      verdict: 'different',
    });
    await waitFor(() =>
      expect(within(row).getByRole('button', { name: 'Correctly separate' })).toHaveAttribute(
        'aria-pressed',
        'true',
      ),
    );
  });

  it('writes a positive verdict on the first click', async () => {
    const user = userEvent.setup();
    renderPage();
    const row = (await screen.findByText(/Why it wasn't merged/)).closest('li')!;
    await user.click(within(row).getByRole('button', { name: 'This IS a duplicate' }));
    expect(api.postAutodedupVerdict).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'pair', verdict: 'same' }),
    );
  });

  it('sends the zone filter as a key', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Why it wasn't merged/);
    await user.selectOptions(screen.getByLabelText('Zone'), 'reject');
    await waitFor(() =>
      expect(api.getAutodedupResidual).toHaveBeenLastCalledWith(
        expect.objectContaining({ zone: 'reject', after: null }),
      ),
    );
  });

  it('links each pair to its full-evidence page', async () => {
    renderPage();
    const link = await screen.findByRole('link', { name: 'Full evidence' });
    expect(link).toHaveAttribute('href', '/autodedup/pair/101/202?generation=g1');
  });


  /* ------------------------------------------------- the source PAIR filter */

  it('composes the source pair from two portal selects, in the order the server compares', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Why it wasn't merged/);
    /* 45 unordered pairs is not a list anyone reads, and the old control was a
     * text box whose placeholder ("bazos+sreality") read as a value. */
    await user.selectOptions(screen.getByLabelText('Portál A'), 'sreality');
    await user.selectOptions(screen.getByLabelText('Portál B'), 'bazos');
    await waitFor(() =>
      /* least() + greatest() server-side, so the pair is sorted here too. */
      expect(api.getAutodedupResidual).toHaveBeenLastCalledWith(
        expect.objectContaining({ source_pair: 'bazos+sreality', after: null }),
      ),
    );
  });

  it('says so rather than filtering silently when only one portal is chosen', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Why it wasn't merged/);
    await user.selectOptions(screen.getByLabelText('Portál A'), 'sreality');
    expect(await screen.findByText(/Vyberte oba portály/)).toBeInTheDocument();
    expect(api.getAutodedupResidual).toHaveBeenLastCalledWith(
      expect.objectContaining({ source_pair: null }),
    );
  });

  /* -------------------------------------------------- the URL is the state */

  it('reads its filters out of the query string', async () => {
    renderPage('/autodedup/residual?zone=reject&min_score=0.5&block=563510');
    await screen.findByText(/Why it wasn't merged/);
    expect(api.getAutodedupResidual).toHaveBeenLastCalledWith(
      expect.objectContaining({
        zone: 'reject',
        min_score: 0.5,
        block: 563510,
        block_grain: null,
      }),
    );
    expect(screen.getByLabelText('Zone')).toHaveValue('reject');
  });

  it('writes the display floor into the url when it is cleared', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Why it wasn't merged/);
    await user.clear(screen.getByLabelText('Score ≥'));
    /* "no floor" is a filter; a url that dropped the key would restore 0.20 on
     * the next reload — a different queue from the one that was shared. */
    await waitFor(() => expect(screen.getByTestId('search')).toHaveTextContent('min_score='));
    /* AND IT HAS TO REACH THE WIRE. `min_score` is the one parameter whose server
     * default is not "no filter" (0.20), so an omitted parameter means the default
     * floor: a cleared field that sent nothing would render empty, share a url that
     * read empty, and still show the default queue. Zero is the floor that says none. */
    await waitFor(() =>
      expect(api.getAutodedupResidual).toHaveBeenLastCalledWith(
        expect.objectContaining({ min_score: 0 }),
      ),
    );
  });

  it('offers the blocks by name', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Why it wasn't merged/);
    const select = screen.getByLabelText('Block');
    expect(select.tagName).toBe('SELECT');
    await waitFor(() =>
      expect(
        within(select).getByRole('option', { name: /Jablonec nad Nisou/ }),
      ).toBeInTheDocument(),
    );
    /* The same vocabulary as the groups queue, and the same two parameters on the
     * wire — this view's block predicate reads the resolved location store the
     * engine derives a block key from, not the never-written fingerprint table. */
    await user.selectOptions(select, 'o563510');
    await waitFor(() =>
      expect(api.getAutodedupResidual).toHaveBeenLastCalledWith(
        expect.objectContaining({ block: 563510, block_grain: 'o' }),
      ),
    );
  });

  /* ------------------------------------------------------------ the rows */

  it('says how much of the filtered queue is on screen', async () => {
    vi.mocked(api.getAutodedupResidual).mockResolvedValue(page([ROW], null, 58));
    renderPage();
    expect(await screen.findByText('1 of 58 pairs')).toBeInTheDocument();
  });

  it('renders the covers as queue-grain thumbnails, not hero photos', async () => {
    const { container } = renderPage();
    await screen.findByText(/Why it wasn't merged/);
    /* jsdom loads no CSS, so the grain is asserted where it is decided: the
     * dense cover box (w-40 = 160px, 4:3). Without it two ~600px photos push
     * the diff table, the reason and the four answers below the fold — the
     * whole decision off screen. */
    expect(container.querySelectorAll('.w-40.shrink-0').length).toBe(2);
  });

  it('labels a cover the portal refuses to serve', async () => {
    renderPage();
    const row = (await screen.findByText(/Why it wasn't merged/)).closest('li')!;
    const img = within(row).getAllByRole('presentation', { hidden: true })[0] as HTMLImageElement;
    fireEvent.error(img);
    await waitFor(() =>
      expect(within(row).getAllByText('foto nedostupné').length).toBeGreaterThan(0),
    );
  });

  it('renders the empty state on an un-migrated store', async () => {
    vi.mocked(api.getAutodedupResidual).mockResolvedValue({ store_ready: false, data: null });
    renderPage();
    expect(await screen.findByText(/Schema not migrated yet/)).toBeInTheDocument();
  });
});
