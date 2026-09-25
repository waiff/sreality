/* Pure helpers behind the property page's price history: the canonical
 * advert's own price snapshots as a chart series, the property's active windows,
 * and the dated price moves. Kept side-effect-free (now injected, never
 * Date.now()) so the transforms are unit-testable. */
import type {
  ListingSnapshotPublic,
  PropertyStatusEventPublic,
} from '@/lib/types';

/* The advert whose price the property shows (its canonical advert). A price step
 * never spans two adverts, so the history is this advert's own series. */
export interface PriceAdvert {
  id: number;
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

/* The advert's price snapshots as one step-line (held flat between changes),
 * extended to `nowMs` while it is live; none when it never had a price. */
export function buildPriceSeries(
  advert: PriceAdvert,
  snapshots: ListingSnapshotPublic[],
  nowMs: number,
): PriceSeries[] {
  const points = snapshots
    .filter((s) => s.listing_id === advert.id && s.price_czk != null)
    .map((s) => ({ t: new Date(s.scraped_at).getTime(), price: s.price_czk as number }))
    .sort((a, b) => a.t - b.t);
  if (points.length === 0 && advert.price_czk != null) {
    points.push({ t: new Date(advert.first_seen_at).getTime(), price: advert.price_czk });
  }
  if (points.length === 0) return [];
  const endT = advert.is_active ? nowMs : new Date(advert.last_seen_at).getTime();
  return [{ id: advert.id, label: 'Price', points, endT: Math.max(endT, points[points.length - 1].t) }];
}

/* Property-grain windows (ms) during which >=1 source was active, derived
 * from property_status_events (migration 392: a trigger-maintained log of
 * properties.is_active flips, reusing the SAME aggregate Browse/badges
 * already trust rather than re-deriving "any active source" from raw listing
 * data here). `fallback.end` is the caller's best current-truth close point
 * (now if the property reads active today, else its last-seen instant) —
 * used both when there are no events at all (nothing seeded/loaded yet) and
 * to close a trailing window the trigger hasn't stamped a deactivation for.
 * With no events this returns one window spanning the whole fallback range,
 * i.e. today's pre-gap-logic behavior exactly — a strict narrowing, never a
 * regression, once real events are present. A property is born active, so a
 * FIRST event that is a deactivation closes a window opened at `fallback.start`
 * (an unmerged property whose only row was the pre-559 merge's 'inactive'). */
export function buildActiveWindows(
  events: PropertyStatusEventPublic[],
  fallback: { start: number; end: number },
): [number, number][] {
  const sorted = [...events]
    .map((e) => ({ isActive: e.is_active, t: new Date(e.event_at).getTime() }))
    .sort((a, b) => a.t - b.t);
  if (sorted.length === 0) return [[fallback.start, fallback.end]];

  const windows: [number, number][] = [];
  let openAt: number | null = sorted[0].isActive ? null : fallback.start;
  for (const e of sorted) {
    if (e.isActive) {
      if (openAt == null) openAt = e.t;
    } else if (openAt != null) {
      windows.push([openAt, e.t]);
      openAt = null;
    }
  }
  if (openAt != null) windows.push([openAt, fallback.end]);
  return windows;
}

function withinWindows(t: number, windows: [number, number][]): boolean {
  return windows.some(([start, end]) => t >= start && t <= end);
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
 * price forward (the step) and NULL outside its own [start, endT] window OR
 * outside every property-level active window (activeWindows, from
 * buildActiveWindows) — a period the property had zero active listings gaps
 * the line for every track at once, not just the track that went inactive.
 * activeWindows is optional so existing callers (and this file's chart-row
 * tests) keep the pre-existing unconstrained behavior. Lives here rather than
 * in the chart component so the step semantics are unit-tested and the
 * component stays pure rendering.
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
  activeWindows?: [number, number][],
  sampleCount = 0,
): PriceChartRow[] {
  const times = new Set<number>();
  for (const s of series) {
    for (const p of s.points) times.add(p.t);
    if (s.points.length) times.add(s.endT);
  }
  if (activeWindows) {
    for (const [start, end] of activeWindows) {
      times.add(start);
      times.add(end);
    }
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
  const gapped = axis.map((t) => !!activeWindows && !withinWindows(t, activeWindows));
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
      if (!s.points.length || t < s.points[0].t || t > s.endT || gapped[i]) {
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
