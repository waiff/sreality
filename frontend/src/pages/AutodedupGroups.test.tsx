/* AutodedupGroups — the proposed-cluster review queue.
 *
 * Hermetic: every API read and the one write are mocked.
 *
 * Pins:
 *   * the page says the trial merges nothing — "Stejné" must never read as
 *     "merge now";
 *   * a card carries its cluster key, its size, its members and the weakest
 *     edge, and shows at most four members with the rest COUNTED, not cropped;
 *   * a filter control sends a KEY on the query, and resets the keyset;
 *   * the verdict control offers exactly three answers, and a stored finer
 *     value reads back as "Různé";
 *   * a verdict click posts the cluster verdict and the badge flips to it
 *     optimistically — with no second confirm, because a cluster verdict writes
 *     nothing permanent;
 *   * an un-migrated store renders the empty state instead of failing, and a
 *     FAILED read never claims the queue is empty;
 *   * "Load more" pages by the cursor the previous page returned;
 *   * a card PAGES each member's photos rather than judging it on one cover;
 *   * the split row appears once two units are actually chosen and STAYS once a
 *     split is stored, so the one-unit assignment — the undo — can be sent;
 *   * Save sends EVERY member of the group, each with a control of its own,
 *     including the ones the card counted rather than showed;
 *   * the split is LETTERS ONLY (D39) — no relation control, no relation on the
 *     wire — while the stored ruling is read back off the members' pair
 *     verdicts, including the finer values a pre-D39 ruling carries, and a save
 *     that would take back an earlier ruling asks before it does;
 *   * the receipt reports what was STORED, not what the selects say afterwards;
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
    getAutodedupGenerations: vi.fn(),
    postAutodedupVerdict: vi.fn(),
    postAutodedupSplitVerdict: vi.fn(),
    getAutodedupVerdictReasons: vi.fn(),
    getAutodedupValidationProgress: vi.fn(),
  };
});

/* The D6 session counter's read — cluster grain on this queue. */
const PROGRESS = {
  store_ready: true,
  data: {
    generation: 'g3',
    surface: 'groups' as const,
    seed: 'v1',
    sample_size: 100,
    grain: 'cluster' as const,
    sample: { n: 100, n_reviewed: 37, n_not_same: 2 },
    total: { n: 870, n_reviewed: 103, n_not_same: 5 },
  },
};

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
    stale_verdict: null,
    member_verdicts: [],
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

/* One PAIR verdict among a group's members — what a stored split actually is. */
function verdictRow(
  over: Partial<AutodedupVerdictRow> & { listing_lo: number; listing_hi: number },
): AutodedupVerdictRow {
  return {
    id: 1,
    kind: 'pair',
    cluster_key: null,
    verdict: 'same',
    note: null,
    decided_by: 'operator@example.invalid',
    decided_at: '2026-09-16T10:00:00Z',
    ...over,
  };
}

function page(
  items: AutodedupGroup[],
  nextAfter: string | null = null,
  total: number | null = null,
) {
  return {
    store_ready: true,
    /* `generation` is the server's ANSWER: the queue asks for "the newest pass"
     * by sending no generation at all, and only the reply says which it was. */
    data: { items, has_more: nextAfter != null, next_after: nextAfter, total, generation: 'g3' },
  };
}

const GENERATIONS = {
  store_ready: true,
  data: {
    latest: 'g3',
    items: [
      {
        generation: 'g3',
        n_clusters: 1204,
        n_members: 2600,
        n_conflicted: 3,
        last_changed_at: '2026-09-17T06:00:00Z',
      },
      {
        generation: 'g1',
        n_clusters: 9,
        n_members: 21,
        n_conflicted: 1,
        last_changed_at: '2026-08-20T06:00:00Z',
      },
    ],
  },
};

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
    vi.mocked(api.getAutodedupGenerations).mockResolvedValue(GENERATIONS);
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
    vi.mocked(api.getAutodedupVerdictReasons).mockResolvedValue([
      { code: 'floor_plan_differs', label: 'Jiný půdorys' },
      { code: 'same_project', label: 'Stejný projekt' },
    ]);
    vi.mocked(api.getAutodedupValidationProgress).mockResolvedValue(PROGRESS);
  });

  /* ------------------------------- E58: a verdict binds the set it was taken on */

  it('reads a carried-over verdict as unreviewed and says which adverts moved', async () => {
    /* The defect this repairs: promoting g5 re-stamped 836 of g4's cluster keys,
     * and 21 of the operator's 224 confirmations landed on a group whose
     * membership had moved under them with nothing on screen to say so. */
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(
      page([
        group({
          cluster_key: 7,
          members: [
            member({ listing_id: 101 }),
            member({ listing_id: 202, source: 'bazos' }),
            member({ listing_id: 303, source: 'idnes' }),
          ],
          verdict: null,
          stale_verdict: {
            verdict: 'same',
            note: null,
            decided_by: 'operator@example.invalid',
            decided_at: '2026-09-18T10:00:00Z',
            generation: 'g4',
            member_ids: [101, 202],
            added: [303],
            removed: [404],
          },
        }),
      ]),
    );
    renderPage();
    const notice = await screen.findByTestId('stale-verdict-notice');
    expect(notice.textContent).toContain('Potvrzeno v g4 pro jinou sestavu inzerátů');
    expect(notice.textContent).toContain('přibyly #303');
    expect(notice.textContent).toContain('ubyly #404');
    /* The advert that arrived is marked on its OWN card, which is where the
     * question "is this one of them too?" is actually asked. */
    expect(screen.getByTestId('stale-added-303')).toBeTruthy();
    expect(screen.queryByTestId('stale-added-101')).toBeNull();
    /* And the group is UNREVIEWED: no button is pressed, so the operator rules
     * it again rather than inheriting a claim about adverts nobody looked at. */
    for (const button of screen.getAllByRole('button')) {
      expect(button.getAttribute('aria-pressed')).not.toBe('true');
    }
  });

  it('names a STALE ruling in the vocabulary the page speaks', async () => {
    /* A stale verdict is by definition an older ruling, so it is exactly where
     * the two retired values turn up — and „verdikt same_project_different_unit"
     * is a word no button on this page says any more (D39). */
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(
      page([
        group({
          cluster_key: 7,
          verdict: null,
          stale_verdict: {
            verdict: 'same_project_different_unit',
            note: null,
            decided_by: 'operator@example.invalid',
            decided_at: '2026-09-18T10:00:00Z',
            generation: 'g4',
            member_ids: [101],
            added: [202],
            removed: [],
          },
        }),
      ]),
    );
    renderPage();
    const notice = await screen.findByTestId('stale-verdict-notice');
    expect(notice.textContent).toContain('verdikt Různé');
    expect(notice.textContent).not.toContain('same_project_different_unit');
  });

  it('hides the notice once this session has ruled the group again', async () => {
    const user = userEvent.setup();
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(
      page([
        group({
          cluster_key: 7,
          verdict: null,
          stale_verdict: {
            verdict: 'same',
            note: null,
            decided_by: 'operator@example.invalid',
            decided_at: '2026-09-18T10:00:00Z',
            generation: 'g4',
            member_ids: [101],
            added: [202],
            removed: [],
          },
        }),
      ]),
    );
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Stejné' }));
    await waitFor(() => expect(screen.queryByTestId('stale-verdict-notice')).toBeNull());
  });

  it('offers "změněno od verdiktu" as a verdict filter and sends it as a key', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/#7/);
    const select = screen.getByLabelText('Verdict');
    await user.selectOptions(select, 'changed');
    await waitFor(() =>
      expect(screen.getByTestId('search').textContent).toContain('verdict=changed'),
    );
    expect(
      within(select).getByRole('option', { name: 'změněno od verdiktu' }),
    ).toBeTruthy();
  });

  /* --------------------------------------- the seeded sample + blind review */

  it('offers the seeded sample order, and the counter counts it', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('#7');
    /* The whole-generation count is on the strip from the start; the sample
     * half only when the queue IS the sample. */
    await screen.findByText(/Zkontrolováno:/);
    expect(screen.queryByTestId('validation-sample')).toBeNull();

    await user.selectOptions(screen.getByLabelText('Sort'), 'random');
    await waitFor(() =>
      expect(api.getAutodedupGroups).toHaveBeenLastCalledWith(
        expect.objectContaining({ sort: 'random', seed: 'v1', after: null }),
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId('validation-sample')).toHaveTextContent(
        /Náhodný vzorek: 37 \/ 100 zkontrolováno · 2 jiných než/,
      ),
    );
    const search = screen.getByTestId('search').textContent ?? '';
    expect(search).toContain('sort=random');
    expect(search).not.toContain('seed=');
  });

  it('is NOT blind by default, and blinds the whole card when asked', async () => {
    const user = userEvent.setup();
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    /* The judged chip is a judge artefact like the chip on a pair is. */
    expect(within(card).getByText('judged 1')).toBeInTheDocument();
    await user.click(screen.getByLabelText(/naslepo/));
    await waitFor(() => expect(screen.queryByText('judged 1')).toBeNull());
    expect(screen.getByTestId('search').textContent).toContain('blind=1');
    /* And it comes back the moment the group carries the operator's ruling. */
    await user.click(within(card).getByRole('button', { name: 'Stejné' }));
    await waitFor(() => expect(screen.getByText('judged 1')).toBeInTheDocument());
  });

  /* ------------------------------------------------- the operator's reasons (mig 533) */

  it('sends the chips and the note with a cluster verdict', async () => {
    const user = userEvent.setup();
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    /* Collapsed on a queue card, like the residual rows. */
    await user.click(within(card).getByRole('button', { name: '+ důvod verdiktu' }));
    await user.click(within(card).getByRole('button', { name: 'Stejný projekt' }));
    await user.click(within(card).getByRole('button', { name: 'Stejné' }));
    expect(api.postAutodedupVerdict).toHaveBeenCalledWith({
      kind: 'cluster',
      cluster_key: 7,
      // WHICH PASS the ruling was taken on (E58) — the server refuses one without it.
      generation: 'g1',
      verdict: 'same',
      reasons: ['same_project'],
      note: null,
    });
  });

  it('stamps ONE reason set on the split it sends', async () => {
    const user = userEvent.setup();
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    await user.selectOptions(within(card).getByLabelText('Jednotka #202'), 'B');
    /* The split row has a picker of its OWN: what the operator saw when they
      * separated the group is not what they saw when they confirmed it. The two
      * are told apart by NAME, not by position — an index would pin the very
      * ambiguity that loses the operator's chips. */
    await user.click(within(card).getByRole('button', { name: '+ důvod rozdělení' }));
    await user.click(within(card).getByRole('button', { name: 'Jiný půdorys' }));
    await user.click(within(card).getByRole('button', { name: 'Save split' }));
    const sent = vi.mocked(api.postAutodedupSplitVerdict).mock.calls[0][0];
    expect(sent.reasons).toEqual(['floor_plan_differs']);
    expect(sent.note).toBeNull();
  });

  it('names the split picker and the verdict picker apart', async () => {
    const user = userEvent.setup();
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    await user.selectOptions(within(card).getByLabelText('Jednotka #202'), 'B');
    /* Two drafts, two destinations: chips ticked in one are NOT sent by the
     * other, so the toggles must say which ruling they belong to. */
    const toggles = within(card)
      .getAllByRole('button', { name: /^\+ důvod/ })
      .map((el) => el.textContent);
    expect(toggles).toEqual(['+ důvod rozdělení', '+ důvod verdiktu']);
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

  it('shows EVERY advert of the group, not the first four', async () => {
    /* The operator's own report: "I see only 4 adverts here while it says there
     * should be 5". A card that counts what it will not show is a card that
     * looks smaller than the group it is asking about. */
    const five = group({
      cluster_key: 8,
      size: 5,
      members: [101, 202, 303, 404, 505].map((id) => member({ listing_id: id })),
    });
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(page([five]));
    renderPage();
    const card = (await screen.findByText('#8')).closest('li')!;
    for (const id of [101, 202, 303, 404, 505]) {
      expect(within(card).getByText(`#${id}`)).toBeInTheDocument();
      expect(within(card).getByLabelText(`Jednotka #${id}`)).toBeInTheDocument();
    }
    expect(within(card).queryByText(/further advert/)).toBeNull();
  });

  it('folds a very large group behind one button that expands it in place', async () => {
    const user = userEvent.setup();
    const ids = Array.from({ length: 14 }, (_, i) => 101 + i);
    const huge = group({
      cluster_key: 11,
      size: ids.length,
      members: ids.map((id) => member({ listing_id: id })),
    });
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(page([huge]));
    renderPage();
    const card = (await screen.findByText('#11')).closest('li')!;
    /* Twelve on screen, the rest one click away — on the same card, never in a
     * drawer and never merely counted. */
    expect(within(card).getAllByLabelText(/^Jednotka #/)).toHaveLength(12);
    const expander = within(card).getByRole('button', { name: /zobrazit všech 14 inzerátů/ });
    await user.click(expander);
    expect(within(card).getAllByLabelText(/^Jednotka #/)).toHaveLength(14);
    expect(within(card).queryByRole('button', { name: /zobrazit všech/ })).toBeNull();
  });

  it('sends every member of a folded group, expanded or not', async () => {
    const user = userEvent.setup();
    const ids = Array.from({ length: 14 }, (_, i) => 101 + i);
    const huge = group({
      cluster_key: 11,
      size: ids.length,
      members: ids.map((id) => member({ listing_id: id })),
    });
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(page([huge]));
    renderPage();
    const card = (await screen.findByText('#11')).closest('li')!;
    /* Touching one unit un-folds the card: an assignment travels WHOLE, and a
     * letter set over adverts nobody can see is a ruling by omission. */
    await user.selectOptions(within(card).getByLabelText('Jednotka #102'), 'B');
    expect(within(card).getAllByLabelText(/^Jednotka #/)).toHaveLength(14);
    await user.click(within(card).getByRole('button', { name: 'Save split' }));
    const sent = vi.mocked(api.postAutodedupSplitVerdict).mock.calls[0][0];
    expect(sent.units.map((u) => u.listing_id)).toEqual(ids);
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
    const confirm = within(card).getByRole('button', { name: 'Stejné' });
    expect(confirm).toHaveAttribute('aria-pressed', 'false');
    await user.click(confirm);
    /* A cluster verdict writes nothing permanent, so it never arms a second
     * click the way a pair's negative verdict does. */
    expect(api.postAutodedupVerdict).toHaveBeenCalledWith({
      kind: 'cluster',
      cluster_key: 7,
      generation: 'g1',
      verdict: 'same',
      /* The annotation rides with every verdict — empty when the operator gave
        * none, never absent, so the stored row is the click's whole statement. */
      reasons: [],
      note: null,
    });
    await waitFor(() => expect(confirm).toHaveAttribute('aria-pressed', 'true'));
    expect(within(card).getByText(/operator@example.invalid/)).toBeInTheDocument();
  });

  /* ---------------------------------------- D39: three answers, not five */

  it('offers exactly three answers on a group card', async () => {
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    for (const word of ['Stejné', 'Různé', 'Nevím']) {
      expect(within(card).getByRole('button', { name: word })).toBeInTheDocument();
    }
    /* The finer breakdown bought no decision — every negative writes the same
     * permanent must-not-link — so it is off the card, not hidden on it. */
    expect(within(card).queryByRole('button', { name: /budova/i })).toBeNull();
    expect(within(card).queryByRole('button', { name: /projekt/i })).toBeNull();
  });

  it('shows a ruling taken under the older vocabulary as "Různé"', async () => {
    const old = group({
      cluster_key: 14,
      verdict: { ...STORED, cluster_key: 14, verdict: 'same_project_different_unit' },
    });
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(page([old]));
    renderPage();
    const card = (await screen.findByText('#14')).closest('li')!;
    /* Nothing is rewritten in the database: the row still says
     * `same_project_different_unit`, and the page says what it MEANS. */
    expect(within(card).getByRole('button', { name: 'Různé' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(within(card).getByRole('button', { name: 'Stejné' })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });

  it('rolls the badge back when the write fails', async () => {
    const user = userEvent.setup();
    vi.mocked(api.postAutodedupVerdict).mockRejectedValue(new Error('nope'));
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    const confirm = within(card).getByRole('button', { name: 'Stejné' });
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


  /* ----------------------------------------------- WHICH GENERATION */

  it('opens on the newest pass instead of a hard-coded generation', async () => {
    /* THE DEFECT. The page and the API both defaulted to `g1` — the first
     * hand-prior pass, which over-merged developer units and was superseded by
     * `g2` and `g3` — so the queue served proposals the live engine no longer
     * makes, and nothing on screen said so. The parameter is now omitted and the
     * server answers with the pass it read. */
    renderPage();
    await screen.findByText('#7');
    expect(api.getAutodedupGroups).toHaveBeenLastCalledWith(
      expect.objectContaining({ generation: null }),
    );
    expect(screen.getByLabelText('Generation')).toHaveValue('');
    await waitFor(() =>
      expect(
        within(screen.getByLabelText('Generation')).getByRole('option', {
          name: 'nejnovější (g3)',
        }),
      ).toBeInTheDocument(),
    );
  });

  it('offers the passes that exist instead of a free-text generation field', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('#7');
    const select = screen.getByLabelText('Generation');
    expect(select.tagName).toBe('SELECT');
    await waitFor(() =>
      expect(within(select).getByRole('option', { name: 'g1 · 9 skupin' })).toBeInTheDocument(),
    );
    /* An older pass stays readable — that is how a past review is re-examined —
     * and the choice lands in the url so the view can be shared. */
    await user.selectOptions(select, 'g1');
    await waitFor(() =>
      expect(api.getAutodedupGroups).toHaveBeenLastCalledWith(
        expect.objectContaining({ generation: 'g1', after: null }),
      ),
    );
    expect(screen.getByTestId('search')).toHaveTextContent('generation=g1');
  });

  it('says so when the queue is an older pass, and returns in one click', async () => {
    const user = userEvent.setup();
    renderPage('/autodedup/groups?generation=g1');
    await screen.findByText('#7');
    const notice = await screen.findByRole('status');
    expect(notice).toHaveTextContent('starší generaci');
    expect(notice).toHaveTextContent('g1');
    expect(notice).toHaveTextContent('g3');
    await user.click(within(notice).getByRole('button', { name: 'Zobrazit nejnovější (g3)' }));
    await waitFor(() =>
      expect(api.getAutodedupGroups).toHaveBeenLastCalledWith(
        expect.objectContaining({ generation: null }),
      ),
    );
    expect(screen.getByTestId('search')).not.toHaveTextContent('generation=');
  });

  it('keeps quiet while the queue is the current pass', async () => {
    renderPage();
    await screen.findByText('#7');
    await waitFor(() => expect(api.getAutodedupGenerations).toHaveBeenCalled());
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
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

  it('folds a link written in the older verdict vocabulary onto „různé"', async () => {
    /* D39 dropped the two finer values from the SELECT, not from the store — so
     * a bookmark asking for them is asking for negatives, and handing it the
     * whole queue would silently widen a saved question. The server widens
     * `different` over all three, so the fold loses nothing. */
    renderPage('/autodedup/groups?verdict=same_project_different_unit');
    await screen.findByText('#7');
    expect(api.getAutodedupGroups).toHaveBeenLastCalledWith(
      expect.objectContaining({ verdict: 'different' }),
    );
    expect(screen.getByLabelText('Verdict')).toHaveValue('different');
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

  it('posts the assignment and every member, and names NO relation (D39)', async () => {
    const user = userEvent.setup();
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    await user.selectOptions(within(card).getByLabelText('Jednotka #202'), 'B');
    await user.click(within(card).getByRole('button', { name: 'Save split' }));
    /* The whole body: two letters are two properties, and that is the entire
     * statement — the server defaults the relation to `different`. */
    expect(api.postAutodedupSplitVerdict).toHaveBeenCalledWith({
      cluster_key: 7,
      generation: 'g1',
      units: [
        { listing_id: 101, unit: 'A' },
        { listing_id: 202, unit: 'B' },
      ],
      reasons: [],
      note: null,
    });
    /* The stored cluster verdict lands on the badge, in the page's own three
     * words — the server answered with a pre-D39 value and it reads "Různé". */
    await waitFor(() =>
      expect(within(card).getByRole('button', { name: 'Různé' })).toHaveAttribute(
        'aria-pressed',
        'true',
      ),
    );
    expect(within(card).getByText(/1 jako různé/)).toBeInTheDocument();
  });

  it('offers NO relation control anywhere on the split', async () => {
    const user = userEvent.setup();
    const three = group({
      cluster_key: 9,
      size: 3,
      members: [101, 202, 303].map((id) => member({ listing_id: id })),
    });
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(page([three]));
    renderPage();
    const card = (await screen.findByText('#9')).closest('li')!;
    await user.selectOptions(within(card).getByLabelText('Jednotka #202'), 'B');
    await user.selectOptions(within(card).getByLabelText('Jednotka #303'), 'C');
    /* Three unit pairs, and not one select among them: the operator says which
     * adverts are one unit, never what KIND of different two units are. */
    expect(within(card).queryByLabelText(/Vztah/)).toBeNull();
    await user.click(within(card).getByRole('button', { name: 'Save split' }));
    const sent = vi.mocked(api.postAutodedupSplitVerdict).mock.calls[0][0];
    expect(sent).not.toHaveProperty('relation');
    expect(sent).not.toHaveProperty('relations');
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

  it('gives EVERY member a unit control, including the ones it only counted', async () => {
    const many = group({
      cluster_key: 8,
      size: 6,
      members: [101, 202, 303, 404, 505, 606].map((id) => member({ listing_id: id })),
    });
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(page([many]));
    renderPage();
    const card = (await screen.findByText('#8')).closest('li')!;
    /* The assignment travels whole — a member with no control on screen would be
     * ruled on by omission, and the operator would have asserted something about
     * adverts they never saw. */
    for (const id of [101, 202, 303, 404, 505, 606]) {
      expect(within(card).getByLabelText(`Jednotka #${id}`)).toBeInTheDocument();
    }
  });

  /* --------------------------------------------- the ruling that is stored */

  it('shows the split that is STORED, not a blank slate', async () => {
    const stored = group({
      cluster_key: 11,
      size: 3,
      members: [101, 202, 303].map((id) => member({ listing_id: id })),
      member_verdicts: [
        verdictRow({ listing_lo: 101, listing_hi: 202, verdict: 'same' }),
        verdictRow({ listing_lo: 101, listing_hi: 303, verdict: 'same_building_different_unit' }),
        verdictRow({ listing_lo: 202, listing_hi: 303, verdict: 'same_building_different_unit' }),
      ],
    });
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(page([stored]));
    renderPage();
    const card = (await screen.findByText('#11')).closest('li')!;
    /* 101 and 202 were ruled one property, 303 is a different unit of that
     * building — the letters say so before the operator can act on them. */
    expect(within(card).getByLabelText('Jednotka #101')).toHaveValue('A');
    expect(within(card).getByLabelText('Jednotka #202')).toHaveValue('A');
    expect(within(card).getByLabelText('Jednotka #303')).toHaveValue('B');
    expect(within(card).getByText(/Uloženo dříve/)).toHaveTextContent('A: 101,202 · B: 303');
  });

  it('keeps the row on screen at one unit, so a split can be UNDONE', async () => {
    const user = userEvent.setup();
    const stored = group({
      cluster_key: 12,
      member_verdicts: [
        verdictRow({ listing_lo: 101, listing_hi: 202, verdict: 'same_project_different_unit' }),
      ],
    });
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(page([stored]));
    renderPage();
    const card = (await screen.findByText('#12')).closest('li')!;
    await user.selectOptions(within(card).getByLabelText('Jednotka #202'), 'A');
    /* The one-unit assignment is what the server implements as "every pair same,
     * every veto retracted". A row that vanished here made the undo unreachable,
     * and the whole-group "Stejné" is not it: that writes `same` on the cluster
     * and leaves the pair vetoes standing. */
    const undo = within(card).getByRole('button', { name: 'Sloučit zpět' });
    await user.click(undo);
    const sent = vi.mocked(api.postAutodedupSplitVerdict).mock.calls[0][0];
    expect(sent.units).toEqual([
      { listing_id: 101, unit: 'A' },
      { listing_id: 202, unit: 'A' },
    ]);
    expect(sent).not.toHaveProperty('relations');
  });

  it('asks before taking back an earlier ruling, then sends the confirmation', async () => {
    const user = userEvent.setup();
    vi.mocked(api.postAutodedupSplitVerdict).mockRejectedValueOnce(
      new api.ApiError('this split takes back your earlier ruling on 1 pair(s) (101-202)', 409, null),
    );
    const stored = group({
      cluster_key: 13,
      member_verdicts: [verdictRow({ listing_lo: 101, listing_hi: 202, verdict: 'different' })],
    });
    vi.mocked(api.getAutodedupGroups).mockResolvedValue(page([stored]));
    renderPage();
    const card = (await screen.findByText('#13')).closest('li')!;
    await user.selectOptions(within(card).getByLabelText('Jednotka #202'), 'A');
    await user.click(within(card).getByRole('button', { name: 'Sloučit zpět' }));
    expect(await within(card).findByText(/takes back your earlier ruling/)).toBeInTheDocument();
    await user.click(within(card).getByRole('button', { name: 'Přepsat a uložit' }));
    expect(vi.mocked(api.postAutodedupSplitVerdict).mock.calls[1][0].confirm_retract).toBe(true);
  });

  it('reports what it STORED, not what the selects say afterwards', async () => {
    const user = userEvent.setup();
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    await user.selectOptions(within(card).getByLabelText('Jednotka #202'), 'B');
    await user.click(within(card).getByRole('button', { name: 'Save split' }));
    const receipt = await within(card).findByText(/Uloženo:/);
    expect(receipt).toHaveTextContent('A: 101 · B: 202');
    expect(receipt).toHaveTextContent('1 jako různé');
    /* Touching a letter after the save must not rewrite the confirmation of a
     * ruling that WAS sent — with the server's own counts lending it authority. */
    await user.selectOptions(within(card).getByLabelText('Jednotka #202'), 'A');
    expect(within(card).getByText(/Uloženo:/)).toHaveTextContent('A: 101 · B: 202');
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

  it('shows each member\'s advert text, marked and collapsed, in the dialog', async () => {
    /* The operator\'s own ask: a developer project\'s units share the photos and
     * the attribute row, so the TEXT is the only place the unit number, the floor
     * and the orientation differ. Long adverts open collapsed — five members of
     * two thousand characters each is a dialog nobody scrolls. */
    const user = userEvent.setup();
    const long = `Byt č. 14 ve 4. patře, 68 m². ${'Klidná lokalita, jižní orientace. '.repeat(20)}`;
    vi.mocked(api.getAutodedupGroup).mockResolvedValue({
      store_ready: true,
      data: {
        cluster: group({ cluster_key: 7 }),
        members: [
          {
            ...member({ listing_id: 101 }),
            images: FRAMES,
            title: 'Prodej bytu 3+kk 68 m²',
            description: long,
            description_truncated: false,
            description_chars: long.length,
          },
          {
            ...member({ listing_id: 202, source: 'bazos' }),
            images: FRAMES,
            title: 'Prodej bytu 3+kk, Jihlava',
            description: 'Byt č. 3 v přízemí.',
            description_truncated: false,
            description_chars: 19,
          },
        ],
        pairs: [],
        judgements: [],
        conflicts: [],
        verdicts: [],
      },
    });
    renderPage();
    const card = (await screen.findByText('#7')).closest('li')!;
    await user.click(within(card).getByRole('button', { name: 'Open' }));
    const dialog = await screen.findByRole('dialog');

    expect(await within(dialog).findByText('Prodej bytu 3+kk 68 m²')).toBeInTheDocument();
    expect(within(dialog).getByText('Prodej bytu 3+kk, Jihlava')).toBeInTheDocument();
    /* The tokens that decide the question are marked; the town is not a compass point. */
    const marks = [...dialog.querySelectorAll('mark')].map((m) => m.textContent);
    expect(marks).toEqual(
      expect.arrayContaining(['Byt č. 14', '4. patře', '68 m²', 'Byt č. 3', 'přízemí']),
    );
    expect(marks).not.toContain('Jihlava');

    /* One toggle: the SHORT advert is simply shown, and a control that does
     * nothing is worse than no control. */
    const toggle = within(dialog).getByRole('button', { name: /zobrazit celý popis/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await user.click(toggle);
    expect(
      within(dialog).getByRole('button', { name: /skrýt/ }),
    ).toHaveAttribute('aria-expanded', 'true');
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
