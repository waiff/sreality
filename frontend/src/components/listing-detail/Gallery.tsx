import { useCallback, useEffect, useRef, useState } from 'react';
import type { ImagePublic } from '@/lib/types';

const R2_BASE = import.meta.env.VITE_R2_PUBLIC_BASE as string | undefined;

interface Props {
  images: ImagePublic[];
  isActive: boolean;
}

export default function Gallery({ images, isActive }: Props) {
  const [openAt, setOpenAt] = useState<number | null>(null);
  const opaque = isActive ? '' : 'opacity-70';

  const openAtIndex = useCallback((i: number) => setOpenAt(i), []);
  const close = useCallback(() => setOpenAt(null), []);

  return (
    <div>
      <ul className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-1.5">
        {images.map((img, i) => (
          <li key={img.id}>
            <Thumbnail
              image={img}
              onClick={() => openAtIndex(i)}
              dim={!isActive}
            />
          </li>
        ))}
      </ul>
      {openAt != null && (
        <Lightbox
          images={images}
          startIndex={openAt}
          onClose={close}
          dim={opaque}
        />
      )}
    </div>
  );
}

function Thumbnail({
  image,
  onClick,
  dim,
}: {
  image: ImagePublic;
  onClick: () => void;
  dim: boolean;
}) {
  const [errored, setErrored] = useState(false);
  return (
    <button
      type="button"
      onClick={onClick}
      className="group block w-full aspect-[4/3] overflow-hidden rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-inset)] focus:outline-none focus-visible:border-[var(--color-copper)]"
      aria-label={`Photo ${image.sequence ?? image.id}`}
    >
      {errored ? (
        <BrokenPlaceholder />
      ) : (
        <img
          src={imageUrl(image)}
          alt=""
          loading="lazy"
          decoding="async"
          onError={() => setErrored(true)}
          className={[
            'w-full h-full object-cover transition-transform duration-200',
            dim ? 'opacity-70' : '',
            'group-hover:scale-[1.02]',
          ].join(' ')}
        />
      )}
    </button>
  );
}

function BrokenPlaceholder() {
  return (
    <div className="w-full h-full flex items-center justify-center text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
      Unavailable
    </div>
  );
}

function Lightbox({
  images,
  startIndex,
  onClose,
  dim,
}: {
  images: ImagePublic[];
  startIndex: number;
  onClose: () => void;
  dim: string;
}) {
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
        className="max-w-[92vw] max-h-[88vh] flex items-center justify-center"
        onClick={(e) => e.stopPropagation()}
      >
        {errored ? (
          <div
            className="px-12 py-10 border border-[var(--color-rule-strong)] text-[var(--color-ink-4)] tracking-[0.14em] uppercase text-sm"
          >
            Image unavailable
          </div>
        ) : (
          <img
            key={current.id}
            src={imageUrl(current)}
            alt=""
            onError={() => setErrored(true)}
            className={[
              'max-w-[92vw] max-h-[88vh] object-contain',
              'border border-[var(--color-copper)]/40',
              dim,
            ].join(' ')}
          />
        )}
      </div>
    </div>
  );
}

function imageUrl(img: ImagePublic): string {
  if (R2_BASE && img.storage_path) {
    return `${R2_BASE.replace(/\/$/, '')}/${img.storage_path}`;
  }
  return img.sreality_url;
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
