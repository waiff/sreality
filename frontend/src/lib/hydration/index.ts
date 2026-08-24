/* Shared card-hydration layer.
 *
 * The rule this module exists to enforce: a card surface issues ONE cohort read
 * for the structure it renders, and every decoration on top of that — cover
 * photo, broker line, anything later — arrives through here, keyed by id,
 * non-blocking, in its own cache namespace. No surface is ever gated on a
 * decoration again.
 *
 * First consumer is the deal-pipeline board. Browse cards and the estimation
 * comparables join in W7a, at which point the four hand-rolled image loaders in
 * lib/queries.ts collapse into this one. Anything new that renders property
 * cards should start here rather than adding a fifth. */

export { CardHydrationProvider, useCardHydration, makeHydration } from './CardHydration';
export type { CardHydration } from './CardHydration';
export { useListingCovers, useListingBrokers } from './useCardHydration';
export type { CoverByListingId, BrokerByListingId } from './useCardHydration';
export { hydrationKeys, idsKey, HYDRATION_NAMESPACE } from './keys';
