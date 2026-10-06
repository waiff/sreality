import { describe, expect, it } from 'vitest';
import {
  buildPriceSeries,
  buildChartRows,
  lowestActivePrice,
  priceChangeEvents,
  seriesValueKey,
  seriesObservedKey,
  type PriceAdvert,
  type PriceSeries,
} from './priceHistory';
import type { ListingSnapshotPublic } from './types';

const NOW = Date.parse('2026-03-01T00:00:00Z');

function snap(
  listing_id: number,
  scraped_at: string,
  price_czk: number | null,
): ListingSnapshotPublic {
  return { id: Math.random(), listing_id, scraped_at, price_czk };
}

const ADVERT: PriceAdvert = {
  id: 100,
  source: 'sreality',
  is_active: true,
  price_czk: 2_400_000,
  first_seen_at: '2026-01-01T00:00:00Z',
  last_seen_at: '2026-03-01T00:00:00Z',
};

/* A second advert on another portal: seen later, delisted before now. */
const IDNES: PriceAdvert = {
  id: 200,
  source: 'idnes',
  is_active: false,
  price_czk: 2_500_000,
  first_seen_at: '2026-01-10T00:00:00Z',
  last_seen_at: '2026-02-20T00:00:00Z',
};

describe('buildPriceSeries', () => {
  it('draws the advert’s own snapshots as one track, extended to now while live', () => {
    const series = buildPriceSeries(
      [ADVERT],
      [
        snap(100, '2026-02-01T00:00:00Z', 2_400_000),
        snap(100, '2026-01-01T00:00:00Z', 2_600_000),
      ],
      NOW,
    );
    expect(series).toHaveLength(1);
    expect(series[0].label).toBe('Sreality');
    expect(series[0].points.map((p) => p.price)).toEqual([2_600_000, 2_400_000]);
    expect(series[0].endT).toBe(NOW);
  });

  /* MS9: every advert is its own line, so the lines together are the property's
     time on the market — no stored on/off log needed. */
  it('draws one track per advert, in the order given, labelled by portal', () => {
    const series = buildPriceSeries(
      [ADVERT, IDNES],
      [snap(100, '2026-01-01T00:00:00Z', 2_600_000), snap(200, '2026-01-15T00:00:00Z', 2_500_000)],
      NOW,
    );
    expect(series.map((s) => [s.id, s.label])).toEqual([
      [100, 'Sreality'],
      [200, 'iDNES Reality'],
    ]);
    // The delisted advert's line ends at its last sighting, not now.
    expect(series[1].endT).toBe(Date.parse('2026-02-20T00:00:00Z'));
  });

  it('never mixes in another advert’s snapshots — a step never spans two adverts', () => {
    const series = buildPriceSeries(
      [ADVERT, IDNES],
      [snap(100, '2026-01-01T00:00:00Z', 2_600_000), snap(200, '2026-01-15T00:00:00Z', 2_500_000)],
      NOW,
    );
    expect(series[0].points.map((p) => p.price)).toEqual([2_600_000]);
    expect(series[1].points.map((p) => p.price)).toEqual([2_500_000]);
  });

  it('dates the label when two tracks share a portal, as the advert rows do', () => {
    const first = { ...ADVERT, first_seen_at: '2026-01-01T12:00:00Z' };
    const relist = { ...ADVERT, id: 300, first_seen_at: '2026-02-05T12:00:00Z' };
    const series = buildPriceSeries(
      [first, relist],
      [snap(100, '2026-01-01T00:00:00Z', 2_600_000), snap(300, '2026-02-05T00:00:00Z', 2_300_000)],
      NOW,
    );
    expect(series.map((s) => s.label)).toEqual([
      'Sreality · 01/01/2026',
      'Sreality · 05/02/2026',
    ]);
  });

  it('counts only the drawn lines: an unpriced advert on the same portal dates nothing', () => {
    const unpriced = { ...ADVERT, id: 300, price_czk: null, first_seen_at: '2026-02-05T12:00:00Z' };
    const series = buildPriceSeries([ADVERT, unpriced], [snap(100, '2026-01-01T00:00:00Z', 2_600_000)], NOW);
    expect(series.map((s) => s.label)).toEqual(['Sreality']);
  });

  it('synthesizes a single point when the advert has no snapshots but a price', () => {
    const series = buildPriceSeries(
      [{ ...ADVERT, is_active: false, last_seen_at: '2026-02-15T00:00:00Z' }],
      [],
      NOW,
    );
    expect(series[0].points).toEqual([
      { t: Date.parse('2026-01-01T00:00:00Z'), price: 2_400_000 },
    ]);
    // delisted → extends only to last-seen, not now
    expect(series[0].endT).toBe(Date.parse('2026-02-15T00:00:00Z'));
  });

  it('draws nothing for an advert that never had a price', () => {
    expect(buildPriceSeries([{ ...ADVERT, price_czk: null }], [], NOW)).toEqual([]);
  });
});

describe('lowestActivePrice', () => {
  const ad = (source: string, price_czk: number | null, is_active = true) => ({
    source,
    price_czk,
    is_active,
  });

  it('is nothing when the lowest active price is the header’s', () => {
    expect(lowestActivePrice([ad('sreality', 5_000_000), ad('idnes', 5_200_000)], 5_000_000))
      .toBeNull();
  });

  it('names the cheaper active advert’s portal', () => {
    expect(lowestActivePrice([ad('sreality', 5_000_000), ad('idnes', 4_900_000)], 5_000_000))
      .toEqual({ price: 4_900_000, sources: ['idnes'] });
  });

  it('treats a header with no price as differing from any active price', () => {
    expect(lowestActivePrice([ad('sreality', null), ad('idnes', 4_900_000)], null))
      .toEqual({ price: 4_900_000, sources: ['idnes'] });
  });

  it('ignores a cheaper advert that is no longer active', () => {
    expect(lowestActivePrice([ad('sreality', 5_000_000), ad('bazos', 3_000_000, false)], 5_000_000))
      .toBeNull();
  });

  it('names every portal quoting the lowest price', () => {
    expect(
      lowestActivePrice(
        [ad('sreality', 5_000_000), ad('idnes', 4_800_000), ad('bazos', 4_800_000)],
        5_000_000,
      ),
    ).toEqual({ price: 4_800_000, sources: ['idnes', 'bazos'] });
  });

  it('is nothing when no active advert states a price', () => {
    expect(lowestActivePrice([ad('sreality', null), ad('idnes', 4_000_000, false)], null))
      .toBeNull();
  });
});

describe('buildChartRows', () => {
  const series = () =>
    buildPriceSeries(
      [ADVERT],
      [
        snap(100, '2026-01-01T00:00:00Z', 2_600_000),
        snap(100, '2026-02-01T00:00:00Z', 2_400_000),
      ],
      NOW,
    );

  it('carries the last known price forward between observations', () => {
    const rows = buildChartRows(series());
    expect(rows.map((r) => r.t)).toEqual([
      Date.parse('2026-01-01T00:00:00Z'),
      Date.parse('2026-02-01T00:00:00Z'),
      NOW,
    ]);
    expect(rows.map((r) => r[seriesValueKey(100)])).toEqual([2_600_000, 2_400_000, 2_400_000]);
  });

  it('flags only the rows where the track was really observed', () => {
    const rows = buildChartRows(series());
    // The trailing row is the live extension to "now", not a snapshot.
    expect(rows.map((r) => r[seriesObservedKey(100)])).toEqual([true, true, false]);
  });

  it('leaves a track NULL outside its own window', () => {
    const rows = buildChartRows([
      { id: 1, label: 'A', points: [{ t: 10, price: 100 }], endT: 20 },
      { id: 2, label: 'B', points: [{ t: 30, price: 200 }], endT: 40 },
    ]);
    expect(rows.map((r) => r[seriesValueKey(1)])).toEqual([100, 100, null, null]);
    expect(rows.map((r) => r[seriesValueKey(2)])).toEqual([null, null, 200, 200]);
  });

  it('returns no rows for an empty series set', () => {
    expect(buildChartRows([])).toEqual([]);
  });

  it('resamples the step so a hover reads the price at that instant, not the nearest change', () => {
    // The reported bug, to scale: first seen 9. 5. at 16M, one step to 21M on
    // 30. 7., still live on 11. 8. Recharts picks the tooltip row by nearest
    // midpoint, so with only those three rows everything after ~19. 6. read
    // 21M — the cursor sat on 1. 7. and the banner said 30. 7. / 21M.
    const first = Date.parse('2026-05-09T00:00:00Z');
    const step = Date.parse('2026-07-30T00:00:00Z');
    const now = Date.parse('2026-08-11T00:00:00Z');
    const s: PriceSeries[] = [
      {
        id: 1,
        label: 'Price',
        points: [{ t: first, price: 16_000_000 }, { t: step, price: 21_000_000 }],
        endT: now,
      },
    ];
    const rows = buildChartRows(s, 400);

    const nearestTo = (t: number) =>
      rows.reduce((best, r) =>
        Math.abs((r.t as number) - t) < Math.abs((best.t as number) - t) ? r : best,
      );
    const july1 = Date.parse('2026-07-01T00:00:00Z');
    expect(nearestTo(july1)[seriesValueKey(1)]).toBe(16_000_000);
    // and the row a hover lands on is genuinely near the cursor, not weeks off
    expect(Math.abs((nearestTo(july1).t as number) - july1)).toBeLessThan(
      (now - first) / 400,
    );
    // the step itself still reads correctly on either side (a day out — the
    // grid is ~6h here, so anything closer legitimately snaps to the step row)
    const DAY = 86_400_000;
    expect(nearestTo(step - DAY)[seriesValueKey(1)]).toBe(16_000_000);
    expect(nearestTo(step + DAY)[seriesValueKey(1)]).toBe(21_000_000);
    // hovering the change instant still reports it as a real observation, so
    // the emphasised dot and the tooltip's delta badge keep working
    expect(rows.find((r) => r.t === step)?.[seriesObservedKey(1)]).toBe(true);
  });

  it('keeps every real observation exact and flags only those as observed', () => {
    const s: PriceSeries[] = [
      { id: 1, label: 'Price', points: [{ t: 0, price: 100 }, { t: 700, price: 90 }], endT: 1000 },
    ];
    const rows = buildChartRows(s, 50);
    // the resampled grid never displaces a real observation...
    for (const t of [0, 700]) {
      const row = rows.find((r) => r.t === t);
      expect(row, `observation at ${t} missing`).toBeDefined();
      expect(row?.[seriesObservedKey(1)]).toBe(true);
    }
    // ...and adds no phantom ones (grid rows are hover targets, not data)
    expect(rows.filter((r) => r[seriesObservedKey(1)] === true)).toHaveLength(2);
    expect(rows.length).toBeGreaterThan(45);
  });

  it('resamples within the domain only, leaving the drawn line identical', () => {
    const s: PriceSeries[] = [
      { id: 1, label: 'Price', points: [{ t: 10, price: 100 }, { t: 20, price: 200 }], endT: 30 },
    ];
    const dense = buildChartRows(s, 100);
    const times = dense.map((r) => r.t as number);
    expect(Math.min(...times)).toBe(10);
    expect(Math.max(...times)).toBe(30);
    // every sampled value is one the sparse step already held at that instant
    for (const r of dense) {
      const t = r.t as number;
      expect(r[seriesValueKey(1)]).toBe(t < 20 ? 100 : 200);
    }
  });

});

describe('priceChangeEvents', () => {
  it('reports each step with its exact instant, delta and direction, newest first', () => {
    const events = priceChangeEvents([
      {
        id: 100,
        label: 'Price',
        points: [
          { t: Date.parse('2026-01-01T00:00:00Z'), price: 4_000_000 },
          { t: Date.parse('2026-01-10T00:00:00Z'), price: 4_000_000 },
          { t: Date.parse('2026-01-20T00:00:00Z'), price: 3_700_000 },
          { t: Date.parse('2026-02-05T00:00:00Z'), price: 3_850_000 },
        ],
        endT: NOW,
      },
    ]);
    expect(events).toHaveLength(2);
    expect(events[0]).toMatchObject({
      t: Date.parse('2026-02-05T00:00:00Z'),
      from: 3_700_000,
      to: 3_850_000,
    });
    expect(events[0].pct).toBeCloseTo(4.054, 3);
    expect(events[1]).toMatchObject({ from: 4_000_000, to: 3_700_000 });
    expect(events[1].pct).toBeCloseTo(-7.5, 5);
  });

  it('keeps tracks separate and labels them', () => {
    const events = priceChangeEvents([
      { id: 1, label: 'Sreality', points: [{ t: 1, price: 100 }, { t: 3, price: 90 }], endT: 5 },
      { id: 2, label: 'Bazos', points: [{ t: 2, price: 200 }, { t: 4, price: 180 }], endT: 5 },
    ]);
    expect(events.map((e) => [e.label, e.t])).toEqual([
      ['Bazos', 4],
      ['Sreality', 3],
    ]);
  });

  it('is empty for a flat or single-point track', () => {
    expect(priceChangeEvents([{ id: 1, label: 'A', points: [{ t: 1, price: 100 }], endT: 2 }]))
      .toEqual([]);
  });
});
