/* THE PROPERTY PAGE (decision 11): one real property, one stable address,
 * `/property/:propertyId`.
 *
 * The header shows the property exactly as its Browse card does: the
 * `properties_public` row, whose advert fields are its canonical advert's and
 * whose physical facts are the first non-empty in the same order (migration 561,
 * decision 18). Photos, broker, price history, manual estimates and freshness
 * checks are that canonical advert's. The merged-adverts section is the only list
 * of adverts, each advert's own facts in its row; `?advert=<id>` opens that row.
 *
 * Every advert address ever handed out (`/listing/{source}/{native}`,
 * `/listing/{sreality_id}`, `/listing?property=`) is an alias: `AdvertRedirect`
 * resolves the advert's property and lands here with its row open. */
import { Suspense, useMemo, useState } from 'react';
import { Link, Navigate, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { ROUTES } from '@/lib/routes';
import {
  useNewEstimationModal,
  type NewEstimationPrefill,
} from '@/components/NewEstimationModal';
import { useExploreAreaModal } from '@/components/ExploreAreaModal';
import { listingTypeLabel } from '@/lib/enums';
import { usePageTitle } from '@/lib/pageTitle';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  fetchAdvertProperty,
  fetchProperty,
  fetchPropertySources,
  fetchPropertyStatusEvents,
  fetchSnapshotsForListings,
  fetchFreshnessChecksByListing,
  fetchImagesByListing,
  type PropertyPublic,
} from '@/lib/queries';
import { fetchListingBroker } from '@/lib/brokers';
import BrokerContactCard from '@/components/BrokerContactCard';
import {
  ApiError,
  fetchPropertyOrigins,
  verifyListingFreshness,
  type FreshnessOutcome,
  type VerifyFreshnessResult,
} from '@/lib/api';
import { useAuth } from '@/lib/auth';
import {
  fmtCzk,
  fmtPct,
  fmtRelative,
  fmtAbsolute,
  fmtShortDate,
} from '@/lib/format';
import type {
  ImagePublic,
  ListingPublic,
  ListingSnapshotPublic,
  ListingFreshnessCheckPublic,
  PropertySource,
  PropertyStatusEventPublic,
} from '@/lib/types';
import {
  buildPriceSeries,
  buildActiveWindows,
  priceChangeEvents,
  type PriceAdvert,
} from '@/lib/priceHistory';
import { timeLabelFull } from '@/lib/chartAxis';
import ErrorBoundary from '@/components/ErrorBoundary';
import Skeleton from '@/components/Skeleton';
import { ListingOverview } from '@/components/listing-detail/ListingOverview';
import PipelineToggle from '@/components/listing-detail/PipelineToggle';
import DismissButton from '@/components/DismissButton';
import CollectionSaveToggle from '@/components/listing-detail/CollectionSaveToggle';
import ExternalMapLinks from '@/components/listing-detail/ExternalMapLinks';
import { lazyChunk } from '@/lib/lazyChunk';
import { Hairline, SectionLabel } from '@/components/section';
import MergedAdvertsSection from '@/components/listing-detail/MergedAdvertsSection';
import { mergedAdvertsKeys, propertyKeys } from '@/lib/mergedAdverts';

const PriceLineChart = lazyChunk(
  () => import('@/components/listing-detail/PriceLineChart'),
);
const CurationBlock = lazyChunk(
  () => import('@/components/listing-detail/CurationBlock'),
);
const ManualEstimatesBlock = lazyChunk(
  () => import('@/components/listing-detail/ManualEstimatesBlock'),
);
const SoldCompsBlock = lazyChunk(
  () => import('@/components/listing-detail/SoldCompsBlock'),
);
const EstimationsBlock = lazyChunk(
  () => import('@/components/listing-detail/EstimationsBlock'),
);

export default function PropertyDetail() {
  const { propertyId: idParam } = useParams();
  const [params] = useSearchParams();
  const propertyId = idParam && /^\d+$/.test(idParam) ? Number(idParam) : null;
  const advertParam = Number(params.get('advert')) || null;

  const propertyQ = useQuery<PropertyPublic | null, Error>({
    queryKey: propertyKeys.row(propertyId),
    queryFn: () => fetchProperty(propertyId as number),
    enabled: propertyId != null,
    staleTime: 60_000,
  });
  const sourcesQ = useQuery<PropertySource[], Error>({
    queryKey: propertyKeys.sources(propertyId),
    queryFn: () => fetchPropertySources(propertyId as number),
    enabled: propertyId != null,
    staleTime: 60_000,
  });
  // Property-grain activity log for the price chart's inactive-period gaps
  // (migration 392) — see priceHistory.buildActiveWindows for how it's used.
  const statusEventsQ = useQuery<PropertyStatusEventPublic[], Error>({
    queryKey: ['property-status-events', propertyId],
    queryFn: () => fetchPropertyStatusEvents(propertyId as number),
    enabled: propertyId != null,
    staleTime: 60_000,
  });

  const property = propertyQ.data ?? null;
  // The canonical advert: its own snapshots, photos and freshness checks.
  const advertId = property?.id ?? null;
  const snapshotsQ = useQuery<ListingSnapshotPublic[], Error>({
    queryKey: ['snapshots', advertId],
    queryFn: () => fetchSnapshotsForListings([advertId as number]),
    enabled: advertId != null,
    staleTime: 60_000,
  });
  // listing_freshness_checks has no listing_id column at all (append-only
  // observability) — sreality_id-keyed forever.
  const checksQ = useQuery<ListingFreshnessCheckPublic[], Error>({
    queryKey: ['freshness', property?.sreality_id],
    queryFn: () => fetchFreshnessChecksByListing(property!.sreality_id!),
    enabled: property?.sreality_id != null,
    staleTime: 60_000,
  });
  const imagesQ = useQuery<ImagePublic[], Error>({
    queryKey: ['images', advertId],
    queryFn: () => fetchImagesByListing(advertId as number),
    enabled: advertId != null,
    staleTime: 5 * 60_000,
  });

  const sources = useMemo(() => sourcesQ.data ?? [], [sourcesQ.data]);
  const canonical = sources.find((s) => s.id === advertId) ?? null;

  // Bind the page's "New estimation" CTA to the canonical advert's stored portal
  // URL (migration 494; never reconstructed). MUST stay above the early returns
  // below — a hook after a conditional return is the React #310 trap.
  const newEstimationPrefill = useMemo<NewEstimationPrefill | undefined>(() => {
    if (!property || !canonical?.source_url) return undefined;
    const categoryMain =
      property.category_main === 'byt'
      || property.category_main === 'dum'
      || property.category_main === 'komercni'
        ? property.category_main
        : undefined;
    return {
      url: canonical.source_url,
      categoryMain,
      estimateKind: property.category_type === 'pronajem' ? 'rent' : 'sale',
    };
  }, [property, canonical]);

  // Tab title = "type · disposition · place" (the ONE place label the header
  // shows, migration 503). MUST stay above the early returns — same #310 trap.
  usePageTitle(
    property
      ? [listingTypeLabel(property), property.disposition, property.display_label?.trim() || null]
          .filter(Boolean)
          .join(' · ') || null
      : null,
  );

  if (propertyId == null) return <NotFoundState id={idParam ?? null} />;
  if (propertyQ.isLoading) return <LoadingState />;
  if (propertyQ.error) return <FailedState error={propertyQ.error} />;
  if (!property) return <SurvivorRedirect propertyId={propertyId} />;

  const images = imagesQ.data ?? [];
  const advertIds = sources.length > 0 ? sources.map((s) => s.id) : [property.id];

  return (
    <Page>
      <div className="flex items-center justify-between gap-3">
        <Crumb />
        {/* The page-level verbs, grouped top-right, in the same order as the
            controls on a Browse card: track this deal in the pipeline, save the
            property to a collection (monitoring rides on the collection), dismiss
            it, then run a new estimation. All property-grain (rule #18). */}
        <div className="flex items-center gap-2">
          <PipelineToggle property_id={propertyId} />
          <CollectionSaveToggle property_id={propertyId} />
          <DismissButton property_id={propertyId} variant="header" />
          <NewEstimationButton prefill={newEstimationPrefill} />
        </div>
      </div>
      <ListingOverview
        listing={property}
        images={images}
        imagesLoading={imagesQ.isLoading}
        mapFooter={
          /* Under the header map: our own market view first, then the external
             links — where exactly is this, and what does it sell for. */
          <div className="space-y-1.5">
            <ExploreAreaButton listing={property} images={images} />
            {property.lat != null && property.lng != null && (
              <ExternalMapLinks
                lat={property.lat}
                lng={property.lng}
                label={property.display_label}
              />
            )}
          </div>
        }
        estimatesSlot={
          /* The estimation chapter: MF reference + our runs, side by side —
             in the prime slot after the description. Renders nothing for a
             property with no estimable data. */
          <Suspense fallback={<Skeleton height={180} />}>
            <EstimationsBlock
              listing={property}
              listingIds={advertIds}
              prefill={newEstimationPrefill}
            />
          </Suspense>
        }
      />
      <BrokerVizitka listingId={property.id} />
      {sources.length > 0 && (
        <>
          <Hairline />
          <MergedAdvertsSection
            propertyId={propertyId}
            canonicalListingId={property.id}
            sources={sources}
            // The header already IS the canonical advert; any other opens its row.
            openAdvertId={advertParam !== property.id ? advertParam : null}
          />
        </>
      )}
      <Hairline />
      <Suspense fallback={<Skeleton height={120} />}>
        {/* Manual estimates + freshness checks are stored against the legacy
            sreality_id, so both are empty by construction for a non-sreality
            advert. Curation below is property-grain and stays rendered. */}
        {property.sreality_id != null && (
          <ManualEstimatesBlock sreality_id={property.sreality_id} />
        )}
      </Suspense>
      <Hairline />
      <Suspense fallback={<Skeleton height={160} />}>
        {/* The only REALIZED prices on the page — registered sales near this
            point, which is why they sit beside the estimates rather than with
            the portal history below. A sale is never linked to a property
            (rule #15 does not reach a transactions fact). */}
        {property.lat != null && property.lng != null && (
          /* Keyed on the property: every property route renders the SAME
             element, so property → property reuses this instance, and the
             block's radius and category seed are mount-time state. */
          <SoldCompsBlock
            key={propertyId}
            categoryMain={property.category_main}
            lat={property.lat}
            lng={property.lng}
            propertyId={propertyId}
          />
        )}
      </Suspense>
      <Hairline />
      <Suspense fallback={<Skeleton height={140} />}>
        <CurationBlock
          property_id={propertyId}
          sreality_id={property.sreality_id}
          listing_id={property.id}
        />
      </Suspense>
      <Hairline />
      <PriceHistoryBlock
        property={property}
        advert={canonical ?? property}
        snapshots={snapshotsQ.data ?? []}
        statusEvents={statusEventsQ.data ?? []}
      />
      <Hairline />
      {property.sreality_id != null && (
        <FreshnessBlock sreality_id={property.sreality_id} checks={checksQ.data ?? []} />
      )}
    </Page>
  );
}

/* -------------------------------------------------------------------------- */
/* Old addresses                                                              */
/* -------------------------------------------------------------------------- */

/* Every advert address ever handed out — `/listing/{source}/{native}` (emails,
   the extension's "Otevřít v aplikaci"), `/listing/{sreality_id}` (every older
   link) and `/listing?property=<id>` — lands on the property page, the advert's
   row open. The query string and hash ride along (`?run=` / `#estimations`). */
export function AdvertRedirect() {
  const { sreality_id: legacyParam, source, nativeId } = useParams();
  const location = useLocation();
  const legacyId = legacyParam && /^-?\d+$/.test(legacyParam) ? Number(legacyParam) : null;
  const key =
    legacyId != null
      ? { srealityId: legacyId }
      : source && nativeId
        ? { source, nativeId }
        : null;
  const q = useQuery({
    queryKey: ['advert-property', legacyId, source ?? null, nativeId ?? null],
    queryFn: () => fetchAdvertProperty(key!),
    enabled: key != null,
    staleTime: 60_000,
  });

  const params = new URLSearchParams(location.search);
  const propertyParam = params.get('property');
  params.delete('property');
  if (q.data) params.set('advert', String(q.data.id));
  const target =
    key != null
      ? (q.data?.property_id ?? null)
      : propertyParam && /^\d+$/.test(propertyParam)
        ? Number(propertyParam)
        : null;

  if (target != null) {
    const qs = params.toString();
    return (
      <Navigate
        replace
        to={`${ROUTES.property.build({ propertyId: target })}${qs ? `?${qs}` : ''}${location.hash}`}
      />
    );
  }
  if (q.isLoading) return <LoadingState />;
  if (q.error) return <FailedState error={q.error} />;
  return <NotFoundState id={legacyParam ?? nativeId ?? propertyParam} />;
}

/* A merged-away property is not in properties_public (active rows only). The
   origins route resolves its merge survivor (admin sessions), so an old property
   address follows its merge rather than dead-ending. */
function SurvivorRedirect({ propertyId }: { propertyId: number }) {
  const { isAdmin } = useAuth();
  const location = useLocation();
  const q = useQuery({
    queryKey: mergedAdvertsKeys.origins(propertyId),
    queryFn: () => fetchPropertyOrigins(propertyId),
    enabled: isAdmin,
    retry: false,
    staleTime: 60_000,
  });
  const survivor = q.data?.property_id;
  if (survivor != null && survivor !== propertyId) {
    return (
      <Navigate
        replace
        to={`${ROUTES.property.build({ propertyId: survivor })}${location.search}${location.hash}`}
      />
    );
  }
  if (isAdmin && q.isLoading) return <LoadingState />;
  return <NotFoundState id={String(propertyId)} />;
}

/* -------------------------------------------------------------------------- */
/* Broker vizitka (who is selling this, and how to reach them)                 */
/* -------------------------------------------------------------------------- */

/* The resolved broker behind the canonical advert: identity + firm + the two
   channels to reach them, one Hairline-separated block under the header (D7).
   Each other advert's broker is on its row of the merged-adverts section.

   THREE fetch outcomes, kept apart. Renders nothing for a listing whose broker
   isn't resolved yet — but a FAILED read is not that answer. fetchListingBroker
   returns null only for the two 404 bodies that mean "nothing is attributed here"
   and rethrows everything else (an expired session, a drifted VITE_API_BASE_URL, a
   5xx), so a bare `if (!b) return null` asserted "no broker" for every outage. That
   is how the PostgREST revocation hid here from 2026-07-12 to 2026-08-12; every
   other repointed broker surface now says so out loud. Gated on `!q.data` too, so a
   failed background refetch never replaces a good card with an error.

   The leading <Hairline /> lives INSIDE the component (unlike the sibling blocks,
   which get one from the page): most listings have no attributed broker, and a
   page-level rule would then stack two hairlines on nothing. */
function BrokerVizitka({ listingId }: { listingId: number }) {
  const q = useQuery({
    queryKey: ['listing-broker', listingId],
    queryFn: () => fetchListingBroker(listingId),
    staleTime: 60_000,
  });
  // W6: contact now rides on the attribution row itself (migration 419 put
  // primary_email / primary_phone on listing_broker_public), so the chained
  // ['broker-contact', brokerId] read by broker_id is gone. It was serialized
  // behind this one — it could not name its broker_id until this response landed —
  // which is why the vizitka used to paint its name, then reflow when the card
  // arrived. One read, one paint, and the "contact failed but identity didn't"
  // split state no longer exists to render.
  const b = q.data ?? null;

  if (q.isError && !b)
    return (
      <BrokerSection>
        <p className="mt-3 text-sm text-[var(--color-brick)]">
          Makléře se nepodařilo načíst
        </p>
      </BrokerSection>
    );
  if (!b) return null;

  return (
    <BrokerSection>
      <div className="mt-3 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <Link
            to={ROUTES.brokerDetail.build({ id: b.broker_id })}
            title={b.broker_display_name ?? undefined}
            className="inline-flex items-center gap-1.5 text-[1.05rem] leading-tight text-[var(--color-ink)] hover:text-[var(--color-copper-2)] transition-colors"
          >
            <span className="truncate">{b.broker_display_name ?? 'Neznámý makléř'}</span>
            <OutArrow />
          </Link>
          <p className="mt-1 text-sm text-[var(--color-ink-3)]">
            {b.broker_firm_label ?? 'nezávislý / neznámá kancelář'}
          </p>
        </div>
        {/* Unconditional now, and that is the point: the contact is part of the
            row we are already holding, so "we have a broker but not his contact"
            has stopped being a reachable state. The card still draws the three
            per-field cases itself (value / masked / none) — only a row we hold may
            claim an empty channel, and contactState keeps "admin-only" apart from
            "unreachable". `b` satisfies BrokerContactFields, which is all the card
            has ever consumed. */}
        <BrokerContactCard broker={b} />
      </div>
    </BrokerSection>
  );
}

function BrokerSection({ children }: { children: React.ReactNode }) {
  return (
    <>
      <Hairline />
      <div>
        <SectionLabel>Makléř</SectionLabel>
        {children}
      </div>
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* Layout primitives                                                          */
/* -------------------------------------------------------------------------- */

function Page({ children }: { children: React.ReactNode }) {
  // max-w-5xl matches the platform's other work surfaces (Estimations,
  // Buildings, Collections); the header uses the width for its two-column
  // identity + map layout.
  return (
    <div className="px-6 py-8 max-w-5xl mx-auto">{children}</div>
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
    <Link to={ROUTES.browse.build()} className={className}>
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

/* Opens the "Explore area" modal (full Browse focused on this property's ~5 km
   neighbourhood, pre-filtered to its category + disposition). Rendered under the
   header map via ListingOverview's mapFooter slot, which only shows when the
   listing has coordinates — the null guard here is defensive. */
function ExploreAreaButton({
  listing,
  images,
}: {
  listing: ListingPublic;
  images: ImagePublic[];
}) {
  const { open } = useExploreAreaModal();
  if (listing.lat == null || listing.lng == null) return null;
  const label = [listing.display_label, listing.disposition]
    .filter(Boolean)
    .join(' · ');
  return (
    <button
      type="button"
      onClick={() =>
        open({
          lat: listing.lat as number,
          lng: listing.lng as number,
          categoryMain: listing.category_main,
          categoryType: listing.category_type,
          disposition: listing.disposition,
          label: label || undefined,
          // The property we came FROM — already loaded on this page, passed
          // through (no refetch) to pin it on the modal map + show its photos
          // and facts in the modal's top panel.
          origin: { listing, images },
        })
      }
      className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 text-[0.8rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink-2)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper)] transition-colors"
      title="Explore the surrounding market on the map — same disposition, all layers"
    >
      <MapPinGlyph />
      <span>Explore area</span>
    </button>
  );
}

function MapPinGlyph() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path
        d="M8 1.5c2.5 0 4.5 2 4.5 4.5 0 3-4.5 8-4.5 8S3.5 9 3.5 6C3.5 3.5 5.5 1.5 8 1.5z"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
      <circle cx="8" cy="6" r="1.6" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  );
}

/* -------------------------------------------------------------------------- */
/* Price history (the canonical advert's own series · the property's span)     */
/* -------------------------------------------------------------------------- */

/* One rule per field (decision 18): the span (first / last seen, days on market)
   is the property's, any advert active; the price moves are the canonical
   advert's own series, the same `price_change_count` / `total_price_change_pct`
   Browse filters on. A price step never spans two adverts; each advert's own
   price and span are in its row of the merged-adverts section. */
function PriceHistoryBlock({
  property,
  advert,
  snapshots,
  statusEvents,
}: {
  property: PropertyPublic;
  advert: PriceAdvert;
  snapshots: ListingSnapshotPublic[];
  statusEvents: PropertyStatusEventPublic[];
}) {
  // Date.now() is captured once at mount (not per render) and threaded into the
  // pure helpers so they stay deterministic. A per-render `now` gave `series` a
  // new reference on every staggered query resolution, re-rendering
  // PriceLineChart mid-measure and tripping recharts' #310 crash.
  const [now] = useState(() => Date.now());
  const series = useMemo(
    () => buildPriceSeries(advert, snapshots, now),
    [advert, snapshots, now],
  );
  const firstSeenT = new Date(property.first_seen_at).getTime();
  const lastSeenT = new Date(property.last_seen_at).getTime();
  // Property-level "had >=1 active advert" windows — gaps the line for any
  // stretch the whole property went dark. Falls back to one window spanning the
  // property's life when statusEvents is empty (still loading).
  const activeWindows = useMemo(
    () =>
      buildActiveWindows(statusEvents, {
        start: firstSeenT,
        end: property.is_active ? now : lastSeenT,
      }),
    [statusEvents, firstSeenT, property.is_active, lastSeenT, now],
  );
  // Dated price moves, from the same series the chart draws: the exact day and
  // size of each step (readable even if the chart itself fails to render).
  const changes = useMemo(() => priceChangeEvents(series), [series]);

  return (
    <div>
      <SectionLabel>Price history</SectionLabel>

      <div className="mt-4 grid grid-cols-2 sm:grid-cols-5 gap-4">
        <Stat
          label="First seen"
          value={fmtShortDate(property.first_seen_at)}
          title={fmtAbsolute(property.first_seen_at)}
        />
        <Stat
          label="Last seen"
          value={property.is_active ? 'now' : fmtShortDate(property.last_seen_at)}
          title={fmtAbsolute(property.last_seen_at)}
        />
        <Stat label="Days on market" value={String(property.tom_days ?? '—')} mono />
        <Stat label="Price changes" value={String(property.price_change_count ?? 0)} mono />
        <Stat
          label="Price change"
          value={fmtPct(property.total_price_change_pct, { signed: true })}
          mono
          pct={property.total_price_change_pct}
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
              <PriceLineChart series={series} activeWindows={activeWindows} />
            </Suspense>
          </ErrorBoundary>
        </div>
      )}

      {changes.length > 0 && (
        <ul className="mt-4 flex flex-wrap gap-x-5 gap-y-1.5">
          {changes.map((c) => (
            <li
              key={`${c.seriesId}-${c.t}`}
              className="flex items-center gap-2 text-[0.78rem] tabular-nums"
            >
              <span className="font-mono text-[var(--color-ink-3)]">
                {timeLabelFull(c.t, 'day')}
              </span>
              <span className="font-mono text-[var(--color-ink-3)]">{fmtCzk(c.from)}</span>
              <span className="text-[var(--color-ink-4)]">→</span>
              <span className="font-mono text-[var(--color-ink)]">{fmtCzk(c.to)}</span>
              <span
                className="font-mono"
                style={{ color: c.pct > 0 ? 'var(--color-brick)' : 'var(--color-sage)' }}
              >
                {fmtPct(c.pct, { signed: true })}
              </span>
            </li>
          ))}
        </ul>
      )}
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
      // Bare prefixes: the page's keys are the property id and the advert's
      // surrogate id, neither of which this component knows.
      qc.invalidateQueries({ queryKey: ['property'] });
      qc.invalidateQueries({ queryKey: ['snapshots'] });
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
/* Loading / failed / 404 states                                              */
/* -------------------------------------------------------------------------- */

function LoadingState() {
  return (
    <Page>
      <Crumb />
      <div className="mt-8 text-sm text-[var(--color-ink-3)]">Loading…</div>
    </Page>
  );
}

function FailedState({ error }: { error: Error }) {
  return (
    <Page>
      <Crumb />
      <div className="mt-8 text-sm text-[var(--color-brick)]">Failed to load: {error.message}</div>
    </Page>
  );
}

function NotFoundState({ id }: { id: string | null }) {
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
          {id == null ? (
            'Nothing requested'
          ) : (
            <>
              No property or advert{' '}
              <span className="font-mono tabular-nums text-[var(--color-ink-2)]">{id}</span>
            </>
          )}
        </h1>
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">
          The id may be wrong, or the record was never imported.
          <Link to={ROUTES.browse.build()} className="ml-1 text-[var(--color-copper)] hover:underline">
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
