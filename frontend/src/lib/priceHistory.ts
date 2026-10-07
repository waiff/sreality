/* Pure helpers behind the property page's price history: every advert's own
 * price snapshots as one chart track each (MS9), and the dated price moves
 * (MS10). Kept side-effect-free (now injected, never Date.now()) so the
 * transforms are unit-testable. */
import { fmtDateSlash } from '@/lib/format';
import { portalLabel } from '@/lib/portals';
import type { ListingSnapshotPublic } from '@/lib/types';

/* One advert of the property. A price step never spans two adverts, so each is
 * its own track; together the tracks are the property's time on the market. */
export interface PriceAdvert {
  id: number;
  source: string;
  is_active: boolean;
  price_czk: number | null;
  first_seen_at: string;
  last_seen_at: string;
}

export interface PriceSeries {
  id: number;
  label: string;
  points: { t: number; price: number }[];
  endT: number;
}

/* One step-line per advert that has a price, in the order given (the canonical
 * advert first), held flat between changes and running to `nowMs` while its
 * advert is live, else to its last sighting. Labelled by portal; two tracks on
 * one portal also carry the day their advert was first seen, as its row does. */
export function buildPriceSeries(
  adverts: readonly PriceAdvert[],
  snapshots: readonly ListingSnapshotPublic[],
  nowMs: number,
): PriceSeries[] {
  const observed = new Map<number, { t: number; price: number }[]>();
  for (const s of snapshots) {
    if (s.price_czk == null) continue;
    const point = { t: new Date(s.scraped_at).getTime(), price: s.price_czk };
    const points = observed.get(s.listing_id);
    if (points) points.push(point);
    else observed.set(s.listing_id, [point]);
  }
  const tracks = adverts.flatMap((advert) => {
    const points = [...(observed.get(advert.id) ?? [])].sort((a, b) => a.t - b.t);
    if (points.length === 0 && advert.price_czk != null) {
      points.push({ t: new Date(advert.first_seen_at).getTime(), price: advert.price_czk });
    }
    return points.length > 0 ? [{ advert, points }] : [];
  });
  const perPortal = new Map<string, number>();
  for (const { advert } of tracks) perPortal.set(advert.source, (perPortal.get(advert.source) ?? 0) + 1);
  return tracks.map(({ advert, points }) => {
    const portal = portalLabel(advert.source) ?? advert.source;
    const endT = advert.is_active ? nowMs : new Date(advert.last_seen_at).getTime();
    return {
      id: advert.id,
      label: (perPortal.get(advert.source) ?? 0) > 1
        ? `${portal} · ${fmtDateSlash(advert.first_seen_at)}`
        : portal,
      points,
      endT: Math.max(endT, points[points.length - 1].t),
    };
  });
}

/* -------------------------------------------------------------------------- */
/* Chart-ready shapes                                                         */
/* -------------------------------------------------------------------------- */

export const seriesValueKey = (id: number): string => `s${id}`;
/* Sibling flag per value key: true only where the track was actually observed,
 * so the chart can dot real observations instead of every merged row. */
export const seriesObservedKey = (id: number): string => `o${id}`;

export type PriceChartRow = Record<string, number | boolean | null>;

/* Every track merged onto one sorted time axis, each carrying its last known
 * price forward (the step) and NULL outside its own [start, endT] window, so a
 * stretch with no live advert is a gap in every line. Lives here rather than in
 * the chart component so the step semantics are unit-tested and the component
 * stays pure rendering.
 *
 * `sampleCount` resamples the step onto that many evenly spaced instants
 * across the domain, ON TOP OF every real observation (which stay exact, and
 * stay the only rows flagged `observed`). It exists for HOVER, not for
 * drawing: recharts picks the tooltip's row by nearest-midpoint over the rows
 * it is given, so with only a handful of real observations the readout snaps
 * to a price change weeks away from the cursor — hovering early July on a
 * 9.5.→30.7. series showed 30.7.'s price. Resampling makes "nearest row" and
 * "the value the line has under the cursor" the same thing. It cannot change
 * the rendered line: a stepAfter curve through held-forward values is the
 * same curve however finely it is sampled. */
export function buildChartRows(
  series: PriceSeries[],
  sampleCount = 0,
): PriceChartRow[] {
  const times = new Set<number>();
  for (const s of series) {
    for (const p of s.points) times.add(p.t);
    if (s.points.length) times.add(s.endT);
  }
  if (sampleCount > 1 && times.size > 1) {
    const known = [...times];
    const from = Math.min(...known);
    const to = Math.max(...known);
    for (let i = 0; i < sampleCount; i++) {
      times.add(Math.round(from + ((to - from) * i) / (sampleCount - 1)));
    }
  }
  const axis = [...times].sort((a, b) => a - b);
  const rows: PriceChartRow[] = axis.map((t) => ({ t }));
  // Track-major with a forward-only cursor into that track's points: both the
  // axis and each track's points are sorted, so the whole grid fills in one
  // linear pass instead of rescanning every point for every row (which the
  // resampling above would otherwise make quadratic).
  for (const s of series) {
    const vKey = seriesValueKey(s.id);
    const oKey = seriesObservedKey(s.id);
    let cursor = 0;
    for (let i = 0; i < axis.length; i++) {
      const t = axis[i];
      while (cursor + 1 < s.points.length && s.points[cursor + 1].t <= t) cursor++;
      if (!s.points.length || t < s.points[0].t || t > s.endT) {
        rows[i][vKey] = null;
        rows[i][oKey] = false;
        continue;
      }
      rows[i][vKey] = s.points[cursor].price;
      rows[i][oKey] = s.points[cursor].t === t;
    }
  }
  return rows;
}

/* MS8: the lowest asking price among the active adverts, when it differs from
 * the price the header shows (a header with no price differs from any price).
 * Display only: it feeds no per-m² figure, yield, alert or filter. `sources`
 * names every portal quoting that price. */
export function lowestActivePrice(
  adverts: ReadonlyArray<Pick<PriceAdvert, 'source' | 'is_active' | 'price_czk'>>,
  headerPrice: number | null,
): { price: number; sources: string[] } | null {
  const priced = adverts.filter((a) => a.is_active && a.price_czk != null);
  if (priced.length === 0) return null;
  const price = Math.min(...priced.map((a) => a.price_czk as number));
  if (price === headerPrice) return null;
  return {
    price,
    sources: [...new Set(priced.filter((a) => a.price_czk === price).map((a) => a.source))],
  };
}

export interface PriceChangeEvent {
  t: number;
  seriesId: number;
  /** The track's label. */
  label: string;
  from: number;
  to: number;
  pct: number;
}

/* The moments the asking price actually moved, newest first. Derived from the
 * same series the chart draws, so the chart and the event list can never
 * disagree. Changes are counted WITHIN a track: two portals quoting different
 * prices are not a price change. */
export function priceChangeEvents(series: PriceSeries[]): PriceChangeEvent[] {
  const events: PriceChangeEvent[] = [];
  for (const s of series) {
    for (let i = 1; i < s.points.length; i++) {
      const prev = s.points[i - 1];
      const cur = s.points[i];
      if (cur.price === prev.price) continue;
      events.push({
        t: cur.t,
        seriesId: s.id,
        label: s.label,
        from: prev.price,
        to: cur.price,
        pct: prev.price === 0 ? 0 : ((cur.price - prev.price) / prev.price) * 100,
      });
    }
  }
  return events.sort((a, b) => b.t - a.t);
}
