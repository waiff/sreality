/* The two card decorations, as independent non-blocking reads.
 *
 * Both are keyed on the SURROGATE `listing_id` (migration 343), never
 * `sreality_id`: a post-Gate-2 non-sreality representative has a NULL
 * sreality_id and would silently lose its thumbnail and its broker line.
 *
 * `placeholderData: keepPreviousData` is what makes a re-sort or a filter
 * change feel free — the previous cohort's decorations stay on screen while the
 * new set loads, instead of every card blinking back to a placeholder. */

import { keepPreviousData, useQueries, useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';

import { fetchListingBrokersByIds } from '@/lib/brokers';
import { type TaggedImageUrl } from '@/lib/imageTags';
import { imageSrc } from '@/lib/imageUrl';
import {
  fetchImagesForListingIds,
  fetchListingCovers,
  pipelineCardBroker,
} from '@/lib/queries';
import type { ImagePublic, PipelineCardBroker } from '@/lib/types';

import { hydrationKeys } from './keys';

/* Decorations are worth re-reading far less often than the cards themselves: a
 * listing's cover photo and its attributed broker change on the scrape's
 * timescale, not the operator's. Five minutes keeps a board that is being
 * actively dragged from re-fetching them at all. */
const DECORATION_STALE_MS = 5 * 60_000;

export type CoverByListingId = ReadonlyMap<number, string>;
export type BrokerByListingId = ReadonlyMap<number, PipelineCardBroker>;
/* Raw rows, not URLs — deliberately lossless where `covers` is not. The
 * comparables modal calls imageSrc itself and its map preview wants the same
 * rows, so narrowing here would just push a second shape onto every consumer.
 * `taggedImageUrls` below is the Browse-card projection. */
export type PhotosByListingId = ReadonlyMap<number, ImagePublic[]>;

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
    queryFn: async () => {
      const byListing = await fetchListingCovers(ids);
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

/* The canonical broker per listing, contact fields included — ONE round trip.
 *
 * W6 (migration 419) put primary_email / primary_phone on listing_broker_public,
 * the view /brokers/by-listings already reads, so the chained /brokers?ids= call
 * that used to follow it is gone. It never bought anything: the contact pair sits
 * on the same `brokers` row this view already joins, so the second statement
 * re-read heap pages the first had in hand (measured: 207 execution + 436
 * planning buffers, all duplicate) and paid a second Railway round trip's
 * ~270-410 ms floor to do it — serialized, because its broker_ids came out of the
 * first response.
 *
 * No `.catch(() => new Map())` swallow. The old inline version had to muffle
 * errors because a broker failure would have taken the whole board's queryFn
 * down with it; as its own query it fails alone, the cards keep their broker
 * line blank, and the error stays visible to React Query (and to the global
 * toast) instead of being silently converted into "this listing has no
 * broker". Structural isolation replaces a hand-written swallow. */
export function useListingBrokers(listingIds: readonly number[]): {
  brokers: BrokerByListingId;
  isPending: boolean;
} {
  const ids = useMemo(
    () => [...new Set(listingIds)].sort((a, b) => a - b),
    [listingIds],
  );
  const q = useQuery({
    queryKey: hydrationKeys.brokers(ids),
    queryFn: async () => {
      const listingBrokers = await fetchListingBrokersByIds(ids);
      const out = new Map<number, PipelineCardBroker>();
      for (const [listingId, lb] of listingBrokers) {
        const projected = pipelineCardBroker(lb);
        if (projected) out.set(listingId, projected);
      }
      return out as BrokerByListingId;
    },
    enabled: ids.length > 0,
    placeholderData: keepPreviousData,
    staleTime: DECORATION_STALE_MS,
  });
  return {
    brokers: q.data ?? EMPTY_BROKERS,
    isPending: ids.length > 0 && q.data === undefined,
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
      queryFn: async () =>
        (await fetchImagesForListingIds(ids, perId as number)) as PhotosByListingId,
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
const EMPTY_PHOTOS: PhotosByListingId = new Map();
export const NO_PHOTOS: readonly ImagePublic[] = [];
