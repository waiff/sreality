/* AUTODEDUP · the cards of the ads a split page names: one facts read and one
 * photo read for the whole page, each ad as `AutodedupMember`. The payload
 * names ads and ships no card; until the reads land a card shows what the
 * payload knows (its portal, whether it is active). */

import { useQuery } from '@tanstack/react-query';

import type { AutodedupMember } from '@/lib/api';
import { useListingPhotos } from '@/lib/hydration/useCardHydration';
import { mergedAdvertsKeys } from '@/lib/mergedAdverts';
import { fetchListingsForListingIds } from '@/lib/queries';
import type { ListingPublic } from '@/lib/types';
import { PHOTOS_PER_ADVERT, memberFromListing } from '@/components/autodedup/memberFromListing';

type NamedAdvert = { listing_id: number; source: string | null; is_active: boolean | null };

export function useAdvertMembers(ids: readonly number[]): {
  member: (a: NamedAdvert) => AutodedupMember;
  error: Error | null;
} {
  const details = useQuery<Map<number, ListingPublic>, Error>({
    queryKey: mergedAdvertsKeys.listings(ids),
    queryFn: ({ signal }) => fetchListingsForListingIds(ids, { signal }),
    enabled: ids.length > 0,
    staleTime: 60_000,
  });
  const { photos } = useListingPhotos(ids, PHOTOS_PER_ADVERT);
  return {
    member: (a) =>
      memberFromListing(a.listing_id, a, details.data?.get(a.listing_id), photos.get(a.listing_id) ?? []),
    error: details.error,
  };
}
