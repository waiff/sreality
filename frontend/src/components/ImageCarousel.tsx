import { useState, type ReactNode } from 'react';
import ImageTagBadge from './ImageTagBadge';
import ImageRenderBadge from './ImageRenderBadge';
import { type TaggedImageUrl } from '@/lib/imageTags';

/* Compact inline image carousel — the photo strip on Browse listing
 * cards. Local index state (the carousel never outlives its mount). Inline
 * only — no lightbox.
 *
 * The chevrons used to preventDefault + stopPropagation on every click,
 * because the Browse card wrapped this whole carousel in its detail <Link> and
 * paging would otherwise navigate. That wrapper is gone (the card is a plain
 * div with a stretched link on its title), so the chevrons are plain buttons
 * again and a click means exactly one thing.
 *
 * Overlays (status badges, etc.) are passed as children and absolutely
 * positioned by the caller; the carousel owns the aspect box, the image,
 * the no-image placeholder, the chevrons, and the "n / total" counter. */

interface Props {
  /* Render-ready images (url + CLIP tag + confidence). The bottom-left tag
   * badge is read from the current image; callers without tags pass null. */
  images: TaggedImageUrl[];
  /* Tailwind aspect-ratio class for the frame. Default matches Browse cards. */
  aspect?: string;
  /* Extra classes on the aspect container. */
  className?: string;
  /* Extra classes on the <img> (e.g. the inactive desaturation filter). */
  imgClassName?: string;
  /* group-hover:scale the image — cards live inside a `.group` Link. */
  hoverZoom?: boolean;
  /* Chevrons fade in on parent `.group` hover (cards) rather than always
   * showing (a panel with no hover-group wrapper). */
  fadeChevrons?: boolean;
  children?: ReactNode;
}

export default function ImageCarousel({
  images,
  aspect = 'aspect-[5/4]',
  className = '',
  imgClassName = '',
  hoverZoom = false,
  fadeChevrons = false,
  children,
}: Props) {
  const [index, setIndex] = useState(0);
  const safeIndex = images.length === 0 ? 0 : Math.min(index, images.length - 1);
  const hasMany = images.length > 1;
  const current = images[safeIndex];

  const step = (delta: number) => () => {
    if (images.length === 0) return;
    setIndex((safeIndex + delta + images.length) % images.length);
  };

  /* --z-card-action is the rung a Browse card reserves for its controls
   * (globals.css): the card's stretched link paints an ::after over the whole
   * card at z-1, and an unraised chevron would sit under it and stop paging.
   * Read the TOKEN, not a literal, so the ledger is a constraint: change the
   * token and the chevrons move with it. Harmless on the carousel's other
   * mounts, which stack nothing over it. */
  const chevronBase =
    'absolute top-1/2 -translate-y-1/2 z-[var(--z-card-action)] w-6 h-6 flex items-center justify-center'
    + ' rounded-full bg-[var(--color-paper-3)]/85 border border-[var(--color-rule)]'
    + ' text-[var(--color-ink-2)] backdrop-blur-sm hover:text-[var(--color-copper)]'
    + ' hover:border-[var(--color-rule-strong)] transition-opacity'
    + (fadeChevrons ? ' opacity-0 group-hover:opacity-100' : '');

  return (
    <div className={`${aspect} bg-[var(--color-inset)] overflow-hidden relative ${className}`}>
      {images.length > 0 ? (
        <img
          src={current.url}
          alt=""
          loading="lazy"
          className={[
            'w-full h-full object-cover transition-transform duration-200',
            hoverZoom ? 'group-hover:scale-[1.02]' : '',
            imgClassName,
          ].join(' ')}
          onError={(e) => {
            (e.currentTarget as HTMLImageElement).style.visibility = 'hidden';
          }}
        />
      ) : (
        <div className="w-full h-full flex items-center justify-center text-[0.6rem] tracking-wider uppercase text-[var(--color-ink-4)]">
          no image
        </div>
      )}

      {children}

      <ImageTagBadge
        tag={current?.tag}
        confidence={current?.confidence}
        className="absolute bottom-1 left-1 z-[1] max-w-[calc(100%-3.5rem)] truncate"
      />

      {/* Bottom-RIGHT metadata stack: the render-vs-photo pill above the photo
          counter (mirrors the listing-detail gallery's placement; the room tag
          owns bottom-left). Kept off the top-left corner, which the caller's
          overlay controls (the Browse card's bookmark + collection buttons,
          --z-card-action) own — the badge used to sit behind them. */}
      <div className="absolute bottom-1 right-1 z-[1] flex flex-col items-end gap-1">
        <ImageRenderBadge renderScore={current?.renderScore} />
        {hasMany && (
          <span className="px-1.5 py-0.5 text-[0.6rem] tracking-[0.08em] tabular-nums rounded-[var(--radius-xs)] bg-[var(--color-paper-3)]/85 border border-[var(--color-rule)] text-[var(--color-ink-2)] backdrop-blur-sm">
            {safeIndex + 1} / {images.length}
          </span>
        )}
      </div>

      {hasMany && (
        <>
          <button
            type="button"
            onClick={step(-1)}
            aria-label="Previous photo"
            className={`${chevronBase} left-1`}
          >
            <Chevron dir="left" />
          </button>
          <button
            type="button"
            onClick={step(1)}
            aria-label="Next photo"
            className={`${chevronBase} right-1`}
          >
            <Chevron dir="right" />
          </button>
        </>
      )}
    </div>
  );
}

function Chevron({ dir }: { dir: 'left' | 'right' }) {
  const d = dir === 'left' ? 'M7.5 3 L4 6 L7.5 9' : 'M4.5 3 L8 6 L4.5 9';
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 12 12"
      aria-hidden
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={d} />
    </svg>
  );
}
