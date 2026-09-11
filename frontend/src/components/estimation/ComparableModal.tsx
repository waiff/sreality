import { useId, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  fmtArea,
  fmtCzk,
  fmtMeasuredPricePerM2,
  fmtRelative,
  fmtAbsolute,
} from '@/lib/format';
import { ppm2BasisFromToken } from '@/lib/measure';
import type { ImagePublic, ListingPublic, ListingSummaryBody } from '@/lib/types';
import { listingKindLabel } from '@/lib/enums';
import { imageSrc } from '@/lib/imageUrl';
import ImageTagBadge from '@/components/ImageTagBadge';
import { portalShort } from '@/lib/portals';
import { listingPath } from '@/lib/listingUrl';
import Dialog, { DialogClose } from '@/components/Dialog';

/* The comparable's detail card, opened from a row of the estimation-detail
 * comparables table — which is itself inside a dialog, so this is the app's
 * one shipped NESTED pair. It renders through <Dialog> (components/Dialog.tsx):
 * standard chrome, a centred card with a close glyph, and nothing about it
 * needs a bespoke backdrop.
 *
 * WHAT LEFT: its own `document` keydown listener (which, with the run-detail
 * modal's own listener on the same document, is exactly why one Escape used to
 * close BOTH), its own body scroll-lock copy, its own initial-focus call, its
 * own close glyph, and the `stopPropagation` on the card that existed only to
 * keep a click inside it from reaching the backdrop's `onClick={onClose}`.
 * <Dialog> dismisses on a mousedown that both starts and lands on the backdrop
 * ITSELF, so there is no handler on the wrong element left to undo. */
interface Props {
  listing: ListingPublic;
  images: ImagePublic[];
  summary: ListingSummaryBody | null;
  summaryError: string | null;
  summaryLoading: boolean;
  onClose: () => void;
}

export default function ComparableModal({
  listing,
  images,
  summary,
  summaryError,
  summaryLoading,
  onClose,
}: Props) {
  /* The dialog is named by the line the operator reads at the top of it
   * ("Comparable · id 1234") rather than by a literal, so the name and the
   * visible words cannot drift apart. */
  const titleId = useId();

  return (
    <Dialog
      open
      onClose={onClose}
      labelledBy={titleId}
      /* `relative` for the pinned close glyph. Height and scrolling are the
       * primitive's: a panel is viewport-safe and scrolls itself by default,
       * which is what the scrolling backdrop this used to sit in provided. */
      className="relative w-full max-w-2xl"
    >
      <DialogClose onClick={onClose} className="absolute top-3 right-3 z-10" />

      <div className="p-6">
        <Header listing={listing} titleId={titleId} />
        <Hairline />
        <Carousel images={images} isActive={listing.is_active} />
        <Hairline />
        <SummarySection
          summary={summary}
          error={summaryError}
          loading={summaryLoading}
        />
        <Hairline />
        <Facts listing={listing} />
        <Hairline />
        <Footer listing={listing} />
      </div>
    </Dialog>
  );
}

function Hairline() {
  return <div className="my-5 h-px bg-[var(--color-rule)]" />;
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
      {children}
    </p>
  );
}

function Header({ listing, titleId }: { listing: ListingPublic; titleId: string }) {
  const ppm = fmtMeasuredPricePerM2(
    listing.price_per_m2,
    ppm2BasisFromToken(listing.price_per_m2_basis),
  );
  return (
    <div className="pr-10">
      <p id={titleId} className="text-[0.65rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
        {/* sreality_id is NULL for a post-Gate-2-flip non-sreality-portal
         * listing (flip not live yet); fall back to the surrogate `id`,
         * which every row always has. */}
        Comparable · id <span className="font-mono tabular-nums text-[var(--color-ink-3)] normal-case tracking-normal">{listing.sreality_id ?? listing.id}</span>
      </p>
      <h2
        className="mt-1 text-[1.7rem] leading-[1.1] tabular-nums"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        {fmtCzk(listing.price_czk)}
        {listing.price_unit && (
          <span className="text-sm font-sans font-normal text-[var(--color-ink-3)] tracking-wide ml-1">
            / {listing.price_unit}
          </span>
        )}
      </h2>
      <p className="mt-2 font-mono tabular-nums text-sm text-[var(--color-ink-2)]">
        <span>{listingKindLabel(listing) ?? '—'}</span>
        <span className="mx-2 text-[var(--color-ink-4)]">·</span>
        <span>{fmtArea(listing.area_m2)}</span>
        {ppm !== '—' && (
          <>
            <span className="mx-2 text-[var(--color-ink-4)]">·</span>
            <span>{ppm}</span>
          </>
        )}
      </p>
      {listing.locality && (
        <p className="mt-1.5 text-sm text-[var(--color-ink-2)]">{listing.locality}</p>
      )}
      <p
        className="mt-2 text-[0.7rem] tracking-wide text-[var(--color-ink-3)] cursor-help"
        title={fmtAbsolute(listing.last_seen_at)}
      >
        last seen {fmtRelative(listing.last_seen_at)}
        {!listing.is_active && (
          <span className="ml-2 text-[var(--color-brick)]">· inactive</span>
        )}
      </p>
    </div>
  );
}

function Carousel({ images, isActive }: { images: ImagePublic[]; isActive: boolean }) {
  const [i, setI] = useState(0);
  if (images.length === 0) {
    return (
      <div>
        <SectionLabel>Photos</SectionLabel>
        <p className="mt-2 text-sm text-[var(--color-ink-3)]">No photos recorded.</p>
      </div>
    );
  }
  const safe = Math.max(0, Math.min(i, images.length - 1));
  const current = images[safe];
  const dim = isActive ? '' : 'opacity-70';
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <SectionLabel>Photos</SectionLabel>
        <p className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)] font-mono tabular-nums">
          {safe + 1} / {images.length}
        </p>
      </div>
      <div className="mt-3 relative aspect-[4/3] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-inset)] overflow-hidden">
        <CarouselImage img={current} dim={dim} />
        {images.length > 1 && (
          <>
            <NavButton dir="left" onClick={() => setI((x) => (x - 1 + images.length) % images.length)} />
            <NavButton dir="right" onClick={() => setI((x) => (x + 1) % images.length)} />
          </>
        )}
      </div>
      {images.length > 1 && (
        <ul className="mt-2 grid grid-cols-6 gap-1.5">
          {images.slice(0, 6).map((img, idx) => (
            <li key={img.id}>
              <button
                type="button"
                onClick={() => setI(idx)}
                aria-label={`Photo ${idx + 1}`}
                className={[
                  'w-full aspect-[4/3] rounded-[var(--radius-xs)] overflow-hidden border bg-[var(--color-inset)]',
                  idx === safe
                    ? 'border-[var(--color-copper)]'
                    : 'border-[var(--color-rule)]',
                ].join(' ')}
              >
                <ThumbImage img={img} dim={dim} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CarouselImage({ img, dim }: { img: ImagePublic; dim: string }) {
  const [errored, setErrored] = useState(false);
  if (errored) return <BrokenPlaceholder />;
  return (
    <>
      <img
        key={img.id}
        src={imageSrc(img)}
        alt=""
        onError={() => setErrored(true)}
        className={['w-full h-full object-cover', dim].join(' ')}
      />
      <ImageTagBadge
        tag={img.clip_fine_tag}
        confidence={img.clip_confidence}
        className="absolute bottom-1 left-1 z-[1] max-w-[calc(100%-0.5rem)] truncate"
      />
    </>
  );
}

function ThumbImage({ img, dim }: { img: ImagePublic; dim: string }) {
  const [errored, setErrored] = useState(false);
  if (errored) return <BrokenPlaceholder small />;
  return (
    <img
      src={imageSrc(img)}
      alt=""
      loading="lazy"
      onError={() => setErrored(true)}
      className={['w-full h-full object-cover', dim].join(' ')}
    />
  );
}

function BrokenPlaceholder({ small = false }: { small?: boolean }) {
  return (
    <div
      className={[
        'w-full h-full flex items-center justify-center text-[var(--color-ink-4)] tracking-[0.14em] uppercase',
        small ? 'text-[0.55rem]' : 'text-[0.65rem]',
      ].join(' ')}
    >
      Unavailable
    </div>
  );
}

function NavButton({ dir, onClick }: { dir: 'left' | 'right'; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={dir === 'left' ? 'Previous' : 'Next'}
      className={[
        'absolute top-1/2 -translate-y-1/2 w-9 h-9 flex items-center justify-center rounded-full',
        'bg-[var(--color-paper-3)]/85 backdrop-blur-sm text-[var(--color-ink-2)]',
        'border border-[var(--color-rule)] hover:text-[var(--color-copper)]',
        dir === 'left' ? 'left-2' : 'right-2',
      ].join(' ')}
    >
      <Arrow dir={dir} />
    </button>
  );
}

function SummarySection({
  summary,
  error,
  loading,
}: {
  summary: ListingSummaryBody | null;
  error: string | null;
  loading: boolean;
}) {
  return (
    <div>
      <SectionLabel>Summary</SectionLabel>
      {loading ? (
        <p className="mt-2 text-sm text-[var(--color-ink-3)]">Generating…</p>
      ) : error ? (
        <p className="mt-2 text-sm text-[var(--color-ink-3)]">
          Summary unavailable: {error}
        </p>
      ) : summary == null ? (
        <p className="mt-2 text-sm text-[var(--color-ink-3)]">No summary available.</p>
      ) : (
        <div className="mt-3 space-y-3">
          <SummaryRow label="Location" text={summary.location_summary} />
          <SummaryRow label="Building" text={summary.building_summary} />
          <SummaryRow label="Apartment" text={summary.apartment_summary} />
        </div>
      )}
    </div>
  );
}

function SummaryRow({ label, text }: { label: string; text?: string | null }) {
  return (
    <div>
      <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        {label}
      </p>
      <p className="mt-1 text-sm text-[var(--color-ink)] leading-relaxed">
        {text || <span className="text-[var(--color-ink-4)]">—</span>}
      </p>
    </div>
  );
}

function Facts({ listing }: { listing: ListingPublic }) {
  const facts: Array<[string, string | null]> = [
    ['District', listing.district],
    ['Floor', listing.floor != null
      ? listing.total_floors != null
        ? `${listing.floor} / ${listing.total_floors}`
        : String(listing.floor)
      : null],
    ['Building', listing.building_type],
    ['Condition', listing.condition],
    ['Energy', listing.energy_rating],
    ['Balcony', yesNo(listing.has_balcony)],
    ['Lift', yesNo(listing.has_lift)],
    ['Parking', yesNo(listing.has_parking)],
  ];
  return (
    <div>
      <SectionLabel>Details</SectionLabel>
      <dl className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-x-5 gap-y-3">
        {facts.map(([label, value]) => (
          <div key={label}>
            <dt className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
              {label}
            </dt>
            <dd
              className={[
                'mt-1 text-sm',
                value == null
                  ? 'text-[var(--color-ink-4)]'
                  : 'text-[var(--color-ink)] font-mono tabular-nums',
              ].join(' ')}
            >
              {value ?? '—'}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function Footer({ listing }: { listing: ListingPublic }) {
  // The origin-portal link is the row's stored `source_url` (migration 494) for
  // every portal; null → only the in-app "View in full" link shows.
  const external = listing.source_url;
  return (
    <div className="flex items-center justify-between">
      {/* listingPath needs a real sreality_id (never route the surrogate
       * through the legacy sreality route — see lib/listingUrl.ts); a
       * post-flip non-sreality comparable has none, so the internal link
       * simply doesn't render rather than building a dead `/listing/null`. */}
      {listing.sreality_id != null ? (
        <Link
          to={listingPath(listing.sreality_id)}
          className="inline-flex items-center gap-1.5 px-4 py-2 text-sm rounded-[var(--radius-sm)] border border-[var(--color-copper)]/40 bg-[var(--color-copper-soft)] text-[var(--color-copper)] hover:text-[var(--color-copper-2)] hover:border-[var(--color-copper)] transition-colors"
        >
          View in full
          <OutArrow />
        </Link>
      ) : (
        <span />
      )}
      {external && (
        <a
          href={external}
          target="_blank"
          rel="noopener noreferrer"
          className="text-[0.78rem] text-[var(--color-ink-3)] hover:text-[var(--color-copper)]"
        >
          {`Open on ${portalShort(listing.source)}`}
        </a>
      )}
    </div>
  );
}

function yesNo(v: boolean | null): string | null {
  if (v == null) return null;
  return v ? 'Yes' : 'No';
}

function Arrow({ dir }: { dir: 'left' | 'right' }) {
  if (dir === 'left') {
    return (
      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden>
        <polyline points="9,2 3,7 9,12" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden>
      <polyline points="5,2 11,7 5,12" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function OutArrow() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
      <line x1="1" y1="9" x2="8.5" y2="1.5" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" />
      <polyline points="3.5,1.5 8.5,1.5 8.5,6.5" stroke="currentColor" strokeWidth="1.25" fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
