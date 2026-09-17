/* AutodedupPair — the full-evidence page for one pair.
 *
 * Hermetic: the evidence read and the verdict write are mocked.
 *
 * Pins:
 *   * the route params reach the API as numbers, and a non-numeric one is
 *     answered as a typo rather than requested;
 *   * an ABSENT feature prints "absent" and never a zero — most near-misses in
 *     this engine are absences;
 *   * each photo carries the best Hamming distance to the other side, and a
 *     photo with no match says so;
 *   * the judge's transcript shows both evidence lists;
 *   * a negative verdict still takes two clicks here;
 *   * no interactive control is nested inside another.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import AutodedupPair from './AutodedupPair';
import * as api from '@/lib/api';
import { ROUTES } from '@/lib/routes';
import { expectNoNestedInteractive } from '@/test/a11y';
import type { AutodedupDigest, AutodedupMember, AutodedupPairDetail } from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    getAutodedupPair: vi.fn(),
    postAutodedupVerdict: vi.fn(),
  };
});

function member(listing_id: number): AutodedupMember {
  return {
    listing_id,
    source: 'sreality',
    source_url: null,
    category_main: 'byt',
    category_type: 'prodej',
    disposition: '2+kk',
    area_m2: 54,
    floor: 3,
    price_czk: 5_900_000,
    first_seen_at: '2026-01-04T00:00:00Z',
    last_seen_at: '2026-03-01T00:00:00Z',
    is_active: true,
    cover: null,
    n_images: 2,
  };
}

function digest(listing_id: number): AutodedupDigest {
  return {
    listing_id,
    portal: 'sreality',
    deal: 'prodej',
    category: 'byt',
    subtype: null,
    disposition: '2+kk',
    area_m2: 54,
    floor: 3,
    total_floors: 5,
    price: 5_900_000,
    price_unit: 'czk',
    price_history: [['2026-01-04', 6_100_000]],
    attributes: { Výtah: 'ano' },
    first_seen: '2026-01-04',
    last_seen: '2026-03-01',
    active: true,
    description: 'Světlý byt v cihlovém domě.',
    description_truncated: false,
    absent: ['floor'],
    source_url: `https://www.sreality.cz/detail/${listing_id}`,
  };
}

const DETAIL: AutodedupPairDetail = {
  pair: {
    listing_lo: 101,
    listing_hi: 202,
    score: 0.44,
    zone: 'band',
    decision: 'evidence_gate',
    guard_veto: null,
    certificate: null,
    families: 1 | 32,
    probes: ['k1', 'k5'],
    cluster_key: null,
  },
  listings: { lo: member(101), hi: member(202) },
  digests: { lo: digest(101), hi: digest(202) },
  images: {
    lo: [
      {
        image_id: 1,
        storage_path: null,
        sreality_url: 'https://img.example.invalid/a.jpg',
        sequence: 1,
        phash: '123',
        best_hamming: 4,
        best_match_image_id: 77,
      },
      {
        image_id: 2,
        storage_path: null,
        sreality_url: 'https://img.example.invalid/b.jpg',
        sequence: 2,
        phash: '124',
        best_hamming: null,
        best_match_image_id: null,
      },
    ],
    hi: [],
  },
  features: [
    { name: 'area_rel_diff', value: 0.01, present: true, contribution: 0.8 },
    { name: 'street_equal', value: null, present: false, contribution: null },
  ],
  judgements: [
    {
      listing_lo: 101,
      listing_hi: 202,
      judge_version: 'j1',
      tier: 'vision',
      model: 'gpt-5-mini',
      verdict: 'same_property',
      confidence: 0.93,
      unit_discriminator: 'floor',
      key_evidence: ['same kitchen tiles'],
      contradicting_evidence: ['different floor stated'],
      developer_project_suspected: false,
      created_at: '2026-09-15T00:00:00Z',
    },
  ],
  verdicts: [],
  family_names: ['ATTR', 'IMG'],
};

/* `string`, not RoutePath: one case below deliberately enters a path the
 * registry could never build. */
function renderPair(path: string = ROUTES.autodedupPair.build({ lo: 101, hi: 202 })) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={ROUTES.autodedupPair.pattern} element={<AutodedupPair />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('<AutodedupPair>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAutodedupPair).mockResolvedValue({ store_ready: true, data: DETAIL });
    vi.mocked(api.postAutodedupVerdict).mockResolvedValue({
      store_ready: true,
      data: {
        id: 1,
        kind: 'pair',
        cluster_key: null,
        listing_lo: 101,
        listing_hi: 202,
        verdict: 'different',
        note: null,
        decided_by: 'operator@example.invalid',
        decided_at: '2026-09-16T10:00:00Z',
      },
      must_not_link: true,
    });
  });

  it('asks for the pair named in the route', async () => {
    renderPair();
    await screen.findByText('area_rel_diff');
    expect(api.getAutodedupPair).toHaveBeenCalledWith(101, 202, null);
  });

  it('answers a non-numeric id as a typo and asks for nothing', async () => {
    renderPair('/autodedup/pair/abc/202');
    expect(await screen.findByText(/not a pair of listing ids/)).toBeInTheDocument();
    expect(api.getAutodedupPair).not.toHaveBeenCalled();
  });

  it('prints an absent feature as absent, never as zero', async () => {
    renderPair();
    const row = (await screen.findByText('street_equal')).closest('tr')!;
    expect(row).toHaveTextContent('absent');
    expect(row).not.toHaveTextContent('0,00');
  });

  it('shows the best photo match, and says so when there is none', async () => {
    renderPair();
    expect(await screen.findByText(/Δ4 → 77/)).toBeInTheDocument();
    expect(screen.getByText('no match')).toBeInTheDocument();
  });

  it('labels an evidence photo the portal refuses to serve, keeping its delta', async () => {
    renderPair();
    const caption = await screen.findByText(/Δ4 → 77/);
    const cell = caption.closest('li')!;
    const img = within(cell).getByRole('presentation', { hidden: true }) as HTMLImageElement;
    /* The R2 copy can be missing and the portal CDN refuses the fallback request
     * cross-origin (ERR_BLOCKED_BY_ORB) — an empty box. This grid is where a per
     * image Hamming delta is judged, so the box has to say why it is empty. */
    fireEvent.error(img);
    expect(within(cell).getByText('foto nedostupné')).toBeInTheDocument();
    expect(within(cell).queryByRole('presentation', { hidden: true })).toBeNull();
    expect(cell).toHaveTextContent('Δ4 → 77');
  });

  it('shows the judge transcript with both evidence lists', async () => {
    renderPair();
    expect(await screen.findByText(/judge: same property/)).toBeInTheDocument();
    expect(screen.getByText(/same kitchen tiles/)).toBeInTheDocument();
    expect(screen.getByText(/different floor stated/)).toBeInTheDocument();
    expect(screen.getByText(/Unit discriminator/)).toBeInTheDocument();
  });

  it('renders the digests without a broker anywhere', async () => {
    renderPair();
    /* One digest panel per side, so the scrubbed description appears twice. */
    expect((await screen.findAllByText(/Světlý byt/)).length).toBe(2);
    expect(screen.queryByText(/makléř/i)).toBeNull();
  });

  it('still takes two clicks for a negative verdict', async () => {
    const user = userEvent.setup();
    renderPair();
    await screen.findByText('area_rel_diff');
    await user.click(screen.getByRole('button', { name: 'Correctly separate' }));
    expect(api.postAutodedupVerdict).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Click again to confirm' }));
    expect(api.postAutodedupVerdict).toHaveBeenCalledWith({
      kind: 'pair',
      listing_lo: 101,
      listing_hi: 202,
      verdict: 'different',
      reasons: [],
      note: null,
    });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Correctly separate' })).toHaveAttribute(
        'aria-pressed',
        'true',
      ),
    );
  });

  it('says so when the pair was never scored', async () => {
    vi.mocked(api.getAutodedupPair).mockResolvedValue({
      store_ready: true,
      data: { ...DETAIL, pair: null },
    });
    renderPair();
    expect(await screen.findByText(/never scored/)).toBeInTheDocument();
  });

  it('nests no interactive control inside another', async () => {
    renderPair();
    const main = (await screen.findByText('area_rel_diff')).closest('div')!;
    expectNoNestedInteractive(within(main).getByRole('table').parentElement!);
  });
});

/* The wire-shape bridge, run against the REAL module (the mock above spreads
 * it). `GET /autodedup/pair` sends `digests.a/b`, `images.a/b` with the
 * per-frame answer NESTED as `best_match`, a separate `top_features` list, no
 * listing summaries and no price trail. Reading any of that wrong is silent:
 * the page prints "no match" on every photo — evidence AGAINST a duplicate
 * that nobody produced — or throws on a key the route never sends. */
describe('normalizePairDetail', () => {
  const WIRE = {
    pair: {
      listing_lo: 101,
      listing_hi: 202,
      score: 0.44,
      zone: 'band' as const,
      decision: 'certificate:K-A:evidence_gate',
      guard_veto: null,
      certificate: 'K-A',
      families: 1 | 32,
      probes: ['k1'],
      cluster_key: null,
    },
    features: [
      { name: 'area_rel_diff', value: 0.01, present: true },
      { name: 'street_equal', value: null, present: false },
    ],
    top_features: [
      { name: 'area_rel_diff', value: 0.01, present: true, contribution: 0.8 },
    ],
    digests: { a: digest(101), b: digest(202) },
    images: {
      a: [
        {
          image_id: 1,
          storage_path: null,
          sreality_url: 'https://img.example.invalid/a.jpg',
          sequence: 1,
          phash: '123',
          best_match: { image_id: 77, hamming: 4 },
        },
        {
          image_id: 2,
          storage_path: null,
          sreality_url: 'https://img.example.invalid/b.jpg',
          sequence: 2,
          phash: null,
          best_match: null,
        },
      ],
      b: [],
      n_frames_shown: { a: 2, b: 0 },
      n_hashed_frames: { a: 1, b: 0 },
    },
    judgements: [],
    verdicts: [],
  };

  it('flattens the nested best_match onto each frame', () => {
    const d = api.normalizePairDetail(WIRE);
    expect(d.images.lo[0].best_hamming).toBe(4);
    expect(d.images.lo[0].best_match_image_id).toBe(77);
    /* A NULL dHash has no distance to anything — absent, never a zero. */
    expect(d.images.lo[1].best_hamming).toBeNull();
  });

  it('reads the a/b sides and joins top_features onto the feature list', () => {
    const d = api.normalizePairDetail(WIRE);
    expect(d.digests.lo?.listing_id).toBe(101);
    expect(d.digests.hi?.listing_id).toBe(202);
    expect(d.features.find((f) => f.name === 'area_rel_diff')?.contribution).toBe(0.8);
    expect(d.features.find((f) => f.name === 'street_equal')?.contribution).toBeNull();
    expect(d.family_names).toEqual(['ATTR', 'IMG']);
  });

  it('builds the listing summaries from the digests, carrying their portal url', () => {
    const d = api.normalizePairDetail(WIRE);
    expect(d.listings.lo?.listing_id).toBe(101);
    expect(d.listings.lo?.source_url).toBe('https://www.sreality.cz/detail/101');
    /* What the digest does not carry stays empty rather than invented. */
    expect(d.listings.lo?.cover).toBeNull();
  });
});
