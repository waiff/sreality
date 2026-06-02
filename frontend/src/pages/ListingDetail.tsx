import { Suspense, lazy, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import {
  useNewEstimationModal,
  type NewEstimationPrefill,
} from '@/components/NewEstimationModal';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  fetchListingById,
  fetchPropertyReprId,
  fetchPropertySources,
  fetchSnapshotsForListings,
  fetchFreshnessChecksByListing,
  fetchImagesByListing,
} from '@/lib/queries';
import {
  ApiError,
  verifyListingFreshness,
  type FreshnessOutcome,
  type VerifyFreshnessResult,
} from '@/lib/api';
import {
  fmtCzk,
  fmtArea,
  fmtPricePerM2,
  fmtRelative,
  fmtAbsolute,
  fmtShortDate,
  fmtFurnished,
  fmtOwnership,
  fmtParkingLots,
  fmtCategorySub,
} from '@/lib/format';
import type {
  ImagePublic,
  ListingPublic,
  ListingSnapshotPublic,
  ListingFreshnessCheckPublic,
  PropertySource,
} from '@/lib/types';
import {
  listingUrlRows,
  buildPriceSeries,
  summarizePriceHistory,
} from '@/lib/priceHistory';
import ErrorBoundary from '@/components/ErrorBoundary';

const DetailMap = lazy(() => import('@/components/listing-detail/DetailMap'));
const Gallery = lazy(() => import('@/components/listing-detail/Gallery'));
const PriceLineChart = lazy(
  () => import('@/components/listing-detail/PriceLineChart'),
);
const CurationBlock = lazy(
  () => import('@/components/listing-detail/CurationBlock'),
);
const ManualEstimatesBlock = lazy(
  () => import('@/components/listing-detail/ManualEstimatesBlock'),
);

export default function ListingDetail() {
  const { sreality_id: idParam } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  // sreality_id is negative for non-sreality portals (synthetic id seq, migration 097)
  const sid = idParam && /^-?\d+$/.test(idParam) ? Number(idParam) : null;

  // /listing?property=ID (the dedup merge feed links this) → resolve the
  // property's representative listing and redirect to /listing/{reprId}.
  const propertyParam = new URLSearchParams(location.search).get('property');
  const propertyId =
    sid == null && propertyParam && /^\d+$/.test(propertyParam)
      ? Number(propertyParam)
      : null;
  const reprQ = useQuery<number | null, Error>({
    queryKey: ['property-repr', propertyId],
    queryFn: () => fetchPropertyReprId(propertyId as number),
    enabled: propertyId != null,
    staleTime: 60_000,
  });
  useLayoutEffect(() => {
    if (reprQ.data != null) {
      navigate(`/listing/${reprQ.data}`, { replace: true });
    }
  }, [reprQ.data, navigate]);

  const listingQ = useQuery<ListingPublic | null, Error>({
    queryKey: ['listing', sid],
    queryFn: () => fetchListingById(sid as number),
    enabled: sid != null,
    staleTime: 60_000,
  });

  const sourcesQ = useQuery<{ property_id: number | null; sources: PropertySource[] }, Error>({
    queryKey: ['property-sources', sid],
    queryFn: () => fetchPropertySources(sid as number),
    enabled: sid != null && !!listingQ.data,
    staleTime: 60_000,
  });

  // Cross-source price history: snapshots across every child of the property,
  // falling back to just this listing until sources load / for singletons.
  const childIds = (sourcesQ.data?.sources ?? [])
    .map((s) => s.sreality_id)
    .filter((x): x is number => x != null);
  const snapshotIds =
    childIds.length > 0
      ? [...childIds].sort((a, b) => a - b)
      : sid != null
        ? [sid]
        : [];

  const snapshotsQ = useQuery<ListingSnapshotPublic[], Error>({
    queryKey: ['snapshots', snapshotIds],
    queryFn: () => fetchSnapshotsForListings(snapshotIds),
    enabled: snapshotIds.length > 0 && !!listingQ.data,
    staleTime: 60_000,
  });

  const checksQ = useQuery<ListingFreshnessCheckPublic[], Error>({
    queryKey: ['freshness', sid],
    queryFn: () => fetchFreshnessChecksByListing(sid as number),
    enabled: sid != null && !!listingQ.data,
    staleTime: 60_000,
  });

  const imagesQ = useQuery<ImagePublic[], Error>({
    queryKey: ['images', sid],
    queryFn: () => fetchImagesByListing(sid as number),
    enabled: sid != null && !!listingQ.data,
    staleTime: 5 * 60_000,
  });

  // Bind the page's "New estimation" CTA to THIS listing: pre-fill its URL +
  // estimate type so the modal drops the URL field and just runs the estimate.
  // MUST stay above the early returns below — a hook after a conditional return
  // is the React #310 "rendered more hooks than during the previous render" trap
  // (it fired on every listing once listingQ resolved, white-screening the page).
  const newEstimationPrefill = useMemo<NewEstimationPrefill | undefined>(() => {
    const listing = listingQ.data;
    if (!listing) return undefined;
    const currentSource = (sourcesQ.data?.sources ?? []).find(
      (s) => s.sreality_id === listing.sreality_id,
    );
    const url =
      currentSource?.source_url
      ?? (listing.source === 'sreality'
        ? `https://www.sreality.cz/detail/${listing.category_type ?? 'prodej'}/${listing.category_main ?? 'byt'}/x/x/${listing.sreality_id}`
        : undefined);
    if (!url) return undefined;
    const categoryMain =
      listing.category_main === 'byt'
      || listing.category_main === 'dum'
      || listing.category_main === 'komercni'
        ? listing.category_main
        : undefined;
    return {
      url,
      categoryMain,
      estimateKind: listing.category_type === 'pronajem' ? 'rent' : 'sale',
    };
  }, [listingQ.data, sourcesQ.data]);

  if (sid == null) {
    // Resolving ?property=ID → redirect (handled by the effect above). Show a
    // loading state while it resolves; only "not found" if there's no such
    // property (or the param was neither a sreality id nor a property id).
    if (propertyId != null && (reprQ.isLoading || reprQ.data != null)) {
      return (
        <Page>
          <Crumb />
          <div className="mt-8 text-sm text-[var(--color-ink-3)]">Loading…</div>
        </Page>
      );
    }
    return <NoListingState id={idParam ?? propertyParam} reason="invalid" />;
  }

  if (listingQ.isLoading) {
    return (
      <Page>
        <Crumb />
        <div className="mt-8 text-sm text-[var(--color-ink-3)]">Loading…</div>
      </Page>
    );
  }

  if (listingQ.error) {
    return (
      <Page>
        <Crumb />
        <div className="mt-8 text-sm text-[var(--color-brick)]">
          Failed to load: {listingQ.error.message}
        </div>
      </Page>
    );
  }

  const listing = listingQ.data;
  if (!listing) {
    return <NoListingState id={idParam ?? null} reason="missing" />;
  }

  const snapshots = snapshotsQ.data ?? [];
  const checks = checksQ.data ?? [];
  const images = imagesQ.data ?? [];
  const sources = sourcesQ.data?.sources ?? [];
  const currentSource = sources.find((s) => s.sreality_id === listing.sreality_id);

  return (
    <Page>
      <div className="flex items-center justify-between gap-3">
        <Crumb />
        <NewEstimationButton prefill={newEstimationPrefill} />
      </div>
      {/* Merged top section: identity + price + property facts in one block,
          above the images. The MF rent estimate + description sit directly
          below it. */}
      <LatestActiveLink listing={listing} sources={sources} />
      <Header listing={listing} />
      <KeyFactsBlock listing={listing} />
      <ReferenceRentBlock listing={listing} />
      <DescriptionBlock listing={listing} />
      <Hairline />
      <MapBlock listing={listing} />
      <Hairline />
      <GalleryBlock images={images} isActive={listing.is_active} loading={imagesQ.isLoading} />
      <Hairline />
      <Suspense fallback={null}>
        <CurationBlock sreality_id={listing.sreality_id} />
      </Suspense>
      <Hairline />
      <Suspense fallback={null}>
        <ManualEstimatesBlock sreality_id={listing.sreality_id} />
      </Suspense>
      <Hairline />
      <ListingHistoryBlock listing={listing} sources={sources} snapshots={snapshots} />
      <Hairline />
      <FreshnessBlock sreality_id={listing.sreality_id} checks={checks} />
      <Hairline />
      <OutboundBlock sreality_id={listing.sreality_id} source={currentSource} />
    </Page>
  );
}

/* -------------------------------------------------------------------------- */
/* Latest-active-listing link (shown when this record is one of several        */
/* observations of a property and a different, still-live one exists)          */
/* -------------------------------------------------------------------------- */

function LatestActiveLink({
  listing,
  sources,
}: {
  listing: ListingPublic;
  sources: PropertySource[];
}) {
  const liveSibling = sources
    .filter((s) => s.is_active && s.sreality_id !== listing.sreality_id)
    .sort(
      (a, b) =>
        new Date(b.last_seen_at).getTime() - new Date(a.last_seen_at).getTime(),
    )[0];
  if (!liveSibling) return null;
  return (
    <Link
      to={`/listing/${liveSibling.sreality_id}`}
      className="mt-4 inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border border-[var(--color-copper)]/30 bg-[var(--color-copper-soft)] px-3 py-1.5 text-[0.8rem] text-[var(--color-copper)] hover:bg-[var(--color-copper)]/15 transition-colors"
    >
      <span className="w-1.5 h-1.5 rounded-full bg-[var(--color-sage)]" aria-hidden />
      View the current active listing
      <span className="capitalize text-[var(--color-ink-3)]">· {liveSibling.source}</span>
      <OutArrow />
    </Link>
  );
}

/* -------------------------------------------------------------------------- */
/* Layout primitives                                                          */
/* -------------------------------------------------------------------------- */

function Page({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-6 py-8 max-w-3xl mx-auto">{children}</div>
  );
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
        <span>Back to browse</span>
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

function NewEstimationButton({ prefill }: { prefill?: NewEstimationPrefill }) {
  const { open } = useNewEstimationModal();
  return (
    <button
      type="button"
      onClick={() => open(prefill)}
      className="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors"
    >
      <span className="text-[0.95em] leading-none">+</span>
      <span>New estimation</span>
    </button>
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

/* -------------------------------------------------------------------------- */
/* Header                                                                     */
/* -------------------------------------------------------------------------- */

function Header({ listing }: { listing: ListingPublic }) {
  const disposition = listing.disposition ?? '—';
  const area = fmtArea(listing.area_m2);
  // A null price means the seller hid it (often quoting it in the description);
  // it's real source state, not missing data — surface it as such.
  const hasPrice = listing.price_czk != null;
  const price = hasPrice ? fmtCzk(listing.price_czk) : 'Cena na vyžádání';
  const ppm = fmtPricePerM2(listing.price_czk, listing.area_m2);
  const unit = hasPrice && listing.price_unit ? ` / ${listing.price_unit}` : '';

  return (
    <div className="mt-5 flex items-start justify-between gap-6">
      <div className="min-w-0">
        <p className="font-mono tabular-nums text-[var(--color-ink-2)] text-sm">
          <span>{disposition}</span>
          <span className="mx-2 text-[var(--color-ink-4)]">·</span>
          <span>{area}</span>
        </p>
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
          {listing.locality ?? listing.district ?? '—'}
        </p>
        <p className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)] mt-2">
          ID <span className="font-mono tabular-nums text-[var(--color-ink-3)] normal-case tracking-normal">{listing.sreality_id}</span>
          {ppm !== '—' && (
            <>
              <span className="mx-2">·</span>
              <span className="font-mono tabular-nums text-[var(--color-ink-3)] normal-case tracking-normal">
                {ppm}
              </span>
            </>
          )}
        </p>
      </div>
      <StatusPill isActive={listing.is_active} lastSeenAt={listing.last_seen_at} />
    </div>
  );
}

const MF_ADJ_LABELS: Record<string, string> = {
  balcony: 'balkón',
  terrace: 'terasa',
  furnished: 'vybavenost',
  garage: 'garáž',
  elevator: 'výtah',
  other_material: 'jiný konstrukční materiál',
};

/* MF Cenová mapa reference-rent breakdown (migration 134) — the numbers
 * behind the stored monthly rent + gross yield. Sale apartments only; the
 * column is NULL otherwise, so the block self-hides. */
function ReferenceRentBlock({ listing }: { listing: ListingPublic }) {
  const ref = listing.mf_reference_rent;
  if (!ref) return null;
  const perM2 = (n: number) => `${n.toLocaleString('cs-CZ')} Kč/m²`;
  return (
    <div className="mt-6 max-w-[27rem] border border-[var(--color-rule)] rounded-[var(--radius-sm)] p-3">
      <p className="text-[0.6rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
        Odhad nájmu · cenová mapa MF
      </p>
      <div className="mt-1 flex items-baseline justify-between gap-3">
        <span className="text-lg font-medium tabular-nums">
          {fmtCzk(ref.monthly_rent_czk)}
          <span className="ml-1 text-[0.7rem] text-[var(--color-ink-3)]">/měs</span>
        </span>
        {listing.mf_gross_yield_pct != null && (
          <span className="text-[0.72rem] text-[var(--color-ink-3)] tabular-nums">
            hrubý výnos{' '}
            <span className="text-[var(--color-ink)] font-medium">
              {listing.mf_gross_yield_pct.toLocaleString('cs-CZ', {
                minimumFractionDigits: 2,
                maximumFractionDigits: 2,
              })}{' '}%
            </span>
          </span>
        )}
      </div>
      <dl className="mt-2 space-y-0.5 text-[0.72rem] tabular-nums">
        <div className="flex justify-between gap-3">
          <dt className="text-[var(--color-ink-3)]">
            Nájemné referenčního bytu
            {ref.is_novostavba ? ' (novostavba)' : ''}
          </dt>
          <dd>{perM2(ref.base_per_m2)}</dd>
        </div>
        {ref.adjustments.map((a) => (
          <div key={a.attribute} className="flex justify-between gap-3">
            <dt className="text-[var(--color-ink-3)]">
              + {MF_ADJ_LABELS[a.attribute] ?? a.attribute}
            </dt>
            <dd>+{perM2(a.czk_per_m2)}</dd>
          </div>
        ))}
        <div className="flex justify-between gap-3 border-t border-[var(--color-rule)] pt-0.5">
          <dt>Celkem za m²</dt>
          <dd>{perM2(ref.total_per_m2)}</dd>
        </div>
        <div className="flex justify-between gap-3">
          <dt className="text-[var(--color-ink-3)]">
            × plocha {ref.area_m2.toLocaleString('cs-CZ')} m²
          </dt>
          <dd>{fmtCzk(ref.monthly_rent_czk)}</dd>
        </div>
      </dl>
      <p className="mt-1.5 text-[0.58rem] text-[var(--color-ink-4)]">
        {ref.territory.name}
        {ref.territory.kraj ? `, ${ref.territory.kraj}` : ''} · VK{ref.vk} ·
        Ministerstvo financí
      </p>
    </div>
  );
}

function StatusPill({ isActive, lastSeenAt }: { isActive: boolean; lastSeenAt: string }) {
  if (isActive) {
    return (
      <span
        className="shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 text-[0.7rem] tracking-wide rounded-[var(--radius-sm)] bg-[var(--color-copper-soft)] text-[var(--color-copper)] border border-[var(--color-copper)]/20"
        title={`Last seen ${fmtAbsolute(lastSeenAt)}`}
      >
        <span className="w-1.5 h-1.5 rounded-full bg-[var(--color-sage)]" aria-hidden />
        Active
      </span>
    );
  }
  return (
    <span
      className="shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 text-[0.7rem] tracking-wide rounded-[var(--radius-sm)] bg-[var(--color-brick-soft)] text-[var(--color-brick)] border border-[var(--color-brick)]/20"
      title={`Last seen ${fmtAbsolute(lastSeenAt)}`}
    >
      Inactive
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Map block                                                                  */
/* -------------------------------------------------------------------------- */

function MapBlock({ listing }: { listing: ListingPublic }) {
  if (listing.lat == null || listing.lng == null) {
    return (
      <div>
        <SectionLabel>Location</SectionLabel>
        <div className="mt-2 h-32 flex items-center justify-center text-sm text-[var(--color-ink-3)] border border-dashed border-[var(--color-rule)] rounded-[var(--radius-md)]">
          No coordinates recorded
        </div>
      </div>
    );
  }
  return (
    <div>
      <SectionLabel>Location</SectionLabel>
      <div className="mt-2">
        <Suspense
          fallback={
            <div className="h-40 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]" />
          }
        >
          <DetailMap lat={listing.lat} lng={listing.lng} isActive={listing.is_active} />
        </Suspense>
      </div>
    </div>
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

function KeyFactsBlock({ listing }: { listing: ListingPublic }) {
  const floor = listing.floor != null
    ? listing.total_floors != null
      ? `${listing.floor} / ${listing.total_floors}`
      : String(listing.floor)
    : null;

  /* Merged into the top section, above the images. Disposition, usable area
   * and district are intentionally omitted here — they're already in the
   * header above, so this grid carries only the facts the header doesn't.
   * Fields that are universally NULL for a given category (estate_area /
   * garden_area on apartments, furnished on for-sale listings) drop out
   * automatically via pruneNulls. */

  const property: Fact[] = pruneNulls([
    { label: 'Subtype', value: fmtCategorySubOrNull(listing.category_sub_cb) },
    { label: 'Lot area', value: fmtAreaOrNull(listing.estate_area), mono: true },
    { label: 'Garden area', value: fmtAreaOrNull(listing.garden_area), mono: true },
    { label: 'Floor', value: floor, mono: true },
  ]);

  const building: Fact[] = pruneNulls([
    { label: 'Building', value: capitalise(listing.building_type) },
    { label: 'Condition', value: capitalise(listing.condition) },
    { label: 'Energy class', value: listing.energy_rating, mono: true },
    {
      label: 'Ownership',
      value: listing.ownership ? fmtOwnership(listing.ownership) : null,
    },
    {
      label: 'Furnished',
      value: listing.furnished ? fmtFurnished(listing.furnished) : null,
    },
  ]);

  const amenities: Fact[] = pruneNulls([
    { label: 'Balcony', value: yesNo(listing.has_balcony) },
    { label: 'Terrace', value: yesNo(listing.terrace) },
    { label: 'Lift', value: yesNo(listing.has_lift) },
    { label: 'Cellar', value: yesNo(listing.cellar) },
    { label: 'Garage', value: yesNo(listing.garage) },
    { label: 'Parking', value: yesNo(listing.has_parking) },
    {
      label: 'Parking spaces',
      value: listing.parking_lots != null && listing.parking_lots > 0
        ? fmtParkingLots(listing.parking_lots)
        : null,
      mono: true,
    },
  ]);

  if (property.length === 0 && building.length === 0 && amenities.length === 0) {
    return null;
  }
  return (
    <div className="mt-8 space-y-7">
      {property.length > 0 && <FactsGrid title="Property" facts={property} />}
      {building.length > 0 && <FactsGrid title="Building" facts={building} />}
      {amenities.length > 0 && <FactsGrid title="Amenities" facts={amenities} />}
    </div>
  );
}

interface Fact {
  label: string;
  value: string | null;
  mono?: boolean;
}

function pruneNulls(facts: Fact[]): Fact[] {
  return facts.filter((f) => f.value != null);
}

function fmtAreaOrNull(n: number | null): string | null {
  return n == null ? null : fmtArea(n);
}

function fmtCategorySubOrNull(cb: number | null): string | null {
  if (cb == null) return null;
  const out = fmtCategorySub(cb);
  return out === '—' ? null : out;
}

function FactsGrid({ title, facts }: { title: string; facts: Fact[] }) {
  return (
    <div>
      <SectionLabel>{title}</SectionLabel>
      <dl className="mt-3 grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-x-6 gap-y-4">
        {facts.map((f) => (
          <div key={f.label}>
            <dt className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
              {f.label}
            </dt>
            <dd
              className={[
                'mt-1 text-sm text-[var(--color-ink)]',
                f.mono ? 'font-mono tabular-nums' : '',
              ].join(' ')}
            >
              {f.value}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Listing & price history (URLs · price chart · summary)                     */
/* -------------------------------------------------------------------------- */

function ListingHistoryBlock({
  listing,
  sources,
  snapshots,
}: {
  listing: ListingPublic;
  sources: PropertySource[];
  snapshots: ListingSnapshotPublic[];
}) {
  const urls = useMemo(() => listingUrlRows(sources, listing), [sources, listing]);
  // Date.now() is captured once at mount (not per render) and threaded into the
  // pure helpers so they stay deterministic (and unit-testable). Reading it per
  // render made `now` a fresh value every time, defeating the useMemo deps
  // below: `series` got a new reference on every one of the page's staggered
  // query resolutions, re-rendering PriceLineChart mid-measure and tripping
  // recharts' "rendered more hooks" (#310) crash.
  const [now] = useState(() => Date.now());
  const series = useMemo(
    () => buildPriceSeries(urls, snapshots, now),
    [urls, snapshots, now],
  );
  const stats = useMemo(
    () => summarizePriceHistory(urls, snapshots, listing.price_czk, now),
    [urls, snapshots, listing.price_czk, now],
  );

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <SectionLabel>Listing &amp; price history</SectionLabel>
        <p className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)] font-mono tabular-nums">
          {urls.length} {urls.length === 1 ? 'URL' : 'URLs'}
        </p>
      </div>

      <div className="mt-4 grid grid-cols-2 sm:grid-cols-5 gap-4">
        <Stat
          label="First seen"
          value={fmtShortDate(new Date(stats.firstSeenT).toISOString())}
          title={fmtAbsolute(new Date(stats.firstSeenT).toISOString())}
        />
        <Stat
          label="Last seen"
          value={stats.anyActive ? 'now' : fmtShortDate(new Date(stats.lastSeenT).toISOString())}
          title={fmtAbsolute(new Date(stats.lastSeenT).toISOString())}
        />
        <Stat label="Days on market" value={String(stats.days)} mono />
        <Stat label="Price changes" value={String(stats.changes)} mono />
        <Stat
          label="Price change"
          value={stats.pct == null ? '—' : fmtPct(stats.pct)}
          mono
          pct={stats.pct}
        />
      </div>

      {series.length > 0 && (
        <div className="mt-6">
          <ErrorBoundary
            label="price-chart"
            fallback={
              <div className="flex h-[230px] items-center justify-center rounded-[var(--radius-md)] border border-dashed border-[var(--color-rule)] text-sm text-[var(--color-ink-3)]">
                Price chart unavailable
              </div>
            }
          >
            <Suspense
              fallback={
                <div className="h-[230px] rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]" />
              }
            >
              <PriceLineChart series={series} />
            </Suspense>
          </ErrorBoundary>
        </div>
      )}

      <ul className="mt-6 space-y-2">
        {urls.map((u) => (
          <li
            key={u.id}
            className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper-2)] px-3 py-2"
          >
            <div className="flex items-center gap-2 min-w-0">
              <span className="text-sm text-[var(--color-ink)] capitalize">{u.source}</span>
              {u.id === listing.sreality_id ? (
                <span className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
                  this listing
                </span>
              ) : null}
              <UrlStatusPill active={u.isActive} />
            </div>
            <div className="flex items-center gap-3 text-[0.8rem] text-[var(--color-ink-3)] tabular-nums">
              <span className="font-mono text-[var(--color-ink-2)]">{fmtCzk(u.price)}</span>
              <span title={`${u.firstSeen} – ${u.lastSeen}`}>
                {fmtShortDate(u.firstSeen)} – {u.isActive ? 'now' : fmtShortDate(u.lastSeen)}
              </span>
              {u.url ? (
                <a
                  href={u.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-[var(--color-copper)] hover:text-[var(--color-copper-2)]"
                >
                  open ↗
                </a>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Stat({
  label,
  value,
  title,
  mono,
  pct,
}: {
  label: string;
  value: string;
  title?: string;
  mono?: boolean;
  pct?: number | null;
}) {
  const color =
    pct == null || pct === 0
      ? undefined
      : pct > 0
        ? 'var(--color-brick)'
        : 'var(--color-sage)';
  return (
    <div>
      <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        {label}
      </p>
      <p
        className={['mt-1 text-sm text-[var(--color-ink)]', mono ? 'font-mono tabular-nums' : ''].join(' ')}
        title={title}
        style={color ? { color } : undefined}
      >
        {value}
      </p>
    </div>
  );
}

function UrlStatusPill({ active }: { active: boolean }) {
  return (
    <span
      className={[
        'inline-block px-1.5 py-0.5 text-[0.6rem] tracking-wide uppercase rounded-[var(--radius-xs)] border border-[var(--color-rule)]',
        active ? 'text-[var(--color-ink-3)]' : 'text-[var(--color-ink-4)]',
      ].join(' ')}
    >
      {active ? 'active' : 'inactive'}
    </span>
  );
}

function fmtPct(pct: number): string {
  const sign = pct > 0 ? '+' : '';
  return `${sign}${pct.toLocaleString('cs-CZ', { maximumFractionDigits: 1 })} %`;
}


/* -------------------------------------------------------------------------- */
/* Freshness checks                                                           */
/* -------------------------------------------------------------------------- */

export function FreshnessBlock({
  sreality_id,
  checks,
}: {
  sreality_id: number;
  checks: ListingFreshnessCheckPublic[];
}) {
  const qc = useQueryClient();
  const count = checks.length;

  const verify = useMutation<VerifyFreshnessResult, Error>({
    mutationFn: () => verifyListingFreshness(sreality_id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['freshness', sreality_id] });
      qc.invalidateQueries({ queryKey: ['snapshots', sreality_id] });
      qc.invalidateQueries({ queryKey: ['listing', sreality_id] });
    },
  });

  return (
    <div>
      <div className="flex items-center justify-between gap-4">
        <SectionLabel>
          <span>Freshness checks</span>
          <span className="ml-2 font-mono tabular-nums text-[var(--color-ink-4)] tracking-normal">
            ({count})
          </span>
        </SectionLabel>
        <button
          type="button"
          onClick={() => verify.mutate()}
          disabled={verify.isPending}
          className="px-3 py-1 text-[0.78rem] rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {verify.isPending ? 'Ověřuji…' : 'Ověřit aktuálnost'}
        </button>
      </div>

      <VerifyResult mutation={verify} />

      {count === 0 ? (
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">
          No on-demand freshness checks recorded.
        </p>
      ) : (
        <div className="mt-3 border border-[var(--color-rule)] rounded-[var(--radius-md)] overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)] bg-[var(--color-paper-2)]">
                <th className="px-3 py-2 font-medium">Checked</th>
                <th className="px-3 py-2 font-medium">Outcome</th>
              </tr>
            </thead>
            <tbody>
              {[...checks]
                .sort((a, b) => new Date(b.checked_at).getTime() - new Date(a.checked_at).getTime())
                .map((c) => (
                  <tr key={c.id} className="border-t border-[var(--color-rule-soft)]">
                    <td className="px-3 py-2 text-[var(--color-ink-2)] cursor-help" title={fmtAbsolute(c.checked_at)}>
                      {fmtRelative(c.checked_at)}
                    </td>
                    <td className="px-3 py-2">
                      <OutcomeChip outcome={c.outcome} />
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function VerifyResult({
  mutation,
}: {
  mutation: ReturnType<
    typeof useMutation<VerifyFreshnessResult, Error>
  >;
}) {
  if (mutation.isPending) {
    return (
      <p className="mt-3 text-sm text-[var(--color-ink-3)]">
        Re-fetching the listing from the source…
      </p>
    );
  }
  if (mutation.isError) {
    const err = mutation.error;
    const msg = err instanceof ApiError ? err.message : err.message;
    return (
      <p className="mt-3 text-sm text-[var(--color-brick)]">
        Verification failed: {msg}
      </p>
    );
  }
  if (mutation.isSuccess) {
    const { outcome, what_changed } = mutation.data.data;
    return (
      <div className="mt-3 flex flex-wrap items-center gap-2 text-sm text-[var(--color-ink-2)]">
        <OutcomeChip outcome={outcome} />
        <span>{freshnessOutcomeMessage(outcome, what_changed)}</span>
      </div>
    );
  }
  return null;
}

function freshnessOutcomeMessage(
  outcome: FreshnessOutcome,
  whatChanged: string[],
): string {
  switch (outcome) {
    case 'unchanged':
      return 'Still listed — nothing changed since the last snapshot.';
    case 'updated':
      return whatChanged.length > 0
        ? `Still listed — updated: ${whatChanged.join(', ')}.`
        : 'Still listed — the listing was updated; a new snapshot was recorded.';
    case 'gone':
      return 'No longer listed — marked inactive.';
    case 'cached':
      return 'Recently verified — still considered fresh, no re-fetch needed.';
    case 'fetch_error':
      return 'Could not reach the source listing; nothing was changed.';
    default:
      return '';
  }
}

function OutcomeChip({ outcome }: { outcome: string }) {
  const lower = outcome.toLowerCase();
  let bg = 'var(--color-rule-soft)';
  let fg = 'var(--color-ink-2)';
  if (lower === 'unchanged') {
    bg = 'var(--color-sage-soft)';
    fg = 'var(--color-sage)';
  } else if (lower === 'updated' || lower === 'changed') {
    bg = 'var(--color-copper-soft)';
    fg = 'var(--color-copper)';
  } else if (
    lower === 'gone' ||
    lower === 'inactive' ||
    lower === 'error' ||
    lower === 'fetch_error'
  ) {
    bg = 'var(--color-brick-soft)';
    fg = 'var(--color-brick)';
  }
  return (
    <span
      className="inline-block px-2 py-0.5 text-[0.65rem] tracking-[0.14em] uppercase rounded-[var(--radius-xs)]"
      style={{ background: bg, color: fg }}
    >
      {outcome}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Outbound link                                                              */
/* -------------------------------------------------------------------------- */

function OutboundBlock({
  sreality_id,
  source,
}: {
  sreality_id: number;
  source?: PropertySource;
}) {
  // Prefer the listing's real source URL (any portal); fall back to sreality
  // for legacy rows where property_sources hasn't been populated.
  const href =
    source?.source_url
    ?? `https://www.sreality.cz/detail/pronajem/byt/x/x/${sreality_id}`;
  const label = source ? `Open on ${source.source}` : 'Open on sreality.cz';
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1.5 text-sm text-[var(--color-copper)] hover:text-[var(--color-copper-2)] transition-colors capitalize"
    >
      {label}
      <OutArrow />
    </a>
  );
}

/* -------------------------------------------------------------------------- */
/* Empty / 404 state                                                          */
/* -------------------------------------------------------------------------- */

function NoListingState({ id, reason }: { id: string | null; reason: 'invalid' | 'missing' }) {
  return (
    <Page>
      <Crumb />
      <div className="mt-12">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Not found
        </p>
        <h1
          className="mt-2 text-2xl"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          {reason === 'invalid'
            ? 'No listing requested'
            : (
              <>
                No listing with id{' '}
                <span className="font-mono tabular-nums text-[var(--color-ink-2)]">{id}</span>
              </>
            )}
        </h1>
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">
          The id may be wrong, or the record was never imported.
          <Link to="/browse" className="ml-1 text-[var(--color-copper)] hover:underline">
            Browse all listings
          </Link>
          .
        </p>
      </div>
    </Page>
  );
}

/* -------------------------------------------------------------------------- */
/* Helpers + glyphs                                                           */
/* -------------------------------------------------------------------------- */

function yesNo(v: boolean | null): string | null {
  if (v == null) return null;
  return v ? 'Yes' : 'No';
}

function capitalise(s: string | null): string | null {
  if (!s) return null;
  return s.charAt(0).toUpperCase() + s.slice(1);
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

function OutArrow() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
      <line
        x1="1"
        y1="9"
        x2="8.5"
        y2="1.5"
        stroke="currentColor"
        strokeWidth="1.25"
        strokeLinecap="round"
      />
      <polyline
        points="3.5,1.5 8.5,1.5 8.5,6.5"
        stroke="currentColor"
        strokeWidth="1.25"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
