import { useMemo, useState } from 'react';
import type { TagPositiveImage } from '@/lib/api';
import { imageSrc } from '@/lib/imageUrl';
import ImageLightbox from '@/components/ImageLightbox';
import type { ImagePublic } from '@/lib/types';

/* "What this tag actually contains" — every image currently positive on it.
 * The point is the DRIFT: the operator writes the sentence with the tag's real
 * contents in view, so a label whose name stopped matching its images can't
 * survive the writing. Clicking a tile stages it as a canonical example; that
 * stages into the draft, like every other edit on this page, and writes nothing. */
interface Props {
  rows: ReadonlyArray<TagPositiveImage>;
  photos: ReadonlyMap<number, ImagePublic>;
  exampleIds: ReadonlyArray<number>;
  onToggleExample: (imageId: number) => void;
  loading: boolean;
  /* The server-side cap this list was fetched with — reaching it exactly means
   * the tag has at least this many positives and the grid is truncated. */
  limit: number;
  /* Mirrors toolkit.tag_definitions.EXAMPLE_IMAGES_MAX. */
  exampleLimit: number;
}

export default function TagContentsGallery({
  rows,
  photos,
  exampleIds,
  onToggleExample,
  loading,
  limit,
  exampleLimit,
}: Props) {
  const [lightboxAt, setLightboxAt] = useState<number | null>(null);
  const examples = useMemo(() => new Set(exampleIds), [exampleIds]);

  /* Staged examples the grid cannot show: the image stopped being positive on
   * this tag (relabeled on the Labeling page), or it fell past the cap. They
   * still count, and they still save into the next version — so they need to be
   * visible and removable HERE, or they are stuck in the document forever. */
  const offList = useMemo(() => {
    const shown = new Set(rows.map((r) => r.image_id));
    return exampleIds.filter((id) => !shown.has(id));
  }, [rows, exampleIds]);

  /* The lightbox operates on ImagePublic rows, so it can only walk the tiles
   * whose photo has actually arrived — kept in grid order so "⤢" opens on the
   * tile that was clicked. */
  const lightboxImages = useMemo(
    () =>
      rows
        .map((r) => photos.get(r.image_id))
        .filter((p): p is ImagePublic => p != null),
    [rows, photos],
  );
  const lightboxIndexOf = (imageId: number) =>
    lightboxImages.findIndex((p) => p.id === imageId);

  return (
    <section className="mt-6 border-t border-[var(--color-rule)] pt-4">
      <div className="flex items-baseline gap-2 flex-wrap">
        <h2 className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
          What this tag actually contains
        </h2>
        <span className="font-mono text-[0.7rem] tabular-nums text-[var(--color-ink-4)]">
          {rows.length} positive images · {examples.size} marked as examples (max{' '}
          {exampleLimit})
        </span>
        {rows.length === limit && (
          <span className="text-[0.7rem] text-[var(--color-ink-4)]">
            showing the {limit} most recent
          </span>
        )}
      </div>
      <p className="mt-1 text-[0.7rem] text-[var(--color-ink-4)]">
        Click a tile to mark it as a canonical example — staged, saved with the definition.
      </p>

      {/* Never while the grid is still loading: rows is empty then, so every
          staged example would flash up as "not in this list". */}
      {!loading && offList.length > 0 && (
        <div className="mt-3 p-2 rounded-[var(--radius-xs)] bg-[var(--color-inset)]">
          <p className="text-[0.7rem] text-[var(--color-ink-3)]">
            {offList.length} staged example{offList.length === 1 ? '' : 's'} not in this list —
            no longer positive on this tag, or past the {limit} shown. Still saved with the
            definition.
          </p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {offList.map((id) => {
              const photo = photos.get(id);
              return (
                <span
                  key={id}
                  className="inline-flex items-center gap-1 pl-1 pr-0.5 py-0.5 rounded-[var(--radius-xs)] border border-[var(--color-copper)]"
                >
                  {photo && (
                    <img
                      src={imageSrc(photo)}
                      alt=""
                      loading="lazy"
                      className="w-8 h-6 object-cover rounded-[var(--radius-xs)]"
                    />
                  )}
                  <span className="font-mono text-[0.68rem] tabular-nums text-[var(--color-ink-2)]">
                    image {id}
                  </span>
                  <button
                    type="button"
                    aria-label={`Remove image ${id} from the examples`}
                    onClick={() => onToggleExample(id)}
                    className="px-1 text-[0.7rem] leading-none text-[var(--color-ink-4)] hover:text-[var(--color-brick)]"
                  >
                    ✕
                  </button>
                </span>
              );
            })}
          </div>
        </div>
      )}

      {loading && <p className="mt-3 text-sm text-[var(--color-ink-3)]">Loading…</p>}
      {!loading && rows.length === 0 && (
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">
          No positive images yet for this tag.
        </p>
      )}

      {rows.length > 0 && (
        <div className="mt-3 grid gap-2 [grid-template-columns:repeat(auto-fill,minmax(9rem,1fr))]">
          {rows.map((r) => {
            const photo = photos.get(r.image_id);
            const isExample = examples.has(r.image_id);
            return (
              <div
                key={r.image_id}
                className={[
                  'relative rounded-[var(--radius-xs)] overflow-hidden border',
                  isExample
                    ? 'border-[var(--color-copper)] ring-2 ring-[var(--color-copper)]'
                    : 'border-[var(--color-rule)]',
                ].join(' ')}
              >
                <button
                  type="button"
                  onClick={() => onToggleExample(r.image_id)}
                  aria-pressed={isExample}
                  aria-label={`Toggle image ${r.image_id} as a canonical example`}
                  className="block w-full"
                >
                  {/* The row already carries storage_path + sreality_url, so the
                      tile paints from the /positive-images answer itself — it
                      never waits on the separate image read that feeds the
                      lightbox. */}
                  <img
                    src={imageSrc(r)}
                    alt=""
                    loading="lazy"
                    onError={(e) => {
                      e.currentTarget.style.visibility = 'hidden';
                    }}
                    className="w-full aspect-[4/3] object-cover bg-[var(--color-inset)]"
                  />
                </button>

                {isExample && (
                  <span className="absolute left-1 bottom-1 px-1 py-px text-[0.6rem] rounded-[var(--radius-xs)] bg-[var(--color-copper)] text-[var(--color-paper)]">
                    example
                  </span>
                )}

                {photo && (
                  <button
                    type="button"
                    onClick={() => setLightboxAt(lightboxIndexOf(r.image_id))}
                    aria-label={`Enlarge image ${r.image_id}`}
                    title="Enlarge"
                    className="absolute right-1 top-1 px-1 py-px text-[0.65rem] leading-none rounded-[var(--radius-xs)] border border-[var(--color-rule-strong)] bg-[var(--color-paper)]/85 text-[var(--color-ink-2)] hover:text-[var(--color-ink)]"
                  >
                    ⤢
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {lightboxAt != null && lightboxAt >= 0 && (
        <ImageLightbox
          images={lightboxImages}
          startIndex={lightboxAt}
          onClose={() => setLightboxAt(null)}
        />
      )}
    </section>
  );
}
