/* The card decorations, as independent non-blocking reads.
 *
 * Per-ad reads are keyed on the SURROGATE `listing_id` (migration 343), never
 * `sreality_id`: a post-Gate-2 non-sreality ad has a NULL sreality_id and would
 * silently lose its thumbnail and its broker. The board's broker line is
 * property-grain (MS7) and keyed on the property.
 *
 * `placeholderData: keepPreviousData` is what makes a re-sort or a filter
 * change feel free — the previous cohort's decorations stay on screen while the
 * new set loads, instead of every card blinking back to a placeholder. */

import { keepPreviousData, useQueries, useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';

import {
  fetchListingBrokersByIds,
  propertyBrokers,
  type ListingBroker,
  type PropertyBrokers,
} from '@/lib/brokers';
import { type TaggedImageUrl } from '@/lib/imageTags';
import { imageSrc } from '@/lib/imageUrl';
import {
  fetchImagesForListingIds,
  fetchListingCovers,
  fetchPropertySourcesByPropertyIds,
} from '@/lib/queries';
import type { ImagePublic } from '@/lib/types';

import { hydrationKeys, type BrokerSubject } from './keys';

/* Decorations are worth re-reading far less often than the cards themselves: a
 * listing's cover photo and its attributed broker change on the scrape's
 * timescale, not the operator's. Five minutes keeps a board that is being
 * actively dragged from re-fetching them at all. */
const DECORATION_STALE_MS = 5 * 60_000;

export type CoverByListingId = ReadonlyMap<number, string>;
export type BrokerByListingId = ReadonlyMap<number, ListingBroker>;
export type BrokersByPropertyId = ReadonlyMap<number, PropertyBrokers>;
/* Raw rows, not URLs — deliberately lossless where `covers` is not. The
 * comparables modal calls imageSrc itself and its map preview wants the same
 * rows, so narrowing here would just push a second shape onto every consumer.
 * `taggedImageUrls` below is the Browse-card projection. */
export type PhotosByListingId = ReadonlyMap<number, ImagePublic[]>;
export type AdCountByPropertyId = ReadonlyMap<number, number>;

/* One cover image per listing, from listing_cover_public (W4) — a server-side
 * DISTINCT ON that returns exactly one row per listing instead of every
 * photo for the client to discard down to one. The board shows a single 48px
 * thumbnail per card, so the row count now equals what actually renders. */
export function useListingCovers(listingIds: readonly number[]): {
  covers: CoverByListingId;
  isPending: boolean;
} {
  const ids = useMemo(
    () => [...new Set(listingIds)].sort((a, b) => a - b),
    [listingIds],
  );
  const q = useQuery({
    queryKey: hydrationKeys.covers(ids),
    queryFn: async ({ signal }) => {
      const byListing = await fetchListingCovers(ids, { signal });
      const out = new Map<number, string>();
      for (const [listingId, image] of byListing) {
        out.set(listingId, imageSrc(image));
      }
      return out as CoverByListingId;
    },
    enabled: ids.length > 0,
    placeholderData: keepPreviousData,
    staleTime: DECORATION_STALE_MS,
  });
  return {
    covers: q.data ?? EMPTY_COVERS,
    isPending: ids.length > 0 && q.data === undefined,
  };
}

/* The broker behind each ad, contact included (migration 419), in ONE round trip:
 * the property page's broker list and every advert row read this one map. No
 * previous-data placeholder (another property's map would read as "unattributed")
 * and no swallow: a failed read is `isError`, which the page says out loud. */
export function useListingBrokers(listingIds: readonly number[]): {
  brokers: BrokerByListingId;
  isPending: boolean;
  isError: boolean;
  refetch: () => void;
} {
  const ids = useMemo(
    () => [...new Set(listingIds)].sort((a, b) => a - b),
    [listingIds],
  );
  const q = useQuery({
    queryKey: hydrationKeys.brokers(ids),
    queryFn: () => fetchListingBrokersByIds(ids) as Promise<BrokerByListingId>,
    enabled: ids.length > 0,
    staleTime: DECORATION_STALE_MS,
  });
  return {
    brokers: q.data ?? EMPTY_BROKERS,
    isPending: ids.length > 0 && q.data === undefined && !q.isError,
    isError: q.isError && q.data === undefined,
    refetch: () => void q.refetch(),
  };
}

/* The board's broker line (MS7): each card's property's list, by the property
 * page's `propertyBrokers` rule, from two batched reads (the cards' ads, then
 * their brokers), never one per card. A failure costs the cards that line only. */
export function usePropertyBrokers(subjects: readonly BrokerSubject[]): {
  brokers: BrokersByPropertyId;
  isPending: boolean;
} {
  const q = useQuery({
    queryKey: hydrationKeys.propertyBrokers(subjects),
    queryFn: async ({ signal }) => {
      const ads = await fetchPropertySourcesByPropertyIds(
        subjects.map((s) => s.property_id),
        { signal },
      );
      const byListing = await fetchListingBrokersByIds([...ads.values()].flat().map((a) => a.id));
      const out = new Map<number, PropertyBrokers>();
      for (const s of subjects) {
        const list = propertyBrokers(ads.get(s.property_id) ?? [], byListing, s.listing_id);
        if (list.brokers.length > 0) out.set(s.property_id, list);
      }
      return out as BrokersByPropertyId;
    },
    enabled: subjects.length > 0,
    placeholderData: keepPreviousData,
    staleTime: DECORATION_STALE_MS,
  });
  return {
    brokers: q.data ?? EMPTY_PROPERTY_BROKERS,
    isPending: subjects.length > 0 && q.data === undefined,
  };
}

/* SEVERAL photos per listing — the Browse card carousel and the comparables
 * modal, as distinct from useListingCovers' one-thumbnail-per-card (W4).
 *
 * W7a. This is the read Browse used to make INSIDE `fetchListingsForCards`'
 * queryFn: 24 cards' photos were awaited before a single card could paint, and
 * measured live on 24 real ids that await is 178 image rows, 178 correlated
 * CLIP-tag lookups, 750 buffers and ~131 ms of server work sitting directly on
 * the paint path. Nothing about it is wasteful — the carousel genuinely renders
 * those rows, which is exactly why the fix is to move it OFF the paint path
 * rather than to shrink it to one cover. Cards paint from browse_list alone;
 * photos arrive here.
 *
 * `perId` is a client-side retention cap, not a server LIMIT — images_public has
 * no per-listing LIMIT, so the server returns every row either way and this
 * decides how many are kept. It is in the cache key for that reason (see
 * keys.ts): the same cohort at 6 and at 50 are different payloads.
 *
 * Pass `perId: null` to hold the hook without fetching — the Pipeline board
 * renders one cover and must not start pulling whole carousels just because it
 * mounts the shared provider.
 *
 * ONE QUERY PER PAGE-SIZED BUCKET, in ARRIVAL order — not one query over the
 * whole cohort, and this is the part worth reading twice. Browse is an infinite
 * list: `rows` accumulates every page loaded so far, so a single cumulative
 * cohort key changes on every append and refetches all the earlier pages'
 * photos with it. Total rows read across n pages would be O(n²) — at page 5 that
 * is ~900 image rows re-read to learn about the 178 that are new. Bucketing in
 * arrival order makes each page's key STABLE once its page has landed: appending
 * page 2 adds exactly one bucket query and leaves bucket 1's cache entry alone,
 * so the cost is O(n) again and matches what the old per-page read cost — with
 * the blocking removed.
 *
 * Arrival order, NOT sorted, is load-bearing: sorting the whole cohort first
 * would reshuffle every bucket boundary on each append and defeat the entire
 * point. `idsKey` still sorts WITHIN a bucket, so re-rendering one page's cards
 * in a different order is still a cache hit.
 *
 * `combine` is what keeps the merged map referentially stable: useQueries hands
 * back a fresh results array on every render, so merging outside it would mint a
 * new Map — and a new context value, and a re-projection in every card — on
 * every keystroke elsewhere in the app. */
const PHOTO_BUCKET_SIZE = 24;

export function useListingPhotos(
  listingIds: readonly number[],
  perId: number | null,
): { photos: PhotosByListingId; isPending: boolean } {
  const buckets = useMemo(() => photoBuckets(listingIds), [listingIds]);
  const enabled = buckets.length > 0 && perId != null;

  return useQueries({
    queries: buckets.map((ids) => ({
      queryKey: hydrationKeys.photos(ids, perId ?? 0),
      queryFn: async ({ signal }: { signal: AbortSignal }) =>
        (await fetchImagesForListingIds(ids, perId as number, { signal })) as PhotosByListingId,
      enabled,
      placeholderData: keepPreviousData,
      staleTime: DECORATION_STALE_MS,
    })),
    combine: (results) => {
      if (!enabled) return { photos: EMPTY_PHOTOS, isPending: false };
      const merged = new Map<number, ImagePublic[]>();
      for (const r of results) {
        if (!r.data) continue;
        for (const [listingId, images] of r.data) merged.set(listingId, images);
      }
      return {
        photos: merged as PhotosByListingId,
        /* Pending only until the FIRST bucket answers. A later page still
         * loading must not make the cards already on screen think their photos
         * are in flight — they are not, they are rendered. */
        isPending: results[0]?.data === undefined,
      };
    },
  });
}

/* De-duplicate, preserving first-seen order, then slice into page-sized
 * buckets. Exported for the test that pins the append-stability above. */
export function photoBuckets(listingIds: readonly number[]): number[][] {
  const seen = new Set<number>();
  const flat: number[] = [];
  for (const id of listingIds) {
    if (!seen.has(id)) {
      seen.add(id);
      flat.push(id);
    }
  }
  const out: number[][] = [];
  for (let i = 0; i < flat.length; i += PHOTO_BUCKET_SIZE) {
    out.push(flat.slice(i, i + PHOTO_BUCKET_SIZE));
  }
  return out;
}

/* How many ads each card's property holds: the Browse card's "N inzeráty".
 * browse_projection carries no `source_count`, so the count is a decoration and
 * not a card column. It counts property_sources_public, every ad active or not,
 * so the badge says the number the property page's advert list says. Bucketed
 * like the photos, so appending a page re-reads only the new page. */
export function usePropertyAdCounts(propertyIds: readonly number[]): AdCountByPropertyId {
  const buckets = useMemo(() => photoBuckets(propertyIds), [propertyIds]);

  return useQueries({
    queries: buckets.map((ids) => ({
      queryKey: hydrationKeys.adCounts(ids),
      queryFn: async ({ signal }: { signal: AbortSignal }) => {
        const ads = await fetchPropertySourcesByPropertyIds(ids, { signal });
        return new Map([...ads].map(([id, rows]) => [id, rows.length])) as AdCountByPropertyId;
      },
      placeholderData: keepPreviousData,
      staleTime: DECORATION_STALE_MS,
    })),
    combine: mergeAdCounts,
  });
}

/* Module-level, so useQueries re-runs it only when a bucket's result changes. */
function mergeAdCounts(
  results: ReadonlyArray<{ data?: AdCountByPropertyId }>,
): AdCountByPropertyId {
  if (results.length === 0) return EMPTY_AD_COUNTS;
  const merged = new Map<number, number>();
  for (const r of results) {
    if (r.data) for (const [id, n] of r.data) merged.set(id, n);
  }
  return merged;
}

/* The Browse carousel's projection, in one place instead of inline in the read
 * it used to ride along with. Kept a pure function so the hook can stay lossless
 * (raw ImagePublic rows, which the comparables modal and map both consume) while
 * the card surface still gets exactly the shape ImageCarousel takes. */
export function taggedImageUrls(
  images: readonly ImagePublic[],
): TaggedImageUrl[] {
  return images.map((im) => ({
    url: imageSrc(im),
    tag: im.clip_fine_tag,
    confidence: im.clip_confidence,
    renderScore: im.clip_render_score,
  }));
}

const EMPTY_COVERS: CoverByListingId = new Map();
const EMPTY_BROKERS: BrokerByListingId = new Map();
const EMPTY_PROPERTY_BROKERS: BrokersByPropertyId = new Map();
const EMPTY_PHOTOS: PhotosByListingId = new Map();
const EMPTY_AD_COUNTS: AdCountByPropertyId = new Map();
export const NO_PHOTOS: readonly ImagePublic[] = [];
