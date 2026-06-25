import ImageCarousel from './ImageCarousel';

/* Floating image-carousel card shown when hovering a map marker — the photo
 * equivalent of the dataset HoverChart popup. Presentational only: the parent
 * positions it absolutely over the map container and owns the hover-bridge
 * timing (so the card stays open while the cursor is on it). `pointer-events`
 * is auto so the carousel chevrons are clickable. Built on the shared
 * ImageCarousel; generic enough for any map to adopt. */

interface Props {
  urls: string[];
  price: string;
  meta: string;
  district?: string | null;
  onMouseEnter?: () => void;
  onMouseLeave?: () => void;
}

export default function MapImagePreview({
  urls,
  price,
  meta,
  district,
  onMouseEnter,
  onMouseLeave,
}: Props) {
  return (
    <div
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      className="w-56 overflow-hidden rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)] shadow-[0_12px_32px_rgba(0,0,0,0.16)]"
    >
      <ImageCarousel
        images={urls.map((url) => ({ url, tag: null, confidence: null, renderScore: null }))}
        aspect="aspect-[4/3]"
      />
      <div className="px-3 py-2">
        <p className="font-mono tabular-nums text-sm text-[var(--color-ink)]">{price}</p>
        <p className="mt-0.5 font-mono tabular-nums text-[0.78rem] text-[var(--color-ink-2)]">
          {meta}
        </p>
        {district && (
          <p className="mt-0.5 truncate text-[0.72rem] text-[var(--color-ink-3)]">{district}</p>
        )}
      </div>
    </div>
  );
}
