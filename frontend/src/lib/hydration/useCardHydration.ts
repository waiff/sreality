/* The two card decorations, as independent non-blocking reads.
 *
 * Both are keyed on the SURROGATE `listing_id` (migration 343), never
 * `sreality_id`: a post-Gate-2 non-sreality representative has a NULL
 * sreality_id and would silently lose its thumbnail and its broker line.
 *
 * `placeholderData: keepPreviousData` is what makes a re-sort or a filter
 * change feel free — the previous cohort's decorations stay on screen while the
 * new set loads, instead of every card blinking back to a placeholder. */

import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';

import { fetchBrokersByIds, fetchListingBrokersByIds } from '@/lib/brokers';
import { imageSrc } from '@/lib/imageUrl';
import { fetchListingCovers, pipelineCardBroker } from '@/lib/queries';
import type { PipelineCardBroker } from '@/lib/types';

import { hydrationKeys } from './keys';

/* Decorations are worth re-reading far less often than the cards themselves: a
 * listing's cover photo and its attributed broker change on the scrape's
 * timescale, not the operator's. Five minutes keeps a board that is being
 * actively dragged from re-fetching them at all. */
const DECORATION_STALE_MS = 5 * 60_000;

export type CoverByListingId = ReadonlyMap<number, string>;
export type BrokerByListingId = ReadonlyMap<number, PipelineCardBroker>;

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

/* The canonical broker per listing, plus its contact fields for the hover box.
 *
 * Still two round trips (listing -> broker, then broker -> contact): the
 * contact columns are not on `listing_broker_public`, and adding them is W6's
 * one-migration job, which then deletes the second call here and on the listing
 * page at once. Both are off the paint path now, so the cost is invisible.
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
      const contacts = await fetchBrokersByIds([
        ...new Set([...listingBrokers.values()].map((b) => b.broker_id)),
      ]);
      const out = new Map<number, PipelineCardBroker>();
      for (const [listingId, lb] of listingBrokers) {
        const projected = pipelineCardBroker(lb, contacts.get(lb.broker_id));
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

const EMPTY_COVERS: CoverByListingId = new Map();
const EMPTY_BROKERS: BrokerByListingId = new Map();
