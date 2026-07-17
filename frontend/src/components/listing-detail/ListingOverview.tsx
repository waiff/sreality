/* Shared "what is this property" overview — the dossier header (identity +
 * price left, location map anchored top-right), a dense facts strip,
 * description, an optional estimates slot, and the photo gallery. Extracted
 * from the Listing Detail page so the Estimation Detail page renders its
 * subject with the SAME structure (one surface, not two). Driven by a
 * ListingPublic row; the estimation page passes the subject's resolved
 * listings row. */
import { Suspense, lazy, useLayoutEffect, useRef, useState } from 'react';
import { fmtCzk, fmtArea, fmtPricePerM2, fmtAbsolute } from '@/lib/format';
import type { ImagePublic, ListingPublic } from '@/lib/types';
import { listingKindParts } from '@/lib/enums';
import { placePrimary } from '@/lib/placeLabel';
import {
  buildFacts,
  buildAmenities,
  FactsList,
  AmenityChips,
} from '@/lib/listingFacts';

const DetailMap = lazy(() => import('@/components/listing-detail/DetailMap'));
const Gallery = lazy(() => import('@/components/listing-detail/Gallery'));

export function ListingOverview({
  listing,
  images = [],
  imagesLoading = false,
  showStatus = true,
  showGallery = true,
  identityLabel,
  headerExtras,
  mapFooter,
  estimatesSlot,
}: {
  listing: ListingPublic;
  images?: ImagePublic[];
  imagesLoading?: boolean;
  showStatus?: boolean;
  /* The /property/:id page (PropertyDetail) reuses this header for the
   * property's own identity but renders per-source photo groups itself
   * instead of one pooled gallery — set false there to skip this component's
   * single-gallery slot (and its leading hairline) entirely. */
  showGallery?: boolean;
  /* Overrides the "ID {sreality_id}" identity line. listing.sreality_id is a
   * REPRESENTATIVE listing id when this component renders a PropertyPublic
   * row (types.ts) — labelling it plain "ID" would misstate it as the
   * specific advert you're viewing, so PropertyDetail passes e.g.
   * "Property #3309" here instead. */
  identityLabel?: string;
  /* Chip row (portal links, active-sibling alert) rendered at the TOP of the
   * header's left column — inside the grid, so the map column starts at the
   * very top instead of below a stack of full-width rows. */
  headerExtras?: React.ReactNode;
  /* Rendered directly UNDER the header map (right column), only when the
   * listing has coordinates. The Listing Detail page fills it with the
   * "Explore area" button; Estimation Detail leaves it empty (so the button
   * doesn't appear on the estimation subject). */
  mapFooter?: React.ReactNode;
  /* The estimation chapter, rendered between description and gallery — the
   * listing page passes its EstimationsBlock here so the estimates sit in
   * the prime slot the location map used to occupy (the map lives in the
   * header now). The slot brings its own leading hairline. */
  estimatesSlot?: React.ReactNode;
}) {
  return (
    <>
      <Header
        listing={listing}
        showStatus={showStatus}
        extras={headerExtras}
        mapFooter={mapFooter}
        identityLabel={identityLabel}
      />
      <KeyFactsBlock listing={listing} />
      <DescriptionBlock listing={listing} />
      {estimatesSlot}
      {showGallery && (
        <>
          <Hairline />
          <GalleryBlock
            images={images}
            isActive={listing.is_active}
            loading={imagesLoading}
          />
        </>
      )}
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* Layout primitives                                                          */
/* -------------------------------------------------------------------------- */

function Hairline() {
  return <div className="my-7 h-px bg-[var(--color-rule)]" />;
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
      {children}
    </p>
  );
}

/* -------------------------------------------------------------------------- */
/* Header (hero)                                                              */
/* -------------------------------------------------------------------------- */

function Header({
  listing,
  showStatus,
  extras,
  mapFooter,
  identityLabel,
}: {
  listing: ListingPublic;
  showStatus: boolean;
  extras?: React.ReactNode;
  mapFooter?: React.ReactNode;
  identityLabel?: string;
}) {
  // Identity tokens, most-specific first: subtype ("Ubytování", "Rodinný dům")
  // and/or disposition ("2+kk"). Commercial/houses keep their kind instead of
  // the old bare "—"; apartments are unchanged (subtype NULL → disposition).
  const kindParts = listingKindParts(listing);
  const area = fmtArea(listing.area_m2);
  const floor =
    listing.floor != null
      ? listing.total_floors != null
        ? `${listing.floor}/${listing.total_floors}`
        : String(listing.floor)
      : null;
  // A null price means the seller hid it (often quoting it in the description);
  // it's real source state, not missing data — surface it as such.
  const hasPrice = listing.price_czk != null;
  const price = hasPrice ? fmtCzk(listing.price_czk) : 'Cena na vyžádání';
  const ppm = fmtPricePerM2(listing.price_czk, listing.area_m2);
  const unit = hasPrice && listing.price_unit ? ` / ${listing.price_unit}` : '';
  const hasId = identityLabel != null || listing.sreality_id > 0;
  const { lat, lng } = listing;
  const hasMap = lat != null && lng != null;

  return (
    <div className="mt-4 grid gap-x-8 gap-y-5 lg:grid-cols-[minmax(0,1fr)_minmax(300px,400px)] items-start">
      <div className="min-w-0">
        {extras && <div className="mb-4">{extras}</div>}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <p className="font-mono tabular-nums text-[var(--color-ink-2)] text-sm">
            <span>{kindParts.length > 0 ? kindParts.join(' · ') : '—'}</span>
            <span className="mx-2 text-[var(--color-ink-4)]">·</span>
            <span>{area}</span>
            {floor != null && (
              <>
                <span className="mx-2 text-[var(--color-ink-4)]">·</span>
                <span title="Floor">
                  floor <span className="text-[var(--color-ink)]">{floor}</span>
                </span>
              </>
            )}
          </p>
          {showStatus && (
            <StatusPill isActive={listing.is_active} lastSeenAt={listing.last_seen_at} />
          )}
        </div>
        <h1
          className={[
            'mt-1.5 leading-[1.05] tabular-nums',
            hasPrice ? 'text-[2.6rem]' : 'text-[1.6rem] text-[var(--color-ink-3)]',
          ].join(' ')}
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          {price}
          <span className="text-base font-sans font-normal text-[var(--color-ink-3)] tracking-wide">
            {unit}
          </span>
        </h1>
        <p className="mt-2 text-sm text-[var(--color-ink-2)]">
          {placePrimary(listing) ?? '—'}
        </p>
        {(hasId || ppm !== '—') && (
          <p className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)] mt-2">
            {hasId && (
              identityLabel != null ? (
                <span className="font-mono tabular-nums text-[var(--color-ink-3)] normal-case tracking-normal">
                  {identityLabel}
                </span>
              ) : (
                <>
                  ID{' '}
                  <span className="font-mono tabular-nums text-[var(--color-ink-3)] normal-case tracking-normal">
                    {listing.sreality_id}
                  </span>
                </>
              )
            )}
            {ppm !== '—' && (
              <>
                {hasId && <span className="mx-2">·</span>}
                <span className="font-mono tabular-nums text-[var(--color-ink-3)] normal-case tracking-normal">
                  {ppm}
                </span>
              </>
            )}
          </p>
        )}
      </div>
      {/* The dossier's "file photo": the location map anchored top-right.
          Missing coordinates keep the slot with an explicit note — silence
          would read as a render failure, not a data fact. */}
      <div className="w-full">
        {hasMap ? (
          <Suspense
            fallback={
              <div className="h-[190px] rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]" />
            }
          >
            <DetailMap
              lat={lat}
              lng={lng}
              isActive={listing.is_active}
              heightClass="h-[190px]"
            />
          </Suspense>
        ) : (
          <div className="h-[190px] flex items-center justify-center text-sm text-[var(--color-ink-3)] border border-dashed border-[var(--color-rule)] rounded-[var(--radius-md)]">
            No coordinates recorded
          </div>
        )}
        {hasMap && mapFooter && <div className="mt-2">{mapFooter}</div>}
      </div>
    </div>
  );
}

function StatusPill({ isActive, lastSeenAt }: { isActive: boolean; lastSeenAt: string }) {
  const title = lastSeenAt ? `Last seen ${fmtAbsolute(lastSeenAt)}` : undefined;
  if (isActive) {
    return (
      <span
        className="shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 text-[0.7rem] tracking-wide rounded-[var(--radius-sm)] bg-[var(--color-copper-soft)] text-[var(--color-copper)] border border-[var(--color-copper)]/20"
        title={title}
      >
        <span className="w-1.5 h-1.5 rounded-full bg-[var(--color-sage)]" aria-hidden />
        Active
      </span>
    );
  }
  return (
    <span
      className="shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 text-[0.7rem] tracking-wide rounded-[var(--radius-sm)] bg-[var(--color-brick-soft)] text-[var(--color-brick)] border border-[var(--color-brick)]/20"
      title={title}
    >
      Inactive
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Gallery                                                                    */
/* -------------------------------------------------------------------------- */

function GalleryBlock({
  images,
  isActive,
  loading,
}: {
  images: ImagePublic[];
  isActive: boolean;
  loading: boolean;
}) {
  if (loading && images.length === 0) {
    return (
      <div>
        <SectionLabel>Photos</SectionLabel>
        <div className="mt-2 grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-1.5">
          {Array.from({ length: 4 }).map((_, i) => (
            <div
              key={i}
              className="aspect-[4/3] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]"
            />
          ))}
        </div>
      </div>
    );
  }
  if (images.length === 0) {
    return (
      <div>
        <SectionLabel>Photos</SectionLabel>
        <p className="mt-2 text-sm text-[var(--color-ink-3)]">No photos recorded.</p>
      </div>
    );
  }
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <SectionLabel>Photos</SectionLabel>
        <p className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)] font-mono tabular-nums">
          {images.length}
        </p>
      </div>
      <div className="mt-3">
        <Suspense fallback={null}>
          <Gallery images={images} isActive={isActive} />
        </Suspense>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Description (original sreality "Popis" free-text)                          */
/* -------------------------------------------------------------------------- */

function DescriptionBlock({ listing }: { listing: ListingPublic }) {
  const text = listing.description?.trim() ?? '';
  if (!text) return null;
  return <DescriptionBody text={text} />;
}

function DescriptionBody({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const ref = useRef<HTMLParagraphElement | null>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setOverflows(el.scrollHeight - el.clientHeight > 1);
  }, [text]);

  return (
    <div className="mt-7">
      <SectionLabel>Description</SectionLabel>
      <p
        ref={ref}
        className={
          'mt-3 text-sm leading-relaxed text-[var(--color-ink)] whitespace-pre-wrap ' +
          (expanded ? '' : 'line-clamp-4')
        }
      >
        {text}
      </p>
      {(overflows || expanded) && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mt-2 inline-flex items-center text-[0.75rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
        >
          {expanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Key facts                                                                  */
/* -------------------------------------------------------------------------- */

/* One dense data strip instead of the old Property/Building grids: the kind
 * (subtype and/or disposition), area, floor and district live in the header.
 * What remains — lot/garden for houses, the building facts — renders as inline
 * label·value pairs on one wrapping line, with the amenity chips below. */
function KeyFactsBlock({ listing }: { listing: ListingPublic }) {
  const facts = buildFacts(listing);
  const amenities = buildAmenities(listing);
  if (facts.length === 0 && amenities.length === 0) {
    return null;
  }
  return (
    <div className="mt-7 space-y-3">
      <FactsList facts={facts} />
      <AmenityChips amenities={amenities} />
    </div>
  );
}
