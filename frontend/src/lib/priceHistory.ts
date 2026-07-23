/* Pure helpers behind the listing-detail "Listing & price history" section:
 * turn a property's URL records + price snapshots into chart series and the
 * summary stats. Kept side-effect-free (now injected, never Date.now()) so the
 * transforms are unit-testable. */
import type { ListingSnapshotPublic, PropertySource, ListingPublic } from '@/lib/types';
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
  let changes = 0;
  for (let i = 1; i < priced.length; i++) {
    if (priced[i].price_czk !== priced[i - 1].price_czk) changes++;
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
