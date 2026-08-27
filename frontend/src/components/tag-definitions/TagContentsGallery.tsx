import { useMemo, useState } from 'react';
import type { TagExcludedReason, TagPositiveImage, TagState } from '@/lib/api';
import { imageSrc } from '@/lib/imageUrl';
import ImageLightbox from '@/components/ImageLightbox';
import type { ImagePublic } from '@/lib/types';

/* An image the operator took OUT of this tag during this sitting, kept so the
 * grid can say what happened and offer it back. `index` is where the row sat in
 * `rows` at removal time — a put-back re-inserts it THERE rather than at the
 * top, because the server orders by updated_at and a put-back genuinely moves
 * the row; converging on the next natural refetch is honest, reshuffling the
 * grid under the operator's cursor is the churn this page exists to avoid. */
export interface MovedOutImage {
  row: TagPositiveImage;
  index: number;
  state: TagState;
  excludedReason: TagExcludedReason | null;
}

/* What each outcome is CALLED, in the same words the panel's four buttons use —
 * one vocabulary, learned once. */
const outcomeWord = (m: MovedOutImage): string =>
  m.state === 'negative'
    ? 'not this tag'
    : (m.excludedReason ?? 'ambiguous') === 'pruned'
      ? 'belongs elsewhere'
      : "can't tell";

/* "What this tag actually contains" — every image currently positive on it.
 * The point is the DRIFT: the operator writes the sentence with the tag's real
 * contents in view, so a label whose name stopped matching its images can't
 * survive the writing. Clicking a tile stages it as a canonical example; that
 * stages into the draft, like every other edit on this page, and writes nothing.
 *
 * The ONE exception is the "all tags" pill: a tri-state cell is ground truth
 * and has no draft, so moving an image out of this tag writes immediately. The
 * helper line says so before the operator ever opens the panel. */
interface Props {
  rows: ReadonlyArray<TagPositiveImage>;
  photos: ReadonlyMap<number, ImagePublic>;
  exampleIds: ReadonlyArray<number>;
  onToggleExample: (imageId: number) => void;
  loading: boolean;
  /* The server-side cap this list was fetched with — a fetch that came back
   * holding it means the tag has at least this many positives and the grid is
   * truncated. Measured against the FETCHED length, not the live one. */
  limit: number;
  /* Mirrors toolkit.tag_definitions.EXAMPLE_IMAGES_MAX. */
  exampleLimit: number;
  onOpenTags: (imageId: number) => void;
  movedOut: ReadonlyArray<MovedOutImage>;
  onPutBack: (imageId: number) => void;
  putBackPending: ReadonlySet<number>;
}

export default function TagContentsGallery({
  rows,
  photos,
  exampleIds,
  onToggleExample,
  loading,
  limit,
  exampleLimit,
  onOpenTags,
  movedOut,
  onPutBack,
  putBackPending,
}: Props) {
  const [lightboxAt, setLightboxAt] = useState<number | null>(null);
  const examples = useMemo(() => new Set(exampleIds), [exampleIds]);

  /* Filtered against the live grid, so a background refetch that re-lists an
   * image can never show a tile and a "moved out" chip for the same one. */
  const movedOutShown = useMemo(
    () => movedOut.filter((m) => !rows.some((r) => r.image_id === m.row.image_id)),
    [movedOut, rows],
  );

  /* How many rows the SERVER returned, which is what the truncation note is
   * about. `rows` is the patched cache and shrinks as images are moved out, so
   * testing it directly would disarm the note on a still-truncated list — and
   * this is the page whose whole claim is showing a tag's real contents. The
   * moved-out rows are exactly the ones subtracted, and `movedOutShown` already
   * drops any a refetch has re-listed, so the sum is the fetched length. */
  const fetched = rows.length + movedOutShown.length;

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
        {fetched >= limit && (
          <span className="text-[0.7rem] text-[var(--color-ink-4)]">
            showing the {limit} most recent
          </span>
        )}
      </div>
      <p className="mt-1 text-[0.7rem] text-[var(--color-ink-4)]">
        Click a tile to mark it as a canonical example — staged, saved with the definition.
      </p>
      <p className="text-[0.7rem] text-[var(--color-ink-4)]">
        Use “all tags” on a tile to fix an image that does not belong here. Unlike everything
        else on this page, that writes immediately.
      </p>

      {movedOutShown.length > 0 && (
        <div className="mt-3 p-2 rounded-[var(--radius-xs)] bg-[var(--color-inset)]">
          <p className="text-[0.7rem] text-[var(--color-ink-3)]">
            {movedOutShown.length} image{movedOutShown.length === 1 ? '' : 's'} moved out of this
            tag in this sitting.
          </p>
          <div className="mt-1.5 flex flex-col gap-1">
            {movedOutShown.map((m) => {
              const id = m.row.image_id;
              const stagedExample = examples.has(id);
              return (
                <span
                  key={id}
                  className="inline-flex items-center gap-1.5 self-start pl-1 pr-0.5 py-0.5 rounded-[var(--radius-xs)] border border-[var(--color-rule)]"
                >
                  <img
                    src={imageSrc(m.row)}
                    alt=""
                    loading="lazy"
                    className="w-8 h-6 object-cover rounded-[var(--radius-xs)]"
                  />
                  <span className="font-mono text-[0.68rem] tabular-nums text-[var(--color-ink-2)]">
                    image {id}
                  </span>
                  <span className="text-[0.68rem] text-[var(--color-ink-3)]">
                    {`· ${outcomeWord(m)}`}
                  </span>
                  {/* The draft is NOT auto-edited — silently rewriting the
                      operator's document is worse than a visible
                      inconsistency, and the off-list strip below already
                      renders that example with a working ✕. */}
                  {stagedExample && (
                    <span className="text-[0.68rem] text-[var(--color-brick)]">
                      still staged as a canonical example
                    </span>
                  )}
                  <button
                    type="button"
                    disabled={putBackPending.has(id)}
                    onClick={() => onPutBack(id)}
                    className="px-1.5 py-0.5 text-[0.68rem] rounded-[var(--radius-xs)] border border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-copper)] hover:border-[var(--color-copper)] disabled:opacity-40"
                  >
                    put back
                  </button>
                </span>
              );
            })}
          </div>
        </div>
      )}

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

                {/* A SIBLING of the tile-wide example button, never nested —
                    nesting would make one click mean both things. Same classes
                    as the ⤢ pill, opposite corner, so the two overlay controls
                    read as one family; top-LEFT rather than bottom-left, which
                    is the "example" badge's slot (no control ever shares a
                    corner with a state badge). Neither is hover-revealed: a
                    workbench scanned by eye must not hide its controls. */}
                <button
                  type="button"
                  onClick={() => onOpenTags(r.image_id)}
                  aria-label={`All tags on image ${r.image_id}`}
                  title="Every tag on this image — set which ones it belongs to, and take it out of this one"
                  className="absolute left-1 top-1 px-1 py-px text-[0.65rem] leading-none rounded-[var(--radius-xs)] border border-[var(--color-rule-strong)] bg-[var(--color-paper)]/85 text-[var(--color-ink-2)] hover:text-[var(--color-ink)]"
                >
                  all tags
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
