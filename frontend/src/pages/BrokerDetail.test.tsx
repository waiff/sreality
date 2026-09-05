/* /brokers/:id — the three states the B2 repoint made distinguishable.
 *
 * Before it, every broker read was dark and the page had exactly one outcome:
 * "Makléř nenalezen.". Now the dossier answers, so the page must tell apart
 * (1) a masked contact from an absent one, (2) a failed dossier read from an
 * unknown broker, and (3) a failed inventory read from a broker with no
 * listings — the last one contradicting the stats strip rendered right above it.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import BrokerDetail from './BrokerDetail';
import * as brokers from '@/lib/brokers';

vi.mock('@/lib/brokers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/brokers')>()),
  fetchBrokerDossier: vi.fn(),
  fetchBrokerListings: vi.fn(),
}));

/* The explore-broker modal is provider-mounted in Shell (like the area modal);
 * the page only needs its `open` — same treatment ListingDetail.test.tsx gives
 * useExploreAreaModal. Hoisted so the spy is reachable from the mock factory. */
const { openExploreBroker } = vi.hoisted(() => ({ openExploreBroker: vi.fn() }));
vi.mock('@/components/ExploreBrokerModal', () => ({
  useExploreBrokerModal: () => ({ open: openExploreBroker, close: vi.fn(), isOpen: false }),
}));

const listing = (l: Partial<brokers.BrokerListing>): brokers.BrokerListing =>
  ({
    broker_id: 7,
    sreality_id: null,
    listing_id: 1,
    source: 'sreality',
    source_url: '',
    locality: 'Praha',
    district: null,
    category_main: 'byt',
    category_type: 'prodej',
    disposition: '2+kk',
    subtype: null,
    area_m2: 50,
    price_czk: 5_000_000,
    is_active: true,
    last_seen_at: new Date().toISOString(),
    property_id: 11,
    ...l,
  }) as brokers.BrokerListing;

const dossier = (broker: Partial<brokers.BrokerPublic>): brokers.BrokerDossier => ({
  broker: {
    broker_id: 7,
    display_name: 'Jan Novák',
    firm_id: null,
    firm_domain: null,
    firm_name: 'RE/MAX',
    listing_count: 52,
    active_listing_count: 37,
    property_count: 50,
    active_property_count: 35,
    ...broker,
  } as brokers.BrokerPublic,
  memberships: [],
  region_shares: [],
  pii_masked: true,
});

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/brokers/7']}>
        <Routes>
          <Route path="/brokers/:id" element={<BrokerDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(brokers.fetchBrokerDossier).mockResolvedValue(dossier({}));
  vi.mocked(brokers.fetchBrokerListings).mockResolvedValue([]);
});

describe('<BrokerDetail> contact card', () => {
  it('shows the real contact for an admin session', async () => {
    vi.mocked(brokers.fetchBrokerDossier).mockResolvedValue(
      dossier({ primary_phone: '420777123456', primary_email: 'jan@remax.cz' }),
    );
    renderPage();
    expect(await screen.findByText('+420 777 123 456')).toBeInTheDocument();
    expect(screen.getByText('jan@remax.cz')).toBeInTheDocument();
  });

  /* The bug this wave exists to kill: a non-admin gets has_* instead of the
     values, and an em-dash there claims the broker is unreachable. */
  it('says a masked contact is on file rather than showing the empty dash', async () => {
    vi.mocked(brokers.fetchBrokerDossier).mockResolvedValue(
      dossier({ has_phone: true, has_email: true }),
    );
    renderPage();
    expect(await screen.findByText(/telefon · kontakt na vyžádání/)).toBeInTheDocument();
    expect(screen.getByText(/e-mail · kontakt na vyžádání/)).toBeInTheDocument();
  });

  it('keeps the plain dash when the broker genuinely has no contact', async () => {
    vi.mocked(brokers.fetchBrokerDossier).mockResolvedValue(
      dossier({ has_phone: false, has_email: false }),
    );
    renderPage();
    expect(await screen.findByText(/telefon —/)).toBeInTheDocument();
    expect(screen.queryByText(/na vyžádání/)).not.toBeInTheDocument();
  });
});

describe('<BrokerDetail> honest error states', () => {
  it('separates a failed dossier read from an unknown broker', async () => {
    vi.mocked(brokers.fetchBrokerDossier).mockRejectedValue(new Error('HTTP 500'));
    renderPage();
    expect(await screen.findByText(/Makléře se nepodařilo načíst: HTTP 500/)).toBeInTheDocument();
    expect(screen.queryByText('Makléř nenalezen.')).not.toBeInTheDocument();
  });

  it('still reports an unknown broker as not found', async () => {
    vi.mocked(brokers.fetchBrokerDossier).mockResolvedValue(null);
    renderPage();
    expect(await screen.findByText('Makléř nenalezen.')).toBeInTheDocument();
  });

  /* The dossier succeeds independently now, so an inventory failure rendered as
     "Žádné inzeráty." would sit directly under a stats strip asserting 37/52. */
  it('does not report a failed inventory read as an empty inventory', async () => {
    vi.mocked(brokers.fetchBrokerListings).mockRejectedValue(new Error('timeout'));
    renderPage();
    expect(
      await screen.findByText(/Inzeráty se nepodařilo načíst: timeout/),
    ).toBeInTheDocument();
    expect(screen.queryByText('Žádné inzeráty.')).not.toBeInTheDocument();
  });

  it('reports a genuinely empty inventory as empty', async () => {
    renderPage();
    expect(await screen.findByText('Žádné inzeráty.')).toBeInTheDocument();
  });
});

/* The modal seeds from the SAME Typ/Nabídka selection the inventory table is
 * filtered to (the state is lifted to the page for exactly this) — so what the
 * operator narrowed the table to is what the map opens scoped to. */
describe('<BrokerDetail> explore this broker', () => {
  beforeEach(() => openExploreBroker.mockClear());

  it('opens the modal scoped to this broker with the current Typ/Nabídka selection', async () => {
    vi.mocked(brokers.fetchBrokerListings).mockResolvedValue([
      listing({ listing_id: 1, category_main: 'byt', category_type: 'prodej' }),
      listing({ listing_id: 2, category_main: 'dum', category_type: 'pronajem' }),
    ]);
    renderPage();
    const button = await screen.findByRole('button', { name: /Explore this broker's listings/ });

    // Vše / Vše (the page's default) seeds NO category constraint.
    fireEvent.click(button);
    expect(openExploreBroker).toHaveBeenLastCalledWith({
      brokerId: 7,
      brokerName: 'Jan Novák',
      categoryMain: null,
      categoryType: null,
    });

    // Narrow the table, then explore again — the seed follows the pills.
    fireEvent.click(await screen.findByText('Domy'));
    fireEvent.click(screen.getByText('Pronájem'));
    fireEvent.click(button);
    expect(openExploreBroker).toHaveBeenLastCalledWith({
      brokerId: 7,
      brokerName: 'Jan Novák',
      categoryMain: 'dum',
      categoryType: 'pronajem',
    });
  });
});
