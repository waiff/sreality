import { describe, expect, it } from 'vitest';
import {
  listingUrlRows,
  buildPriceSeries,
  summarizePriceHistory,
  type UrlRow,
} from './priceHistory';
import type {
  ListingPublic,
  ListingSnapshotPublic,
  PropertySource,
} from './types';

const NOW = Date.parse('2026-03-01T00:00:00Z');

function snap(
  listing_id: number,
  scraped_at: string,
  price_czk: number | null,
): ListingSnapshotPublic {
  return {
    id: Math.random(),
    sreality_id: listing_id,
    listing_id,
    scraped_at,
    price_czk,
    description: null,
  };
}

function source(over: Partial<PropertySource>): PropertySource {
  return {
    property_id: 1,
    id: 100,
    sreality_id: 100,
    source: 'sreality',
    source_url: 'https://example.cz/1',
    source_id_native: '100',
    is_active: true,
    price_czk: 2_500_000,
    first_seen_at: '2026-01-01T00:00:00Z',
    last_seen_at: '2026-03-01T00:00:00Z',
    ...over,
  };
}

const listing = {
  id: 100,
  sreality_id: 100,
  source: 'sreality',
  is_active: true,
  price_czk: 2_500_000,
  first_seen_at: '2026-01-01T00:00:00Z',
  last_seen_at: '2026-03-01T00:00:00Z',
} as unknown as ListingPublic;

describe('listingUrlRows', () => {
  it('maps property sources newest-seen first', () => {
    const rows = listingUrlRows(
      [
        source({ id: 1, sreality_id: 1, last_seen_at: '2026-01-10T00:00:00Z' }),
        source({ id: 2, sreality_id: 2, last_seen_at: '2026-02-20T00:00:00Z' }),
      ],
      listing,
    );
    expect(rows.map((r) => r.id)).toEqual([2, 1]);
  });

  it('falls back to a single synthesized row when there are no sources', () => {
    const rows = listingUrlRows([], listing);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ id: 100, source: 'sreality', url: null });
  });

  it('reconstructs a sreality source URL from the property category triple', () => {
    const withCategory = {
      ...listing,
      category_type: 'prodej',
      category_main: 'byt',
      category_sub_cb: 5, // 2+1
    } as unknown as ListingPublic;
    const rows = listingUrlRows(
      [source({ id: 100, sreality_id: 100, source: 'sreality', source_url: null })],
      withCategory,
    );
    expect(rows[0].url).toBe('https://www.sreality.cz/detail/prodej/byt/2+1/x/100');
  });

  it('keys rows on the surrogate id, not sreality_id, so two NULL-sreality sources never collide', () => {
    const rows = listingUrlRows(
      [
        source({
          id: 11,
          sreality_id: null as unknown as number,
          source: 'idnes',
          source_url: 'https://idnes.cz/a',
          last_seen_at: '2026-01-10T00:00:00Z',
        }),
        source({
          id: 12,
          sreality_id: null as unknown as number,
          source: 'bezrealitky',
          source_url: 'https://bezrealitky.cz/b',
          last_seen_at: '2026-02-20T00:00:00Z',
        }),
      ],
      listing,
    );
    expect(rows.map((r) => r.id)).toEqual([12, 11]);
  });
});

describe('buildPriceSeries', () => {
  const urls: UrlRow[] = [
    {
      id: 100,
      source: 'sreality',
      url: null,
      isActive: true,
      price: 2_400_000,
      firstSeen: '2026-01-01T00:00:00Z',
      lastSeen: '2026-03-01T00:00:00Z',
    },
  ];

  it('groups snapshots into a track and extends a live URL to now', () => {
    const series = buildPriceSeries(
      urls,
      [
        snap(100, '2026-01-01T00:00:00Z', 2_600_000),
        snap(100, '2026-02-01T00:00:00Z', 2_400_000),
      ],
      NOW,
    );
    expect(series).toHaveLength(1);
    expect(series[0].label).toBe('Price'); // single URL → generic label
    expect(series[0].points.map((p) => p.price)).toEqual([2_600_000, 2_400_000]);
    expect(series[0].endT).toBe(NOW);
  });

  it('synthesizes a single point when a URL has no snapshots but a price', () => {
    const series = buildPriceSeries(
      [{ ...urls[0], isActive: false, lastSeen: '2026-02-15T00:00:00Z' }],
      [],
      NOW,
    );
    expect(series[0].points).toEqual([
      { t: Date.parse('2026-01-01T00:00:00Z'), price: 2_400_000 },
    ]);
    // delisted → extends only to last-seen, not now
    expect(series[0].endT).toBe(Date.parse('2026-02-15T00:00:00Z'));
  });

  it('labels each track by its portal when more than one URL exists', () => {
    const series = buildPriceSeries(
      [
        { ...urls[0], id: 1, source: 'sreality' },
        { ...urls[0], id: 2, source: 'bazos' },
      ],
      [snap(1, '2026-01-01T00:00:00Z', 1), snap(2, '2026-01-01T00:00:00Z', 2)],
      NOW,
    );
    expect(series.map((s) => s.label)).toEqual(['Sreality', 'Bazos']);
  });
});

describe('summarizePriceHistory', () => {
  const liveUrls: UrlRow[] = [
    {
      id: 100,
      source: 'sreality',
      url: null,
      isActive: true,
      price: 2_400_000,
      firstSeen: '2026-01-01T00:00:00Z',
      lastSeen: '2026-03-01T00:00:00Z',
    },
  ];

  it('counts price changes and computes the % move first→last', () => {
    const stats = summarizePriceHistory(
      liveUrls,
      [
        snap(100, '2026-01-01T00:00:00Z', 2_500_000),
        snap(100, '2026-01-15T00:00:00Z', 2_500_000), // unchanged → not a change
        snap(100, '2026-02-01T00:00:00Z', 2_400_000), // change #1
        snap(100, '2026-02-20T00:00:00Z', 2_300_000), // change #2
      ],
      2_300_000,
      NOW,
    );
    expect(stats.changes).toBe(2);
    expect(stats.pct).toBeCloseTo(((2_300_000 - 2_500_000) / 2_500_000) * 100, 5);
    expect(stats.anyActive).toBe(true);
    // active → days measured to NOW (2026-01-01 → 2026-03-01 = 59 days)
    expect(stats.days).toBe(59);
  });

  it('measures days-on-market to last-seen for a fully delisted property', () => {
    const stats = summarizePriceHistory(
      [{ ...liveUrls[0], isActive: false, lastSeen: '2026-01-31T00:00:00Z' }],
      [snap(100, '2026-01-01T00:00:00Z', 2_500_000)],
      2_500_000,
      NOW,
    );
    expect(stats.anyActive).toBe(false);
    expect(stats.days).toBe(30); // 2026-01-01 → 2026-01-31
    expect(stats.changes).toBe(0);
    expect(stats.pct).toBe(0);
  });
});
