import { Suspense, lazy, useLayoutEffect, useMemo, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import {
  useNewEstimationModal,
  type NewEstimationPrefill,
} from '@/components/NewEstimationModal';
import { useExploreAreaModal } from '@/components/ExploreAreaModal';
import { placePrimary } from '@/lib/placeLabel';
import { listingTypeLabel } from '@/lib/enums';
import { usePageTitle } from '@/lib/pageTitle';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  fetchListingById,
  fetchListingBySreality,
  fetchListingIdByNaturalKey,
  fetchPropertyReprNaturalKey,
  fetchPropertySources,
  fetchPropertyMf,
  fetchSnapshotsForListings,
  fetchFreshnessChecksByListing,
  fetchImagesByListing,
  type PropertyMf,
} from '@/lib/queries';
import { fetchListingBroker } from '@/lib/brokers';
import {
  ApiError,
  getDedupAudit,
  verifyListingFreshness,
  type FreshnessOutcome,
  type VerifyFreshnessResult,
} from '@/lib/api';
import {
  fmtCzk,
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
} from '@/lib/types';
import {
  listingUrlRows,
  buildPriceSeries,
  summarizePriceHistory,
} from '@/lib/priceHistory';
import { portalShort, srealityListingUrl } from '@/lib/portals';
import ErrorBoundary from '@/components/ErrorBoundary';
import { ListingOverview } from '@/components/listing-detail/ListingOverview';
import PipelineToggle from '@/components/listing-detail/PipelineToggle';
import { listingCanonicalPath, listingRowPath } from '@/lib/listingUrl';

const PriceLineChart = lazy(
  () => import('@/components/listing-detail/PriceLineChart'),
);
const CurationBlock = lazy(
  () => import('@/components/listing-detail/CurationBlock'),
);
const ManualEstimatesBlock = lazy(
  () => import('@/components/listing-detail/ManualEstimatesBlock'),
);
const EstimationsBlock = lazy(
  () => import('@/components/listing-detail/EstimationsBlock'),
);

export default function ListingDetail() {
  const { sreality_id: idParam, source: natSourceParam, nativeId: natIdParam } =
    useParams();
  const location = useLocation();
  const navigate = useNavigate();

  // Legacy/resolver route /listing/{id}: sreality_id is negative for non-sreality
  // portals (synthetic id seq, migration 097). Kept forever (deep links, bookmarks) —
  // and stays a single round trip forever: the URL literally IS the sreality_id, so
  // there's no forward-compat concern resolving it (a listing only ever gets a
  // legacy numeric URL when it has a sreality_id to put in it).
  const legacyId = idParam && /^-?\d+$/.test(idParam) ? Number(idParam) : null;

  // In-SPA navs (Browse cards/table) seed the repr child's surrogate id via Link
  // `state`, so the canonical route can skip the natural-key round trip below and
  // load the listing directly. Cold loads / shared links / map popups (a raw
  // <a href> can't carry router state) have no seed and fall back to the resolver.
  const stateListingId =
    (location.state as { listingId?: number } | null)?.listingId ?? null;

  // Canonical route /listing/{source}/{native}: resolve the natural key
  // (migration 091) to the listing's SURROGATE id (R2 Phase C cutover — was
  // sreality_id, which a future non-sreality row created after Gate 2 may not
  // have at all), then reuse the id-keyed loaders. Disabled when `state` already
  // carries the surrogate id (in-SPA nav fast path).
  const natKeyQ = useQuery<number | null, Error>({
    queryKey: ['listing-natkey', natSourceParam, natIdParam],
    queryFn: () =>
      fetchListingIdByNaturalKey(natSourceParam as string, natIdParam as string),
    enabled: !!natSourceParam && !!natIdParam && stateListingId == null,
    staleTime: 60_000,
  });
  // The surrogate id the id-keyed loaders use: the seeded one when present, else
  // whatever the natural-key resolver returned. Both name the SAME row — the
  // Browse row that supplied the canonical URL also supplied its `listing_id`, so
  // the seed can never disagree with what the resolver would find for that key.
  const resolvedListingId = stateListingId ?? natKeyQ.data ?? null;
  const unresolved = legacyId == null && resolvedListingId == null;

  // /listing?property=ID (the dedup merge feed links this) → resolve the
  // property's representative listing and redirect to its detail page. Only when
  // neither the legacy nor the canonical route matched. Resolve to the repr's
  // NATURAL KEY and redirect to the CANONICAL route — never to listingPath(id):
  // the surrogate id and sreality_id spaces overlap (~435), so the legacy route
  // would load the wrong listing, and a post-Gate-2 repr may have no sreality_id
  // at all (which is why the old sreality-id resolver dead-ended to "not found").
  const propertyParam = new URLSearchParams(location.search).get('property');
  const propertyId =
    legacyId == null && !natSourceParam && propertyParam && /^\d+$/.test(propertyParam)
      ? Number(propertyParam)
      : null;
  const reprQ = useQuery<{ source: string; source_id_native: string } | null, Error>({
    queryKey: ['property-repr', propertyId],
    queryFn: () => fetchPropertyReprNaturalKey(propertyId as number),
    enabled: propertyId != null,
    staleTime: 60_000,
  });
  useLayoutEffect(() => {
    if (reprQ.data != null) {
      navigate(
        listingCanonicalPath(reprQ.data.source, reprQ.data.source_id_native),
        { replace: true },
      );
    }
  }, [reprQ.data, navigate]);

  // Two shapes converging on ListingPublic: the legacy route fetches directly by
  // sreality_id (one round trip, unchanged); the canonical route reuses the id
  // natKeyQ just resolved. Never both — legacyId and the natural-key params are
  // mutually exclusive route matches.
  const listingQ = useQuery<ListingPublic | null, Error>({
    queryKey: ['listing', legacyId, resolvedListingId],
    queryFn: () =>
      legacyId != null
        ? fetchListingBySreality(legacyId)
        : fetchListingById(resolvedListingId as number),
    enabled: legacyId != null || resolvedListingId != null,
    staleTime: 60_000,
  });

  const sourcesQ = useQuery<{ property_id: number | null; sources: PropertySource[] }, Error>({
    queryKey: ['property-sources', listingQ.data?.id],
    queryFn: () => fetchPropertySources(listingQ.data!.id),
    enabled: !!listingQ.data,
    staleTime: 60_000,
  });

  // Canonicalize the legacy numeric route to /listing/{source}/{native} once the
  // listing + its sources load, so the negative synthetic id disappears from the
  // URL bar. Query string + hash are preserved (?run= / #anchor deep links). Only
  // from the legacy route, and only when the natural key is known — a NULL one
  // (a pre-migration-314 straggler) simply stays on the still-valid legacy URL.
  // Match THIS listing's source row by the surrogate id (never null, never
  // overlaps), not sreality_id — `null === null` would wrongly match the first
  // null-sreality sibling. (Only reached from the legacy route, where sreality_id
  // is present, but the surrogate match is correct regardless.)
  const canonicalNative = (sourcesQ.data?.sources ?? []).find(
    (s) => s.id === listingQ.data?.id,
  )?.source_id_native;
  useLayoutEffect(() => {
    if (legacyId == null || !listingQ.data || !canonicalNative) return;
    navigate(
      listingCanonicalPath(listingQ.data.source, canonicalNative) +
        location.search +
        location.hash,
      { replace: true },
    );
  }, [
    legacyId,
    listingQ.data,
    canonicalNative,
    location.search,
    location.hash,
    navigate,
  ]);

  // PROPERTY-grain MF (the golden record): the one figure for the real-world
  // property, so the header shows the same MF whichever portal's advert opened
  // it — not the subject listing's possibly-under-stated per-advert parse.
  const propPid = sourcesQ.data?.property_id ?? null;
  const propertyMfQ = useQuery<PropertyMf | null, Error>({
    queryKey: ['property-mf', propPid],
    queryFn: () => fetchPropertyMf(propPid as number),
    enabled: propPid != null,
    staleTime: 60_000,
  });

  // childIds = the property's children's sreality_ids, for the (sreality-keyed)
  // EstimationsBlock lookup. Post-Gate-2 a non-sreality child has none, so these
  // are null-filtered — that block simply won't find runs for a null-sreality
  // child (a separate, sreality-keyed cutover).
  const childIds = (sourcesQ.data?.sources ?? [])
    .map((s) => s.sreality_id)
    .filter((x): x is number => x != null);

  // Cross-source price history: snapshots across every child of the property,
  // falling back to just this listing for singletons / until sources load. Keyed
  // on the SURROGATE listing_id (fetchSnapshotsForListings, migration 343) — the
  // children's ids come from property_sources_public.id and the singleton
  // fallback from the already-loaded listing's own surrogate id, both NEVER null,
  // so a null-sreality listing's chart no longer silently empties on `[null]`.
  const snapshotListingIds = (() => {
    const ids = (sourcesQ.data?.sources ?? [])
      .map((s) => s.id)
      .filter((x): x is number => x != null);
    if (ids.length > 0) return [...ids].sort((a, b) => a - b);
    return listingQ.data != null ? [listingQ.data.id] : [];
  })();

  const snapshotsQ = useQuery<ListingSnapshotPublic[], Error>({
    queryKey: ['snapshots', snapshotListingIds],
    queryFn: () => fetchSnapshotsForListings(snapshotListingIds),
    enabled: snapshotListingIds.length > 0 && !!listingQ.data,
    staleTime: 60_000,
  });

  // listing_freshness_checks has no listing_id column at all (append-only
  // observability, not an R2 carrier) — stays sreality_id-keyed forever, read from
  // the already-loaded listing.
  const checksQ = useQuery<ListingFreshnessCheckPublic[], Error>({
    queryKey: ['freshness', listingQ.data?.sreality_id],
    queryFn: () => fetchFreshnessChecksByListing(listingQ.data!.sreality_id),
    enabled: !!listingQ.data,
    staleTime: 60_000,
  });

  const imagesQ = useQuery<ImagePublic[], Error>({
    queryKey: ['images', listingQ.data?.id],
    queryFn: () => fetchImagesByListing(listingQ.data!.id),
    enabled: !!listingQ.data,
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
      (s) => s.id === listing.id,
    );
    const url =
      currentSource?.source_url
      ?? (listing.source === 'sreality'
        ? (srealityListingUrl(listing.sreality_id, {
            categoryType: listing.category_type,
            categoryMain: listing.category_main,
            categorySubCb: listing.category_sub_cb,
          }) ?? undefined)
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

  // Tab title = "type · disposition · street-or-city" once the listing loads
  // (falls back to the route's "Listing" handle while loading / on the
  // ?property redirect). Location prefers the parsed street, else the
  // municipality (obec), else the richer place label. MUST stay above the early
  // returns — same React #310 hook-order trap as above.
  usePageTitle(
    listingQ.data
      ? [
          listingTypeLabel(listingQ.data),
          listingQ.data.disposition,
          listingQ.data.street?.trim()
            || listingQ.data.obec?.trim()
            || placePrimary(listingQ.data),
        ]
          .filter(Boolean)
          .join(' · ') || null
      : null,
  );

  if (unresolved) {
    // Resolving ?property=ID → redirect, or resolving the canonical
    // /listing/{source}/{native} natural key → surrogate id (both handled above).
    // Show a loading state while either resolves; only "not found" once we know
    // there's no such property/listing.
    const resolvingProperty =
      propertyId != null && (reprQ.isLoading || reprQ.data != null);
    const resolvingNatural =
      !!natSourceParam && !!natIdParam && stateListingId == null && natKeyQ.isLoading;
    if (resolvingProperty || resolvingNatural) {
      return (
        <Page>
          <Crumb />
          <div className="mt-8 text-sm text-[var(--color-ink-3)]">Loading…</div>
        </Page>
      );
    }
    return <NoListingState id={idParam ?? natIdParam ?? propertyParam} reason="invalid" />;
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

  // Property-grain figures (MF / estimate) are built on the canonical asking
  // price; flag any ACTIVE sibling advert listed at a different number so the
  // operator knows the same flat is on the market at >1 price.
  const goldenPrice = propertyMfQ.data?.price_czk ?? null;
  const priceDivergentSiblings =
    goldenPrice != null
      ? sources.filter(
          (s) => s.is_active && s.price_czk != null && s.price_czk !== goldenPrice,
        )
      : [];
  const priceDivergence =
    goldenPrice != null && priceDivergentSiblings.length > 0
      ? {
          usedPrice: goldenPrice,
          siblings: priceDivergentSiblings.map((s) => ({
            source: s.source,
            price_czk: s.price_czk as number,
          })),
        }
      : null;

  return (
    <Page>
      <div className="flex items-center justify-between gap-3">
        <Crumb />
        {/* The two page-level deal verbs, grouped top-right: track this deal in
            the pipeline (the ★, same contract as the Browse-card bookmark) and
            run a new estimation. The pipeline toggle needs the property_id,
            which resolves from the sources query. */}
        <div className="flex items-center gap-2">
          {sourcesQ.data?.property_id != null && (
            <PipelineToggle property_id={sourcesQ.data.property_id} />
          )}
          <NewEstimationButton prefill={newEstimationPrefill} />
        </div>
      </div>
      <ListingOverview
        listing={listing}
        images={images}
        imagesLoading={imagesQ.isLoading}
        headerExtras={
          /* Portal links live at the top — jumping out to the source listing
             is a first-class action. One row: a chip per portal observation,
             plus the active-sibling alert when this record is delisted.
             Rendered inside the header grid so the map starts at the top. */
          <div className="flex flex-wrap items-center gap-2">
            <PortalLinksRow listing={listing} sources={sources} />
            <MergeDecisionsChip
              propertyId={sourcesQ.data?.property_id ?? null}
              multiSource={sources.length > 1}
            />
            <LatestActiveLink listing={listing} sources={sources} />
            <BrokerChip listingId={listing.id} />
          </div>
        }
        mapFooter={<ExploreAreaButton listing={listing} images={images} />}
        estimatesSlot={
          /* The estimation chapter: MF reference + our runs, side by side —
             in the prime slot after the description (the map moved into the
             header). Renders nothing for listings with no estimable data. */
          <Suspense fallback={null}>
            <EstimationsBlock
              listing={listing}
              listingIds={childIds.length > 0 ? childIds : [listing.sreality_id]}
              propertyMf={propertyMfQ.data ?? null}
              priceDivergence={priceDivergence}
              prefill={newEstimationPrefill}
            />
          </Suspense>
        }
      />
      <Hairline />
      <Suspense fallback={null}>
        <ManualEstimatesBlock sreality_id={listing.sreality_id} />
      </Suspense>
      <Hairline />
      <Suspense fallback={null}>
        {sourcesQ.data?.property_id != null && (
          <CurationBlock
            property_id={sourcesQ.data.property_id}
            sreality_id={listing.sreality_id}
            listing_id={listing.id}
          />
        )}
      </Suspense>
      <Hairline />
      <ListingHistoryBlock listing={listing} sources={sources} snapshots={snapshots} />
      <Hairline />
      <FreshnessBlock sreality_id={listing.sreality_id} checks={checks} />
    </Page>
  );
}

/* -------------------------------------------------------------------------- */
/* Portal links — one chip per portal observation, at the top of the page     */
/* -------------------------------------------------------------------------- */

function PortalLinksRow({
  listing,
  sources,
}: {
  listing: ListingPublic;
  sources: PropertySource[];
}) {
  const urls = useMemo(() => listingUrlRows(sources, listing), [sources, listing]);
  const linkable = urls.filter((u) => u.url != null);
  if (linkable.length === 0) return null;
  // Bare chips — the header's extras row provides the flex container.
  return (
    <>
      {linkable.map((u) => (
        <a
          key={u.id}
          href={u.url as string}
          target="_blank"
          rel="noopener noreferrer"
          title={`${fmtCzk(u.price)} · ${fmtShortDate(u.firstSeen)} – ${
            u.isActive ? 'now' : fmtShortDate(u.lastSeen)
          }`}
          className="inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-3 py-1.5 text-[0.8rem] text-[var(--color-ink-2)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper)] transition-colors"
        >
          <span
            className={[
              'w-1.5 h-1.5 rounded-full',
              u.isActive ? 'bg-[var(--color-sage)]' : 'bg-[var(--color-ink-4)]',
            ].join(' ')}
            aria-hidden
          />
          <span>{portalShort(u.source)}</span>
          {linkable.length > 1 && u.id === listing.id && (
            <span className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
              this
            </span>
          )}
          <OutArrow />
        </a>
      ))}
    </>
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
  // Exclude the current listing by SURROGATE id (never null) — `sreality_id !==
  // sreality_id` would keep a null-sreality sibling (null !== 5 is true) and then
  // build /listing/null. listingRowPath handles the null-sreality sibling.
  const liveSibling = sources
    .filter((s) => s.is_active && s.id !== listing.id)
    .sort(
      (a, b) =>
        new Date(b.last_seen_at).getTime() - new Date(a.last_seen_at).getTime(),
    )[0];
  if (!liveSibling) return null;
  return (
    <Link
      to={listingRowPath(liveSibling)}
      className="inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border border-[var(--color-copper)]/30 bg-[var(--color-copper-soft)] px-3 py-1.5 text-[0.8rem] text-[var(--color-copper)] hover:bg-[var(--color-copper)]/15 transition-colors"
    >
      <span className="w-1.5 h-1.5 rounded-full bg-[var(--color-sage)]" aria-hidden />
      View the current active listing
      <span className="capitalize text-[var(--color-ink-3)]">· {liveSibling.source}</span>
      <OutArrow />
    </Link>
  );
}

/* The resolved broker behind this listing → its broker-intelligence detail.
   Renders nothing for listings whose broker isn't resolved yet. */
function BrokerChip({ listingId }: { listingId: number }) {
  const q = useQuery({
    queryKey: ['listing-broker', listingId],
    queryFn: () => fetchListingBroker(listingId),
    staleTime: 60_000,
  });
  const b = q.data;
  if (!b) return null;
  return (
    <Link
      to={`/brokers/${b.broker_id}`}
      title={`Zobrazit makléře${b.broker_firm_label ? ` · ${b.broker_firm_label}` : ''}`}
      className="inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-3)] px-3 py-1.5 text-[0.8rem] text-[var(--color-ink-2)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper-2)] transition-colors"
    >
      <span className="text-[var(--color-ink-3)]">Makléř:</span>
      <span className="font-medium truncate max-w-[12rem]">
        {b.broker_display_name ?? 'detail'}
      </span>
      <OutArrow />
    </Link>
  );
}

/* When this property groups several portal observations (the chips above), it was
   built by the dedup engine. This chip links to the Decision history scoped to
   exactly the merges that created THIS property — the evidence + inline undo. It
   renders nothing for singletons or properties with no recorded merge. */
function MergeDecisionsChip({
  propertyId,
  multiSource,
}: {
  propertyId: number | null;
  multiSource: boolean;
}) {
  const q = useQuery({
    queryKey: ['merge-decisions-count', propertyId],
    queryFn: () =>
      getDedupAudit({ property_id: propertyId as number, outcome: 'merged', limit: 1 }),
    enabled: propertyId != null && multiSource,
    staleTime: 60_000,
  });
  const n = q.data?.total ?? 0;
  if (propertyId == null || !multiSource || n === 0) return null;
  return (
    <Link
      to={`/dedup?audit_property=${propertyId}#history`}
      title="Zobrazit rozhodnutí o sloučení (dedup)"
      className="inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-3)] px-3 py-1.5 text-[0.8rem] text-[var(--color-ink-2)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper-2)] transition-colors"
    >
      <span className="text-[var(--color-ink-3)]">Sloučení:</span>
      <span className="tabular-nums">{n} rozhodnutí</span>
      <OutArrow />
    </Link>
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
  const label = [placePrimary(listing), listing.disposition]
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
              {u.id === listing.id ? (
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
      // Bare prefix, not ['listing', sreality_id]: listingQ's real key is
      // ['listing', legacyId, natKeyId] (R2 Phase C resolver-chain cutover) —
      // this component only knows the loaded row's sreality_id, which no longer
      // matches either route-param slot for a natural-key-resolved listing.
      qc.invalidateQueries({ queryKey: ['listing'] });
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
