/* One advert in the shape every review surface renders (`AutodedupMember`),
 * built from its `listings_public` row and its hydrated photos — for the pages
 * whose payload names adverts but ships no card (the proposed splits, the
 * rulings). `fallback` carries what the payload itself knows, so a card renders
 * its portal before the facts read lands. */

import type { AutodedupMember } from '@/lib/api';
import type { ImagePublic, ListingPublic } from '@/lib/types';

/* The frames each card pages, as the review queues ship them. */
export const PHOTOS_PER_ADVERT = 12;

export function memberFromListing(
  listingId: number,
  fallback: { source: string | null; is_active: boolean | null },
  l: ListingPublic | undefined,
  images: ImagePublic[],
): AutodedupMember {
  return {
    listing_id: listingId,
    source: l?.source ?? fallback.source ?? '—',
    is_active: l?.is_active ?? fallback.is_active,
    source_url: l?.source_url ?? null,
    source_id_native: l?.source_id_native ?? null,
    sreality_id: l?.sreality_id ?? null,
    category_main: l?.category_main ?? null,
    category_type: l?.category_type ?? null,
    disposition: l?.disposition ?? null,
    area_m2: l?.area_m2 ?? null,
    floor: l?.floor ?? null,
    total_floors: l?.total_floors ?? null,
    price_czk: l?.price_czk ?? null,
    first_seen_at: l?.first_seen_at ?? null,
    last_seen_at: l?.last_seen_at ?? null,
    cover: images[0] ?? null,
    n_images: images.length,
    images: images.slice(0, PHOTOS_PER_ADVERT),
  };
}
