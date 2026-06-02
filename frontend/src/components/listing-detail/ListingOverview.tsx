/* Shared "what is this property" overview — the single-column hero + property
 * facts + MF reference rent + description + compact map + photo gallery. Extracted
 * from the Listing Detail page so the Estimation Detail page renders its subject
 * with the SAME structure (one surface, not two). Driven by a ListingPublic row;
 * the estimation page passes the subject's resolved listings row. */
import { Suspense, lazy, useLayoutEffect, useRef, useState } from 'react';
import {
  fmtCzk,
  fmtArea,
  fmtPricePerM2,
  fmtAbsolute,
  fmtFurnished,
  fmtOwnership,
  fmtParkingLots,
  fmtCategorySub,
} from '@/lib/format';
import type { ImagePublic, ListingPublic } from '@/lib/types';

const DetailMap = lazy(() => import('@/components/listing-detail/DetailMap'));
const Gallery = lazy(() => import('@/components/listing-detail/Gallery'));

export function ListingOverview({
  listing,
  images = [],
  imagesLoading = false,
  showStatus = true,
}: {
  listing: ListingPublic;
  images?: ImagePublic[];
  imagesLoading?: boolean;
  showStatus?: boolean;
}) {
  return (
    <>
      <Header listing={listing} showStatus={showStatus} />
      <KeyFactsBlock listing={listing} />
      <ReferenceRentBlock listing={listing} />
      <DescriptionBlock listing={listing} />
      <Hairline />
      <MapBlock listing={listing} />
      <Hairline />
      <GalleryBlock
        images={images}
        isActive={listing.is_active}
        loading={imagesLoading}
      />
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
}: {
  listing: ListingPublic;
  showStatus: boolean;
}) {
  const disposition = listing.disposition ?? '—';
  const area = fmtArea(listing.area_m2);
  // A null price means the seller hid it (often quoting it in the description);
  // it's real source state, not missing data — surface it as such.
  const hasPrice = listing.price_czk != null;
  const price = hasPrice ? fmtCzk(listing.price_czk) : 'Cena na vyžádání';
  const ppm = fmtPricePerM2(listing.price_czk, listing.area_m2);
  const unit = hasPrice && listing.price_unit ? ` / ${listing.price_unit}` : '';
  const hasId = listing.sreality_id > 0;

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
        {(hasId || ppm !== '—') && (
          <p className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)] mt-2">
            {hasId && (
              <>
                ID{' '}
                <span className="font-mono tabular-nums text-[var(--color-ink-3)] normal-case tracking-normal">
                  {listing.sreality_id}
                </span>
              </>
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
      {showStatus && (
        <StatusPill isActive={listing.is_active} lastSeenAt={listing.last_seen_at} />
      )}
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
/* Map block (compact)                                                        */
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

  /* Disposition, usable area and district are intentionally omitted — they're
   * already in the header above, so this grid carries only the facts the header
   * doesn't. Fields universally NULL for a category drop out via pruneNulls. */

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

function capitalise(s: string | null): string | null {
  if (!s) return null;
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function yesNo(v: boolean | null): string | null {
  if (v == null) return null;
  return v ? 'Yes' : 'No';
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
