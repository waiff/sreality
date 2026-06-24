/* Single source of truth for a property's tabular "facts" + amenity chips.
 *
 * Extracted from ListingOverview's KeyFactsBlock so every surface that shows
 * "what we know about this property" — the listing-detail header AND the
 * Explore-area origin-property panel — renders the SAME facts, the SAME amenity
 * glyphs, and the SAME null-handling. Two surfaces, one definition: they can't
 * drift on label order, the parking-spaces note, or present!=null pruning.
 *
 * Pure data-prep (buildFacts / buildAmenities) + the presentational pieces
 * (FactsList / AmenityChips). All inputs are null-tolerant — a bazos/idnes row
 * with NULL disposition/area/amenities yields short-but-valid output, never a
 * broken box. */
import type { ReactElement, ReactNode } from 'react';
import { fmtArea, fmtFurnished, fmtOwnership, fmtParkingLots } from '@/lib/format';
import type { ListingPublic } from '@/lib/types';

export interface Fact {
  label: string;
  value: string | null;
  mono?: boolean;
}

export interface Amenity {
  label: string;
  present: boolean | null;
  Glyph: () => ReactElement;
  note?: string | null;
}

/* The building/lot facts that live in the dense strip (the kind, area, floor
 * and district live in the page header, not here). NULL values are pruned —
 * we show a fact only when we have it. */
export function buildFacts(listing: ListingPublic): Fact[] {
  return pruneNulls([
    { label: 'Lot', value: fmtAreaOrNull(listing.estate_area), mono: true },
    { label: 'Garden', value: fmtAreaOrNull(listing.garden_area), mono: true },
    { label: 'Building', value: capitalise(listing.building_type) },
    { label: 'Condition', value: capitalise(listing.condition) },
    { label: 'Energy', value: listing.energy_rating, mono: true },
    {
      label: 'Ownership',
      value: listing.ownership ? fmtOwnership(listing.ownership) : null,
    },
    {
      label: 'Furnished',
      value: listing.furnished ? fmtFurnished(listing.furnished) : null,
    },
  ]);
}

/* Amenities as compact pictogram chips (present = lit glyph, absent = dimmed +
 * slashed). Unknown (null) amenities drop out — null != absent, so we never
 * slash something we can't confirm. The parking-spaces count rides along as a
 * note on the Parking chip.
 *
 * TODO(amenity-canon): this renders BOTH the legacy combined booleans
 * (has_balcony conflates balcony+terrace+loggia; has_parking conflates
 * parking+garage) AND the granular columns (terrace, garage). That double-render
 * is pre-existing behaviour preserved verbatim by this extraction. Deprecating
 * the legacy chips in favour of the granular columns (CLAUDE.md schema note) is
 * a separate, operator-approved UI refactor — it would change the listing-detail
 * header too, so it must not ride in unrelated work. */
export function buildAmenities(listing: ListingPublic): Amenity[] {
  return [
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
}

/* -------------------------------------------------------------------------- */
/* Presentational                                                             */
/* -------------------------------------------------------------------------- */

export function FactsList({ facts }: { facts: Fact[] }) {
  if (facts.length === 0) return null;
  return (
    <dl className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
      {facts.map((f) => (
        <div key={f.label} className="flex items-baseline gap-1.5">
          <dt className="text-[0.62rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
            {f.label}
          </dt>
          <dd
            className={[
              'text-sm text-[var(--color-ink)]',
              f.mono ? 'font-mono tabular-nums' : '',
            ].join(' ')}
          >
            {f.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function AmenityChips({ amenities }: { amenities: Amenity[] }) {
  if (amenities.length === 0) return null;
  return (
    <ul className="flex flex-wrap gap-1.5">
      {amenities.map((a) => (
        <AmenityChip key={a.label} amenity={a} />
      ))}
    </ul>
  );
}

function AmenityChip({ amenity }: { amenity: Amenity }) {
  const on = amenity.present === true;
  const { Glyph } = amenity;
  return (
    <li
      className={[
        'inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border px-2 py-1',
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
          'text-[0.62rem] tracking-[0.12em] uppercase',
          on ? 'text-[var(--color-ink-2)]' : 'text-[var(--color-ink-4)]',
        ].join(' ')}
      >
        {amenity.label}
        {amenity.note != null && (
          <span className="ml-1 font-mono tabular-nums normal-case tracking-normal text-[var(--color-ink-3)]">
            {amenity.note}
          </span>
        )}
        <span className="sr-only">: {on ? 'yes' : 'no'}</span>
      </span>
    </li>
  );
}

/* -------------------------------------------------------------------------- */
/* Data-prep helpers                                                          */
/* -------------------------------------------------------------------------- */

function pruneNulls(facts: Fact[]): Fact[] {
  return facts.filter((f) => f.value != null);
}

function fmtAreaOrNull(n: number | null): string | null {
  return n == null ? null : fmtArea(n);
}

function capitalise(s: string | null): string | null {
  if (!s) return null;
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/* -------------------------------------------------------------------------- */
/* Amenity pictograms — simple line glyphs in a shared 24×24 grid, drawn in    */
/* currentColor so the chip decides lit (copper) vs dimmed (ink-4). When an    */
/* amenity is absent the chip overlays CrossSlash to strike the glyph out.     */
/* -------------------------------------------------------------------------- */

function GlyphSvg({ children }: { children: ReactNode }) {
  return (
    <svg
      width="16"
      height="16"
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
      width="16"
      height="16"
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
