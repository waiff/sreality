/* Decorations reach cards through context, never props.
 *
 * The board renders each card's face TWICE — once in its column, once inside
 * the drag overlay (pages/Pipeline.tsx) — from the same `CardFace`. Threading
 * `cover` and `broker` as props means passing them down two separate paths and
 * keeping them in step by hand; the moment one call site is updated and the
 * other is not, a dragged card silently loses its thumbnail or its broker line
 * while the identical card in the column keeps them. Context removes that whole
 * class of drift: `CardFace` asks for what it needs, wherever it is mounted.
 *
 * The default value is deliberately a working empty provider rather than a
 * throw. A card rendered outside a provider (a test, a future surface that has
 * not adopted the layer yet) shows no decorations and works fine — decorations
 * are decorations. */

import { createContext, useContext, useMemo, type ReactNode } from 'react';

import type { ImagePublic, PipelineCardBroker } from '@/lib/types';

import {
  NO_PHOTOS,
  useListingBrokers,
  useListingCovers,
  useListingPhotos,
  type BrokerByListingId,
  type CoverByListingId,
  type PhotosByListingId,
} from './useCardHydration';

export interface CardHydration {
  /* The cover image URL for a listing, or null when there is none / not yet. */
  coverFor: (listingId: number | null | undefined) => string | null;
  /* The canonical broker for a listing, or null when there is none / not yet. */
  brokerFor: (listingId: number | null | undefined) => PipelineCardBroker | null;
  /* Every photo a listing's carousel renders (W7a) — as opposed to coverFor's
   * single thumbnail. Always an array, never null: a card with no photos and a
   * card whose photos have not landed both render the carousel's own empty
   * state, and `photosPending` is what tells them apart. Returns a stable empty
   * singleton so a consumer can memoize on identity. */
  photosFor: (listingId: number | null | undefined) => readonly ImagePublic[];
  /* True while the decoration is still in flight, so a card can reserve its
   * space instead of reflowing when the value lands. Distinct from "resolved to
   * nothing", which is a real answer and must not render as a skeleton. */
  coversPending: boolean;
  brokersPending: boolean;
  photosPending: boolean;
}

const EMPTY: CardHydration = {
  coverFor: () => null,
  brokerFor: () => null,
  photosFor: () => NO_PHOTOS,
  coversPending: false,
  brokersPending: false,
  photosPending: false,
};

const Ctx = createContext<CardHydration>(EMPTY);

export const useCardHydration = (): CardHydration => useContext(Ctx);

/* What a surface actually renders. EVERY decoration is opt-in, and that is the
 * north star written as a prop signature: "every surface pays only for what it
 * renders". Mounting the shared provider must never sign a surface up for a read
 * it has no use for.
 *
 * This was learned the expensive way. W7a's first cut made only `photos` opt-in
 * and left covers + brokers always-on, reasoning about the direction the board
 * cared about and not the one Browse did — so Browse mounted the provider and
 * silently began fetching a cover per card and a broker per card that nothing on
 * the page displays. Caught on the live post-deploy smoke run: /browse 22 -> 24
 * requests, the two extra being `listing_cover_public` and
 * `POST /brokers/by-listings`. Asymmetric defaults are how that happens; there
 * are none now. */
export interface CardDecorations {
  /* One thumbnail per card, via listing_cover_public's server-side DISTINCT ON. */
  covers?: boolean;
  /* The canonical broker line + its contact pair. */
  brokers?: boolean;
  /* Several photos per card for a carousel — the value is the client-side
   * retention cap (perId), which is part of the cache key. Omitted = off. */
  photos?: number;
}

/* Fetches the decorations for one cohort of listing ids and serves them to
 * every card beneath it. The ids are the caller's business: the board passes
 * the representative listing id of every card currently on it.
 *
 * An unwanted decoration is switched off by handing its hook an EMPTY id list,
 * which its existing `ids.length > 0` gate already turns into "no query" — no
 * second enabled flag to keep in step, and `isPending` correctly reads false
 * rather than "forever loading". */
export function CardHydrationProvider({
  listingIds,
  renders,
  children,
}: {
  listingIds: readonly number[];
  renders: CardDecorations;
  children: ReactNode;
}) {
  const wanted = (on: boolean | undefined) => (on ? listingIds : NO_IDS);
  const { covers, isPending: coversPending } = useListingCovers(
    wanted(renders.covers),
  );
  const { brokers, isPending: brokersPending } = useListingBrokers(
    wanted(renders.brokers),
  );
  const { photos, isPending: photosPending } = useListingPhotos(
    listingIds,
    renders.photos ?? null,
  );

  const value = useMemo<CardHydration>(
    () =>
      makeHydration(covers, brokers, photos, {
        coversPending,
        brokersPending,
        photosPending,
      }),
    [covers, brokers, photos, coversPending, brokersPending, photosPending],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/* Pure projection of the decoration maps into the context shape — exported so
 * the lookup semantics (null listing id, missing entry) are unit-testable
 * without mounting React Query.
 *
 * The pending flags moved into one object when photos made them three: a third
 * positional boolean is exactly the kind of argument that gets passed in the
 * wrong order and silently makes a surface claim it is loading forever. */
export function makeHydration(
  covers: CoverByListingId,
  brokers: BrokerByListingId,
  photos: PhotosByListingId = EMPTY_PHOTOS_MAP,
  pending: {
    coversPending?: boolean;
    brokersPending?: boolean;
    photosPending?: boolean;
  } = {},
): CardHydration {
  return {
    coverFor: (id) => (id == null ? null : covers.get(id) ?? null),
    brokerFor: (id) => (id == null ? null : brokers.get(id) ?? null),
    photosFor: (id) => (id == null ? NO_PHOTOS : photos.get(id) ?? NO_PHOTOS),
    coversPending: pending.coversPending ?? false,
    brokersPending: pending.brokersPending ?? false,
    photosPending: pending.photosPending ?? false,
  };
}

const EMPTY_PHOTOS_MAP: PhotosByListingId = new Map();
/* A stable identity, so switching a decoration off doesn't hand its hook a fresh
 * array every render and re-run its memo. */
const NO_IDS: readonly number[] = [];
