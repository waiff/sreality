/* AutodedupGroups — the proposed-cluster review queue.
 *
 * Hermetic: every API read and the one write are mocked.
 *
 * Pins:
 *   * the page says the trial merges nothing — "Confirm" must never read as
 *     "merge now";
 *   * a card carries its cluster key, its size, its members and the weakest
 *     edge, and shows at most four members with the rest COUNTED, not cropped;
 *   * a filter control sends a KEY on the query, and resets the keyset;
 *   * a verdict click posts the cluster verdict and the badge flips to it
 *     optimistically — with no second confirm, because a cluster verdict writes
 *     nothing permanent;
 *   * an un-migrated store renders the empty state instead of failing, and a
 *     FAILED read never claims the queue is empty;
 *   * "Load more" pages by the cursor the previous page returned;
 *   * a card PAGES each member's photos rather than judging it on one cover;
 *   * the split row appears only once two units are actually chosen, and Save
 *     sends EVERY member of the group — including the ones the card counted
 *     rather than showed;
 *   * no interactive control is nested inside another.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';

import AutodedupGroups from './AutodedupGroups';
import * as api from '@/lib/api';
import { expectNoNestedInteractive } from '@/test/a11y';
import type { AutodedupGroup, AutodedupMember, AutodedupVerdictRow } from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    getAutodedupGroups: vi.fn(),
    getAutodedupGroup: vi.fn(),
    getAutodedupBlocks: vi.fn(),
    postAutodedupVerdict: vi.fn(),
    postAutodedupSplitVerdict: vi.fn(),
  };
});

const PAGE_SIZE = 20;

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

function group(over: Partial<AutodedupGroup> & { cluster_key: number }): AutodedupGroup {
  return {
    generation: 'g1',
    size: 2,
    block_key: 42,
    cat_group: 'byt',
    category_main: 'byt',
    category_type: 'prodej',
    area_min: 54,
    area_max: 54,
    sources: ['sreality', 'bazos'],
    medoid_listing_id: 101,
    min_edge_score: 0.61,
    mean_edge_score: 0.78,
    n_judged_edges: 1,
    n_certificate_edges: 0,
    evidence_families: 1 | 32,
    family_names: ['ATTR', 'IMG'],
    max_gap_days: 0,
    shared_photo_warning: false,
    status: 'proposed',
    model_version: 'm1',
    feature_version: 1,
    members: [member({ listing_id: 101 }), member({ listing_id: 202, source: 'bazos' })],
    edges: { n_edges: 1, min_score: 0.61, mean_score: 0.61, n_certificates: 0 },
    verdict: null,
    ...over,
  };
}

const STORED: AutodedupVerdictRow = {
  id: 9,
  kind: 'cluster',
  cluster_key: 7,
  listing_lo: null,
  listing_hi: null,
  verdict: 'same',
  note: null,
  decided_by: 'operator@example.invalid',
  decided_at: '2026-09-16T10:00:00Z',
};

function page(
  items: AutodedupGroup[],
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
      { block_key: 490245, block_grain: 'c', name: 'Žižkov', n_clusters: 88, n_listings: 190 },
    ],
  },
};

/* MemoryRouter keeps its own history, so the shared url is read back from the
 * router rather than from window.location (which a memory router never sets). */
function LocationProbe() {
  return <i data-testid="search">{useLocation().search}</i>;
}

function renderPage(entry = '/autodedup/groups') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <AutodedupGroups />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/* The wire-shape bridge. The API assembles a queue item as four sibling objects
 * and decodes the evidence families server-side; the page wants one flat row
 * with names. These run against the REAL module (the mock above spreads it), so
 * a server that changes its spelling fails here rather than in a blank card. */
describe('normalizeGroup', () => {
  it('flattens the server\'s {cluster, members, edges, verdict} into one row', () => {
    const row = api.normalizeGroup({
      cluster: {
        cluster_key: 7,
        generation: 'g1',
        size: 2,
        block_key: 42,
        cat_group: 'byt',
        category_main: 'byt',
        category_type: 'prodej',
        area_min: 54,
        area_max: 54,
        sources: ['sreality'],
        medoid_listing_id: 101,
        min_edge_score: 0.61,
        mean_edge_score: 0.61,
        n_judged_edges: 0,
        n_certificate_edges: 0,
        evidence_families: 1 | 32,
        max_gap_days: 0,
        shared_photo_warning: false,
        status: 'proposed',
        model_version: 'm1',
        feature_version: 1,
      },
      members: [member({ listing_id: 101 })],
      edges: { n_edges: 1, min_score: 0.61, mean_score: 0.61, n_certificates: 0 },
      verdict: null,
    });
    expect(row.cluster_key).toBe(7);
    expect(row.members).toHaveLength(1);
    /* Decoded from the bitmask when the server sends no names. */
    expect(row.family_names).toEqual(['ATTR', 'IMG']);
  });

  it('prefers the names the server decoded over the raw mask', () => {
    const row = api.normalizeGroup({
      cluster: { ...group({ cluster_key: 9 }), evidence_family_names: ['LOC'] },
    });
    expect(row.family_names).toEqual(['LOC']);
  });
});

describe('normalizeResidual', () => {
  it('accepts the server\'s a/b sides, top_features and judge summary', () => {
    const row = api.normalizeResidual({
      listing_lo: 101,
      listing_hi: 202,
      score: 0.44,
      zone: 'band',
      decision: 'evidence_gate',
      guard_veto: null,
      certificate: null,
      probes: ['k1'],
      families: ['ATTR'],
      why_not_merged: 'only one family',
      a: member({ listing_id: 101 }),
      b: member({ listing_id: 202 }),
      top_features: [{ name: 'area_rel_diff', value: 0.01, present: true, contribution: 0.8 }],
      judge: { verdict: 'insufficient_evidence', confidence: 0.5, tier: 'text' },
      verdict: null,
    });
    expect(row.lo.listing_id).toBe(101);
    expect(row.hi.listing_id).toBe(202);
    expect(row.contributions).toHaveLength(1);
    expect(row.judgement?.verdict).toBe('insufficient_evidence');
    expect(row.family_names).toEqual(['ATTR']);
  });
});

describe('<AutodedupGroups>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(page([group({ cluster_key: 7 })]));
    vi.mocked(api.getAutodedupBlocks).mockResolvedValue(BLOCKS);
    vi.mocked(api.postAutodedupVerdict).mockResolvedValue({ store_ready: true, data: STORED, must_not_link: false });
    vi.mocked(api.postAutodedupSplitVerdict).mockResolvedValue({
      store_ready: true,
      data: {
        cluster_verdict: { ...STORED, verdict: 'same_project_different_unit', note: 'A: 101 · B: 202' },
        n_pairs_same: 0,
        n_pairs_negative: 1,
        must_not_link_written: 1,
        must_not_link_retracted: 0,
      },
    });
  });

  it('says nothing has been merged', async () => {
    renderPage();
    const lede = (await screen.findByText(/Nothing has been merged/)).closest('p')!;
    expect(lede).toHaveTextContent('shadow mode');
  });

  it('renders a group with its key, size, members and weakest edge', async () => {
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    expect(card).toHaveTextContent('2 adverts');
    expect(card).toHaveTextContent('sreality + bazos');
    /* The weakest edge is the number the queue is sorted by, so it is on the
     * card and not only in the drawer. */
    expect(within(card).getByText(/weakest 0,61/)).toBeInTheDocument();
    expect(within(card).getByText('#101')).toBeInTheDocument();
    expect(within(card).getByText('#202')).toBeInTheDocument();
    expectNoNestedInteractive(card);
  });

  it('counts the members it cannot show rather than cropping them', async () => {
    const many = group({
      cluster_key: 8,
      size: 6,
      members: [101, 202, 303, 404, 505, 606].map((id) => member({ listing_id: id })),
    });
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(page([many]));
    renderPage();
    const card = (await screen.findByText('#8')).closest('li')!;
    expect(within(card).getByText(/\+2 further advert/)).toBeInTheDocument();
    expect(within(card).queryByText('#606')).toBeNull();
  });

  it('sends a filter as a key on the query', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('#7');
    await user.selectOptions(screen.getByLabelText('Druh'), 'byt');
    await waitFor(() =>
      expect(api.getAutodedupGroups).toHaveBeenLastCalledWith(
        expect.objectContaining({ category_main: 'byt', after: null, sort: 'weakest' }),
      ),
    );
  });

  it('records a cluster verdict on the first click and flips the badge', async () => {
    const user = userEvent.setup();
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    const confirm = within(card).getByRole('button', { name: 'Confirm' });
    expect(confirm).toHaveAttribute('aria-pressed', 'false');
    await user.click(confirm);
    /* A cluster verdict writes nothing permanent, so it never arms a second
     * click the way a pair's negative verdict does. */
    expect(api.postAutodedupVerdict).toHaveBeenCalledWith({
      kind: 'cluster',
      cluster_key: 7,
      verdict: 'same',
    });
    await waitFor(() => expect(confirm).toHaveAttribute('aria-pressed', 'true'));
    expect(within(card).getByText(/operator@example.invalid/)).toBeInTheDocument();
  });

  it('rolls the badge back when the write fails', async () => {
    const user = userEvent.setup();
    vi.mocked(api.postAutodedupVerdict).mockRejectedValue(new Error('nope'));
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    const confirm = within(card).getByRole('button', { name: 'Confirm' });
    await user.click(confirm);
    await waitFor(() => expect(confirm).toHaveAttribute('aria-pressed', 'false'));
  });

  it('renders the empty state on an un-migrated store', async () => {
    vi.mocked(api.getAutodedupGroups).mockResolvedValue({ store_ready: false, data: null });
    renderPage();
    expect(await screen.findByText(/Schema not migrated yet/)).toBeInTheDocument();
  });

  it('never claims the queue is empty when the read failed', async () => {
    vi.mocked(api.getAutodedupGroups).mockRejectedValue(new Error('boom'));
    renderPage();
    expect(await screen.findByText(/boom/)).toBeInTheDocument();
    expect(screen.queryByText(/No group matches these filters/)).toBeNull();
  });

  it('pages by the cursor the previous page returned', async () => {
    const user = userEvent.setup();
    const first = Array.from({ length: PAGE_SIZE }, (_, i) => group({ cluster_key: 100 + i }));
    vi.mocked(api.getAutodedupGroups)
      .mockResolvedValueOnce(page(first, 'cur-2'))
      .mockResolvedValueOnce(page([group({ cluster_key: 900 })]));
    renderPage();
    await screen.findByText('#100');
    await user.click(screen.getByRole('button', { name: 'Load more' }));
    await waitFor(() =>
      expect(api.getAutodedupGroups).toHaveBeenLastCalledWith(
        expect.objectContaining({ after: 'cur-2' }),
      ),
    );
    expect(await screen.findByText('#900')).toBeInTheDocument();
  });


  /* ---------------------------------------------------- the BLOCK filter */

  it('offers the generation\'s blocks by name instead of a numeric text field', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('#7');
    const select = screen.getByLabelText('Block');
    /* A free-text field for a bigint RUIAN code: typing "Jablonec" produced NaN,
     * the query layer dropped the param and the queue answered unfiltered. */
    expect(select.tagName).toBe('SELECT');
    await waitFor(() =>
      expect(
        within(select).getByRole('option', { name: /Jablonec nad Nisou \(563510\)/ }),
      ).toBeInTheDocument(),
    );
    /* The count rides on the option, so the operator picks a block that has work. */
    expect(within(select).getByRole('option', { name: /412 skupin/ })).toBeInTheDocument();
    /* The option's VALUE is grain + code: a cast-obce code and an obec code share
     * one number space (migration 529), so the code alone would let two options
     * that mean two different blocks resolve to one query. */
    await user.selectOptions(select, 'o563510');
    await waitFor(() =>
      expect(api.getAutodedupGroups).toHaveBeenLastCalledWith(
        expect.objectContaining({ block: 563510, block_grain: 'o', after: null }),
      ),
    );
    expect(screen.getByTestId('search')).toHaveTextContent('block=o563510');
  });

  it('sends the quarter and the town as two different filters', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('#7');
    const select = screen.getByLabelText('Block');
    await waitFor(() =>
      expect(within(select).getByRole('option', { name: /Žižkov/ })).toBeInTheDocument(),
    );
    await user.selectOptions(select, 'c490245');
    await waitFor(() =>
      expect(api.getAutodedupGroups).toHaveBeenLastCalledWith(
        expect.objectContaining({ block: 490245, block_grain: 'c' }),
      ),
    );
  });

  it('reads a link written before the grain existed as "either"', async () => {
    renderPage('/autodedup/groups?block=563510');
    await screen.findByText('#7');
    /* A bare code still filters, grain-blind — the server reads a null grain as
     * "either", which is exactly what that older link meant. */
    expect(api.getAutodedupGroups).toHaveBeenLastCalledWith(
      expect.objectContaining({ block: 563510, block_grain: null }),
    );
  });

  it('shows the queue rather than a banner when the url carries a stale verdict', async () => {
    /* The server 400s an unknown verdict; a hand-edited or stale shared link must
     * not turn the queue into a red banner. */
    renderPage('/autodedup/groups?verdict=confirmed');
    await screen.findByText('#7');
    expect(api.getAutodedupGroups).toHaveBeenLastCalledWith(
      expect.objectContaining({ verdict: null }),
    );
  });

  it('keeps the filter working when the block vocabulary cannot be read', async () => {
    vi.mocked(api.getAutodedupBlocks).mockRejectedValue(new Error('nope'));
    renderPage('/autodedup/groups?block=563510');
    await screen.findByText('#7');
    /* The names are gone; the filter the link carried is not. */
    expect(screen.getByLabelText('Block')).toHaveValue('563510');
    expect(api.getAutodedupGroups).toHaveBeenLastCalledWith(
      expect.objectContaining({ block: 563510 }),
    );
  });

  /* ------------------------------------------------ the URL is the state */

  it('reads its filters out of the query string', async () => {
    renderPage('/autodedup/groups?generation=g2&verdict=same&sort=largest&min_size=3');
    await screen.findByText('#7');
    expect(api.getAutodedupGroups).toHaveBeenLastCalledWith(
      expect.objectContaining({
        generation: 'g2',
        verdict: 'same',
        sort: 'largest',
        min_size: 3,
      }),
    );
    /* And the controls show what the link said — a bar that sent one filter and
     * displayed another would be worse than no url state at all. */
    expect(screen.getByLabelText('Verdict')).toHaveValue('same');
    expect(screen.getByLabelText('Sort')).toHaveValue('largest');
  });

  it('writes a changed filter into the url so the view can be shared', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('#7');
    await user.selectOptions(screen.getByLabelText('Druh'), 'byt');
    await waitFor(() =>
      expect(screen.getByTestId('search')).toHaveTextContent('category_main=byt'),
    );
  });

  it('falls back to the default sort rather than 400ing on a stale link', async () => {
    renderPage('/autodedup/groups?sort=cheapest');
    await screen.findByText('#7');
    expect(api.getAutodedupGroups).toHaveBeenLastCalledWith(
      expect.objectContaining({ sort: 'weakest' }),
    );
  });

  /* ------------------------------------------------------- how many rows */

  it('says how much of the filtered queue is on screen', async () => {
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(
      page([group({ cluster_key: 7 })], null, 412),
    );
    renderPage();
    expect(await screen.findByText('1 of 412 groups')).toBeInTheDocument();
  });

  it('says what it loaded rather than inventing a total', async () => {
    renderPage();
    expect(await screen.findByText('1 group loaded')).toBeInTheDocument();
  });

  /* ------------------------------------------------------- broken covers */

  it('labels a cover the portal refuses to serve instead of leaving a blank tile', async () => {
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    const img = within(card).getAllByRole('presentation', { hidden: true })[0] as HTMLImageElement;
    /* The portal CDN can answer ERR_BLOCKED_BY_ORB cross-origin; the <img>
     * fails with nothing for the page to style, so the tile takes over. */
    fireEvent.error(img);
    await waitFor(() =>
      expect(within(card).getAllByText('foto nedostupné').length).toBeGreaterThan(0),
    );
  });


  /* ------------------------------------------- the card gallery (12 frames) */

  const FRAMES = [
    { image_id: 1, storage_path: null, sreality_url: 'https://img.example.invalid/1.jpg', sequence: 1 },
    { image_id: 2, storage_path: null, sreality_url: 'https://img.example.invalid/2.jpg', sequence: 2 },
  ];

  it('pages a member\'s photos on the card itself', async () => {
    const user = userEvent.setup();
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(
      page([
        group({
          cluster_key: 7,
          members: [
            member({ listing_id: 101, images: FRAMES, n_images: 30 }),
            member({ listing_id: 202, source: 'bazos' }),
          ],
        }),
      ]),
    );
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    expect(within(card).getByText('1 / 2')).toBeInTheDocument();
    /* The album is bigger than the card's gallery, and the card says so rather
     * than implying the advert has two photos. */
    expect(within(card).getByText('+28 fotek v detailu')).toBeInTheDocument();
    await user.click(within(card).getByRole('button', { name: 'Next photo' }));
    expect(within(card).getByText('2 / 2')).toBeInTheDocument();
    expectNoNestedInteractive(card);
  });

  it('falls back to the cover when the payload carries no gallery', async () => {
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    /* No carousel: nothing to page, so no chevrons and no counter. */
    expect(within(card).queryByRole('button', { name: 'Next photo' })).toBeNull();
  });

  /* ------------------------------------------------------- the unit split */

  it('offers the split only once two units are actually chosen', async () => {
    const user = userEvent.setup();
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    /* Every member starts in unit A — which is precisely what the engine
     * proposed, so there is nothing to split yet. */
    expect(within(card).queryByRole('button', { name: 'Save split' })).toBeNull();
    await user.selectOptions(within(card).getByLabelText('Jednotka #202'), 'B');
    expect(within(card).getByRole('button', { name: 'Save split' })).toBeInTheDocument();
    expect(within(card).getByText(/A: 101 · B: 202/)).toBeInTheDocument();
  });

  it('posts the assignment, the relation and every member', async () => {
    const user = userEvent.setup();
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    await user.selectOptions(within(card).getByLabelText('Jednotka #202'), 'B');
    await user.click(within(card).getByRole('button', { name: 'Save split' }));
    expect(api.postAutodedupSplitVerdict).toHaveBeenCalledWith({
      cluster_key: 7,
      generation: 'g1',
      units: [
        { listing_id: 101, unit: 'A' },
        { listing_id: 202, unit: 'B' },
      ],
      /* The default is the shape this was built for: one development, several
       * buildings. The other two relations are one select away. */
      relation: 'same_project_different_unit',
    });
    /* The stored cluster verdict lands on the badge, like any other verdict. */
    await waitFor(() =>
      expect(
        within(card).getByRole('button', { name: 'Same project, different unit' }),
      ).toHaveAttribute('aria-pressed', 'true'),
    );
    expect(within(card).getByText(/1 oddělených/)).toBeInTheDocument();
  });

  it('sends the members the card counted rather than showed', async () => {
    const user = userEvent.setup();
    const many = group({
      cluster_key: 8,
      size: 6,
      members: [101, 202, 303, 404, 505, 606].map((id) => member({ listing_id: id })),
    });
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(page([many]));
    renderPage();
    const card = (await screen.findByText('#8')).closest('li')!;
    await user.selectOptions(within(card).getByLabelText('Jednotka #202'), 'B');
    await user.click(within(card).getByRole('button', { name: 'Save split' }));
    const sent = vi.mocked(api.postAutodedupSplitVerdict).mock.calls[0][0];
    /* The server refuses an assignment that does not name the whole cluster —
     * rightly: the two hidden members would otherwise be ruled on by omission. */
    expect(sent.units.map((u) => u.listing_id)).toEqual([101, 202, 303, 404, 505, 606]);
    expect(sent.units.filter((u) => u.unit === 'A')).toHaveLength(5);
  });

  it('sends the relation the operator picked', async () => {
    const user = userEvent.setup();
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    await user.selectOptions(within(card).getByLabelText('Jednotka #202'), 'B');
    await user.selectOptions(within(card).getByLabelText('Vztah mezi jednotkami'), 'different');
    await user.click(within(card).getByRole('button', { name: 'Save split' }));
    expect(vi.mocked(api.postAutodedupSplitVerdict).mock.calls[0][0].relation).toBe('different');
  });

  it('keeps the operator\'s letters when the split is refused', async () => {
    const user = userEvent.setup();
    vi.mocked(api.postAutodedupSplitVerdict).mockRejectedValue(new Error('migration 532'));
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    await user.selectOptions(within(card).getByLabelText('Jednotka #202'), 'B');
    await user.click(within(card).getByRole('button', { name: 'Save split' }));
    expect(await within(card).findByText(/migration 532/)).toBeInTheDocument();
    /* The work is not thrown away: the assignment is still on screen to correct. */
    expect(within(card).getByLabelText('Jednotka #202')).toHaveValue('B');
    expect(within(card).getByRole('button', { name: 'Save split' })).toBeInTheDocument();
  });

  it('carries the card\'s assignment into the dialog', async () => {
    const user = userEvent.setup();
    vi.mocked(api.getAutodedupGroup).mockResolvedValue({
      store_ready: true,
      data: {
        cluster: group({ cluster_key: 7 }),
        members: [101, 202].map((id) => ({ ...member({ listing_id: id }), images: FRAMES })),
        pairs: [],
        judgements: [],
        conflicts: [],
        verdicts: [],
      },
    });
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    await user.selectOptions(within(card).getByLabelText('Jednotka #202'), 'B');
    await user.click(within(card).getByRole('button', { name: 'Open' }));
    const dialog = await screen.findByRole('dialog');
    /* ONE assignment, two views of it: a dialog that started blank would throw
     * away the letters the operator had already set. */
    expect(await within(dialog).findByLabelText('Jednotka #202')).toHaveValue('B');
    expect(within(dialog).getByRole('button', { name: 'Save split' })).toBeInTheDocument();
  });

  it('opens a group onto its members and its edges', async () => {
    const user = userEvent.setup();
    vi.mocked(api.getAutodedupGroup).mockResolvedValue({
      store_ready: true,
      data: {
        cluster: group({ cluster_key: 7 }),
        members: [
          {
            ...member({ listing_id: 101 }),
            images: [
              { storage_path: null, sreality_url: 'https://img.example.invalid/a.jpg', sequence: 1, phash: '1', image_id: 1 },
            ],
          },
        ],
        pairs: [
          {
            listing_lo: 101,
            listing_hi: 202,
            score: 0.61,
            zone: 'band',
            decision: 'model',
            guard_veto: null,
            certificate: null,
            families: 1,
            probes: ['k1'],
            cluster_key: 7,
          },
        ],
        judgements: [
          {
            listing_lo: 101,
            listing_hi: 202,
            judge_version: 'j1',
            tier: 'text',
            model: 'gpt-5-mini',
            verdict: 'same_property',
            confidence: 0.93,
            unit_discriminator: null,
            key_evidence: ['same street and number'],
            contradicting_evidence: [],
            developer_project_suspected: false,
            created_at: '2026-09-15T00:00:00Z',
          },
        ],
        conflicts: [],
        verdicts: [],
      },
    });
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    await user.click(within(card).getByRole('button', { name: 'Open' }));
    const dialog = await screen.findByRole('dialog');
    expect(api.getAutodedupGroup).toHaveBeenCalledWith(7, 'g1');
    expect(await within(dialog).findByText(/judge: same property/)).toBeInTheDocument();
    /* The drill-down carries the generation the queue was reading — otherwise
     * the evidence page silently answers for the default one. */
    expect(within(dialog).getByRole('link', { name: 'Evidence' })).toHaveAttribute(
      'href',
      '/autodedup/pair/101/202?generation=g1',
    );
  });
});
