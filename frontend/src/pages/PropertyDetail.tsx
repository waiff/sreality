/* PropertyDetail (`/property/:id`) — the canonical multi-portal PROPERTY
 * (migration 091), one page per real-world unit, however many portal adverts
 * point at it.
 *
 * This is the ONE place the property-grain view lives. It exists so a listing
 * page (ListingDetail, /listing/:sreality_id) can stay scoped to exactly the
 * one advert it renders — its own images, its own description, its own price
 * history — while still linking out (PropertyLinkChip) to "the same real
 * property, seen everywhere it's listed." property_sources_public gives each
 * child its own thumbnails/price/dates here too (SourcesList) — a merge groups
 * records, it never blends their content.
 *
 * properties_public mirrors listings_public column-for-column (migration 093),
 * so PropertyPublic (types.ts) is a structural superset of ListingPublic — the
 * fetched row feeds ListingOverview directly, with its gallery slot switched
 * off (showGallery=false) since SourcesList below owns the per-source photos. */
import { Suspense, lazy } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { usePageTitle } from '@/lib/pageTitle';
import { listingTypeLabel } from '@/lib/enums';
import { placePrimary } from '@/lib/placeLabel';
import {
  fetchImagesByListingIds,
  fetchPropertyById,
  fetchPropertySourcesByPropertyIds,
} from '@/lib/queries';
import { ListingOverview } from '@/components/listing-detail/ListingOverview';
import { MergeDecisionsChip } from '@/components/listing-detail/MergeDecisionsChip';
import PipelineToggle from '@/components/listing-detail/PipelineToggle';
import { MfReferenceCard } from '@/components/estimation/MfReferenceCard';
import { SourcesList } from '@/components/property-detail/SourcesList';

const CurationBlock = lazy(
  () => import('@/components/listing-detail/CurationBlock'),
);

export default function PropertyDetail() {
  const { id: idParam } = useParams();
  const propertyId = idParam && /^\d+$/.test(idParam) ? Number(idParam) : null;

  const propertyQ = useQuery({
    queryKey: ['property', propertyId],
    queryFn: () => fetchPropertyById(propertyId as number),
    enabled: propertyId != null,
    staleTime: 60_000,
  });

  const sourcesQ = useQuery({
    queryKey: ['property-sources-of', propertyId],
    queryFn: async () => {
      const byProperty = await fetchPropertySourcesByPropertyIds([
        propertyId as number,
      ]);
      return byProperty.get(propertyId as number) ?? [];
    },
    enabled: propertyId != null && !!propertyQ.data,
    staleTime: 60_000,
  });

  const sources = sourcesQ.data ?? [];
  const sourceIds = sources.map((s) => s.sreality_id);
  const imagesQ = useQuery({
    queryKey: ['property-source-images', propertyId, sourceIds],
    // 6 thumbnails/source comfortably covers the compact card strip below —
    // full per-listing galleries live on each source's own /listing/:id page.
    queryFn: () => fetchImagesByListingIds(sourceIds, 6),
    enabled: sourceIds.length > 0,
    staleTime: 5 * 60_000,
  });

  // Tab title, same composition as ListingDetail — MUST stay above the early
  // returns (hook-order; see ListingDetail's identical comment on the #310 trap).
  usePageTitle(
    propertyQ.data
      ? [
          'Nemovitost',
          listingTypeLabel(propertyQ.data),
          propertyQ.data.disposition,
          propertyQ.data.street?.trim()
            || propertyQ.data.obec?.trim()
            || placePrimary(propertyQ.data),
        ]
          .filter(Boolean)
          .join(' · ') || null
      : null,
  );

  if (propertyId == null) {
    return (
      <Page>
        <Crumb />
        <NotFoundState id={idParam ?? null} reason="invalid" />
      </Page>
    );
  }

  if (propertyQ.isLoading) {
    return (
      <Page>
        <Crumb />
        <div className="mt-8 text-sm text-[var(--color-ink-3)]">Loading…</div>
      </Page>
    );
  }

  if (propertyQ.error) {
    return (
      <Page>
        <Crumb />
        <div className="mt-8 text-sm text-[var(--color-brick)]">
          Failed to load: {propertyQ.error.message}
        </div>
      </Page>
    );
  }

  const property = propertyQ.data;
  if (!property) {
    return (
      <Page>
        <Crumb />
        <NotFoundState id={String(propertyId)} reason="missing" />
      </Page>
    );
  }

  return (
    <Page>
      <div className="flex items-center justify-between gap-3">
        <Crumb />
        <PipelineToggle property_id={property.property_id} />
      </div>
      <ListingOverview
        listing={property}
        showGallery={false}
        identityLabel={`Nemovitost #${property.property_id}`}
        headerExtras={
          <MergeDecisionsChip
            propertyId={property.property_id}
            multiSource={sources.length > 1}
          />
        }
      />
      {property.mf_reference_rent && (
        <>
          <Hairline />
          <section>
            <SectionLabel>Reference</SectionLabel>
            <div className="mt-3 max-w-xs">
              <MfReferenceCard
                refRent={property.mf_reference_rent}
                yieldPct={property.mf_gross_yield_pct}
              />
            </div>
          </section>
        </>
      )}
      <Hairline />
      {sourcesQ.isLoading ? (
        <p className="text-sm text-[var(--color-ink-3)]">Loading sources…</p>
      ) : (
        <SourcesList
          sources={sources}
          imagesBySource={imagesQ.data ?? new Map()}
          category={{
            categoryType: property.category_type,
            categoryMain: property.category_main,
            categorySubCb: property.category_sub_cb,
          }}
        />
      )}
      <Hairline />
      <Suspense fallback={null}>
        <CurationBlock
          property_id={property.property_id}
          sreality_id={property.sreality_id}
        />
      </Suspense>
    </Page>
  );
}

/* -------------------------------------------------------------------------- */
/* Layout primitives — each detail page owns its own tiny copies (same         */
/* convention as ListingDetail / BuildingDetail / CollectionDetail).           */
/* -------------------------------------------------------------------------- */

function Page({ children }: { children: React.ReactNode }) {
  return <div className="px-6 py-8 max-w-5xl mx-auto">{children}</div>;
}

function Crumb() {
  const navigate = useNavigate();
  const location = useLocation();
  const className =
    'inline-flex items-center gap-1.5 text-[0.75rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors';
  if (location.key !== 'default') {
    return (
      <button type="button" onClick={() => navigate(-1)} className={className}>
        <BackArrow />
        <span>Back</span>
      </button>
    );
  }
  return (
    <Link to="/browse" className={className}>
      <BackArrow />
      <span>Back to browse</span>
    </Link>
  );
}

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

function BackArrow() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
      <polyline
        points="5.5,1.5 1.5,5 5.5,8.5"
        stroke="currentColor"
        strokeWidth="1.25"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <line
        x1="1.5"
        y1="5"
        x2="9"
        y2="5"
        stroke="currentColor"
        strokeWidth="1.25"
        strokeLinecap="round"
      />
    </svg>
  );
}

function NotFoundState({ id, reason }: { id: string | null; reason: 'invalid' | 'missing' }) {
  return (
    <div className="mt-12">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Not found
      </p>
      <h1
        className="mt-2 text-2xl"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        {reason === 'invalid' ? (
          'No property requested'
        ) : (
          <>
            No property with id{' '}
            <span className="font-mono tabular-nums text-[var(--color-ink-2)]">{id}</span>
          </>
        )}
      </h1>
      <p className="mt-3 text-sm text-[var(--color-ink-3)]">
        The id may be wrong, or the property was never created.
        <Link to="/browse" className="ml-1 text-[var(--color-copper)] hover:underline">
          Browse all listings
        </Link>
        .
      </p>
    </div>
  );
}
