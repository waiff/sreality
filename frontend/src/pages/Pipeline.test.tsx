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
import type { PipelineBoardCard, PipelineStage } from '@/lib/types';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';
import * as brokersApi from '@/lib/brokers';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    movePipelineCard: vi.fn(),
    removePipelineCard: vi.fn(),
  };
});

vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  return {
    ...actual,
    fetchPipelineStages: vi.fn(),
    fetchPipelineBoard: vi.fn(),
    fetchListingCovers: vi.fn(),
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
    street: 'Sadová',
    district: 'Praha',
    disposition: '2+kk',
    subtype: null,
    area_m2: 55,
    price_czk: 5_000_000,
    mf_gross_yield_pct: 4.3,
    total_price_change_pct: -4.2,
    price_change_count: 1,
    obec_id: 554782,
    okres_id: null,
    region_id: 19,
    place_search_text: 'Sadová, Praha',
    obec: 'Praha',
    locality: 'Sadová, Praha',
    okres: 'Praha',
    region: 'Hlavní město Praha',
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
  street: 'Lesní',
  district: 'Brno',
  disposition: '4+1',
  subtype: null,
  area_m2: 140,
  price_czk: 9_000_000,
  mf_gross_yield_pct: null,
  total_price_change_pct: null,
  price_change_count: 0,
  obec_id: 582786,
  okres_id: 3702,
  region_id: 116,
  place_search_text: 'Lesní, Brno',
  obec: 'Brno',
  locality: 'Lesní, Brno',
  okres: 'Brno-město',
  region: 'Jihomoravský kraj',
  is_active: true,
};

// A delisted property, for the active/inactive status-filter test.
const CARD_INACTIVE: PipelineBoardCard = {
  ...CARD_DUM,
  property_id: 44,
  sreality_id: 333,
  listing_id: 333,
  street: 'Polní',
  district: 'Ostrava',
  place_search_text: 'Polní, Ostrava',
  // The card's place line is placePrimary(), which prefers the free-text
  // locality — so a fixture that overrides street/district must override these
  // too or it silently keeps the base card's town.
  obec: 'Ostrava',
  locality: 'Polní, Ostrava',
  okres: 'Ostrava-město',
  is_active: false,
};

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

function renderBoard() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Pipeline />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('<Pipeline> board', () => {
  beforeEach(() => {
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
    expect(screen.getByText('Sadová, Praha')).toBeInTheDocument();
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
    expect(screen.getByText('Sadová, Praha')).toBeInTheDocument();
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
    expect(await screen.findByText('Sadová, Praha')).toBeInTheDocument();
    expect(screen.getByText('Lesní, Brno')).toBeInTheDocument();
    const domy = screen.getByRole('button', { name: 'Domy' });
    expect(screen.getByRole('button', { name: 'Byty' })).toBeInTheDocument();
    // Filter to Domy → only the dům card remains.
    fireEvent.click(domy);
    await waitFor(() =>
      expect(screen.queryByText('Sadová, Praha')).not.toBeInTheDocument(),
    );
    expect(screen.getByText('Lesní, Brno')).toBeInTheDocument();
  });

  it('filters the board by active/inactive status', async () => {
    vi.mocked(queries.fetchPipelineBoard).mockResolvedValue([CARDS[0], CARD_INACTIVE]);
    renderBoard();
    // Both cards render; the status pills appear (a delisted card is present).
    expect(await screen.findByText('Sadová, Praha')).toBeInTheDocument();
    expect(screen.getByText('Polní, Ostrava')).toBeInTheDocument();
    const aktivni = screen.getByRole('button', { name: 'Aktivní' });
    const neaktivni = screen.getByRole('button', { name: 'Neaktivní' });
    // Filter to Aktivní → only the live card remains.
    fireEvent.click(aktivni);
    await waitFor(() =>
      expect(screen.queryByText('Polní, Ostrava')).not.toBeInTheDocument(),
    );
    expect(screen.getByText('Sadová, Praha')).toBeInTheDocument();
    // Switching to Neaktivní flips which card is visible.
    fireEvent.click(neaktivni);
    await waitFor(() =>
      expect(screen.queryByText('Sadová, Praha')).not.toBeInTheDocument(),
    );
    expect(screen.getByText('Polní, Ostrava')).toBeInTheDocument();
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
