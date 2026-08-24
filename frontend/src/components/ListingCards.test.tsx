/* Browse card grid — the W7a non-blocking split.
 *
 * Photos used to be awaited INSIDE the cards read (`fetchListingsForCards`),
 * so `CardRow` carried an `images` array and no card painted until every card's
 * carousel had landed. Measured live on 24 real ids, that await was 178 image
 * rows, 178 correlated CLIP-tag lookups, 750 buffers and ~131 ms of server work
 * sitting directly on the paint path.
 *
 * Two things have to stay true, and they pull in opposite directions — which is
 * why both are pinned here:
 *
 *   1. The grid paints WITHOUT its photos. A hanging photo read must cost a card
 *      its carousel and nothing else.
 *   2. The carousel keeps EVERY row. This wave is the non-blocking split, not a
 *      move to cover-only; collapsing Browse cards onto the board's one-cover
 *      read would "fix" the paint path by deleting the feature.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import ListingCards from './ListingCards';
import { CardHydrationProvider } from '@/lib/hydration';
import * as queries from '@/lib/queries';
import type { CardRow } from '@/lib/queries';
import type { ImagePublic } from '@/lib/types';

vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  return {
    ...actual,
    fetchImagesForListingIds: vi.fn(async () => new Map()),
    fetchListingCovers: vi.fn(async () => new Map()),
    fetchPropertyCollectionMemberSet: vi.fn(async () => new Map()),
  };
});
vi.mock('@/lib/brokers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/brokers')>()),
  fetchListingBrokersByIds: vi.fn(async () => new Map()),
}));

const ROW = {
  property_id: 42,
  listing_id: 111,
  sreality_id: 900,
  price_czk: 5_400_000,
  area_m2: 62,
  street: 'Sadová',
  obec: 'Praha',
  disposition: '2+kk',
  first_seen_at: '2026-01-01T00:00:00Z',
  last_seen_at: '2026-01-02T00:00:00Z',
  is_active: true,
  tom_days: 3,
  category_main: 'byt',
  category_type: 'prodej',
  source: 'sreality',
  source_id_native: '900',
  mf_gross_yield_pct: null,
  total_price_change_pct: null,
  price_change_count: null,
} as unknown as CardRow;

const photo = (id: number): ImagePublic =>
  ({
    id,
    sreality_url: `https://img/${id}.jpg`,
    storage_path: null,
    clip_fine_tag: null,
    clip_confidence: null,
    clip_render_score: null,
  }) as unknown as ImagePublic;

/* The carousel's "n / total" pill, matched on the LEAF node: every ancestor's
   textContent contains it too, so an unguarded matcher finds several. */
const counterIs = (want: string) => (_t: string, el: Element | null) =>
  el != null && el.children.length === 0 && el.textContent?.trim() === want;
const anyCounter = (_t: string, el: Element | null) =>
  el != null && el.children.length === 0 && /^\d+ \/ \d+$/.test(el.textContent?.trim() ?? '');

function renderGrid() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CardHydrationProvider listingIds={[111]} photosPerId={50}>
          <ListingCards
            rows={[ROW]}
            total={1}
            sort={{ field: 'last_seen_at', dir: 'desc' } as never}
            imageLarge={false}
            isLoading={false}
            isFetchingNextPage={false}
            hasNextPage={false}
            onReachEnd={() => {}}
            restorationKey="test"
            hasFilters={false}
            hasBounds={false}
            hoveredIds={new Set()}
            onHover={() => {}}
            onSort={() => {}}
            onClearFilters={() => {}}
            onClearBounds={() => {}}
            mergeMode={false}
            selectedPropertyIds={new Set()}
            onToggleSelect={() => {}}
            pipelineScoped={false}
            estimates={undefined}
            estimatingIds={new Set()}
            onEstimate={() => {}}
          />
        </CardHydrationProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(queries.fetchImagesForListingIds).mockReset();
  vi.mocked(queries.fetchImagesForListingIds).mockResolvedValue(new Map());
});

describe('<ListingCards> photo hydration', () => {
  /* The split, pinned: with the photo read hanging forever, the card is fully
     rendered. Before W7a this state was unreachable in the other direction —
     there was no card at all until the photos resolved. */
  it('paints the card while the photo read is still in flight', async () => {
    vi.mocked(queries.fetchImagesForListingIds).mockReturnValue(
      new Promise(() => {}) as ReturnType<typeof queries.fetchImagesForListingIds>,
    );

    renderGrid();

    // Structure: place and price are on screen with zero photos resolved.
    expect(await screen.findByText(/Sadová/)).toBeInTheDocument();
    expect(screen.getByText(/5\s*400\s*000/)).toBeInTheDocument();
    // The carousel renders its own empty frame — no loading string stands in
    // for the card, and no photo counter claims images that have not arrived.
    expect(screen.getByText('no image')).toBeInTheDocument();
    expect(screen.queryByText(anyCounter)).toBeNull();
  });

  /* The other half of the contract. The ledger's warning for this wave was
     explicit — the win is the non-blocking split, NOT cover-only — so a card
     must still get every photo it had. The "n / total" counter is the carousel's
     own report of how many rows it holds, so it is the honest assertion here. */
  it('keeps the whole carousel, not just a cover', async () => {
    vi.mocked(queries.fetchImagesForListingIds).mockResolvedValue(
      new Map([[111, [1, 2, 3, 4, 5, 6, 7].map(photo)]]),
    );

    renderGrid();

    // The counter is JSX-interpolated ({i + 1} / {n}), so it arrives as several
    // text nodes — match the LEAF element's normalized text (every ancestor
    // contains it too, hence the childless guard).
    expect(await screen.findByText(counterIs('1 / 7'))).toBeInTheDocument();
    expect(screen.getByLabelText('Next photo')).toBeInTheDocument();
    expect(screen.queryByText('no image')).toBeNull();
  });

  /* A listing with no photos at all is a real answer, not a pending one: the
     empty frame is correct and must not be mistaken for the case above. */
  it('draws the empty frame for a listing that genuinely has no photos', async () => {
    vi.mocked(queries.fetchImagesForListingIds).mockResolvedValue(new Map());

    renderGrid();

    expect(await screen.findByText(/Sadová/)).toBeInTheDocument();
    expect(screen.getByText('no image')).toBeInTheDocument();
  });

  /* One cohort read for the whole grid, keyed on the SURROGATE listing_id —
     never sreality_id, which is NULL on a post-Gate-2 non-sreality card and
     would silently cost it every photo. */
  it('reads photos once for the cohort, keyed on the surrogate id', async () => {
    renderGrid();

    expect(await screen.findByText(/Sadová/)).toBeInTheDocument();
    expect(queries.fetchImagesForListingIds).toHaveBeenCalledTimes(1);
    expect(queries.fetchImagesForListingIds).toHaveBeenCalledWith([111], 50);
  });
});
