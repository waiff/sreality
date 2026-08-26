/* Shared card-hydration layer.
 *
 * The rule this module exists to enforce: a card surface issues ONE cohort read
 * for the structure it renders, and every decoration on top of that — cover
 * photo, broker line, anything later — arrives through here, keyed by id,
 * non-blocking, in its own cache namespace. No surface is ever gated on a
 * decoration again.
 *
 * First consumer was the deal-pipeline board; Browse cards and the estimation
 * comparables joined in W7a. Anything new that renders property cards should
 * start here rather than hand-rolling another loader.
 *
 * The image lane after W7a — three fetchers, deliberately not one, and the shape
 * of each is load-bearing:
 *   - `useListingPhotos` (images_public, keyed on the surrogate listing_id) is
 *     THE multi-image read. Browse cards, the comparables modal and its map
 *     preview, and the listing-detail gallery all reach images through it.
 *   - `useListingCovers` (listing_cover_public, W4) stays separate because it is
 *     a different QUERY, not a different caller: a server-side DISTINCT ON that
 *     returns one row per listing. The board renders one 48px thumbnail, and
 *     asking the multi-image read for `perId: 1` is exactly the fetch-everything-
 *     then-discard W4 measured at 901 rows and 3,995 buffers for 44 cards.
 *   - `fetchImagesByListingIds` (keyed on sreality_id) survives in lib/queries
 *     for the callers whose upstream read model carries no surrogate id — the
 *     frozen pre-#879 estimation runs.
 *     Flipping it in place would be a silent half-swap: the id spaces overlap,
 *     so a sreality_id fed into an `IN listing_id` matches a DIFFERENT listing.
 *     Moving those needs a backend change to their payloads, not a rename. */

export { CardHydrationProvider, useCardHydration, makeHydration } from './CardHydration';
export type { CardHydration } from './CardHydration';
export {
  useListingCovers,
  useListingBrokers,
  useListingPhotos,
  taggedImageUrls,
  NO_PHOTOS,
} from './useCardHydration';
export type {
  CoverByListingId,
  BrokerByListingId,
  PhotosByListingId,
} from './useCardHydration';
export { hydrationKeys, idsKey, HYDRATION_NAMESPACE } from './keys';
