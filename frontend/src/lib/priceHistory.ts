/* Pure helpers behind the listing-detail "Listing & price history" section:
 * turn a property's URL records + price snapshots into chart series and the
 * summary stats. Kept side-effect-free (now injected, never Date.now()) so the
 * transforms are unit-testable. */
import type {
  ListingSnapshotPublic,
  PropertySource,
  ListingPublic,
  PropertyStatusEventPublic,
} from '@/lib/types';
import { portalListingUrl } from '@/lib/portals';

const DAY_MS = 86_400_000;

/* One place the property has been seen = one URL record. Re-listings on the
 * same portal with a fresh URL are separate `listings` rows → separate rows. */
export interface UrlRow {
  // The SURROGATE listing id (property_sources_public.id / the viewed
  // listing's own id), never sreality_id — a post-Gate-2 non-sreality source
  // has a NULL sreality_id, and every such row would collide onto the same
  // key (`null === null`) instead of getting its own price track.
  id: number;
  source: string;
  url: string | null;
  isActive: boolean;
  price: number | null;
  firstSeen: string;
  lastSeen: string;
}

export interface PriceSeries {
  id: number;
  label: string;
  points: { t: number; price: number }[];
  endT: number;
}

export interface PriceHistoryStats {
  changes: number;
  pct: number | null;
  firstSeenT: number;
  lastSeenT: number;
  anyActive: boolean;
  days: number;
}

function capitalise(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/* Property's URL records, newest-seen first. Falls back to a single
 * synthesized row for the rare listing with no property_sources entry. */
export function listingUrlRows(
  sources: PropertySource[],
  listing: ListingPublic,
): UrlRow[] {
  // sreality stores no source_url; reconstruct it from the property's category
  // triple (shared across its sources) so the per-source link resolves instead
  // of pointing nowhere. Other portals keep their stored source_url.
  const srealityCategory = {
    categoryType: listing.category_type,
    categoryMain: listing.category_main,
    categorySubCb: listing.category_sub_cb,
  };
  if (sources.length > 0) {
    return [...sources]
      .sort(
        (a, b) =>
          new Date(b.last_seen_at).getTime() - new Date(a.last_seen_at).getTime(),
      )
      .map((s) => ({
        // s.id is the surrogate (property_sources_public.id) — NEVER null on
        // a real row (only optional in the type for ClipAudit's synthetic
        // fallback). s.sreality_id still drives the sreality URL below since
        // that's a portal-native id, not an internal identity key.
        id: s.id as number,
        source: s.source,
        url: portalListingUrl(s.source, s.source_url, s.sreality_id, srealityCategory),
        isActive: s.is_active,
        price: s.price_czk,
        firstSeen: s.first_seen_at,
        lastSeen: s.last_seen_at,
      }));
  }
  return [
    {
      id: listing.id,
      source: listing.source ?? 'sreality',
      url: portalListingUrl(
        listing.source ?? 'sreality',
        null,
        listing.sreality_id,
        srealityCategory,
      ),
      isActive: listing.is_active,
      price: listing.price_czk,
      firstSeen: listing.first_seen_at,
      lastSeen: listing.last_seen_at,
    },
  ];
}

/* One step-line per URL: its price snapshots (held flat between changes),
 * extended to `nowMs` while the URL is live. */
export function buildPriceSeries(
  urls: UrlRow[],
  snapshots: ListingSnapshotPublic[],
  nowMs: number,
): PriceSeries[] {
  const byId = new Map<number, { t: number; price: number }[]>();
  for (const s of snapshots) {
    if (s.price_czk == null) continue;
    // Grouped on the surrogate listing_id, not sreality_id: a post-Gate-2
    // non-sreality source's snapshots all carry NULL sreality_id and would
    // otherwise collapse onto one shared (wrong) track.
    const arr = byId.get(s.listing_id) ?? [];
    arr.push({ t: new Date(s.scraped_at).getTime(), price: s.price_czk });
    byId.set(s.listing_id, arr);
  }
  const out: PriceSeries[] = [];
  for (const u of urls) {
    const pts = (byId.get(u.id) ?? []).sort((a, b) => a.t - b.t);
    if (pts.length === 0 && u.price != null) {
      pts.push({ t: new Date(u.firstSeen).getTime(), price: u.price });
    }
    if (pts.length === 0) continue;
    const endT = u.isActive ? nowMs : new Date(u.lastSeen).getTime();
    out.push({
      id: u.id,
      label: urls.length > 1 ? capitalise(u.source) : 'Price',
      points: pts,
      endT: Math.max(endT, pts[pts.length - 1].t),
    });
  }
  return out;
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
 * regression, once real events are present. */
export function buildActiveWindows(
  events: PropertyStatusEventPublic[],
  fallback: { start: number; end: number },
): [number, number][] {
  const sorted = [...events]
    .map((e) => ({ isActive: e.is_active, t: new Date(e.event_at).getTime() }))
    .sort((a, b) => a.t - b.t);
  if (sorted.length === 0) return [[fallback.start, fallback.end]];

  const windows: [number, number][] = [];
  let openAt: number | null = null;
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
 * component stays pure rendering. */
export function buildChartRows(
  series: PriceSeries[],
  activeWindows?: [number, number][],
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
  return [...times]
    .sort((a, b) => a - b)
    .map((t) => {
      const row: PriceChartRow = { t };
      const gapped = !!activeWindows && !withinWindows(t, activeWindows);
      for (const s of series) {
        const vKey = seriesValueKey(s.id);
        const oKey = seriesObservedKey(s.id);
        if (!s.points.length || t < s.points[0].t || t > s.endT || gapped) {
          row[vKey] = null;
          row[oKey] = false;
          continue;
        }
        let v = s.points[0].price;
        let observed = false;
        for (const p of s.points) {
          if (p.t > t) break;
          v = p.price;
          if (p.t === t) observed = true;
        }
        row[vKey] = v;
        row[oKey] = observed;
      }
      return row;
    });
}

export interface PriceChangeEvent {
  t: number;
  seriesId: number;
  /** Track label — only distinguishing when the property has several URLs. */
  label: string;
  from: number;
  to: number;
  pct: number;
}

/* The moments the asking price actually moved, newest first. Derived from the
 * same series the chart draws, so the chart, the event list, and the "price
 * changes" stat can never disagree. Changes are counted WITHIN a track: two
 * portals quoting different prices are not a price change. */
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

/* Summary across every snapshot of the property, chronologically. */
export function summarizePriceHistory(
  urls: UrlRow[],
  snapshots: ListingSnapshotPublic[],
  currentPrice: number | null,
  nowMs: number,
): PriceHistoryStats {
  const priced = [...snapshots]
    .filter((s) => s.price_czk != null)
    .sort(
      (a, b) =>
        new Date(a.scraped_at).getTime() - new Date(b.scraped_at).getTime(),
    );
  // Count moves WITHIN each URL's own track. Counting them over the merged
  // chronology would read two portals quoting different prices as a price
  // change on every alternating snapshot.
  const byTrack = new Map<number, (number | null)[]>();
  for (const s of priced) {
    const arr = byTrack.get(s.listing_id) ?? [];
    arr.push(s.price_czk);
    byTrack.set(s.listing_id, arr);
  }
  let changes = 0;
  for (const prices of byTrack.values()) {
    for (let i = 1; i < prices.length; i++) {
      if (prices[i] !== prices[i - 1]) changes++;
    }
  }
  const firstPrice = priced.length ? priced[0].price_czk : currentPrice;
  const lastPrice = priced.length ? priced[priced.length - 1].price_czk : currentPrice;
  const pct =
    firstPrice != null && lastPrice != null && firstPrice !== 0
      ? ((lastPrice - firstPrice) / firstPrice) * 100
      : null;

  const firstSeenT = Math.min(...urls.map((u) => new Date(u.firstSeen).getTime()));
  const anyActive = urls.some((u) => u.isActive);
  const lastSeenT = Math.max(...urls.map((u) => new Date(u.lastSeen).getTime()));
  const days = Math.max(
    0,
    Math.floor(((anyActive ? nowMs : lastSeenT) - firstSeenT) / DAY_MS),
  );
  return { changes, pct, firstSeenT, lastSeenT, anyActive, days };
}
