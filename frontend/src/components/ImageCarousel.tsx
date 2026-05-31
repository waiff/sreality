import { useState, type MouseEvent, type ReactNode } from 'react';

/* Compact inline image carousel — the photo strip shared by Browse listing
 * cards and the /dedup review panels. Local index state (the carousel never
 * outlives its mount); chevrons stopPropagation + preventDefault so paging
 * inside a wrapping <Link> doesn't navigate. Inline only — no lightbox.
 *
 * Overlays (status badges, etc.) are passed as children and absolutely
 * positioned by the caller; the carousel owns the aspect box, the image,
 * the no-image placeholder, the chevrons, and the "n / total" counter. */

interface Props {
  urls: string[];
  /* Tailwind aspect-ratio class for the frame. Default matches Browse cards. */
  aspect?: string;
  /* Extra classes on the aspect container. */
  className?: string;
  /* Extra classes on the <img> (e.g. the inactive desaturation filter). */
  imgClassName?: string;
  /* group-hover:scale the image — cards live inside a `.group` Link. */
  hoverZoom?: boolean;
  /* Chevrons fade in on parent `.group` hover (cards) rather than always
   * showing (the dedup panel, which has no hover-group wrapper). */
  fadeChevrons?: boolean;
  children?: ReactNode;
}

export default function ImageCarousel({
  urls,
  aspect = 'aspect-[5/4]',
  className = '',
  imgClassName = '',
  hoverZoom = false,
  fadeChevrons = false,
  children,
}: Props) {
  const [index, setIndex] = useState(0);
  const safeIndex = urls.length === 0 ? 0 : Math.min(index, urls.length - 1);
  const hasMany = urls.length > 1;

  const step = (delta: number) => (e: MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (urls.length === 0) return;
    setIndex((safeIndex + delta + urls.length) % urls.length);
  };

  const chevronBase =
    'absolute top-1/2 -translate-y-1/2 w-6 h-6 flex items-center justify-center'
    + ' rounded-full bg-[var(--color-paper-3)]/85 border border-[var(--color-rule)]'
    + ' text-[var(--color-ink-2)] backdrop-blur-sm hover:text-[var(--color-copper)]'
    + ' hover:border-[var(--color-rule-strong)] transition-opacity'
    + (fadeChevrons ? ' opacity-0 group-hover:opacity-100' : '');

  return (
    <div className={`${aspect} bg-[var(--color-inset)] overflow-hidden relative ${className}`}>
      {urls.length > 0 ? (
        <img
          src={urls[safeIndex]}
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
          <span className="absolute bottom-1 right-1 px-1.5 py-0.5 text-[0.6rem] tracking-[0.08em] tabular-nums rounded-[var(--radius-xs)] bg-[var(--color-paper-3)]/85 border border-[var(--color-rule)] text-[var(--color-ink-2)] backdrop-blur-sm">
            {safeIndex + 1} / {urls.length}
          </span>
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
