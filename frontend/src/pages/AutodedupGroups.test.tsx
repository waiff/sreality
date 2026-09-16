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
 *   * no interactive control is nested inside another.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

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
    postAutodedupVerdict: vi.fn(),
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

function page(items: AutodedupGroup[], nextAfter: string | null = null) {
  return {
    store_ready: true,
    data: { items, has_more: nextAfter != null, next_after: nextAfter },
  };
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AutodedupGroups />
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
    vi.mocked(api.postAutodedupVerdict).mockResolvedValue({ store_ready: true, data: STORED, must_not_link: false });
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
