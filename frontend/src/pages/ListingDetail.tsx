import { Suspense, lazy, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  fetchListingById,
  fetchPropertySources,
  fetchSnapshotsForListings,
  fetchFreshnessChecksByListing,
  fetchImagesByListing,
} from '@/lib/queries';
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
import SnapshotTimeline from '@/components/SnapshotTimeline';

const DetailMap = lazy(() => import('@/components/listing-detail/DetailMap'));
const Gallery = lazy(() => import('@/components/listing-detail/Gallery'));
const CurationBlock = lazy(
  () => import('@/components/listing-detail/CurationBlock'),
);
const ManualEstimatesBlock = lazy(
  () => import('@/components/listing-detail/ManualEstimatesBlock'),
);

const DAY_MS = 86_400_000;

export default function ListingDetail() {
  const { sreality_id: idParam } = useParams();
  const sid = idParam && /^\d+$/.test(idParam) ? Number(idParam) : null;

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

  if (sid == null) {
    return <NoListingState id={idParam ?? null} reason="invalid" />;
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
      <Crumb />
      <Header listing={listing} />
      <Hairline />
      <MapBlock listing={listing} />
      <Hairline />
      <GalleryBlock images={images} isActive={listing.is_active} loading={imagesQ.isLoading} />
      <Hairline />
      <DescriptionBlock listing={listing} />
      <KeyFactsBlock listing={listing} />
      <Hairline />
      <Suspense fallback={null}>
        <CurationBlock sreality_id={listing.sreality_id} />
      </Suspense>
      <Hairline />
      <Suspense fallback={null}>
        <ManualEstimatesBlock sreality_id={listing.sreality_id} />
      </Suspense>
      <Hairline />
      <TimestampsBlock listing={listing} />
      <Hairline />
      <HistoryBlock listing={listing} snapshots={snapshots} checks={checks} />
      <Hairline />
      <FreshnessBlock checks={checks} />
      {sources.length > 1 ? (
        <>
          <Hairline />
          <SourcesBlock sources={sources} currentId={listing.sreality_id} />
        </>
      ) : null}
      <Hairline />
      <OutboundBlock sreality_id={listing.sreality_id} source={currentSource} />
    </Page>
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
  const price = fmtCzk(listing.price_czk);
  const ppm = fmtPricePerM2(listing.price_czk, listing.area_m2);
  const unit = listing.price_unit ? ` / ${listing.price_unit}` : '';

  return (
    <div className="mt-5 flex items-start justify-between gap-6">
      <div className="min-w-0">
        <p className="font-mono tabular-nums text-[var(--color-ink-2)] text-sm">
          <span>{disposition}</span>
          <span className="mx-2 text-[var(--color-ink-4)]">·</span>
          <span>{area}</span>
        </p>
        <h1
          className="mt-1.5 text-[2.6rem] leading-[1.05] tabular-nums"
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
            <div className="h-60 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]" />
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
  return (
    <>
      <DescriptionBody text={text} />
      <Hairline />
    </>
  );
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
    <div>
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

  /* Three sub-sections to keep cells from wrapping awkwardly under the
   * 18 facts surface (was 8 before migration 022). Each row uses the
   * same 2/3/4-col responsive grid, so the visual rhythm matches the
   * earlier single-block layout. Fields that are universally NULL for a
   * given category (estate_area / garden_area on apartments, furnished
   * on for-sale listings) drop out automatically — Detail() filters
   * each list before rendering. */

  const property: Fact[] = pruneNulls([
    { label: 'Disposition', value: listing.disposition, mono: true },
    { label: 'Subtype', value: fmtCategorySubOrNull(listing.category_sub_cb) },
    { label: 'Usable area', value: fmtAreaOrNull(listing.usable_area), mono: true },
    { label: 'Lot area', value: fmtAreaOrNull(listing.estate_area), mono: true },
    { label: 'Garden area', value: fmtAreaOrNull(listing.garden_area), mono: true },
    { label: 'Floor', value: floor, mono: true },
    { label: 'District', value: listing.district },
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

  return (
    <div className="space-y-7">
      <FactsGrid title="Property" facts={property} />
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
/* Timestamps                                                                 */
/* -------------------------------------------------------------------------- */

function TimestampsBlock({ listing }: { listing: ListingPublic }) {
  const firstT = new Date(listing.first_seen_at).getTime();
  const lastT = new Date(listing.last_seen_at).getTime();
  const endT = listing.is_active ? Date.now() : lastT;
  const days = Math.max(0, Math.floor((endT - firstT) / DAY_MS));

  return (
    <div>
      <SectionLabel>History</SectionLabel>
      <div className="mt-3 grid grid-cols-3 gap-6">
        <TsCell label="First seen" iso={listing.first_seen_at} />
        <TsCell label="Last seen" iso={listing.last_seen_at} />
        <div>
          <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
            {listing.is_active ? 'Days on market' : 'Lifetime'}
          </p>
          <p className="mt-1 text-sm font-mono tabular-nums text-[var(--color-ink)]">
            {days} {days === 1 ? 'day' : 'days'}
          </p>
        </div>
      </div>
    </div>
  );
}

function TsCell({ label, iso }: { label: string; iso: string }) {
  return (
    <div>
      <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        {label}
      </p>
      <p
        className="mt-1 text-sm text-[var(--color-ink)] cursor-help"
        title={fmtAbsolute(iso)}
      >
        {fmtRelative(iso)}
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Snapshot history (the hero block)                                          */
/* -------------------------------------------------------------------------- */

function HistoryBlock({
  listing,
  snapshots,
  checks,
}: {
  listing: ListingPublic;
  snapshots: ListingSnapshotPublic[];
  checks: ListingFreshnessCheckPublic[];
}) {
  const sorted = useMemo(
    () => [...snapshots].sort((a, b) => new Date(a.scraped_at).getTime() - new Date(b.scraped_at).getTime()),
    [snapshots],
  );
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <SectionLabel>Price history</SectionLabel>
        <p className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)] font-mono tabular-nums">
          {sorted.length} {sorted.length === 1 ? 'snapshot' : 'snapshots'}
        </p>
      </div>

      <div className="mt-4">
        <SnapshotTimeline
          firstSeenAt={listing.first_seen_at}
          lastSeenAt={listing.last_seen_at}
          isActive={listing.is_active}
          snapshots={sorted}
          freshnessChecks={checks}
        />
      </div>

      {sorted.length === 1 && (
        <p className="mt-3 text-sm text-[var(--color-ink-2)]">
          No price changes recorded — only seen once at{' '}
          <span className="font-mono tabular-nums">{fmtCzk(sorted[0].price_czk)}</span>
          {' '}on{' '}
          <span title={fmtAbsolute(sorted[0].scraped_at)}>{shortDate(sorted[0].scraped_at)}</span>.
        </p>
      )}

      {sorted.length >= 2 && <SnapshotTable snapshots={sorted} />}
    </div>
  );
}

function SnapshotTable({ snapshots }: { snapshots: ListingSnapshotPublic[] }) {
  return (
    <div className="mt-5 border border-[var(--color-rule)] rounded-[var(--radius-md)] overflow-hidden">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)] bg-[var(--color-paper-2)]">
            <th className="px-3 py-2 font-medium">ID</th>
            <th className="px-3 py-2 font-medium">Scraped</th>
            <th className="px-3 py-2 font-medium text-right">Price</th>
            <th className="px-3 py-2 font-medium text-right">Δ</th>
            <th className="px-3 py-2 font-medium text-right">Desc</th>
          </tr>
        </thead>
        <tbody>
          {snapshots.map((s, i) => {
            const prev = i > 0 ? snapshots[i - 1].price_czk : null;
            const delta = s.price_czk != null && prev != null ? s.price_czk - prev : null;
            const prevDesc = i > 0 ? snapshots[i - 1].description ?? '' : null;
            const descChanged = prevDesc != null && prevDesc !== (s.description ?? '');
            return (
              <tr
                key={s.id}
                className="border-t border-[var(--color-rule-soft)]"
              >
                <td className="px-3 py-2 font-mono tabular-nums text-[var(--color-ink-3)] text-[0.78rem]">
                  {s.id}
                </td>
                <td className="px-3 py-2 text-[var(--color-ink-2)] cursor-help" title={fmtAbsolute(s.scraped_at)}>
                  {fmtRelative(s.scraped_at)}
                </td>
                <td className="px-3 py-2 font-mono tabular-nums text-right text-[var(--color-ink)]">
                  {fmtCzk(s.price_czk)}
                </td>
                <td className="px-3 py-2 text-right">
                  <DeltaCell delta={delta} />
                </td>
                <td className="px-3 py-2 text-right">
                  <DescChangeCell changed={descChanged} hasPrior={i > 0} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function DescChangeCell({ changed, hasPrior }: { changed: boolean; hasPrior: boolean }) {
  if (!hasPrior) {
    return <span className="text-[var(--color-ink-4)]">—</span>;
  }
  if (!changed) {
    return <span className="font-mono tabular-nums text-[var(--color-ink-3)]">·</span>;
  }
  return (
    <span
      className="cursor-help text-[var(--color-copper)]"
      title="Description changed at this snapshot"
    >
      ✎
    </span>
  );
}

function DeltaCell({ delta }: { delta: number | null }) {
  if (delta == null) {
    return <span className="text-[var(--color-ink-4)]">—</span>;
  }
  if (delta === 0) {
    return <span className="font-mono tabular-nums text-[var(--color-ink-3)]">±0</span>;
  }
  const up = delta > 0;
  const colour = up ? 'var(--color-brick)' : 'var(--color-sage)';
  return (
    <span
      className="inline-flex items-center gap-1 font-mono tabular-nums"
      style={{ color: colour }}
    >
      <Triangle up={up} />
      {fmtCzk(Math.abs(delta))}
    </span>
  );
}

function Triangle({ up }: { up: boolean }) {
  return (
    <svg width="8" height="8" viewBox="0 0 8 8" aria-hidden>
      {up ? (
        <polygon points="4,0.5 7.5,7 0.5,7" fill="currentColor" />
      ) : (
        <polygon points="0.5,1 7.5,1 4,7.5" fill="currentColor" />
      )}
    </svg>
  );
}

/* -------------------------------------------------------------------------- */
/* Freshness checks                                                           */
/* -------------------------------------------------------------------------- */

function FreshnessBlock({ checks }: { checks: ListingFreshnessCheckPublic[] }) {
  const count = checks.length;
  return (
    <details className="group">
      <summary className="cursor-pointer list-none flex items-center justify-between gap-4">
        <SectionLabel>
          <span>Freshness checks</span>
          <span className="ml-2 font-mono tabular-nums text-[var(--color-ink-4)] tracking-normal">
            ({count})
          </span>
        </SectionLabel>
        <span className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] group-open:hidden">
          Show
        </span>
        <span className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] hidden group-open:inline">
          Hide
        </span>
      </summary>
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
    </details>
  );
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
  } else if (lower === 'gone' || lower === 'inactive' || lower === 'error') {
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
/* Listed-on-N-sites — multi-portal link history                              */
/* -------------------------------------------------------------------------- */

function SourcesBlock({
  sources,
  currentId,
}: {
  sources: PropertySource[];
  currentId: number;
}) {
  return (
    <div>
      <SectionLabel>Listed on {sources.length} sites</SectionLabel>
      <ul className="mt-3 space-y-2">
        {sources.map((s) => (
          <li
            key={s.sreality_id}
            className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper-2)] px-3 py-2"
          >
            <div className="flex items-center gap-2 min-w-0">
              <span className="text-sm text-[var(--color-ink)] capitalize">{s.source}</span>
              {s.sreality_id === currentId ? (
                <span className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
                  this listing
                </span>
              ) : null}
              <SourceStatusPill active={s.is_active} />
            </div>
            <div className="flex items-center gap-3 text-[0.8rem] text-[var(--color-ink-3)] tabular-nums">
              <span className="font-mono text-[var(--color-ink-2)]">{fmtCzk(s.price_czk)}</span>
              <span title={`${s.first_seen_at} – ${s.last_seen_at}`}>
                {fmtShortDate(s.first_seen_at)} – {s.is_active ? 'now' : fmtShortDate(s.last_seen_at)}
              </span>
              {s.source_url ? (
                <a
                  href={s.source_url}
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

function SourceStatusPill({ active }: { active: boolean }) {
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

function shortDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  return d.toLocaleDateString('cs-CZ', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  });
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
