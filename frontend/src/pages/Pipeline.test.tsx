/* Pipeline kanban — drag-and-drop move resolution + board interaction.
 *
 * The real DnD gesture (pointer drag across columns) can't be faithfully
 * simulated in jsdom, so the bug-prone part — resolving a drag-end into a
 * stage move — is extracted into the pure `planMove` and unit-tested directly.
 * A render smoke test then pins the board's columns and the trash → confirm →
 * remove flow (stage moves are drag-only; the select fallback was removed).
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import Pipeline, { planMove } from './Pipeline';
import type { Collection, PipelineBoardCard, PipelineStage } from '@/lib/types';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';
import * as brokersApi from '@/lib/brokers';

/* Every read the page makes has to be named here: the factories spread the
   real module, so an unnamed one runs for real and fails SOFT (request()
   throws on an unset VITE_API_BASE_URL) — a green suite proving nothing. */
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    movePipelineCard: vi.fn(),
    removePipelineCard: vi.fn(),
    listCollections: vi.fn(),
  };
});

vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  return {
    ...actual,
    fetchPipelineStages: vi.fn(),
    fetchPipelineBoard: vi.fn(),
    fetchListingCovers: vi.fn(),
    fetchPropertyCollectionMemberSet: vi.fn(),
  };
});

/* Decorations (cover photo, broker line) no longer come off the board query —
 * they load through lib/hydration keyed on listing_id. Mocking the two batch
 * readers rather than the hooks means these tests drive the REAL provider,
 * hooks, key namespace and projection, so what they pin is the path that
 * actually runs in the browser. `pipelineCardBroker` is deliberately left
 * unmocked: the masking projection is part of what the broker test asserts. */
vi.mock('@/lib/brokers', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/brokers')>();
  return {
    ...actual,
    fetchListingBrokersByIds: vi.fn(),
  };
});

/* The board shares Browse's stored-chip reader (W3 S3): a URL written before
 * chips carried codes is resolved through this one call. */
const resolveChipNames = vi.hoisted(() => vi.fn());
vi.mock('@/lib/maps', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/maps')>();
  return { ...actual, resolveChipNames };
});

const CARDS: PipelineBoardCard[] = [
  {
    property_id: 42,
    stage_id: 1,
    board_position: 0,
    entered_stage_at: '2026-06-01T00:00:00Z',
    added_at: '2026-05-20T00:00:00Z',
    sreality_id: 111,
    source: 'sreality',
    source_id_native: '111',
    listing_id: 111,
    category_main: 'byt',
    category_type: 'prodej',
    price_per_m2: 90_909,
    price_per_m2_basis: 'sale_capital_czk_m2',
    display_label: 'Sadová, Praha',
    disposition: '2+kk',
    subtype: null,
    area_m2: 55,
    price_czk: 5_000_000,
    mf_gross_yield_pct: 4.3,
    total_price_change_pct: -4.2,
    price_change_count: 1,
    obec_id: 554782,
    cast_obce_id: null,
    okres_id: null,
    region_id: 19,
    obec: 'Praha',
    is_active: true,
  },
];

/* The decoration fixtures, keyed on listing_id like the real layer. */
/* W6: identity AND contact are one row now (migration 419), so there is no second
   fixture to pair with this one — `masked` picks which half of the contact pair the
   API sent, which is a property of the CALLER, not of the row. */
const listingBroker = (masked: boolean) => ({
  sreality_id: 111,
  listing_id: 111,
  broker_id: 7,
  broker_display_name: 'Jan Novák',
  broker_firm_label: 'RE/MAX',
  ...(masked
    ? { has_email: true, has_phone: true }
    : { primary_email: 'jan@remax.cz', primary_phone: '420777123456' }),
});

// A second card of a different property type, for the type-filter test.
const CARD_DUM: PipelineBoardCard = {
  property_id: 43,
  stage_id: 3,
  board_position: 0,
  entered_stage_at: '2026-06-02T00:00:00Z',
  added_at: '2026-05-25T00:00:00Z',
  sreality_id: 222,
  source: 'idnes',
  source_id_native: null,
  listing_id: 222,
  category_main: 'dum',
  category_type: 'prodej',
  price_per_m2: 64_286,
  price_per_m2_basis: 'sale_capital_czk_m2',
  display_label: 'Lesní, Brno',
  disposition: '4+1',
  subtype: null,
  area_m2: 140,
  price_czk: 9_000_000,
  mf_gross_yield_pct: null,
  total_price_change_pct: null,
  price_change_count: 0,
  obec_id: 582786,
  cast_obce_id: null,
  okres_id: 3702,
  region_id: 116,
  obec: 'Brno',
  is_active: true,
};

// A delisted property, for the active/inactive status-filter test.
const CARD_INACTIVE: PipelineBoardCard = {
  ...CARD_DUM,
  property_id: 44,
  sreality_id: 333,
  listing_id: 333,
  display_label: 'Polní, Ostrava',
  // The card's place line is display_label alone now, but the remaining place
  // columns still feed the in-memory chip predicate, so an overriding fixture
  // must move them together or the card filters as if it were still in Brno.
  obec: 'Ostrava',
  is_active: false,
};

const collection = (id: number, name: string): Collection => ({
  id,
  name,
  description: null,
  created_at: '2026-05-01T00:00:00Z',
  updated_at: '2026-05-01T00:00:00Z',
  listing_count: 1,
  monitoring_enabled: false,
  notify_channels: [],
  is_system: false,
});

// Šortlist holds a board property; Archiv holds none — only the first earns a chip.
const COLLECTIONS = [collection(7, 'Šortlist'), collection(8, 'Archiv')];

describe('planMove', () => {
  it('resolves a cross-column drop into a stage move', () => {
    expect(planMove('card:42', 'stage:3', CARDS)).toEqual({
      propertyId: 42,
      stageId: 3,
    });
  });

  it('is a no-op for a same-column drop', () => {
    expect(planMove('card:42', 'stage:1', CARDS)).toBeNull();
  });

  it('is a no-op when dropped outside any column', () => {
    expect(planMove('card:42', null, CARDS)).toBeNull();
  });

  it('is a no-op when over is not a stage droppable', () => {
    expect(planMove('card:42', 'card:99', CARDS)).toBeNull();
  });

  it('is a no-op for an unknown card', () => {
    expect(planMove('card:999', 'stage:3', CARDS)).toBeNull();
  });
});

const STAGES: PipelineStage[] = [
  { id: 1, key: 'interested', label: 'Zájem', position: 1, color: 'copper', is_terminal: false, is_entry: true, code: '1' },
  /* No `code` — exercises stageBadge's ordinal fallback (migration 377). */
  { id: 3, key: 'offer', label: 'Nabídka', position: 3, color: 'teal', is_terminal: false, is_entry: false, code: null },
];

function renderBoard(entry = '/pipeline') {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <Pipeline />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('<Pipeline> board', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(queries.fetchPipelineStages).mockResolvedValue(STAGES);
    vi.mocked(queries.fetchPipelineBoard).mockResolvedValue(CARDS);
    vi.mocked(api.movePipelineCard).mockResolvedValue({
      property_id: 42,
      stage_id: 3,
      stage_key: 'offer',
    });
    vi.mocked(api.removePipelineCard).mockResolvedValue({ removed: true });
    vi.mocked(queries.fetchListingCovers).mockResolvedValue(new Map());
    vi.mocked(brokersApi.fetchListingBrokersByIds).mockResolvedValue(
      new Map([[111, listingBroker(false)]]),
    );
    vi.mocked(api.listCollections).mockResolvedValue({
      data: COLLECTIONS,
      total: COLLECTIONS.length,
    });
    vi.mocked(queries.fetchPropertyCollectionMemberSet).mockResolvedValue(new Map());
  });

  /* The card-size switch. jsdom lays nothing out, so what is pinned here is
     that the choice REACHES the board — column width, the card's own photo
     frame, and the stored preference — not how any of it looks; the geometry
     itself was measured in a real browser and is pinned in
     lib/pipelineCardSize.test.ts. */
  it('restyles the board and remembers the card size', async () => {
    renderBoard();
    await screen.findByLabelText('Přetáhnout kartu do jiné fáze');
    const column = () => screen.getByRole('list', { name: 'Zájem' });
    /* The header sits in its own pinned row, apart from its column, so the
       two only line up while they carry the same width at every size. */
    const header = () => screen.getByText('Zájem').parentElement;
    /* The cover read is mocked empty, so every card draws the placeholder
       frame — which carries exactly the geometry the photo would. */
    const thumb = () => document.querySelector('ul li div[aria-hidden]');

    expect(column().className).toContain('w-72');
    expect(header()?.className).toContain('w-72');
    expect(thumb()?.className).toContain('h-12 w-12');

    fireEvent.click(screen.getByRole('button', { name: 'Velké' }));
    expect(column().className).toContain('w-[26rem]');
    expect(header()?.className).toContain('w-[26rem]');
    /* lg is a different card design, not a scaled one: the photo spans the
       card instead of sitting in a fixed square beside the text. */
    expect(thumb()?.className).toContain('aspect-[16/10]');
    expect(localStorage.getItem('sreality.pipeline.cardSize')).toBe('lg');

    fireEvent.click(screen.getByRole('button', { name: 'Střední' }));
    expect(column().className).toContain('w-[24rem]');
    expect(header()?.className).toContain('w-[24rem]');
    expect(thumb()?.className).toContain('h-24 w-24');
    expect(localStorage.getItem('sreality.pipeline.cardSize')).toBe('md');
  });

  /* jsdom lays nothing out, so the pin itself was checked in a real browser;
     what this holds is the wiring. The header row must sit OUTSIDE the
     sideways scroller (inside it, `sticky` pins to the scroller, never the
     page), which means it has to be carried along by hand when the columns
     scroll. */
  it('pins the stage headers above the columns and scrolls them together', async () => {
    renderBoard();
    const headerRow = (await screen.findByText('Zájem')).parentElement!.parentElement!;
    const columns = screen.getByRole('list', { name: 'Zájem' }).parentElement!;

    expect(headerRow.className).toContain('sticky');
    expect(columns.contains(headerRow)).toBe(false);
    expect(headerRow).toHaveTextContent('Nabídka');

    Object.defineProperty(headerRow, 'scrollLeft', { value: 0, writable: true });
    Object.defineProperty(columns, 'scrollLeft', { value: 240, writable: true });
    fireEvent.scroll(columns);
    expect(headerRow.scrollLeft).toBe(240);
  });

  it('renders draggable cards with a drag handle + enriched content', async () => {
    renderBoard();
    // One card → one grip handle; proves the column + draggable card mounted.
    expect(
      await screen.findByLabelText('Přetáhnout kartu do jiné fáze'),
    ).toBeInTheDocument();
    // Both stage columns render their header label.
    expect(screen.getByText('Zájem')).toBeInTheDocument();
    expect(screen.getByText('Nabídka')).toBeInTheDocument();
    // Enriched card content: street + MF yield + broker name linking to the broker page.
    expect(screen.getByText('Sadová')).toBeInTheDocument();
    expect(screen.getByText(/MF\s*4,3\s*%/)).toBeInTheDocument();
    /* The broker line is a DECORATION now: the card paints without it and it
       arrives on its own query, so this is findBy (async) rather than getBy.
       That asymmetry is the feature — the assertions above it all passed
       before this resolves. */
    const broker = await screen.findByText('Jan Novák');
    expect(broker.closest('a')).toHaveAttribute('href', '/brokers/7');
    expect(broker.closest('a')).toHaveAttribute(
      'title',
      expect.stringContaining('jan@remax.cz'),
    );
  });

  /* The board is a triage surface worked a column at a time, so following a
     card must not unload it — the property link opens in a NEW TAB. `rel` rides
     along so the opened document can't reach back through `window.opener`.
     The link is the address, which leads the card; the price below it is a
     figure, not a second way in. */
  it('opens the property in a new tab from the address line', async () => {
    renderBoard();
    const place = await screen.findByText('Sadová');
    const link = place.closest('a');
    expect(link).toHaveAttribute('href', '/listing/sreality/111');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'));

    const price = screen.getByText(/5\s*000\s*000/);
    expect(price.closest('a')).toBeNull();
    expect(
      place.compareDocumentPosition(price) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  /* The address is the card's only link, so a property whose place never
     resolved must still offer something to click. */
  it('still links a card whose place is unresolved', async () => {
    vi.mocked(queries.fetchPipelineBoard).mockResolvedValue([
      { ...CARDS[0], display_label: null, obec: null },
    ]);
    renderBoard();
    const link = (await screen.findByText('Lokalita neurčena')).closest('a');
    expect(link).toHaveAttribute('href', '/listing/sreality/111');
  });

  /* The town is the second row on EVERY card, on a line of its own so a long
     street can never truncate it away. */
  it('prints the town on its own second row, after the street link', async () => {
    renderBoard();
    const street = await screen.findByText('Sadová');
    const town = screen.getByText('Praha', { selector: 'p' });
    expect(town.closest('a')).toBeNull();
    expect(street.closest('p')).not.toBe(town);
    expect(street.closest('p')?.nextElementSibling).toBe(town);
  });

  it('keeps the town row on a card whose label is only the town', async () => {
    vi.mocked(queries.fetchPipelineBoard).mockResolvedValue([
      { ...CARDS[0], display_label: 'Praha' },
    ]);
    renderBoard();
    const street = await screen.findByText('Ulice neuvedena');
    expect(street.closest('a')).toHaveAttribute('href', '/listing/sreality/111');
    expect(street.closest('p')?.nextElementSibling).toHaveTextContent('Praha');
  });

  /* The point of the split, pinned: with BOTH decoration reads hanging
     forever, the board is still fully rendered and interactive. Before the
     split this was six serialized round trips and a "Načítání…" string — a
     single slow broker call blanked the entire kanban. */
  it('paints cards while cover and broker reads are still in flight', async () => {
    vi.mocked(queries.fetchListingCovers).mockReturnValue(
      new Promise(() => {}) as ReturnType<typeof queries.fetchListingCovers>,
    );
    vi.mocked(brokersApi.fetchListingBrokersByIds).mockReturnValue(
      new Promise(() => {}) as ReturnType<typeof brokersApi.fetchListingBrokersByIds>,
    );
    renderBoard();

    // Structure: columns, the card, its price, place and drag handle.
    expect(
      await screen.findByLabelText('Přetáhnout kartu do jiné fáze'),
    ).toBeInTheDocument();
    expect(screen.getByText('Zájem')).toBeInTheDocument();
    expect(screen.getByText('Sadová')).toBeInTheDocument();
    expect(screen.getByText(/MF\s*4,3\s*%/)).toBeInTheDocument();
    // Decoration: absent, and no loading string stands in for the board.
    expect(screen.queryByText('Jan Novák')).not.toBeInTheDocument();
    expect(screen.queryByText('Načítání…')).not.toBeInTheDocument();
  });

  /* The non-admin shape: the API sends has_email/has_phone INSTEAD of the
     values. The card must keep the broker and say the contact is admin-only —
     dropping it renders identically to a broker with no contact at all. */
  it('marks a masked contact as admin-only instead of omitting it', async () => {
    vi.mocked(brokersApi.fetchListingBrokersByIds).mockResolvedValue(
      new Map([[111, listingBroker(true)]]),
    );
    renderBoard();
    const broker = await screen.findByText('Jan Novák');
    expect(broker.closest('a')).toHaveAttribute(
      'title',
      'Jan Novák · RE/MAX · kontakt jen pro adminy',
    );
  });

  it('filters the board by property type', async () => {
    vi.mocked(queries.fetchPipelineBoard).mockResolvedValue([CARDS[0], CARD_DUM]);
    renderBoard();
    // Both cards render; the type chips appear (≥2 types present).
    expect(await screen.findByText('Sadová')).toBeInTheDocument();
    expect(screen.getByText('Lesní')).toBeInTheDocument();
    const domy = screen.getByRole('button', { name: 'Domy' });
    expect(screen.getByRole('button', { name: 'Byty' })).toBeInTheDocument();
    // Filter to Domy → only the dům card remains.
    fireEvent.click(domy);
    await waitFor(() =>
      expect(screen.queryByText('Sadová')).not.toBeInTheDocument(),
    );
    expect(screen.getByText('Lesní')).toBeInTheDocument();
  });

  /* W3 S3. The board filters its cards in the BROWSER, so it needs the same
     chip treatment Browse gets: under the one code predicate a chip with no
     code matches nothing, and a URL written before chips carried codes would
     empty the board while the very same link still shows a cohort on /browse. */
  it('resolves a pre-code chip in the URL instead of emptying the board', async () => {
    resolveChipNames.mockResolvedValue([[{ level: 'obec', id: 554782 }]]);
    renderBoard('/pipeline?districts=Praha');
    expect(await screen.findByText('Sadová')).toBeInTheDocument();
    expect(resolveChipNames).toHaveBeenCalledWith([
      { name: 'Praha', context: null },
    ]);
  });

  it('filters the board on the resolved code, not on the name', async () => {
    resolveChipNames.mockResolvedValue([[{ level: 'obec', id: 582786 }]]);
    renderBoard('/pipeline?districts=Brno');
    await waitFor(() => expect(resolveChipNames).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.queryByText('Sadová')).not.toBeInTheDocument(),
    );
  });

  it('filters the board by active/inactive status', async () => {
    vi.mocked(queries.fetchPipelineBoard).mockResolvedValue([CARDS[0], CARD_INACTIVE]);
    renderBoard();
    // Both cards render; the status pills appear (a delisted card is present).
    expect(await screen.findByText('Sadová')).toBeInTheDocument();
    expect(screen.getByText('Polní')).toBeInTheDocument();
    const aktivni = screen.getByRole('button', { name: 'Aktivní' });
    const neaktivni = screen.getByRole('button', { name: 'Neaktivní' });
    // Filter to Aktivní → only the live card remains.
    fireEvent.click(aktivni);
    await waitFor(() =>
      expect(screen.queryByText('Polní')).not.toBeInTheDocument(),
    );
    expect(screen.getByText('Sadová')).toBeInTheDocument();
    // Switching to Neaktivní flips which card is visible.
    fireEvent.click(neaktivni);
    await waitFor(() =>
      expect(screen.queryByText('Sadová')).not.toBeInTheDocument(),
    );
    expect(screen.getByText('Polní')).toBeInTheDocument();
  });

  /* The collection lens (rule #18): the fail-open contract, on both ways the
     member map can be missing — in flight here, errored in the next case. */
  it('applies no collection constraint and offers no row while membership is unresolved', async () => {
    vi.mocked(queries.fetchPipelineBoard).mockResolvedValue([CARDS[0], CARD_DUM]);
    vi.mocked(queries.fetchPropertyCollectionMemberSet).mockReturnValue(
      new Promise(() => {}),
    );
    renderBoard('/pipeline?collections=7');
    expect(await screen.findByText('Sadová')).toBeInTheDocument();
    expect(screen.getByText('Lesní')).toBeInTheDocument();
    expect(screen.queryByText('Kolekce')).not.toBeInTheDocument();
    // Uncounted: the header reads the plain total, and there is nothing to reset.
    expect(screen.getByText(/nemovitostí/).textContent).toBe('2 nemovitostí');
    expect(screen.queryByRole('button', { name: 'Reset' })).not.toBeInTheDocument();
  });

  it('applies no collection constraint when the member map errors', async () => {
    vi.mocked(queries.fetchPipelineBoard).mockResolvedValue([CARDS[0], CARD_DUM]);
    vi.mocked(queries.fetchPropertyCollectionMemberSet).mockRejectedValue(new Error('403'));
    renderBoard('/pipeline?collections=7');
    await waitFor(() =>
      expect(screen.getByText(/nemovitostí/).textContent).toBe('2 nemovitostí'),
    );
    expect(screen.getByText('Sadová')).toBeInTheDocument();
    expect(screen.queryByText('Kolekce')).not.toBeInTheDocument();
  });

  /* The row reads the member map, NOT the collections list: a selection is a
     live constraint, so its chip renders (pressed, under a placeholder name)
     before the list arrives — otherwise the only way out is Reset. */
  it('offers the selected collection before the collections list resolves', async () => {
    vi.mocked(queries.fetchPipelineBoard).mockResolvedValue([CARDS[0], CARD_DUM]);
    vi.mocked(queries.fetchPropertyCollectionMemberSet).mockResolvedValue(
      new Map([[42, [7]]]),
    );
    vi.mocked(api.listCollections).mockReturnValue(
      new Promise(() => {}) as ReturnType<typeof api.listCollections>,
    );
    renderBoard('/pipeline?collections=7');
    expect(await screen.findByRole('button', { name: '#7' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(screen.getByText('Kolekce')).toBeInTheDocument();
    expect(screen.getByText(/nemovitostí/).textContent).toBe('1 z 2 nemovitostí');
  });

  it('narrows the board to a collection and counts it in the header', async () => {
    vi.mocked(queries.fetchPipelineBoard).mockResolvedValue([CARDS[0], CARD_DUM]);
    vi.mocked(queries.fetchPropertyCollectionMemberSet).mockResolvedValue(
      new Map([[42, [7]]]),
    );
    renderBoard();
    expect(await screen.findByText('Sadová')).toBeInTheDocument();
    // Only the collection with a member ON the board is offered.
    const chip = await screen.findByRole('button', { name: 'Šortlist' });
    expect(screen.getByText('Kolekce')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Archiv' })).not.toBeInTheDocument();
    fireEvent.click(chip);
    await waitFor(() =>
      expect(screen.queryByText('Lesní')).not.toBeInTheDocument(),
    );
    expect(screen.getByText('Sadová')).toBeInTheDocument();
    expect(screen.getByText(/nemovitostí/).textContent).toBe('1 z 2 nemovitostí');
  });

  /* A resolved selection that matches nothing is ZERO cards, never the whole
     board — the failure mode that makes a filter silently meaningless. */
  it('shows no cards for a resolved collection nothing on the board is in', async () => {
    vi.mocked(queries.fetchPipelineBoard).mockResolvedValue([CARDS[0], CARD_DUM]);
    vi.mocked(queries.fetchPropertyCollectionMemberSet).mockResolvedValue(
      new Map([[42, [7]]]),
    );
    renderBoard('/pipeline?collections=8');
    await waitFor(() =>
      expect(screen.getByText(/nemovitostí/).textContent).toBe('0 z 2 nemovitostí'),
    );
    expect(screen.queryByText('Sadová')).not.toBeInTheDocument();
    expect(screen.queryByText('Lesní')).not.toBeInTheDocument();
    // Its chip renders pressed although nothing on the board is in it: a
    // constraint you cannot see is one you can only leave through Reset.
    expect(screen.getByRole('button', { name: 'Archiv' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  /* Stav defaults to 'any' precisely so a delisted member of a collection is
     still in the cohort — a shortlist you can't see the dead deals in is a
     different question than the one the operator asked. */
  it('keeps a delisted member visible under a collection filter', async () => {
    vi.mocked(queries.fetchPipelineBoard).mockResolvedValue([CARDS[0], CARD_INACTIVE]);
    vi.mocked(queries.fetchPropertyCollectionMemberSet).mockResolvedValue(
      new Map([[44, [7]]]),
    );
    renderBoard('/pipeline?collections=7');
    expect(await screen.findByText('Polní')).toBeInTheDocument();
    expect(screen.queryByText('Sadová')).not.toBeInTheDocument();
  });

  it('clears every filter through the one header Reset', async () => {
    vi.mocked(queries.fetchPipelineBoard).mockResolvedValue([CARDS[0], CARD_DUM]);
    vi.mocked(queries.fetchPropertyCollectionMemberSet).mockResolvedValue(
      new Map([[42, [7]]]),
    );
    renderBoard('/pipeline?cat=dum&collections=7');
    // Type and collection disagree, so the board is empty until Reset.
    const reset = await screen.findByRole('button', { name: 'Reset' });
    expect(screen.queryByText('Sadová')).not.toBeInTheDocument();
    fireEvent.click(reset);
    expect(await screen.findByText('Sadová')).toBeInTheDocument();
    expect(screen.getByText('Lesní')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Reset' })).not.toBeInTheDocument();
  });

  it('names the stage-manager create field by its visible words', async () => {
    renderBoard();
    fireEvent.click(await screen.findByRole('button', { name: 'Spravovat fáze' }));
    const create = await screen.findByRole('textbox', { name: 'Nová fáze' });
    expect(create).toHaveAccessibleName('Nová fáze');
  });

  it('trash → confirm removes the card via removePipelineCard', async () => {
    renderBoard();
    const trash = await screen.findByLabelText('Odebrat z pipeline');
    fireEvent.click(trash);
    // Inline two-step confirm appears; nothing removed until confirmed.
    expect(api.removePipelineCard).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Odebrat'));
    await waitFor(() =>
      expect(api.removePipelineCard).toHaveBeenCalledWith(42),
    );
  });
});
