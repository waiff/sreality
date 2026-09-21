import { describe, expect, it } from 'vitest';
import {
  externalMapLinks,
  googleMapsPointUrl,
  iKatastrPointUrl,
  mapyCzPointUrl,
  SREALITY_PRICE_MAP_URL,
  srealityPriceMapLink,
} from './geoLinks';

/* Wenceslas Square, Prague — lat 50.08, lon 14.42. The two numbers are far
   enough apart that a swapped pair is unmistakable (14.42°N, 50.08°E is open
   ocean off Somalia), which is exactly the bug these cases exist to catch. */
const LAT = 50.081234;
const LNG = 14.428765;

describe('mapyCzPointUrl', () => {
  it('builds the documented key-free showmap URL with center as lon,lat', () => {
    expect(mapyCzPointUrl(LAT, LNG)).toBe(
      'https://mapy.com/fnc/v1/showmap?center=14.428765,50.081234&zoom=17&marker=true',
    );
  });
  it('takes an explicit zoom', () => {
    expect(mapyCzPointUrl(LAT, LNG, 14)).toContain('&zoom=14&');
  });
});

describe('googleMapsPointUrl', () => {
  it('builds the Maps URLs API search form with query as lat,lng', () => {
    expect(googleMapsPointUrl(LAT, LNG)).toBe(
      'https://www.google.com/maps/search/?api=1&query=50.081234,14.428765',
    );
  });
});

describe('iKatastrPointUrl', () => {
  it('puts the point in both kde (view) and info (parcel panel), as lat,lon', () => {
    expect(iKatastrPointUrl(LAT, LNG)).toBe(
      'https://ikatastr.cz/#kde=50.081234,14.428765,18&mapa=zakladni' +
        '&vrstvy=parcelybudovy&info=50.081234,14.428765',
    );
  });
});

describe('coordinate rounding', () => {
  it('trims geocoder noise to 6 decimals without padding whole numbers', () => {
    // 1e-7 differences are below the 11 cm the 6th decimal buys, and a
    // toFixed-style builder would emit "50,14.000000" for the clean case.
    expect(googleMapsPointUrl(50.0812339999, 14.0)).toBe(
      'https://www.google.com/maps/search/?api=1&query=50.081234,14',
    );
  });
  it('keeps the southern/western sign', () => {
    expect(googleMapsPointUrl(-33.8688, -151.2093)).toContain(
      'query=-33.8688,-151.2093',
    );
  });
});

describe('externalMapLinks', () => {
  it('returns the three services in reading order, each with a label and a title', () => {
    const links = externalMapLinks(LAT, LNG);
    expect(links.map((l) => l.key)).toEqual(['mapy', 'google', 'katastr']);
    for (const l of links) {
      expect(l.label).toBeTruthy();
      expect(l.title).toBeTruthy();
      expect(l.url.startsWith('https://')).toBe(true);
    }
  });
  it('sends every service the SAME point (no per-service coordinate drift)', () => {
    const [mapy, google, katastr] = externalMapLinks(LAT, LNG);
    expect(mapy.url).toContain('14.428765,50.081234');
    expect(google.url).toContain('50.081234,14.428765');
    expect(katastr.url).toContain('50.081234,14.428765');
  });
});

describe('srealityPriceMapLink', () => {
  it('uses the looked-up place and names it in the hover text', () => {
    const url =
      'https://www.sreality.cz/cenova-mapa/hledani/byty/hlavni-mesto-praha-10/' +
      'hlavni-mesto-praha-47/praha-3468?ulice=rasinovo-nabrezi-122977';
    const link = srealityPriceMapLink({ url, name: 'ulice Rašínovo nábřeží' });
    expect(link).toMatchObject({ key: 'cenova-mapa', group: 'price', label: 'Cenová mapa', url });
    expect(link.title).toContain('ulice Rašínovo nábřeží');
  });

  /* Pending, failed and "nothing near the point" all land on the national map —
     a working link, only less specific, and the title says so. */
  it.each([undefined, { url: null, name: null }])('falls back to the national map (%j)', (place) => {
    const link = srealityPriceMapLink(place);
    expect(link.url).toBe(SREALITY_PRICE_MAP_URL);
    expect(SREALITY_PRICE_MAP_URL).toBe('https://www.sreality.cz/cenova-mapa');
    expect(link.title).toContain('national map');
  });
});

describe('link groups', () => {
  /* Registered sales are a table on the listing page now (SoldCompsBlock), so
     the price row holds the Cenova mapa chip alone — the discriminator stays
     because the two questions still split into two rows. */
  it('puts the three maps in the place row and leaves the price row to Cenova mapa', () => {
    expect(externalMapLinks(LAT, LNG).map((l) => [l.key, l.group])).toEqual([
      ['mapy', 'place'],
      ['google', 'place'],
      ['katastr', 'place'],
    ]);
    expect(srealityPriceMapLink(undefined).group).toBe('price');
  });
});
