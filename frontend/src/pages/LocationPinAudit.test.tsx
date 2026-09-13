/* The audit page — the set W5 hides from consumers (migration 514). Three
 * properties are worth pinning down, because each is a way the page could
 * quietly mislead the operator:
 *   - the matrix reports what the summary payload says (no client arithmetic
 *     of its own invents or drops a listing);
 *   - a filter reaches the SERVER, so the list under a matrix cell is the same
 *     cohort the cell counted;
 *   - the listing link is built by the shared helper, not hand-assembled;
 *   - W7-b: the page shows ONE state at a time, says how big the other one is,
 *     and the quality buckets never leak into „čeká na zpracování“.
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import LocationPinAudit from './LocationPinAudit';
import * as pinAudit from '@/lib/pinAudit';
import * as listingUrl from '@/lib/listingUrl';

vi.mock('@/lib/pinAudit', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pinAudit')>();
  return {
    ...actual,
    fetchPinAuditSummary: vi.fn(),
    fetchPinAuditPage: vi.fn(),
  };
});

const REFRESHED = '2026-09-13T05:25:00Z';

const SUMMARY: pinAudit.PinAuditSummaryRow[] = [
  { state: 'unresolved', source: 'sreality', category_main: 'byt', quality: 'delisted_no_claims', sibling_has_pin: false, n: 290, refreshed_at: REFRESHED },
  { state: 'unresolved', source: 'sreality', category_main: 'byt', quality: 'delisted_no_claims', sibling_has_pin: true, n: 10, refreshed_at: REFRESHED },
  { state: 'unresolved', source: 'sreality', category_main: 'byt', quality: 'active_unresolved', sibling_has_pin: false, n: 20, refreshed_at: REFRESHED },
  { state: 'unresolved', source: 'sreality', category_main: 'pozemek', quality: 'delisted_no_claims', sibling_has_pin: false, n: 7, refreshed_at: REFRESHED },
  { state: 'unresolved', source: 'bazos', category_main: 'byt', quality: 'active_no_claims', sibling_has_pin: false, n: 1000, refreshed_at: REFRESHED },
  { state: 'unresolved', source: 'bazos', category_main: 'pozemek', quality: 'active_no_claims', sibling_has_pin: false, n: 5, refreshed_at: REFRESHED },
  /* The other state: the lane simply has not reached these yet. Deliberately a
   * portal and a type the unresolved set does not carry, so a leak between the
   * two shows up as an extra matrix row rather than a bigger number. */
  { state: 'pending', source: 'idnes', category_main: 'dum', quality: 'active_no_claims', sibling_has_pin: false, n: 40, refreshed_at: REFRESHED },
  { state: 'pending', source: 'idnes', category_main: 'dum', quality: 'active_unresolved', sibling_has_pin: true, n: 5, refreshed_at: REFRESHED },
];

const ROW: pinAudit.PinAuditRow = {
  listing_id: 4242,
  property_id: 77,
  sreality_id: 876654668,
  source: 'sreality',
  source_id_native: '876654668',
  source_url: 'https://www.sreality.cz/detail/pronajem/byt/1+1/x/876654668',
  category_main: 'byt',
  category_type: 'pronajem',
  disposition: '1+1',
  area_m2: 47,
  display_label: 'Korunní 123/4, Praha 2',
  price_czk: 24000,
  is_active: false,
  first_seen_at: '2026-05-01T20:18:00Z',
  last_seen_at: '2026-05-05T11:51:57Z',
  country_status: 'undetermined',
  granularity: 'unknown',
  match_confidence: 'low',
  resolver_version: 'resolver:v4.1',
  resolved_at: '2026-09-12T19:58:11Z',
  has_row: true,
  has_claims: false,
  claims_now: false,
  sibling_has_pin: true,
  quality: 'delisted_no_claims',
  state: 'unresolved',
  refreshed_at: REFRESHED,
};

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/new-dedup/pin-audit']}>
        <LocationPinAudit />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('LocationPinAudit', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(pinAudit.fetchPinAuditSummary).mockResolvedValue(SUMMARY);
    vi.mocked(pinAudit.fetchPinAuditPage).mockResolvedValue({
      rows: [ROW],
      nextCursor: null,
    });
  });

  it('renders the portal x type matrix from the summary payload', async () => {
    renderPage();
    /* sreality: 300 + 20 byty, 7 pozemky, 327 total. bazos: 1000 + 5 = 1005. */
    const srealityRow = await screen.findByTestId('pin-audit-matrix-sreality');
    expect(within(srealityRow).getByText('320')).toBeInTheDocument();
    expect(within(srealityRow).getByText('7')).toBeInTheDocument();
    expect(within(srealityRow).getByText('327')).toBeInTheDocument();

    const bazosRow = screen.getByTestId('pin-audit-matrix-bazos');
    expect(within(bazosRow).getByText('1 005')).toBeInTheDocument();

    /* The grand total is the sum of every cell, nothing else. */
    const totalRow = screen.getByTestId('pin-audit-matrix-total');
    expect(within(totalRow).getByText('1 332')).toBeInTheDocument();
  });

  it('shows the refresh time so the operator knows how old the list is', async () => {
    renderPage();
    await waitFor(() =>
      expect(screen.getByText(/stav k/)).toBeInTheDocument(),
    );
  });

  it('sends a quality-bucket filter to the server, not just to the client', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('pin-audit-matrix-sreality');

    await user.click(
      screen.getByRole('button', {
        name: 'inzerát běží · nebylo z čeho polohu určit',
      }),
    );

    await waitFor(() => {
      const calls = vi.mocked(pinAudit.fetchPinAuditPage).mock.calls;
      const last = calls[calls.length - 1];
      expect(last[0].qualities).toEqual(['active_no_claims']);
    });
  });

  it('clicking a matrix cell scopes every read to that portal and type', async () => {
    const user = userEvent.setup();
    renderPage();
    const srealityRow = await screen.findByTestId('pin-audit-matrix-sreality');

    await user.click(within(srealityRow).getByRole('button', { name: '320' }));

    await waitFor(() => {
      const calls = vi.mocked(pinAudit.fetchPinAuditPage).mock.calls;
      const last = calls[calls.length - 1];
      expect(last[0].sources).toEqual(['sreality']);
      expect(last[0].categories).toEqual(['byt']);
    });
  });

  it('links the listing through the shared route helper', async () => {
    const spy = vi.spyOn(listingUrl, 'listingRowPath');
    renderPage();
    await screen.findByTestId('pin-audit-row-4242');

    const expected = listingUrl.listingRowPath({
      source: 'sreality',
      source_id_native: '876654668',
      sreality_id: 876654668,
      property_id: 77,
    });
    const link = screen
      .getByTestId('pin-audit-row-4242')
      .querySelector('a[href]') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe(expected);
    spy.mockRestore();
  });

  it('keeps the portal link and the resolver verdict on the row', async () => {
    renderPage();
    const row = await screen.findByTestId('pin-audit-row-4242');
    expect(
      within(row).getByRole('link', { name: /portál/ }).getAttribute('href'),
    ).toBe(ROW.source_url);
    expect(within(row).getByText(/undetermined/)).toBeInTheDocument();
    /* W6-a: the superseded-evidence column is GONE, because the rows behind it
     * are. A claim is deleted the moment its contract version is retired, so a
     * column that could only ever say "žádná" would read as a finding. */
    expect(screen.queryByText('Stará data')).not.toBeInTheDocument();
    expect(within(row).getByText('má polohu')).toBeInTheDocument();
  });

  it('states the split the ruling turns on, over the selected state', async () => {
    renderPage();
    /* The unresolved set: 1332 rows, 1025 active (1000 + 5 + 20), 307 delisted,
     * 10 recoverable from a sibling listing — all off the same payload. */
    await waitFor(() =>
      expect(screen.getByText(/1 025 běží · 307 stažených/)).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/u 10 má jiný inzerát téže nemovitosti polohu určenou/),
    ).toBeInTheDocument();
  });

  it('prints BOTH state totals, and says which one is the problem', async () => {
    renderPage();
    const totals = await screen.findByTestId('pin-audit-state-totals');
    /* 45 waiting (40 + 5) and 1 332 processed-but-undecided. */
    await waitFor(() =>
      expect(totals).toHaveTextContent(/čeká na zpracování:\s*45/),
    );
    expect(totals).toHaveTextContent(/není problém/);
    expect(totals).toHaveTextContent(/zpracováno, nerozhodnuto:\s*1\s*332/);
    expect(totals).toHaveTextContent(/toto je problém/);
  });

  it('opens on the issue, not on the waiting room', async () => {
    renderPage();
    await waitFor(() =>
      expect(vi.mocked(pinAudit.fetchPinAuditPage)).toHaveBeenCalled(),
    );
    const first = vi.mocked(pinAudit.fetchPinAuditPage).mock.calls[0];
    expect(first[0].state).toBe('unresolved');
    /* And the matrix is the unresolved cohort only: idnes is pending-only. */
    expect(screen.queryByTestId('pin-audit-matrix-idnes')).toBeNull();
  });

  it('the state toggle re-scopes the matrix AND the server read', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('pin-audit-matrix-sreality');

    await user.click(
      screen.getByRole('button', { name: /čeká na zpracování \(45\)/ }),
    );

    await waitFor(() => {
      const calls = vi.mocked(pinAudit.fetchPinAuditPage).mock.calls;
      expect(calls[calls.length - 1][0].state).toBe('pending');
    });
    const idnes = screen.getByTestId('pin-audit-matrix-idnes');
    const cells = within(idnes).getAllByRole('cell');
    expect(cells[cells.length - 1]).toHaveTextContent('45');
    expect(screen.queryByTestId('pin-audit-matrix-sreality')).toBeNull();
  });

  it('hides the verdict buckets under pending — there is no verdict yet', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('pin-audit-matrix-sreality');
    /* Chosen first, so the assertion cannot pass on a stale render. */
    await user.click(
      screen.getByRole('button', {
        name: 'inzerát běží · nebylo z čeho polohu určit',
      }),
    );

    await user.click(
      screen.getByRole('button', { name: /čeká na zpracování \(45\)/ }),
    );

    await waitFor(() =>
      expect(
        screen.queryByRole('button', {
          name: 'inzerát běží · nebylo z čeho polohu určit',
        }),
      ).toBeNull(),
    );
    /* And the filter goes with the chips: a leftover bucket must not narrow the
     * pending list behind the operator's back. */
    const calls = vi.mocked(pinAudit.fetchPinAuditPage).mock.calls;
    expect(calls[calls.length - 1][0].qualities).toEqual([]);
  });

  it('sends the sibling filter to the server', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('pin-audit-matrix-sreality');

    await user.click(screen.getByRole('button', { name: 'Má' }));

    await waitFor(() => {
      const calls = vi.mocked(pinAudit.fetchPinAuditPage).mock.calls;
      expect(calls[calls.length - 1][0].sibling).toBe('yes');
    });
    /* And the matrix narrows with it: sreality's row total falls 327 -> 10. */
    const cells = within(
      screen.getByTestId('pin-audit-matrix-sreality'),
    ).getAllByRole('cell');
    expect(cells[cells.length - 1]).toHaveTextContent('10');
  });

  it('shows the one place string, never a hand-assembled one', async () => {
    renderPage();
    const row = await screen.findByTestId('pin-audit-row-4242');
    expect(within(row).getByText('Korunní 123/4, Praha 2')).toBeInTheDocument();
  });

  it('says what the set IS: hidden from customers until the location is decided', async () => {
    renderPage();
    expect(
      await screen.findByText(/nezobrazují, dokud nemají rozhodnutou polohu/),
    ).toBeInTheDocument();
  });
});
