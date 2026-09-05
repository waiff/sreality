/* /brokers — the leaderboard page is agenda-gated, not admin-gated, so anything
 * it asks of an admin-only route must be gated client-side or every non-admin
 * page view spends two 403s (retry: 1, refetched on every remount) on a badge
 * that links somewhere they cannot go.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import Brokers from './Brokers';
import * as api from '@/lib/api';
import * as auth from '@/lib/auth';
import * as brokers from '@/lib/brokers';
import type { BrokerLeaderRow } from '@/lib/brokers';
import { ROUTES } from '@/lib/routes';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  listBrokerMergeCandidates: vi.fn(),
}));
vi.mock('@/lib/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/auth')>()),
  useAuth: vi.fn(),
}));
vi.mock('@/lib/brokers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/brokers')>()),
  fetchBrokerLeaderboard: vi.fn(),
  searchBrokerFirms: vi.fn(),
  searchBrokersByName: vi.fn(),
}));
vi.mock('@/lib/supabase', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/supabase')>()),
  isSupabaseConfigured: () => true,
}));

function asUser(isAdmin: boolean): void {
  vi.mocked(auth.useAuth).mockReturnValue({
    isAdmin,
    session: null,
    user: null,
    loading: false,
    agendas: { brokers: true },
  } as unknown as ReturnType<typeof auth.useAuth>);
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/brokers']}>
        <Brokers />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(brokers.fetchBrokerLeaderboard).mockResolvedValue([]);
  vi.mocked(brokers.searchBrokerFirms).mockResolvedValue([]);
  vi.mocked(brokers.searchBrokersByName).mockResolvedValue([]);
  vi.mocked(api.listBrokerMergeCandidates).mockResolvedValue({
    data: [],
    count: 0,
    reason_counts: { name_firm: 3 },
  } as unknown as Awaited<ReturnType<typeof api.listBrokerMergeCandidates>>);
});

describe('<Brokers> merge-candidates badge', () => {
  it('never asks the admin-only route for a non-admin viewer', async () => {
    asUser(false);
    renderPage();
    await waitFor(() => expect(brokers.fetchBrokerLeaderboard).toHaveBeenCalled());
    expect(api.listBrokerMergeCandidates).not.toHaveBeenCalled();
  });

  it('still sizes the badge for an admin', async () => {
    asUser(true);
    const { findByText } = renderPage();
    await waitFor(() => expect(api.listBrokerMergeCandidates).toHaveBeenCalledWith(1));
    expect(await findByText(/Sloučit duplicity \(3\)/)).toBeInTheDocument();
  });
});

function leaderRow(broker_id: number, display_name: string): BrokerLeaderRow {
  return {
    broker_id, display_name,
    firm_id: null, firm_name: null, firm_domain: null,
    listing_count: 1, property_count: 1, active_listing_count: 1, active_property_count: 1,
  };
}

describe('<Brokers> W10a: keepPreviousData', () => {
  it('keeps the previous ledger on screen (not the loading text) while a filter change refetches', async () => {
    asUser(false);
    vi.mocked(brokers.fetchBrokerLeaderboard).mockResolvedValueOnce([
      leaderRow(1, 'První Makléř'),
    ]);
    let resolveSecond: (rows: BrokerLeaderRow[]) => void = () => {};
    const second = new Promise<BrokerLeaderRow[]>((resolve) => { resolveSecond = resolve; });
    vi.mocked(brokers.fetchBrokerLeaderboard).mockReturnValueOnce(second);

    renderPage();
    expect(await screen.findByText('První Makléř')).toBeInTheDocument();

    // "Typ" defaults to Byty (categoryMain: 'byt') — switch to Domy, which
    // changes boardQ's key and fires the second (still-pending) fetch.
    fireEvent.click(screen.getByRole('button', { name: 'Domy' }));
    await waitFor(() =>
      expect(brokers.fetchBrokerLeaderboard).toHaveBeenLastCalledWith(
        expect.objectContaining({ categoryMain: 'dum' }),
      ),
    );

    // The old row is STILL rendered — never blanked to "Načítám žebříček…" —
    // and the subtle "updating" hint is showing instead.
    expect(screen.getByText('První Makléř')).toBeInTheDocument();
    expect(screen.queryByText('Načítám žebříček…')).not.toBeInTheDocument();
    expect(screen.getByText('Aktualizuji…')).toBeInTheDocument();

    resolveSecond([leaderRow(2, 'Druhý Makléř')]);
    await waitFor(() => expect(screen.getByText('Druhý Makléř')).toBeInTheDocument());
    expect(screen.queryByText('První Makléř')).not.toBeInTheDocument();
    expect(screen.queryByText('Aktualizuji…')).not.toBeInTheDocument();
  });
});

describe('<Brokers> company filter', () => {
  it('renders the top companies as toggleable buttons, filters the leaderboard by firmIds, and untoggling clears it', async () => {
    asUser(false);
    // mmreality.cz is a franchise domain with a NULL display_name (migration
    // 190's _FIRM_DISPLAY_NAMES excludes franchises), so the button must fall
    // back to canonical_domain. Two options confirm toggling one leaves the
    // other's state alone. broker_count stays under 1000 so the expected
    // button text doesn't depend on cs-CZ's thousands-separator character.
    vi.mocked(brokers.searchBrokerFirms).mockResolvedValue([
      { firm_id: 3, canonical_domain: 'mmreality.cz', display_name: null,
        is_franchise: true, broker_count: 42 },
      { firm_id: 7, canonical_domain: 're-max.cz', display_name: null,
        is_franchise: true, broker_count: 30 },
    ]);
    const { getAllByRole } = renderPage();
    await waitFor(() => expect(brokers.fetchBrokerLeaderboard).toHaveBeenCalled());

    // Selected by plain textContent, not the accessible-name matcher: the
    // label and the "(count)" span are separate DOM nodes, and dom-testing-
    // library's name computation collapses them in a way a role/name query
    // doesn't reliably match, even though the rendered text is unambiguous.
    // Retried via waitFor — Typ/Nabídka's static buttons exist from the
    // first render, so a one-shot query resolves before the async company
    // buttons (behind searchBrokerFirms) have rendered at all.
    let mmreality: HTMLElement | undefined;
    let remax: HTMLElement | undefined;
    await waitFor(() => {
      const buttons = getAllByRole('button');
      mmreality = buttons.find((b) => b.textContent?.includes('mmreality.cz'));
      remax = buttons.find((b) => b.textContent?.includes('re-max.cz'));
      expect(mmreality).toBeDefined();
      expect(remax).toBeDefined();
    });
    if (!mmreality || !remax) throw new Error('company buttons not found');
    expect(mmreality.textContent).toContain('42');
    expect(remax.textContent).toContain('30');
    expect(mmreality).toHaveAttribute('aria-pressed', 'false');

    fireEvent.click(mmreality);
    expect(mmreality).toHaveAttribute('aria-pressed', 'true');
    expect(remax).toHaveAttribute('aria-pressed', 'false');
    await waitFor(() =>
      expect(brokers.fetchBrokerLeaderboard).toHaveBeenLastCalledWith(
        expect.objectContaining({ firmIds: [3] }),
      ),
    );

    // Back to firmIds: [] is the SAME query key as the initial mount, so
    // react-query serves it from cache — assert the button's own toggled
    // state rather than a fresh fetchBrokerLeaderboard call, which caching
    // correctly skips.
    fireEvent.click(mmreality);
    expect(mmreality).toHaveAttribute('aria-pressed', 'false');
  });
});

// Field wraps each control group in a <label>, so the FIRST pill in a group inherits the
// whole row's text as its accessible name (pre-existing on every filter group on this
// page — the company-filter test above works around the same thing). Match on
// textContent instead of the computed accessible name.
function pill(text: string): HTMLElement {
  const found = screen.getAllByRole('button').find((b) => b.textContent?.includes(text));
  if (!found) throw new Error(`no pill containing ${text}`);
  return found;
}

function hasPill(text: string): boolean {
  return screen.getAllByRole('button').some((b) => b.textContent?.includes(text));
}

describe('<Brokers> subtype filter', () => {
  // Subtype is only populated for houses / commercial, and its slugs are partitioned by
  // category_main — the same rule (and the same SUBTYPE_LABELS_BY_MAIN vocabulary) Browse
  // applies in its sidebar.
  it('appears only for Domy / Komerční, not for the Byty default', async () => {
    asUser(false);
    renderPage();
    await waitFor(() => expect(brokers.fetchBrokerLeaderboard).toHaveBeenCalled());
    expect(screen.queryByText('Podtyp')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Komerční' }));
    expect(await screen.findByText('Podtyp')).toBeInTheDocument();
    expect(hasPill('Kancelář')).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: 'Domy' }));
    await waitFor(() => expect(hasPill('Rodinný dům')).toBe(true));
    expect(hasPill('Kancelář')).toBe(false);
  });

  it('filters by the picked subtypes and gates the "bez podtypu" toggle on a selection', async () => {
    asUser(false);
    renderPage();
    await waitFor(() => expect(brokers.fetchBrokerLeaderboard).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: 'Komerční' }));

    const toggle = await screen.findByRole('button', { name: 'Počítat nabídky bez podtypu' });
    expect(toggle).toBeDisabled();

    fireEvent.click(pill('Kancelář'));
    await waitFor(() =>
      expect(brokers.fetchBrokerLeaderboard).toHaveBeenLastCalledWith(
        expect.objectContaining({ subtypes: ['kancelar'], includeUnknownSubtype: false }),
      ),
    );
    expect(toggle).not.toBeDisabled();

    fireEvent.click(toggle);
    await waitFor(() =>
      expect(brokers.fetchBrokerLeaderboard).toHaveBeenLastCalledWith(
        expect.objectContaining({ subtypes: ['kancelar'], includeUnknownSubtype: true }),
      ),
    );
  });

  // Subtype coverage is a PORTAL gap (sreality labels everything, ceskereality /
  // realitymix / mmreality label nothing), so a subtype-filtered ranking is partly a
  // ranking of which portals a broker lists on. Stating that inline is the point — the
  // caveat must appear exactly when the filter is doing something.
  it('states the portal-coverage caveat only while a subtype is selected', async () => {
    asUser(false);
    renderPage();
    await waitFor(() => expect(brokers.fetchBrokerLeaderboard).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: 'Komerční' }));
    await screen.findByText('Podtyp');
    expect(screen.queryByText(/Podtyp uvádí jen některé portály/)).not.toBeInTheDocument();

    fireEvent.click(pill('Sklad'));
    expect(await screen.findByText(/Podtyp uvádí jen některé portály/)).toBeInTheDocument();
  });

  // The slugs are partitioned by category_main, so one carried across would match
  // nothing and silently empty the ledger — Browse clears its own for the same reason.
  it('clears the picked subtypes when Typ changes', async () => {
    asUser(false);
    renderPage();
    await waitFor(() => expect(brokers.fetchBrokerLeaderboard).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: 'Komerční' }));
    fireEvent.click(pill('Kancelář'));
    await waitFor(() =>
      expect(brokers.fetchBrokerLeaderboard).toHaveBeenLastCalledWith(
        expect.objectContaining({ subtypes: ['kancelar'] }),
      ),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Domy' }));
    await waitFor(() =>
      expect(brokers.fetchBrokerLeaderboard).toHaveBeenLastCalledWith(
        expect.objectContaining({
          categoryMain: 'dum', subtypes: [], includeUnknownSubtype: false,
        }),
      ),
    );
  });
});

describe('<Brokers> value filter', () => {
  it('filters by minimum price; the "bez ceny" toggle stays disabled until a value is set', async () => {
    asUser(false);
    renderPage();
    await waitFor(() => expect(brokers.fetchBrokerLeaderboard).toHaveBeenCalled());

    const toggle = screen.getByRole('button', { name: 'Počítat nabídky bez ceny' });
    expect(toggle).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Minimální cena'), {
      target: { value: '5000000' },
    });
    await waitFor(() =>
      expect(brokers.fetchBrokerLeaderboard).toHaveBeenLastCalledWith(
        expect.objectContaining({ minPriceCzk: 5_000_000, includeUnpriced: false }),
      ),
    );
    expect(toggle).not.toBeDisabled();

    fireEvent.click(toggle);
    await waitFor(() =>
      expect(brokers.fetchBrokerLeaderboard).toHaveBeenLastCalledWith(
        expect.objectContaining({ minPriceCzk: 5_000_000, includeUnpriced: true }),
      ),
    );
  });

  // price_czk is the total asking price for a sale but the MONTHLY rent for a
  // rental (same column, same dual meaning as Browse's own price filter) — the
  // placeholder is the only cue distinguishing them, so it has to track Nabídka.
  it('labels the unit Kč for Prodej and Kč/měsíc for Pronájem', async () => {
    asUser(false);
    renderPage();
    await waitFor(() => expect(brokers.fetchBrokerLeaderboard).toHaveBeenCalled());
    expect(screen.getByLabelText('Minimální cena')).toHaveAttribute('placeholder', 'Kč');

    fireEvent.click(screen.getByRole('button', { name: 'Pronájem' }));
    await waitFor(() =>
      expect(brokers.fetchBrokerLeaderboard).toHaveBeenLastCalledWith(
        expect.objectContaining({ categoryType: 'pronajem' }),
      ),
    );
    expect(screen.getByLabelText('Minimální cena')).toHaveAttribute('placeholder', 'Kč/měsíc');
  });

  // Switching directly between Prodej and Pronájem must not silently carry a number
  // typed for one scale into the other (5 000 000 Kč is a normal sale price and an
  // impossible monthly rent) — clear rather than reinterpret.
  it('clears the value filter when the offer type actually changes', async () => {
    asUser(false);
    renderPage();
    await waitFor(() => expect(brokers.fetchBrokerLeaderboard).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText('Minimální cena'), {
      target: { value: '5000000' },
    });
    await waitFor(() =>
      expect(brokers.fetchBrokerLeaderboard).toHaveBeenLastCalledWith(
        expect.objectContaining({ minPriceCzk: 5_000_000 }),
      ),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Pronájem' }));
    await waitFor(() =>
      expect(brokers.fetchBrokerLeaderboard).toHaveBeenLastCalledWith(
        expect.objectContaining({ categoryType: 'pronajem', minPriceCzk: null, includeUnpriced: false }),
      ),
    );
    expect(screen.getByLabelText('Minimální cena')).toHaveValue('');
  });

  // "Vše" mixes prodej/pronájem/podíl/dražba under one price column, so a value
  // filter has no single meaning there — the control disappears rather than
  // silently keeping a stale, ambiguous filter applied underneath.
  it('hides the value filter behind an explanatory hint when Nabídka is "Vše"', async () => {
    asUser(false);
    renderPage();
    await waitFor(() => expect(brokers.fetchBrokerLeaderboard).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText('Minimální cena'), {
      target: { value: '3000000' },
    });
    await waitFor(() =>
      expect(brokers.fetchBrokerLeaderboard).toHaveBeenLastCalledWith(
        expect.objectContaining({ minPriceCzk: 3_000_000 }),
      ),
    );

    // Three "Vše" pills exist (Typ / Nabídka / Počet) — scope to Nabídka's field.
    const nabidkaField = screen.getByText('Nabídka').closest('label');
    if (!nabidkaField) throw new Error('Nabídka field not found');
    fireEvent.click(within(nabidkaField).getByRole('button', { name: 'Vše' }));

    expect(screen.queryByLabelText('Minimální cena')).not.toBeInTheDocument();
    expect(screen.getByText('Zvolte Prodej nebo Pronájem')).toBeInTheDocument();
    await waitFor(() =>
      expect(brokers.fetchBrokerLeaderboard).toHaveBeenLastCalledWith(
        expect.objectContaining({ categoryType: null, minPriceCzk: null, includeUnpriced: false }),
      ),
    );
  });
});

/* Link semantics. An analyst comparing brokers opens several in tabs, so these
 * rows must be real anchors — a <button> with a navigate() handler offers no
 * right-click "open in new tab", no ctrl/middle-click and no copyable address.
 * Asserted via getByRole('link'), which encodes "this IS a link" rather than
 * merely "an ancestor anchor exists", and against ROUTES.*.build() so a route
 * rename moves this test instead of leaving it stale-but-green. */
describe('<Brokers> leaderboard rows are links, not buttons', () => {
  it('renders each ledger row as an anchor to that broker', async () => {
    asUser(false);
    vi.mocked(brokers.fetchBrokerLeaderboard).mockResolvedValue([
      leaderRow(7, 'Jan Fleischner'),
      leaderRow(42, 'Michaela Koudelova'),
    ]);
    renderPage();

    const first = await screen.findByRole('link', { name: /Jan Fleischner/ });
    expect(first).toHaveAttribute('href', ROUTES.brokerDetail.build({ id: 7 }));
    expect(screen.getByRole('link', { name: /Michaela Koudelova/ })).toHaveAttribute(
      'href',
      ROUTES.brokerDetail.build({ id: 42 }),
    );
  });

  it('exposes no row-shaped button once the ledger has rendered', async () => {
    asUser(false);
    vi.mocked(brokers.fetchBrokerLeaderboard).mockResolvedValue([
      leaderRow(7, 'Jan Fleischner'),
    ]);
    renderPage();
    await screen.findByRole('link', { name: /Jan Fleischner/ });
    expect(screen.queryByRole('button', { name: /Jan Fleischner/ })).toBeNull();
  });

  it('renders each name-search result as an anchor to that broker', async () => {
    asUser(false);
    vi.mocked(brokers.searchBrokersByName).mockResolvedValue([
      {
        broker_id: 99,
        display_name: 'Hledany Makler',
        firm_name: null,
        firm_domain: null,
        cz_active_property_count: 3,
        active_property_count: 5,
      } as unknown as Awaited<ReturnType<typeof brokers.searchBrokersByName>>[number],
    ]);
    renderPage();

    fireEvent.change(screen.getByPlaceholderText(/Hledat makl/), {
      target: { value: 'Hledany' },
    });

    const hit = await screen.findByRole('link', { name: /Hledany Makler/ }, { timeout: 3000 });
    expect(hit).toHaveAttribute('href', ROUTES.brokerDetail.build({ id: 99 }));
  });
});
