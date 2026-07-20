import { useCallback, useState } from 'react';
import type { ImagePublic } from '@/lib/types';
import { imageSrc } from '@/lib/imageUrl';
import ImageTagBadge from '@/components/ImageTagBadge';
import ImageRenderBadge from '@/components/ImageRenderBadge';
import ImageLightbox from '@/components/ImageLightbox';

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
        <ImageLightbox
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
      className="group relative block w-full aspect-[4/3] overflow-hidden rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-inset)] focus:outline-none focus-visible:border-[var(--color-copper)]"
      aria-label={`Photo ${image.sequence ?? image.id}`}
    >
      {errored ? (
        <BrokenPlaceholder />
      ) : (
        <>
          <img
            src={imageSrc(image)}
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
          <ImageTagBadge
            tag={image.clip_fine_tag}
            confidence={image.clip_confidence}
            className="absolute bottom-1 left-1 max-w-[calc(100%-0.5rem)] truncate"
          />
          <ImageRenderBadge
            renderScore={image.clip_render_score}
            className="absolute bottom-1 right-1"
          />
        </>
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
