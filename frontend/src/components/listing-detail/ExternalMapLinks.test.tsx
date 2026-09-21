/* The four external chips under the header map. Cheap surface, but the parts
   that silently rot are the ones pinned here: that all four render, that each
   opens in a new tab without leaking the referrer, that the hrefs carry THIS
   listing's place rather than a default view of Czechia — and that the one chip
   needing a lookup (Cenová mapa) is a working link whatever the lookup does. */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import ExternalMapLinks from './ExternalMapLinks';
import * as maps from '@/lib/maps';

vi.mock('@/lib/maps', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/maps')>();
  return { ...actual, fetchSrealityPriceMap: vi.fn() };
});

const lookup = vi.mocked(maps.fetchSrealityPriceMap);

const LABEL = 'Popelky Biliánové 534, Králův Dvůr';
const STREET_URL =
  'https://www.sreality.cz/cenova-mapa/hledani/byty/stredocesky-kraj-11/' +
  'beroun-49/kraluv-dvur-3605?ulice=popelky-bilianove-78029';

function renderLinks(props: { lat: number; lng: number; label: string | null }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ExternalMapLinks {...props} />
    </QueryClientProvider>,
  );
}

const priceMapLink = () => screen.getByRole('link', { name: /Cenová mapa/ });

describe('<ExternalMapLinks>', () => {
  beforeEach(() => {
    lookup.mockReset();
    lookup.mockResolvedValue({ url: STREET_URL, level: 'street', name: 'ulice Popelky Biliánové' });
  });

  it('renders one external link per service, pointed at the given place', async () => {
    renderLinks({ lat: 50.081234, lng: 14.428765, label: LABEL });

    expect(screen.getByRole('link', { name: /Mapy\.cz/ })).toHaveAttribute(
      'href',
      'https://mapy.com/fnc/v1/showmap?center=14.428765,50.081234&zoom=17&marker=true',
    );
    expect(screen.getByRole('link', { name: /Google/ })).toHaveAttribute(
      'href',
      'https://www.google.com/maps/search/?api=1&query=50.081234,14.428765',
    );
    expect(screen.getByRole('link', { name: /Katastr/ })).toHaveAttribute(
      'href',
      'https://ikatastr.cz/#kde=50.081234,14.428765,18&mapa=zakladni' +
        '&vrstvy=parcelybudovy&info=50.081234,14.428765',
    );
    await waitFor(() => expect(priceMapLink()).toHaveAttribute('href', STREET_URL));
    expect(lookup).toHaveBeenCalledWith(LABEL, 50.081234, 14.428765, expect.anything());
    expect(priceMapLink()).toHaveAttribute('title', expect.stringContaining('ulice Popelky Biliánové'));
  });

  it('opens every link in a new tab, with the noopener/noreferrer pair', () => {
    renderLinks({ lat: 50, lng: 14, label: LABEL });

    const links = screen.getAllByRole('link');
    expect(links).toHaveLength(4);
    for (const a of links) {
      expect(a).toHaveAttribute('target', '_blank');
      expect(a.getAttribute('rel')).toBe('noopener noreferrer');
      // The hover text is the only place the full service name and its purpose
      // live — the chip label is abbreviated to fit the map column.
      expect(a.getAttribute('title')).toBeTruthy();
    }
  });

  /* Rendering must not wait on the lookup: the chip is a link from the first
     paint, to the national map, and only sharpens. */
  it('links the national map while the lookup is still out', () => {
    lookup.mockReturnValue(new Promise(() => {}));
    renderLinks({ lat: 50, lng: 14, label: LABEL });

    expect(priceMapLink()).toHaveAttribute('href', 'https://www.sreality.cz/cenova-mapa');
  });

  it('keeps the national map when the lookup fails or finds nothing', async () => {
    lookup.mockRejectedValue(new Error('HTTP 503'));
    const { unmount } = renderLinks({ lat: 50, lng: 14, label: LABEL });
    await waitFor(() => expect(lookup).toHaveBeenCalled());
    expect(priceMapLink()).toHaveAttribute('href', 'https://www.sreality.cz/cenova-mapa');
    unmount();

    lookup.mockResolvedValue({ url: null, level: null, name: null });
    renderLinks({ lat: 50, lng: 14, label: LABEL });
    await waitFor(() => expect(lookup).toHaveBeenCalledTimes(2));
    expect(priceMapLink()).toHaveAttribute('href', 'https://www.sreality.cz/cenova-mapa');
    expect(priceMapLink()).toHaveAttribute('title', expect.stringContaining('national map'));
  });

  it('does not ask when the listing has no place label', () => {
    renderLinks({ lat: 50, lng: 14, label: null });

    expect(lookup).not.toHaveBeenCalled();
    expect(priceMapLink()).toHaveAttribute('href', 'https://www.sreality.cz/cenova-mapa');
  });

  /* Where it is vs what it sells for — two rows, so four chips never share the
     400px map column. */
  it('splits the chips into a place row and a price row', () => {
    renderLinks({ lat: 50, lng: 14, label: LABEL });

    const rowOf = (name: RegExp) => screen.getByRole('link', { name }).parentElement;
    expect(rowOf(/Mapy\.cz/)).toBe(rowOf(/Katastr/));
    expect(rowOf(/Mapy\.cz/)).not.toBe(rowOf(/Cenová mapa/));
  });

  /* Registered sales moved in-app (SoldCompsBlock), so the source is no longer
     linked out to — a chip that opens what we now hold would be a second,
     un-synced answer to the same question. */
  it('no longer links out to reas.cz', () => {
    renderLinks({ lat: 50, lng: 14, label: LABEL });

    expect(screen.queryByRole('link', { name: /Reas/i })).toBeNull();
    for (const a of screen.getAllByRole('link')) {
      expect(a.getAttribute('href')).not.toContain('reas.cz');
    }
  });
});
