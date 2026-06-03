import { Suspense, lazy, useLayoutEffect, useMemo, useState } from 'react';
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
import { portalListingUrl, srealityListingUrl, type SrealityCategory } from '@/lib/portals';
import ErrorBoundary from '@/components/ErrorBoundary';
import { ListingOverview } from '@/components/listing-detail/ListingOverview';

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
      <ListingOverview
        listing={listing}
        images={images}
        imagesLoading={imagesQ.isLoading}
      />
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
      <OutboundBlock
        sreality_id={listing.sreality_id}
        source={currentSource}
        category={{
          categoryType: listing.category_type,
          categoryMain: listing.category_main,
          categorySubCb: listing.category_sub_cb,
        }}
      />
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
  category,
}: {
  sreality_id: number;
  source?: PropertySource;
  category: SrealityCategory;
}) {
  // Prefer the listing's real source URL (any portal); for sreality (which
  // stores none) reconstruct from the category triple. portalListingUrl returns
  // null when it can't build a resolvable URL — link to the in-app view then,
  // never to a sreality 404.
  const portal = source?.source ?? 'sreality';
  const href = portalListingUrl(
    portal,
    source?.source_url,
    source?.source_id_native ?? sreality_id,
    category,
  );
  const cls =
    'inline-flex items-center gap-1.5 text-sm text-[var(--color-copper)] hover:text-[var(--color-copper-2)] transition-colors capitalize';
  if (!href) {
    return (
      <Link to={`/listing/${sreality_id}`} className={cls}>
        View listing
        <OutArrow />
      </Link>
    );
  }
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className={cls}>
      {`Open on ${portal}`}
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
