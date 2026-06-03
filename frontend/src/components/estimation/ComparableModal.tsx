import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  fmtArea,
  fmtCzk,
  fmtPricePerM2,
  fmtRelative,
  fmtAbsolute,
} from '@/lib/format';
import type { ImagePublic, ListingPublic, ListingSummaryBody } from '@/lib/types';
import { imageSrc } from '@/lib/imageUrl';
import { portalListingUrl, portalShort } from '@/lib/portals';

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
  const closeBtnRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handler);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    closeBtnRef.current?.focus();
    return () => {
      document.removeEventListener('keydown', handler);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto px-4 py-10"
      style={{ background: 'rgba(20, 22, 27, 0.6)' }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative w-full max-w-2xl bg-[var(--color-paper)] rounded-[var(--radius-md)] border border-[var(--color-rule)] shadow-[0_24px_60px_rgba(0,0,0,0.18)]"
      >
        <button
          ref={closeBtnRef}
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="absolute top-3 right-3 w-9 h-9 flex items-center justify-center text-[var(--color-ink-3)] hover:text-[var(--color-ink)] rounded-[var(--radius-sm)] focus:outline-none focus-visible:border focus-visible:border-[var(--color-copper)]"
        >
          <CloseGlyph />
        </button>

        <div className="p-6">
          <Header listing={listing} />
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
      </div>
    </div>
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

function Header({ listing }: { listing: ListingPublic }) {
  const ppm = fmtPricePerM2(listing.price_czk, listing.area_m2);
  return (
    <div className="pr-10">
      <p className="text-[0.65rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
        Comparable · id <span className="font-mono tabular-nums text-[var(--color-ink-3)] normal-case tracking-normal">{listing.sreality_id}</span>
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
        <span>{listing.disposition ?? '—'}</span>
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
                className={[
                  'w-full aspect-[4/3] rounded-[var(--radius-xs)] overflow-hidden border bg-[var(--color-inset)] focus:outline-none',
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
    <img
      key={img.id}
      src={imageSrc(img)}
      alt=""
      onError={() => setErrored(true)}
      className={['w-full h-full object-cover', dim].join(' ')}
    />
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
  // Reconstruct the origin-portal link from the category triple (sreality stores
  // no source_url); null → we can't reach a resolvable external page, so only the
  // in-app "View in full" link shows rather than a sreality 404.
  const external = portalListingUrl(listing.source, null, listing.sreality_id, {
    categoryType: listing.category_type,
    categoryMain: listing.category_main,
    categorySubCb: listing.category_sub_cb,
  });
  return (
    <div className="flex items-center justify-between">
      <Link
        to={`/listing/${listing.sreality_id}`}
        className="inline-flex items-center gap-1.5 px-4 py-2 text-sm rounded-[var(--radius-sm)] border border-[var(--color-copper)]/40 bg-[var(--color-copper-soft)] text-[var(--color-copper)] hover:text-[var(--color-copper-2)] hover:border-[var(--color-copper)] transition-colors"
      >
        View in full
        <OutArrow />
      </Link>
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

function CloseGlyph() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden>
      <line x1="3" y1="3" x2="13" y2="13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <line x1="13" y1="3" x2="3" y2="13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
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
