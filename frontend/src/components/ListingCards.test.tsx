/* Browse card grid — the W7a non-blocking split, and the W5 card semantics.
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
 *
 * W5 added the second half of this file. Until then NOT ONE assertion here
 * called getByRole — which is exactly why nobody saw that the card was an <a>
 * wrapping eight controls: every query matched on text, and text is blind to
 * roles, to accessible names and to nesting. The rails below query the way a
 * keyboard or a screen reader reaches the card, and nothing else.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';

import ListingCards from './ListingCards';
import { CardHydrationProvider } from '@/lib/hydration';
import { expectNoNestedInteractive } from '@/test/a11y';
import * as api from '@/lib/api';
import * as brokers from '@/lib/brokers';
import * as queries from '@/lib/queries';
import type { CardRow } from '@/lib/queries';
import type { ImagePublic, ListingEstimate } from '@/lib/types';

vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  return {
    ...actual,
    fetchImagesForListingIds: vi.fn(async () => new Map()),
    fetchListingCovers: vi.fn(async () => new Map()),
    fetchPropertyCollectionMemberSet: vi.fn(async () => new Map()),
    /* The card's pipeline funnel reads these two shared queries. Unmocked they
       reach the network, so every role assertion below would depend on it. */
    fetchPipelineMembers: vi.fn(async () => new Map()),
    fetchPipelineStages: vi.fn(async () => []),
  };
});
vi.mock('@/lib/brokers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/brokers')>()),
  fetchListingBrokersByIds: vi.fn(async () => new Map()),
}));
vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  addPipelineCard: vi.fn(),
  listCollections: vi.fn(),
  addPropertiesToCollection: vi.fn(),
  removePropertyFromCollection: vi.fn(),
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

/* The card's own title, and therefore the accessible name its ONE link must
   have. `\s` rather than a literal space: fmtArea joins the number and the unit
   with a non-breaking space, and the anchors make this an equality test rather
   than a substring one. */
const TITLE_RE = /^Byt na prodej · 2\+kk · 62\sm²$/;
const SELECT_RE = /^Vybrat ke sloučení: Byt na prodej · 2\+kk · 62\sm²$/;

const ESTIMATE: ListingEstimate = {
  sreality_id: 900,
  run_id: 77,
  status: 'success',
  estimate_kind: 'rent',
  gross_yield_pct: 4.2,
  estimated_monthly_rent_czk: null,
  created_at: null,
};

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

/* The router's live location, rendered so a test can assert that activating a
   control did NOT navigate — the defect this wave exists for. */
function LocationProbe() {
  const { pathname, search, hash } = useLocation();
  return <span data-testid="loc">{`${pathname}${search}${hash}`}</span>;
}
const locationNow = () => screen.getByTestId('loc').textContent;

function renderGrid(
  opts: {
    merge?: { selected: boolean };
    onToggleSelect?: (propertyId: number) => void;
    estimates?: Record<number, ListingEstimate>;
  } = {},
) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <LocationProbe />
        <CardHydrationProvider listingIds={[111]} renders={{ photos: 50 }}>
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
            mergeMode={opts.merge != null}
            selectedPropertyIds={new Set(opts.merge?.selected ? [42] : [])}
            onToggleSelect={opts.onToggleSelect ?? (() => {})}
            pipelineScoped={false}
            estimates={opts.estimates}
            estimatingIds={new Set()}
            onEstimate={() => {}}
          />
        </CardHydrationProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/* Two photos, so the carousel actually renders its chevrons. */
const withPhotos = () =>
  vi.mocked(queries.fetchImagesForListingIds).mockResolvedValue(
    new Map([[111, [1, 2].map(photo)]]),
  );

beforeEach(() => {
  vi.mocked(queries.fetchImagesForListingIds).mockReset();
  vi.mocked(queries.fetchImagesForListingIds).mockResolvedValue(new Map());
  vi.mocked(queries.fetchPipelineMembers).mockResolvedValue(new Map());
  vi.mocked(queries.fetchPipelineStages).mockResolvedValue([]);
  vi.mocked(api.listCollections).mockResolvedValue({ data: [], total: 0 });
  vi.mocked(api.addPipelineCard).mockResolvedValue({
    property_id: 42,
    stage_key: 'review',
    added: true,
  });
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

  /* The north star as a test: every surface pays only for what it RENDERS.
     Browse cards show no cover thumbnail and no broker line, so mounting the
     shared provider must not fetch either.

     This is a REGRESSION TEST for a defect that reached production. W7a's first
     cut made only `photos` opt-in and left covers and brokers always-on, so
     Browse began quietly fetching a cover per card and a broker per card that
     nothing on the page displays — caught on the live post-deploy smoke run,
     /browse 22 -> 24 requests. Asymmetric defaults are how that happens. */
  it('fetches ONLY what the grid renders — no covers, no brokers', async () => {
    renderGrid();

    expect(await screen.findByText(/Sadová/)).toBeInTheDocument();
    expect(queries.fetchListingCovers).not.toHaveBeenCalled();
    expect(brokers.fetchListingBrokersByIds).not.toHaveBeenCalled();
  });
});

/* THE W5 DEFECT, pinned from the outside.
 *
 * The card used to be `<Link>` wrapped around its whole body, with the carousel
 * chevrons, the pipeline funnel, the collection trigger and the estimate corner
 * as DOM descendants. Consequences, all of them invisible to a text query: the
 * anchor's accessible name was the entire card read aloud; one card was five
 * tab stops nested inside one; and a preventDefault/stopPropagation regime in
 * four files existed only to keep those controls from navigating. */
describe('<ListingCards> the card is not a link — its TITLE is', () => {
  it('gives the card ONE link, named by the listing and nothing else', async () => {
    renderGrid();

    const link = await screen.findByRole('link', { name: /^Byt na prodej/ });
    expect(link).toHaveAccessibleName(TITLE_RE);
    // What the wrapping anchor used to absorb: the card's every other word.
    expect(link).not.toHaveAccessibleName(/Sadová/);
    expect(link).not.toHaveAccessibleName(/Odhad/);
    // And it is the only link on the card — the title, not the card, is the
    // destination.
    expect(screen.getAllByRole('link')).toHaveLength(1);
  });

  it('exposes every control by its OWN role and name', async () => {
    withPhotos();
    renderGrid();

    expect(await screen.findByRole('button', { name: 'Previous photo' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next photo' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Přidat do pipeline' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Uložit do kolekce' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Odhad' })).toBeInTheDocument();
  });

  /* The whole point of un-nesting: a control does its own job and only its own
     job. Every one of these clicks used to need a preventDefault to avoid
     opening the listing. */
  it('never navigates when a control on the card is activated', async () => {
    withPhotos();
    renderGrid();

    expect(await screen.findByRole('button', { name: 'Previous photo' })).toBeInTheDocument();
    expect(locationNow()).toBe('/');

    fireEvent.click(screen.getByRole('button', { name: 'Next photo' }));
    fireEvent.click(screen.getByRole('button', { name: 'Odhad' }));
    fireEvent.click(screen.getByRole('button', { name: 'Uložit do kolekce' }));
    fireEvent.click(screen.getByRole('button', { name: 'Přidat do pipeline' }));

    expect(locationNow()).toBe('/');
  });

  it('navigates to the listing on a plain click of the title link', async () => {
    renderGrid();

    fireEvent.click(await screen.findByRole('link', { name: /^Byt na prodej/ }));

    expect(locationNow()).toBe('/listing/sreality/900');
  });

  /* Same rule lib/linkGestures encodes for the surfaces the router does not
     own: a modified click is the user asking the BROWSER for a tab, and the SPA
     must not swallow it. A real <a> gets this for free; the old role="button"
     merge wrapper and navigate()-in-onClick could not.

     jsdom prints "Not implemented: navigation" on this run. That log IS the
     assertion's other half: it only happens because nothing called
     preventDefault, i.e. the gesture really was handed to the browser. */
  it('leaves a ctrl-click to the browser', async () => {
    renderGrid();

    fireEvent.click(await screen.findByRole('link', { name: /^Byt na prodej/ }), {
      ctrlKey: true,
    });

    expect(locationNow()).toBe('/');
  });

  /* The query axe structurally cannot run for an <a> wrapper (see test/a11y.ts)
     — and an <a> wrapper is exactly what this card was. */
  it('has no interactive element nested inside another', async () => {
    withPhotos();
    const { container } = renderGrid();

    expect(await screen.findByRole('button', { name: 'Previous photo' })).toBeInTheDocument();
    expectNoNestedInteractive(container);
  });

  /* "Open the run" is a destination, so it is a link — it was a <button>
     calling navigate() only because an <a> inside the card's <a> is invalid
     markup. With the wrapper gone the honest element is available again. */
  it('makes a finished estimate a real link to the run surface', async () => {
    renderGrid({ estimates: { 900: ESTIMATE } });

    const link = await screen.findByRole('link', { name: /Výnos/ });
    expect(link).toHaveAttribute('href', '/listing/900?run=77#estimations');

    fireEvent.click(link);
    expect(locationNow()).toBe('/listing/900?run=77#estimations');
  });
});

/* Merge mode used to swap the card's <Link> for a role="button" div carrying
 * aria-pressed, while DRAWING a checkbox that was pure decoration. The drawn
 * thing is now the real thing. */
describe('<ListingCards> merge mode selects with a real checkbox', () => {
  it('exposes a checkbox named by the listing it selects', () => {
    renderGrid({ merge: { selected: false } });

    const box = screen.getByRole('checkbox', { name: /^Vybrat ke sloučení/ });
    expect(box).toHaveAccessibleName(SELECT_RE);
    expect(box).not.toBeChecked();
  });

  it('carries the selection as real checked state', () => {
    renderGrid({ merge: { selected: true } });

    expect(screen.getByRole('checkbox', { name: /^Vybrat ke sloučení/ })).toBeChecked();
  });

  it('toggles on Space', async () => {
    const onToggleSelect = vi.fn();
    const user = userEvent.setup();
    renderGrid({ merge: { selected: false }, onToggleSelect });

    const box = screen.getByRole('checkbox', { name: /^Vybrat ke sloučení/ });
    box.focus();
    await user.keyboard(' ');

    expect(onToggleSelect).toHaveBeenCalledWith(42);
  });

  /* One meaning per click at any moment (TagContentsGallery's rule): in merge
     mode a click selects, so there is no listing link and no estimate control
     competing for the same gesture. */
  it('offers no link and no estimate corner while selecting', () => {
    renderGrid({ merge: { selected: false } });

    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Odhad' })).toBeNull();
  });

  it('has no interactive element nested inside another', () => {
    const { container } = renderGrid({ merge: { selected: true } });

    expectNoNestedInteractive(container);
  });
});

/* The collection popover was a hand-rolled `absolute` panel inside the card's
 * anchor: it announced nothing, dismissed itself with a document-mousedown
 * listener of its own, preventDefault'ed every click in the panel to stop the
 * card navigating, and rendered its empty state as an <a> inside that <a>. It
 * is the shared portalled AnchoredPopover now. */
describe('<ListingCards> the collection popover is a real popup', () => {
  it('announces itself, and Escape closes it and hands focus back', async () => {
    vi.mocked(api.listCollections).mockResolvedValue({
      data: [{ id: 7, name: 'Sledované', monitoring_enabled: true } as never],
      total: 1,
    });
    renderGrid();

    const trigger = await screen.findByRole('button', { name: 'Uložit do kolekce' });
    // No aria-haspopup: "true" means MENU, and this panel is a list of toggles,
    // not a menu. aria-expanded + aria-controls carry the disclosure contract.
    expect(trigger).not.toHaveAttribute('aria-haspopup');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(document.getElementById(trigger.getAttribute('aria-controls') ?? '')).not.toBeNull();
    expect(await screen.findByRole('button', { name: /Sledované/ })).toBeInTheDocument();
    // Portalled onto <body>, so nothing in the panel is nested in the card.
    expectNoNestedInteractive(document.body);

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(document.activeElement).toBe(trigger);
  });

  /* The empty state is a <Link>. Inside the old panel that was an anchor inside
     the card's anchor — invalid markup React itself warns about. */
  it('offers "create a collection" as a plain link, not an anchor in an anchor', async () => {
    renderGrid();

    fireEvent.click(await screen.findByRole('button', { name: 'Uložit do kolekce' }));

    const create = await screen.findByRole('link', { name: /Create a collection/ });
    expect(create).toHaveAttribute('href', '/collections');
    expectNoNestedInteractive(document.body);
  });
});

/* The portalled popover must be keyboard-reachable: the panel lands at the END
 * of <body>, so without an explicit focus move Tab would walk the whole page
 * before reaching it. */
describe('<ListingCards> collection popover keyboard entry', () => {
  it('moves focus into the panel on open and back to the trigger on Escape', async () => {
    renderGrid();
    const trigger = await screen.findByRole('button', { name: 'Uložit do kolekce' });
    trigger.focus();
    fireEvent.click(trigger);
    const panel = await screen.findByRole('group', { name: 'Uložit do kolekce' });
    expect(panel.contains(document.activeElement)).toBe(true);
    expect(trigger).toHaveAttribute('aria-controls', panel.id);
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('group', { name: 'Uložit do kolekce' })).toBeNull());
    expect(document.activeElement).toBe(trigger);
  });
});

describe('<ListingCards> merge mode with a multi-photo card', () => {
  it('keeps the checkbox named and lets a chevron page without selecting', async () => {
    withPhotos();
    const onToggleSelect = vi.fn();
    renderGrid({ merge: { selected: false }, onToggleSelect });
    const box = await screen.findByRole('checkbox');
    expect(box).toHaveAccessibleName(/Vybrat ke sloučení/);
    const next = screen.queryByRole('button', { name: /Další|Next|další/i });
    if (next) {
      fireEvent.click(next);
      expect(onToggleSelect).not.toHaveBeenCalled();
    }
  });
});
