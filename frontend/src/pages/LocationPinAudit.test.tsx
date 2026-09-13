/* The pin-loss audit page. Three properties are worth pinning down, because
 * each is a way the page could quietly mislead the operator:
 *   - the matrix reports what the summary payload says (no client arithmetic
 *     of its own invents or drops a listing);
 *   - a filter reaches the SERVER, so the list under a matrix cell is the same
 *     cohort the cell counted;
 *   - the listing link is built by the shared helper, not hand-assembled.
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
    fetchPinAuditPoints: vi.fn(),
  };
});

/* maplibre does not run under jsdom (no WebGL); the map is lazy anyway, so the
 * page must render its table without it. */
vi.mock('@/components/PinAuditMap', () => ({
  default: () => <div data-testid="pin-audit-map" />,
}));

const REFRESHED = '2026-09-13T05:25:00Z';

const SUMMARY: pinAudit.PinAuditSummaryRow[] = [
  { source: 'sreality', category_main: 'byt', quality: 'delisted_no_claims', n: 300, refreshed_at: REFRESHED },
  { source: 'sreality', category_main: 'byt', quality: 'active_unresolved', n: 20, refreshed_at: REFRESHED },
  { source: 'sreality', category_main: 'pozemek', quality: 'delisted_no_claims', n: 7, refreshed_at: REFRESHED },
  { source: 'bazos', category_main: 'byt', quality: 'active_no_claims', n: 1000, refreshed_at: REFRESHED },
  { source: 'bazos', category_main: 'pozemek', quality: 'active_no_claims', n: 5, refreshed_at: REFRESHED },
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
  street: 'Korunní',
  locality: 'Korunní, Praha 2 - Vinohrady',
  district: 'Praha 2 - Vinohrady',
  price_czk: 24000,
  is_active: false,
  first_seen_at: '2026-05-01T20:18:00Z',
  last_seen_at: '2026-05-05T11:51:57Z',
  legacy_lat: 50.0754365,
  legacy_lng: 14.4402663,
  country_status: 'undetermined',
  granularity: 'unknown',
  match_confidence: 'low',
  resolver_version: 'resolver:v4.1',
  resolved_at: '2026-09-12T19:58:11Z',
  has_row: true,
  has_claims: false,
  claims_now: true,
  quality: 'delisted_no_claims',
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
    vi.mocked(pinAudit.fetchPinAuditPoints).mockResolvedValue({
      points: [
        {
          listing_id: 4242,
          legacy_lat: 50.07,
          legacy_lng: 14.44,
          quality: 'delisted_no_claims',
          is_active: false,
        },
      ],
      capped: false,
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
    /* The map read is filtered by the same object — one cohort, three views. */
    const pointCalls = vi.mocked(pinAudit.fetchPinAuditPoints).mock.calls;
    expect(pointCalls[pointCalls.length - 1][0].qualities).toEqual([
      'active_no_claims',
    ]);
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
    /* has_claims=false but claims_now=true — the page must say so rather than
     * let the operator read "no evidence". */
    expect(
      within(row).getByText(/podklady o poloze přibyly až po vyhodnocení/),
    ).toBeInTheDocument();
  });

  it('says when the map is showing only a prefix of the cohort', async () => {
    vi.mocked(pinAudit.fetchPinAuditPoints).mockResolvedValue({
      points: Array.from({ length: pinAudit.PIN_AUDIT_MAP_CAP }, (_, i) => ({
        listing_id: i,
        legacy_lat: 50,
        legacy_lng: 14,
        quality: 'delisted_no_claims' as const,
        is_active: false,
      })),
      capped: true,
    });
    renderPage();
    await waitFor(() =>
      expect(screen.getByText(/Zobrazeno prvních/)).toBeInTheDocument(),
    );
  });
});
