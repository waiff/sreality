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

  /* Amenities render as pictogram tiles (present = lit glyph, absent =
   * dimmed + slashed) rather than Yes/No text. Unknown (null) amenities
   * drop out — null ≠ absent, so we never slash something we can't confirm.
   * The parking-spaces count rides along as a note on the Parking tile. */
  const amenities: Amenity[] = [
    { label: 'Balcony', present: listing.has_balcony, Glyph: BalconyGlyph },
    { label: 'Terrace', present: listing.terrace, Glyph: TerraceGlyph },
    { label: 'Lift', present: listing.has_lift, Glyph: LiftGlyph },
    { label: 'Cellar', present: listing.cellar, Glyph: CellarGlyph },
    { label: 'Garage', present: listing.garage, Glyph: GarageGlyph },
    {
      label: 'Parking',
      present: listing.has_parking,
      Glyph: ParkingGlyph,
      note:
        listing.parking_lots != null && listing.parking_lots > 0
          ? fmtParkingLots(listing.parking_lots)
          : null,
    },
  ].filter((a) => a.present != null);

  if (property.length === 0 && building.length === 0 && amenities.length === 0) {
    return null;
  }
  return (
    <div className="mt-8 space-y-7">
      {property.length > 0 && <FactsGrid title="Property" facts={property} />}
      {building.length > 0 && <FactsGrid title="Building" facts={building} />}
      {amenities.length > 0 && <AmenitiesGrid amenities={amenities} />}
    </div>
  );
}

interface Fact {
  label: string;
  value: string | null;
  mono?: boolean;
}

interface Amenity {
  label: string;
  present: boolean | null;
  Glyph: () => React.ReactElement;
  note?: string | null;
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

function AmenitiesGrid({ amenities }: { amenities: Amenity[] }) {
  return (
    <div>
      <SectionLabel>Amenities</SectionLabel>
      <ul className="mt-3 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
        {amenities.map((a) => (
          <AmenityTile key={a.label} amenity={a} />
        ))}
      </ul>
    </div>
  );
}

function AmenityTile({ amenity }: { amenity: Amenity }) {
  const on = amenity.present === true;
  const { Glyph } = amenity;
  return (
    <li
      className={[
        'flex flex-col items-center justify-center gap-2 rounded-md border px-2 py-4 text-center',
        on
          ? 'border-[var(--color-rule)] bg-[var(--color-paper-2)]'
          : 'border-[var(--color-rule-soft)] bg-transparent',
      ].join(' ')}
    >
      <span
        className={[
          'relative inline-flex',
          on ? 'text-[var(--color-copper)]' : 'text-[var(--color-ink-4)]',
        ].join(' ')}
        aria-hidden
      >
        <Glyph />
        {!on && <CrossSlash />}
      </span>
      <span
        className={[
          'text-[0.65rem] tracking-[0.12em] uppercase',
          on ? 'text-[var(--color-ink-2)]' : 'text-[var(--color-ink-4)]',
        ].join(' ')}
      >
        {amenity.label}
        <span className="sr-only">: {on ? 'yes' : 'no'}</span>
      </span>
      {amenity.note != null && (
        <span className="text-[0.7rem] font-mono tabular-nums text-[var(--color-ink-3)]">
          {amenity.note}
        </span>
      )}
    </li>
  );
}

/* Amenity pictograms — simple line glyphs in a shared 24×24 grid, drawn in
 * currentColor so the tile decides lit (copper) vs dimmed (ink-4). When an
 * amenity is absent the tile overlays CrossSlash to strike the glyph out. */

function GlyphSvg({ children }: { children: React.ReactNode }) {
  return (
    <svg
      width="26"
      height="26"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      {children}
    </svg>
  );
}

function CrossSlash() {
  return (
    <svg
      className="absolute inset-0"
      width="26"
      height="26"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      aria-hidden
    >
      <line x1="4" y1="20" x2="20" y2="4" />
    </svg>
  );
}

function BalconyGlyph() {
  return (
    <GlyphSvg>
      <line x1="3" y1="4" x2="3" y2="13" />
      <line x1="3" y1="8" x2="21" y2="8" />
      <line x1="3" y1="13" x2="21" y2="13" />
      <line x1="21" y1="8" x2="21" y2="13" />
      <line x1="8.5" y1="8" x2="8.5" y2="13" />
      <line x1="12" y1="8" x2="12" y2="13" />
      <line x1="15.5" y1="8" x2="15.5" y2="13" />
    </GlyphSvg>
  );
}

function TerraceGlyph() {
  return (
    <GlyphSvg>
      <path d="M4 11 C 7 4, 17 4, 20 11 Z" />
      <line x1="12" y1="11" x2="12" y2="20" />
      <line x1="8" y1="20" x2="16" y2="20" />
    </GlyphSvg>
  );
}

function LiftGlyph() {
  return (
    <GlyphSvg>
      <rect x="5" y="3" width="14" height="18" rx="2" />
      <path d="M9 11 L12 7.5 L15 11" />
      <path d="M9 13 L12 16.5 L15 13" />
    </GlyphSvg>
  );
}

function CellarGlyph() {
  return (
    <GlyphSvg>
      <path d="M3 5 H7 V9 H11 V13 H15 V17 H21" />
    </GlyphSvg>
  );
}

function GarageGlyph() {
  return (
    <GlyphSvg>
      <path d="M3 21 V9 L12 4.5 L21 9 V21" />
      <path d="M7 21 V13 H17 V21" />
      <line x1="7" y1="16.5" x2="17" y2="16.5" />
      <line x1="7" y1="19" x2="17" y2="19" />
    </GlyphSvg>
  );
}

function ParkingGlyph() {
  return (
    <GlyphSvg>
      <rect x="3" y="3" width="18" height="18" rx="3" />
      <path d="M9 17 V7 H13 A3 3 0 0 1 13 13 H9" />
    </GlyphSvg>
  );
}
