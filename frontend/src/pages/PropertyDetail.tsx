/* THE PROPERTY PAGE (decision 11): one real property, one stable address,
 * `/property/:propertyId`.
 *
 * The header shows the property exactly as its Browse card does: the
 * `properties_public` row, whose advert fields are its canonical advert's and
 * whose physical facts are the first non-empty in the same order (migration 561,
 * decision 18). Photos and freshness checks are that canonical advert's. The brokers are a list over the adverts (MS7), the price chart draws
 * every advert's own line (MS9), and a lowest active price that differs from the
 * header's is named under it (MS8). The merged-adverts section is the only list
 * of adverts, each advert's own facts in its row; `?advert=<id>` opens that row.
 *
 * Under the header (photos, identity, map) and the "Moje" line, the page is
 * tabs (operator ruling 2026-10-22, design A — components/listing-detail/
 * PropertyTabs): Přehled answers each question in one card, the other tabs hold
 * the full sections. The open tab is `?tab=`; older deep links keep landing on
 * their section's tab.
 *
 * Every advert address ever handed out (`/listing/{source}/{native}`,
 * `/listing/{sreality_id}`, `/listing?property=`) is an alias: `AdvertRedirect`
 * resolves the advert's property and lands here with its row open. */
import { Suspense, useMemo } from 'react';
import { Link, Navigate, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { ROUTES } from '@/lib/routes';
import { lastBrowsePath } from '@/lib/lastBrowseView';
import {
  useNewEstimationModal,
  type NewEstimationPrefill,
} from '@/components/NewEstimationModal';
import Spinner from '@/components/Spinner';
import { listingTypeLabel } from '@/lib/enums';
import { usePageTitle } from '@/lib/pageTitle';
import { useQuery } from '@tanstack/react-query';
import {
  fetchAdvertProperty,
  fetchListingsForListingIds,
  fetchProperty,
  fetchPropertySources,
  fetchSnapshotsForListings,
  fetchFreshnessChecksByListing,
  fetchImagesByListing,
  type PropertyPublic,
} from '@/lib/queries';
import { portalLabel } from '@/lib/portals';
import { fetchPropertyOrigins } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import { fmtCzk } from '@/lib/format';
import type {
  ImagePublic,
  ListingPublic,
  ListingSnapshotPublic,
  ListingFreshnessCheckPublic,
  PropertySource,
} from '@/lib/types';
import { lowestActivePrice, type PriceAdvert } from '@/lib/priceHistory';
import Skeleton from '@/components/Skeleton';
import {
  GalleryBlock,
  LocationMap,
  PropertyIdentity,
  type PriceNote,
} from '@/components/listing-detail/ListingOverview';
import { PropertyTabs, usePropertyTab } from '@/components/listing-detail/PropertyTabs';
import type { LowestActive } from '@/components/listing-detail/PropertyOverview';
import PipelineToggle from '@/components/listing-detail/PipelineToggle';
import DismissButton from '@/components/DismissButton';
import CollectionSaveToggle from '@/components/listing-detail/CollectionSaveToggle';
import ExternalMapLinks from '@/components/listing-detail/ExternalMapLinks';
import DownloadPropertyButton from '@/components/listing-detail/DownloadPropertyButton';
import { ActingBanner, EditAsOwnerToggle } from '@/components/listing-detail/EditAsOwner';
import { ActingAs, useEditAsOwnerShare } from '@/lib/acting';
import { useBoardSelection } from '@/lib/pipelineShares';
import { lazyChunk } from '@/lib/lazyChunk';
import MergedAdvertsSection from '@/components/listing-detail/MergedAdvertsSection';
import PriceHistoryBlock from '@/components/listing-detail/PriceHistoryBlock';
import FreshnessBlock from '@/components/listing-detail/FreshnessBlock';
import PhonePropertySkeleton from '@/components/phone/PhonePropertySkeleton';
import { mergedAdvertsKeys, parseLetters, propertyKeys } from '@/lib/mergedAdverts';
import { usePhoneShell } from '@/lib/phoneShell';
import type { PastedLinkState } from '@/pages/Link';

/* The freshness block moved to its own file (shared with the phone layout);
 * re-exported so older imports keep resolving. */
export { FreshnessBlock };
const CurationBlock = lazyChunk(
  () => import('@/components/listing-detail/CurationBlock'),
);
const SoldCompsBlock = lazyChunk(
  () => import('@/components/listing-detail/SoldCompsBlock'),
);
const EstimationsBlock = lazyChunk(
  () => import('@/components/listing-detail/EstimationsBlock'),
);
const AdvertNotFoundBody = lazyChunk(() => import('@/components/listing-detail/AdvertNotFound'));
const ValuoSection = lazyChunk(() => import('@/components/listing-detail/ValuoSection'));
/* The overview tab reads the estimate, Valuo and sold-sales queries, and the
 * header's photos and "Moje" line read their own: each its own chunk, so Browse's
 * first visit (scripts/bundle-budget.mjs) does not carry them. */
const PropertyOverview = lazyChunk(() => import('@/components/listing-detail/PropertyOverview'));
const PhotoMosaic = lazyChunk(() => import('@/components/listing-detail/PhotoMosaic'));
/* Opens through the neighbourhood tile's own opener, which reads the Stats chunk. */
const ExploreAreaButton = lazyChunk(() => import('@/components/listing-detail/ExploreAreaButton'));
const PropertyPhotos = lazyChunk(() => import('@/components/listing-detail/PropertyPhotos'));
/* The tag assign list rides in with it (scripts/bundle-budget.mjs). */
const TagsToggle = lazyChunk(() => import('@/components/listing-detail/TagsToggle'));
const AuditPanel = lazyChunk(() => import('@/components/listing-detail/AuditPanel'));

/* The phone layout is its own chunk: the desktop never downloads it, and the
 * first visit stays inside scripts/bundle-budget.mjs. */
const PhoneProperty = lazyChunk(() => import('@/components/phone/PhoneProperty'));

const NO_SNAPSHOTS: ListingSnapshotPublic[] = [];

export default function PropertyDetail() {
  const { propertyId: idParam } = useParams();
  const [params] = useSearchParams();
  const propertyId = idParam && /^\d+$/.test(idParam) ? Number(idParam) : null;
  const advertParam = Number(params.get('advert')) || null;
  // A review page's link to the split: the letters it proposes (MS18).
  const lettersParam = params.get('letters');
  const initialLetters = useMemo(() => parseLetters(lettersParam), [lettersParam]);

  const propertyQ = useQuery<PropertyPublic | null, Error>({
    queryKey: propertyKeys.row(propertyId),
    queryFn: ({ signal }) => fetchProperty(propertyId as number, { signal }),
    enabled: propertyId != null,
    staleTime: 60_000,
  });
  const sourcesQ = useQuery<PropertySource[], Error>({
    queryKey: propertyKeys.sources(propertyId),
    queryFn: ({ signal }) => fetchPropertySources(propertyId as number, { signal }),
    enabled: propertyId != null,
    staleTime: 60_000,
  });

  const property = propertyQ.data ?? null;
  // The canonical advert: its own photos and freshness checks.
  const advertId = property?.id ?? null;
  const sources = useMemo(() => sourcesQ.data ?? [], [sourcesQ.data]);
  const sourcesFailed = sourcesQ.isError && sourcesQ.data === undefined;
  const ids = useMemo(() => sources.map((s) => s.id), [sources]);
  // Every advert's snapshots (MS9); the canonical advert's if the list failed.
  const chartIds = useMemo(
    () => (sourcesFailed && advertId != null ? [advertId] : ids),
    [sourcesFailed, advertId, ids],
  );
  const snapshotsQ = useQuery<ListingSnapshotPublic[], Error>({
    queryKey: ['snapshots', chartIds],
    queryFn: ({ signal }) => fetchSnapshotsForListings(chartIds, { signal }),
    enabled: chartIds.length > 0,
    staleTime: 60_000,
  });
  // The merged-adverts section's read (same key, one request): each advert's deal type.
  const detailsQ = useQuery<Map<number, ListingPublic>, Error>({
    queryKey: mergedAdvertsKeys.listings(ids),
    queryFn: ({ signal }) => fetchListingsForListingIds(ids, { signal }),
    enabled: ids.length > 0,
    staleTime: 60_000,
  });
  // listing_freshness_checks has no listing_id column at all (append-only
  // observability) — sreality_id-keyed forever.
  const checksQ = useQuery<ListingFreshnessCheckPublic[], Error>({
    queryKey: ['freshness', property?.sreality_id],
    queryFn: ({ signal }) => fetchFreshnessChecksByListing(property!.sreality_id!, { signal }),
    enabled: property?.sreality_id != null,
    staleTime: 60_000,
  });
  const imagesQ = useQuery<ImagePublic[], Error>({
    queryKey: ['images', advertId],
    queryFn: ({ signal }) => fetchImagesByListing(advertId as number, { signal }),
    enabled: advertId != null,
    staleTime: 5 * 60_000,
  });

  const canonical = sources.find((s) => s.id === advertId) ?? null;
  // Canonical advert first; memoized, as a fresh array re-renders the chart mid-measure (#310).
  const chartAdverts = useMemo<PriceAdvert[]>(() => {
    if (!property) return [];
    if (sources.length === 0) return [property];
    return canonical ? [canonical, ...sources.filter((s) => s !== canonical)] : sources;
  }, [property, sources, canonical]);

  // MS8, display only, like with like: against the canonical advert's live price
  // (the header's stored one waits for the recompute). Only the property's deal
  // type counts, so a legacy sale-and-rent property never offers a rent as its
  // price; if the deal types cannot be read, every active advert counts.
  // undefined = not known yet, null = none cheaper.
  const lowest = useMemo<LowestActive | null | undefined>(() => {
    if (!property || (detailsQ.data === undefined && !detailsQ.isError)) return undefined;
    const deals = detailsQ.data;
    const low = lowestActivePrice(
      deals
        ? sources.filter((s) => deals.get(s.id)?.category_type === property.category_type)
        : sources,
      canonical ? canonical.price_czk : property.price_czk,
    );
    if (!low) return null;
    return { price: low.price, portals: low.sources.map((s) => portalLabel(s) ?? s).join(', ') };
  }, [property, detailsQ.data, detailsQ.isError, sources, canonical]);
  const priceNote = useMemo<PriceNote | undefined>(() => {
    if (!property || !lowest) return undefined;
    const perMonth = property.category_type === 'pronajem' ? ' / měs' : '';
    return {
      text: `Nejnižší aktivní cena: ${fmtCzk(lowest.price)}${perMonth} · ${lowest.portals}`,
      title:
        'Nejnižší cena mezi aktivními inzeráty — jen pro informaci; nevstupuje do ceny za m², výnosu, hlídání ani filtrů',
    };
  }, [property, lowest]);

  // Bind the page's "New estimation" CTA to the canonical advert (`id`, its
  // listings.id) — any portal's, so a property without a sreality advert is
  // estimable too. MUST stay above the early returns below — a hook after a
  // conditional return is the React #310 trap.
  const newEstimationPrefill = useMemo<NewEstimationPrefill | undefined>(() => {
    if (!property) return undefined;
    const categoryMain =
      property.category_main === 'byt'
      || property.category_main === 'dum'
      || property.category_main === 'komercni'
        ? property.category_main
        : undefined;
    return {
      listingId: property.id,
      categoryMain,
      estimateKind: property.category_type === 'pronajem' ? 'rent' : 'sale',
    };
  }, [property]);

  // Tab title = "type · disposition · place" (the ONE place label the header
  // shows, migration 503). MUST stay above the early returns — same #310 trap.
  usePageTitle(
    property
      ? [listingTypeLabel(property), property.disposition, property.display_label?.trim() || null]
          .filter(Boolean)
          .join(' · ') || null
      : null,
  );

  // The phone frame's own layout (#1767): reading + curation, no maps, no
  // estimation runs. Read above the early returns (#310); the reads stay here.
  const phone = usePhoneShell();

  // The active board (kept honest against GET /pipeline/shares while a shared
  // one is set) and, on an EDIT share with "Upravit jako" on, the share the
  // curation controls act through (design §3.5). Desktop only (the phone
  // never reads the lists); above the early returns (#310). Nothing is offered
  // or acted on until the server's lists confirm the remembered board: this
  // device may remember a board another sign-in picked.
  const { active: rememberedBoard, sharesQ } = useBoardSelection({ always: false, enabled: !phone });
  const activeBoard =
    rememberedBoard != null &&
    sharesQ.data?.shared_with_me.some(
      (s) => s.account_id === rememberedBoard.account_id && s.mode === rememberedBoard.mode,
    )
      ? rememberedBoard
      : null;
  const editAsShare = useEditAsOwnerShare();
  const actingShare = activeBoard != null ? editAsShare : null;

  /* The status pill opens the latest active advert on its portal (operator
     2026-10-23) — the one seen most recently, the canonical one on a tie; with
     none active, the advert seen last. Its stored URL, never rebuilt (rule 21). */
  const statusHref = useMemo(() => {
    const live = sources.filter((s) => s.is_active && s.source_url);
    const pool = live.length > 0 ? live : sources.filter((s) => s.source_url);
    const latest = [...pool].sort(
      (a, b) => b.last_seen_at.localeCompare(a.last_seen_at) || Number(b.id === advertId) - Number(a.id === advertId),
    )[0];
    return latest?.source_url ?? property?.source_url ?? null;
  }, [sources, advertId, property?.source_url]);

  // The open tab, from the address. Above the early returns (#310).
  const [tab, setTab] = usePropertyTab(property?.id ?? null);

  if (propertyId == null) return <NotFoundState id={idParam ?? null} />;
  if (propertyQ.isLoading) return <LoadingState />;
  if (propertyQ.error) return <FailedState error={propertyQ.error} />;
  if (!property) return <SurvivorRedirect propertyId={propertyId} />;

  const images = imagesQ.data ?? [];
  const advertIds = sources.length > 0 ? sources.map((s) => s.id) : [property.id];

  if (phone) {
    return (
      <Suspense fallback={<PhonePropertySkeleton />}>
      <PhoneProperty
        property={property}
        sources={sources}
        sourcesFailed={sourcesFailed}
        onRetrySources={() => void sourcesQ.refetch()}
        adDetails={detailsQ.data}
        images={images}
        imagesLoading={imagesQ.isLoading}
        chartAdverts={chartAdverts}
        snapshots={snapshotsQ.data ?? NO_SNAPSHOTS}
        checks={checksQ.data ?? []}
        priceNote={priceNote}
        advertIds={advertIds}
        openedAdvertId={advertParam}
      />
      </Suspense>
    );
  }

  return (
    <Page>
      <div className="flex items-center justify-between gap-3">
        <Crumb />
        {/* The page-level verbs, grouped top-right, in the same order as the
            controls on a Browse card: track this deal in the pipeline, save the
            property to a collection (monitoring rides on the collection), dismiss
            it — all property-grain (rule #18) — then download the ad (a PDF, the
            gallery's photos and my own) and run a new estimation. */}
        <div className="flex flex-wrap items-center justify-end gap-2">
          {activeBoard?.mode === 'edit' && (
            <EditAsOwnerToggle share={activeBoard} on={actingShare != null} />
          )}
          {/* Keyed by the identity: a menu or draft never carries over from
              myself to the owner, nor back. */}
          <ActingAs key={actingShare?.account_id ?? 'own'} share={actingShare}>
            <PipelineToggle property_id={propertyId} />
            <CollectionSaveToggle property_id={propertyId} />
            <Suspense fallback={<span className="inline-flex h-[1.9rem] w-24" aria-hidden />}>
              <TagsToggle property_id={propertyId} />
            </Suspense>
            <DismissButton property_id={propertyId} variant="header" />
            {/* Its zip packs the acting identity's own photos. */}
            <DownloadPropertyButton propertyId={propertyId} images={images} />
          </ActingAs>
          <NewEstimationButton prefill={newEstimationPrefill} />
        </div>
      </div>
      {actingShare && <ActingBanner share={actingShare} />}
      {/* The header: the canonical advert's photos left; the property as its
          Browse card shows it, and where it is, right. */}
      <div className="mt-4 grid items-start gap-x-8 gap-y-6 lg:items-stretch lg:grid-cols-[minmax(0,1.2fr)_minmax(340px,1fr)]">
        <Suspense fallback={<div className="h-[260px] sm:h-[360px] lg:h-full lg:min-h-[430px]" />}>
          <PhotoMosaic
            images={images}
            loading={imagesQ.isLoading}
            isActive={property.is_active}
            onAll={() => setTab('fotky')}
          />
        </Suspense>
        <div className="min-w-0 space-y-5">
          <PropertyIdentity listing={property} priceNote={priceNote} statusHref={statusHref} />
          <LocationMap
            listing={property}
            heightClass="h-[170px]"
            footer={
              /* Our own look at the neighbourhood first (the twin of the
                 neighbourhood tile's "Prozkoumat okolí"), then where exactly
                 this is, elsewhere. */
              property.lat != null && property.lng != null ? (
                <div className="space-y-1.5">
                  <Suspense fallback={<div className="h-[33px]" aria-hidden />}>
                    <ExploreAreaButton property={property} images={images} />
                  </Suspense>
                  <ExternalMapLinks lat={property.lat} lng={property.lng} label={property.display_label} />
                </div>
              ) : null
            }
          />
        </div>
      </div>
      {/* The operator's notes (and the artifact link), read and written in place
          on every tab — what has been decided comes before the numbers (rule #18:
          property-grain). The tags are the header's Štítky button. */}
      <Suspense fallback={<div className="mt-6 h-[120px]" />}>
        <ActingAs key={actingShare?.account_id ?? 'own'} share={actingShare}>
          <CurationBlock property_id={propertyId} listing_id={property.id} />
        </ActingAs>
      </Suspense>
      {/* Keyed on the property: which tabs were opened is per property. */}
      <PropertyTabs
        key={propertyId}
        tab={tab}
        onSelect={setTab}
        counts={{ fotky: images.length, inzeraty: sources.length }}
        panels={{
          prehled: (
            <Suspense fallback={<Skeleton height={320} />}>
              <PropertyOverview
                property={property}
                sources={sources}
                sourcesFailed={sourcesFailed}
                onRetrySources={() => void sourcesQ.refetch()}
                advertIds={advertIds}
                chartAdverts={chartAdverts}
                snapshots={snapshotsQ.data ?? NO_SNAPSHOTS}
                lowest={lowest ?? null}
                images={images}
                onOpen={setTab}
              />
            </Suspense>
          ),
          fotky: (
            /* My own photos (Photos W2), then the canonical advert's. */
            <div className="space-y-10">
              <Suspense fallback={<Skeleton height={120} />}>
                <ActingAs key={actingShare?.account_id ?? 'own'} share={actingShare}>
                  <PropertyPhotos property_id={propertyId} lang="cs" />
                </ActingAs>
              </Suspense>
              <GalleryBlock
                images={images}
                isActive={property.is_active}
                loading={imagesQ.isLoading}
                label="Fotky z inzerátu"
                emptyText="Žádné fotky."
              />
            </div>
          ),
          hodnota: (
            /* The operator's order (2026-10-23): how the ask moved; Valuo's paid
               outside valuation; the registered sales near this point (reas.cz,
               the only REALIZED prices on the page — a sale is never linked to a
               property, rule #15); then our estimation chapter (MF reference +
               our runs), never mixed with Valuo's (rule 12). */
            <div className="space-y-12">
              <PriceHistoryBlock
                property={property}
                adverts={chartAdverts}
                snapshots={snapshotsQ.data ?? NO_SNAPSHOTS}
                label="Vývoj ceny"
              />
              <Suspense fallback={<Skeleton height={140} />}>
                <ValuoSection
                  property={property}
                  listingIds={advertIds}
                  adUrls={sources.map((s) => s.source_url)}
                  bare
                />
              </Suspense>
              <Suspense fallback={<Skeleton height={160} />}>
                {property.lat != null && property.lng != null ? (
                  /* Keyed on the property: the block's radius and category seed
                     are mount-time state. */
                  <SoldCompsBlock
                    key={propertyId}
                    categoryMain={property.category_main}
                    lat={property.lat}
                    lng={property.lng}
                    propertyId={propertyId}
                  />
                ) : (
                  <p className="text-sm text-[var(--color-ink-3)]">
                    Bez souřadnic nelze hledat prodeje v okolí.
                  </p>
                )}
              </Suspense>
              <Suspense fallback={<Skeleton height={180} />}>
                <EstimationsBlock
                  listing={property}
                  listingIds={advertIds}
                  prefill={newEstimationPrefill}
                  label="Odhady"
                  bare
                />
              </Suspense>
            </div>
          ),
          inzeraty:
            sources.length > 0 ? (
              /* Keyed on the property: the split letters, reason and refusal
                 are mount-time state, and property → property reuses this
                 instance. */
              <MergedAdvertsSection
                key={propertyId}
                propertyId={propertyId}
                canonicalListingId={property.id}
                sources={sources}
                // The header already IS the canonical advert; any other opens its row.
                openAdvertId={advertParam !== property.id ? advertParam : null}
                initialLetters={initialLetters}
              />
            ) : sourcesFailed ? (
              <p className="text-sm text-[var(--color-brick)]">
                Inzeráty se nepodařilo načíst
                <button
                  type="button"
                  onClick={() => void sourcesQ.refetch()}
                  className="ml-3 font-medium underline underline-offset-2 hover:no-underline"
                >
                  Zkusit znovu
                </button>
              </p>
            ) : (
              <Skeleton height={120} />
            ),
          audit: (
            <Suspense fallback={<Skeleton height={160} />}>
              <AuditPanel property={property} sources={sources} checks={checksQ.data ?? []} />
            </Suspense>
          ),
        }}
      />
    </Page>
  );
}

/* -------------------------------------------------------------------------- */
/* Old addresses                                                              */
/* -------------------------------------------------------------------------- */

const ATTACHING_POLL_MS = 2_000;

/* Every advert address ever handed out — `/listing/{source}/{native}` (emails,
   the extension's "Otevřít v aplikaci"), `/listing/{sreality_id}` (every older
   link) and `/listing?property=<id>` — lands on the property page, the advert's
   row open. The query string and hash ride along (`?run=` / `#estimations`).
   An advert we hold whose property is not born yet (a new row waits for the
   maintenance lane, ~30 s) is ATTACHING, not missing: re-read every 2 s. */
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
    queryFn: ({ signal }) => fetchAdvertProperty(key!, { signal }),
    enabled: key != null,
    staleTime: 60_000,
    refetchInterval: (query) =>
      query.state.data != null && query.state.data.property_id == null ? ATTACHING_POLL_MS : false,
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
  if (q.data && q.data.property_id == null) return <AttachingState />;
  if (q.error) return <FailedState error={q.error} />;
  if (source && nativeId) {
    return (
      <AdvertNotFound
        source={source}
        nativeId={nativeId}
        pastedUrl={(location.state as Partial<PastedLinkState> | null)?.pastedUrl ?? null}
      />
    );
  }
  return <NotFoundState id={legacyParam ?? nativeId ?? propertyParam} />;
}

/* The advert is ours, its property is on the way (AdvertRedirect polls). */
function AttachingState() {
  const phone = usePhoneShell();
  const line = (
    <p role="status" className="flex items-center gap-2 text-sm text-[var(--color-ink-2)]">
      <Spinner size={12} />
      <span>Zakládám nemovitost…</span>
    </p>
  );
  if (phone) return <div className="px-4 py-8">{line}</div>;
  return (
    <Page>
      <Crumb />
      <div className="mt-8">{line}</div>
    </Page>
  );
}

/* A portal advert we hold no row for: one state for both shells, its body its
   own chunk (components/listing-detail/AdvertNotFound). */
function AdvertNotFound(props: { source: string; nativeId: string; pastedUrl: string | null }) {
  const phone = usePhoneShell();
  const body = (
    <Suspense fallback={null}>
      <AdvertNotFoundBody {...props} heading={phone ? 'h2' : 'h1'} />
    </Suspense>
  );
  if (phone) return <div className="px-4 py-8">{body}</div>;
  return (
    <Page>
      <Crumb />
      <div className="mt-12">{body}</div>
    </Page>
  );
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
/* Layout primitives                                                          */
/* -------------------------------------------------------------------------- */

function Page({ children }: { children: React.ReactNode }) {
  // One step wider than the other work surfaces (max-w-5xl): the header sets
  // the photos beside the identity + map column.
  return (
    <div className="px-6 py-8 max-w-6xl mx-auto">{children}</div>
  );
}

/* A history entry behind this page = step back to it (the exact Browse URL).
 * None (a new tab, a map pin's plain link, a pasted address) = the Browse view
 * this browser last had, not a bare, unfiltered /browse. */
export function Crumb() {
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
    <Link to={lastBrowsePath()} className={className}>
      <BackArrow />
      <span>Back to browse</span>
    </Link>
  );
}

function NewEstimationButton({ prefill }: { prefill?: NewEstimationPrefill }) {
  const { open } = useNewEstimationModal();
  if (!prefill) return null;
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

/* -------------------------------------------------------------------------- */
/* Loading / failed / 404 states                                              */
/* -------------------------------------------------------------------------- */

/* The phone frame draws its own back affordance, so its states drop the crumb. */
function LoadingState() {
  const phone = usePhoneShell();
  if (phone) return <PhonePropertySkeleton />;
  return (
    <Page>
      <Crumb />
      <div className="mt-8 text-sm text-[var(--color-ink-3)]">Loading…</div>
    </Page>
  );
}

function FailedState({ error }: { error: Error }) {
  const phone = usePhoneShell();
  if (phone) {
    return (
      <div className="px-4 py-8 text-[14px] text-[var(--color-brick)]">
        Nepodařilo se načíst: {error.message}
      </div>
    );
  }
  return (
    <Page>
      <Crumb />
      <div className="mt-8 text-sm text-[var(--color-brick)]">Failed to load: {error.message}</div>
    </Page>
  );
}

function NotFoundState({ id }: { id: string | null }) {
  const phone = usePhoneShell();
  if (phone) {
    return (
      <div className="px-4 py-8">
        <p className="text-[0.7rem] font-medium uppercase tracking-[0.18em] text-[var(--color-ink-3)]">
          Nenalezeno
        </p>
        <h2 className="mt-2 font-display text-[22px] font-medium leading-tight text-[var(--color-ink)]">
          {id == null ? 'Nic nevybráno' : 'Tuto nemovitost nemáme'}
        </h2>
        {id != null && (
          <p className="mt-2 break-all font-mono text-[12px] tabular-nums text-[var(--color-ink-2)]">{id}</p>
        )}
      </div>
    );
  }
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
