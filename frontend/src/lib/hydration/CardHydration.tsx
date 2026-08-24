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

import type { PipelineCardBroker } from '@/lib/types';

import {
  useListingBrokers,
  useListingCovers,
  type BrokerByListingId,
  type CoverByListingId,
} from './useCardHydration';

export interface CardHydration {
  /* The cover image URL for a listing, or null when there is none / not yet. */
  coverFor: (listingId: number | null | undefined) => string | null;
  /* The canonical broker for a listing, or null when there is none / not yet. */
  brokerFor: (listingId: number | null | undefined) => PipelineCardBroker | null;
  /* True while the decoration is still in flight, so a card can reserve its
   * space instead of reflowing when the value lands. Distinct from "resolved to
   * nothing", which is a real answer and must not render as a skeleton. */
  coversPending: boolean;
  brokersPending: boolean;
}

const EMPTY: CardHydration = {
  coverFor: () => null,
  brokerFor: () => null,
  coversPending: false,
  brokersPending: false,
};

const Ctx = createContext<CardHydration>(EMPTY);

export const useCardHydration = (): CardHydration => useContext(Ctx);

/* Fetches the decorations for one cohort of listing ids and serves them to
 * every card beneath it. The ids are the caller's business: the board passes
 * the representative listing id of every card currently on it. */
export function CardHydrationProvider({
  listingIds,
  children,
}: {
  listingIds: readonly number[];
  children: ReactNode;
}) {
  const { covers, isPending: coversPending } = useListingCovers(listingIds);
  const { brokers, isPending: brokersPending } = useListingBrokers(listingIds);

  const value = useMemo<CardHydration>(
    () => makeHydration(covers, brokers, coversPending, brokersPending),
    [covers, brokers, coversPending, brokersPending],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/* Pure projection of the two maps into the context shape — exported so the
 * lookup semantics (null listing id, missing entry) are unit-testable without
 * mounting React Query. */
export function makeHydration(
  covers: CoverByListingId,
  brokers: BrokerByListingId,
  coversPending = false,
  brokersPending = false,
): CardHydration {
  return {
    coverFor: (id) => (id == null ? null : covers.get(id) ?? null),
    brokerFor: (id) => (id == null ? null : brokers.get(id) ?? null),
    coversPending,
    brokersPending,
  };
}
