/* /brokers/:id — the three states the B2 repoint made distinguishable.
 *
 * Before it, every broker read was dark and the page had exactly one outcome:
 * "Makléř nenalezen.". Now the dossier answers, so the page must tell apart
 * (1) a masked contact from an absent one, (2) a failed dossier read from an
 * unknown broker, and (3) a failed inventory read from a broker with no
 * listings — the last one contradicting the stats strip rendered right above it.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import BrokerDetail from './BrokerDetail';
import * as brokers from '@/lib/brokers';

vi.mock('@/lib/brokers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/brokers')>()),
  fetchBrokerDossier: vi.fn(),
  fetchBrokerListings: vi.fn(),
}));

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
