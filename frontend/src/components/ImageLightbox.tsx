import { useCallback, useEffect, useRef, useState } from 'react';
import type { ImagePublic } from '@/lib/types';
import { imageSrc } from '@/lib/imageUrl';
import ImageTagBadge from '@/components/ImageTagBadge';
import ImageRenderBadge from '@/components/ImageRenderBadge';

/* The full-screen photo modal — Escape/arrow-key nav, focus trap, the tag/render badges
 * on the enlarged photo. Extracted from listing-detail/Gallery (the property-detail image
 * expand) so the CLIP/pHash audit pages open the SAME modal instead of a second one-off —
 * it already operates on ImagePublic[], the exact shape images_public rows already are. */

interface Props {
  images: ImagePublic[];
  startIndex: number;
  onClose: () => void;
  /* Extra classes on the enlarged <img> (Gallery's inactive-listing desaturation). */
  dim?: string;
}

export default function ImageLightbox({ images, startIndex, onClose, dim = '' }: Props) {
  const [i, setI] = useState(startIndex);
  const [errored, setErrored] = useState(false);
  const total = images.length;
  const closeBtnRef = useRef<HTMLButtonElement>(null);

  const prev = useCallback(() => {
    setErrored(false);
    setI((x) => (x - 1 + total) % total);
  }, [total]);
  const next = useCallback(() => {
    setErrored(false);
    setI((x) => (x + 1) % total);
  }, [total]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
      else if (e.key === 'ArrowLeft') prev();
      else if (e.key === 'ArrowRight') next();
    };
    document.addEventListener('keydown', handler);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    closeBtnRef.current?.focus();
    return () => {
      document.removeEventListener('keydown', handler);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose, prev, next]);

  const current = images[i];
  if (!current) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: 'rgba(20, 22, 27, 0.92)' }}
    >
      <div
        className="absolute top-3 left-1/2 -translate-x-1/2 px-2.5 py-1 text-[0.72rem] tracking-[0.18em] uppercase text-[var(--color-ink-4)] font-mono tabular-nums"
        onClick={(e) => e.stopPropagation()}
      >
        {i + 1} / {total}
      </div>

      <button
        ref={closeBtnRef}
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onClose();
        }}
        aria-label="Close"
        className="absolute top-3 right-3 w-9 h-9 flex items-center justify-center text-[var(--color-ink-4)] hover:text-[var(--color-paper)] focus:outline-none focus-visible:border focus-visible:border-[var(--color-copper)] rounded-[var(--radius-sm)]"
      >
        <CloseGlyph />
      </button>

      {total > 1 && (
        <>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              prev();
            }}
            aria-label="Previous photo"
            className="absolute left-2 md:left-6 top-1/2 -translate-y-1/2 w-11 h-11 flex items-center justify-center rounded-full text-[var(--color-ink-4)] hover:text-[var(--color-paper)] hover:bg-[var(--color-paper)]/10 focus:outline-none focus-visible:border focus-visible:border-[var(--color-copper)]"
          >
            <ArrowGlyph dir="left" />
          </button>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              next();
            }}
            aria-label="Next photo"
            className="absolute right-2 md:right-6 top-1/2 -translate-y-1/2 w-11 h-11 flex items-center justify-center rounded-full text-[var(--color-ink-4)] hover:text-[var(--color-paper)] hover:bg-[var(--color-paper)]/10 focus:outline-none focus-visible:border focus-visible:border-[var(--color-copper)]"
          >
            <ArrowGlyph dir="right" />
          </button>
        </>
      )}

      <div
        className="relative max-w-[92vw] max-h-[88vh] flex items-center justify-center"
        onClick={(e) => e.stopPropagation()}
      >
        {errored ? (
          <div
            className="px-12 py-10 border border-[var(--color-rule-strong)] text-[var(--color-ink-4)] tracking-[0.14em] uppercase text-sm"
          >
            Image unavailable
          </div>
        ) : (
          <>
            <img
              key={current.id}
              src={imageSrc(current)}
              alt=""
              onError={() => setErrored(true)}
              className={[
                'max-w-[92vw] max-h-[88vh] object-contain',
                'border border-[var(--color-copper)]/40',
                dim,
              ].join(' ')}
            />
            <ImageTagBadge
              tag={current.clip_fine_tag}
              confidence={current.clip_confidence}
              className="absolute bottom-2 left-2 text-[0.7rem]"
            />
            <ImageRenderBadge
              renderScore={current.clip_render_score}
              className="absolute bottom-2 right-2 text-[0.7rem]"
            />
          </>
        )}
      </div>
    </div>
  );
}

function CloseGlyph() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden>
      <line x1="3" y1="3" x2="13" y2="13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <line x1="13" y1="3" x2="3" y2="13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function ArrowGlyph({ dir }: { dir: 'left' | 'right' }) {
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
