/* The "Origin property" strip pinned at the top of the Explore-area modal: a
 * compact photo slider + the property's core facts (kind · area · price) and
 * amenity chips, so the operator can always compare the listing they came FROM
 * against whatever the active filter shows below.
 *
 * Pure reuse — no rendering invented here: ImageCarousel (the same slider as
 * Browse cards), the Header's exact price/kind helpers, and the shared
 * buildAmenities/AmenityChips from lib/listingFacts (so the chips match the
 * listing-detail header and can never drift). Every field is null-tolerant: a
 * bazos/idnes row with NULL disposition/area/amenities renders a sparse-but-
 * valid strip (photos + price + whatever facts exist), never a broken box. */
import { useMemo } from 'react';
import ImageCarousel from '@/components/ImageCarousel';
import { fmtArea, fmtCzk, fmtPricePerM2 } from '@/lib/format';
import { listingKindParts } from '@/lib/enums';
import { imageSrc, type ImageRef } from '@/lib/imageUrl';
import { AmenityChips, buildAmenities } from '@/lib/listingFacts';
import type { ListingPublic } from '@/lib/types';

export default function OriginPropertyPanel({
  listing,
  images,
}: {
  listing: ListingPublic;
  images: ImageRef[];
}) {
  const urls = useMemo(() => images.map(imageSrc), [images]);
  const amenities = useMemo(() => buildAmenities(listing), [listing]);

  const kindParts = listingKindParts(listing);
  const kind = kindParts.length > 0 ? kindParts.join(' · ') : '—';
  const area = fmtArea(listing.area_m2);
  // Mirror the listing-detail Header exactly: a null price is real seller state
  // ("on request"), not missing data; rentals carry a "/ měsíc" unit.
  const hasPrice = listing.price_czk != null;
  const price = hasPrice ? fmtCzk(listing.price_czk) : 'Cena na vyžádání';
  const unit = hasPrice && listing.price_unit ? ` / ${listing.price_unit}` : '';
  const ppm = fmtPricePerM2(listing.price_czk, listing.area_m2);

  return (
    <div className="shrink-0 border-b border-[var(--color-rule)] bg-[var(--color-paper-2)] px-6 py-3">
      <div className="flex items-stretch gap-4">
        {/* Compact slider. w-32→40 + aspect-[4/3] keeps the strip ~120px tall so
            the map keeps the modal's vertical budget; on a phone it shrinks
            rather than crushing the facts column. */}
        <div className="w-32 sm:w-40 shrink-0">
          <ImageCarousel
            urls={urls}
            aspect="aspect-[4/3]"
            className="rounded-[var(--radius-sm)] border border-[var(--color-rule)]"
          />
        </div>
        <div className="min-w-0 flex-1 flex flex-col justify-center gap-1.5">
          <p className="text-[0.58rem] tracking-[0.2em] uppercase text-[var(--color-ink-3)]">
            Origin property
          </p>
          <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-0.5 font-mono tabular-nums text-sm text-[var(--color-ink)]">
            <span>{kind}</span>
            <span className="text-[var(--color-ink-4)]">·</span>
            <span>{area}</span>
          </div>
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span
              className="tabular-nums text-[var(--color-ink)] text-base leading-tight"
              style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
            >
              {price}
              <span className="text-xs font-sans font-normal text-[var(--color-ink-3)] tracking-wide">
                {unit}
              </span>
            </span>
            {ppm !== '—' && (
              <>
                <span className="text-[var(--color-ink-4)]">·</span>
                <span className="font-mono tabular-nums text-xs text-[var(--color-ink-3)]">
                  {ppm}
                </span>
              </>
            )}
          </div>
          {amenities.length > 0 && <AmenityChips amenities={amenities} />}
        </div>
      </div>
    </div>
  );
}
