import { useEffect, useMemo, useState } from 'react';
import type {
  NewDedupTag,
  TagExcludedReason,
  TagPositiveImage,
  TagState,
} from '@/lib/api';
import { imageSrc } from '@/lib/imageUrl';
import { tagShortLabel } from '@/lib/tagFamily';
import ImageLightbox from '@/components/ImageLightbox';
import Spinner from '@/components/Spinner';
import TagPicker from '@/components/tag-definitions/TagPicker';
import type { ImagePublic } from '@/lib/types';
import ContentsOrderControl, { OutlierRankBadge, type ContentsOrder } from './ContentsOrder';

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

/* What the SOURCE tag gets when a batch is filed under another tag. Three, not
 * four: SUBJECT_ACTIONS minus "can't tell", because a batch being filed
 * somewhere specific is by construction not undecidable, and manufacturing 145
 * ambiguous exclusions at once would make the ambiguity rate report a broken
 * definition that isn't. */
export type BatchSourceOutcome = 'keeps' | 'not-this' | 'elsewhere';

export interface BatchFileRequest {
  destTagId: number;
  /* Already narrowed to rows on screen, in grid order. */
  imageIds: number[];
  outcome: BatchSourceOutcome;
}

/* Never a thrown error: the batch mutation resolves with this for all four
 * terminal cases, so partial failure has exactly one handler. */
export interface BatchFileResult {
  status: 'ok' | 'dest-failed' | 'source-failed';
  destTagId: number;
  destLabel: string;
  outcome: BatchSourceOutcome;
  requestedIds: number[];
  destWrittenIds: number[];
  /* [] for 'keeps' — that outcome has no source write at all. */
  sourceWrittenIds: number[];
  /* What "press Write again" must act on. dest-failed: requested − destWritten.
   * source-failed: destWritten − sourceWritten. ok: []. */
  unresolvedIds: number[];
  /* The server's message, verbatim. */
  message: string | null;
}

/* The default is `keeps` because it is both the SAFE answer and the motivating
 * case: 145 images that genuinely are bathrooms-with-bathtubs AND bathrooms.
 * "source becomes negative" is not a safe default — it would be a lie about
 * those 145 and would poison the child head if the tag is ever revived. */
const BATCH_OUTCOMES: ReadonlyArray<{
  key: BatchSourceOutcome;
  label: string;
  helper: string;
}> = [
  {
    key: 'keeps',
    label: 'keeps it',
    helper: 'They stay positive here too — a copy, nothing is removed.',
  },
  {
    key: 'not-this',
    label: 'not this tag',
    helper: 'They were mis-filed here. A real negative the classifier learns from.',
  },
  {
    key: 'elsewhere',
    label: 'belongs elsewhere',
    helper:
      "The subject IS here, but the other tag fits better — excluded · pruned, never negative.",
  },
];

/* "What this tag actually contains" — every image currently positive on it.
 * The point is the DRIFT: the operator writes the sentence with the tag's real
 * contents in view, so a label whose name stopped matching its images can't
 * survive the writing. Clicking a tile stages it as a canonical example; that
 * stages into the draft, like every other edit on this page, and writes nothing.
 *
 * Two exceptions write immediately, and both say so. The "all tags" pill: a
 * tri-state cell is ground truth and has no draft. And SELECTION MODE, which
 * files a whole block of images under another tag — a mode rather than a
 * modifier, because a tile click already means "stage as a canonical example"
 * and shift-click would make one click mean two things. With the mode off the
 * tile is byte-identical to what it has always been. */
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

  subjectTagId: number;
  subjectLabel: string;
  /* Active tags only: filing images onto a retired tag would put them where
   * list_tags_for_image can no longer show them. */
  destinationTags: ReadonlyArray<NewDedupTag>;

  selecting: boolean;
  onEnterSelection: () => void;
  onLeaveSelection: () => void;
  /* Already intersected with `rows` by the page — the gallery never has to
   * defend against an id that has left the grid. */
  selectedIds: ReadonlySet<number>;
  onToggleSelect: (imageId: number) => void;
  onSelectAll: () => void;
  onClearSelection: () => void;

  onBatchFile: (req: BatchFileRequest) => void;
  batchPending: boolean;
  /* Ids submitted so far by the in-flight write — the progress readout for a
   * selection spanning more than one chunk. */
  batchWritten: number;
  batchResult: BatchFileResult | null;
  onDismissBatchResult: () => void;

  onPutBackAll: () => void;
  putBackAllPending: boolean;
  /* Which end of the tag is being read. The page hands `rows` already in this
   * order — the gallery never sorts, it only says which order is on. */
  order: ContentsOrder;
  onOrderChange: (order: ContentsOrder) => void;
  /* The SERVER's verdict, not a re-derivation: a tag under the centroid floor
   * comes back 'recent' however it was asked, and then there is no distance to
   * badge and nothing to sort by. */
  outlierApplied: boolean;
  centroidPositives: number | null;
  minPositives: number;
}

/* 145 chips is a wall, not a receipt. */
const MOVED_OUT_CHIPS_MAX = 12;

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
  subjectTagId,
  subjectLabel,
  destinationTags,
  selecting,
  onEnterSelection,
  onLeaveSelection,
  selectedIds,
  onToggleSelect,
  onSelectAll,
  onClearSelection,
  onBatchFile,
  batchPending,
  batchWritten,
  batchResult,
  onDismissBatchResult,
  onPutBackAll,
  putBackAllPending,
  order,
  onOrderChange,
  outlierApplied,
  centroidPositives,
  minPositives,
}: Props) {
  const [lightboxAt, setLightboxAt] = useState<number | null>(null);
  const examples = useMemo(() => new Set(exampleIds), [exampleIds]);

  /* Transient form state, local by design: only the selection, the pending flag
   * and the result are shared with the page. */
  const [destTagId, setDestTagId] = useState<number | null>(null);
  const [outcome, setOutcome] = useState<BatchSourceOutcome>('keeps');

  /* This component is NOT remounted when the page switches tags (same branch,
   * no key), so without this both values outlive the tag they were chosen for.
   * The destination is the dangerous half: the natural next move after filing a
   * batch is to open the destination tag and look at what arrived, which would
   * leave the picker naming the tag now being read. The outcome is the quieter
   * half — a fresh tag must start at `keeps`, the safe answer, not at whatever
   * the previous sitting ended on. */
  useEffect(() => {
    setDestTagId(null);
    setOutcome('keeps');
  }, [subjectTagId]);

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

  /* GRID ORDER, so the chunks a batch is split into are deterministic and the
   * payload can never name an id that is not currently positive on this tag. */
  const orderedSelected = useMemo(
    () => rows.filter((r) => selectedIds.has(r.image_id)).map((r) => r.image_id),
    [rows, selectedIds],
  );
  const allShownSelected = rows.length > 0 && orderedSelected.length === rows.length;
  const destTag = destinationTags.find((t) => t.id === destTagId) ?? null;
  const destLabel = destTag ? tagShortLabel(destTag.label) : '';
  const subjectShort = tagShortLabel(subjectLabel);
  /* Belt to the reset effect's braces, and it carries the render the effect has
   * not run on yet: a batch aimed at the tag it is reading would write positive
   * and then negative on ONE tag, turning that tag's own positives into
   * manufactured human negatives — the exact lie the outcome vocabulary exists
   * to prevent. Derived, so the pre-write summary can never narrate it either. */
  const destReady = destTagId != null && destTagId !== subjectTagId;
  const canWrite = destReady && orderedSelected.length > 0 && !batchPending;

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
            showing the {limit}{' '}
            {outlierApplied ? 'farthest from this tag’s centre' : 'most recent'}
          </span>
        )}
        {/* Never while the grid is still loading — the same guard the off-list
            strip already uses, for the same reason: there is no server verdict
            yet, so the floor note would flash "this tag has 0" at every tag
            switch and the active button would flip under the operator. */}
        {!loading && (
          <ContentsOrderControl
            order={order}
            onOrderChange={onOrderChange}
            outlierApplied={outlierApplied}
            centroidPositives={centroidPositives}
            minPositives={minPositives}
            /* The same `fetched >= limit` fact the note above is keyed on: the
               window was chosen by distance, so a time re-sort of it is the
               newest OF THESE, not the tag's newest. */
            truncated={fetched >= limit}
            limit={limit}
          />
        )}
        <button
          type="button"
          disabled={!selecting && rows.length === 0}
          onClick={selecting ? onLeaveSelection : onEnterSelection}
          className={[
            'ml-auto px-2 py-1 text-[0.7rem] rounded-[var(--radius-xs)] border disabled:opacity-40',
            selecting
              ? 'border-[var(--color-copper)] text-[var(--color-copper)]'
              : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
          ].join(' ')}
        >
          {selecting ? 'Done selecting' : 'Select images'}
        </button>
      </div>

      {selecting ? (
        <p className="mt-1 text-[0.7rem] text-[var(--color-ink-4)]">
          Click a tile to select it. Marking canonical examples is paused while you are
          selecting.
        </p>
      ) : (
        <>
          <p className="mt-1 text-[0.7rem] text-[var(--color-ink-4)]">
            Click a tile to mark it as a canonical example — staged, saved with the definition.
          </p>
          <p className="text-[0.7rem] text-[var(--color-ink-4)]">
            Use “all tags” on a tile to fix an image that does not belong here. Unlike everything
            else on this page, that writes immediately.
          </p>
        </>
      )}

      {selecting && (
        <div className="mt-3 rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-inset)] p-2.5">
          <div className="flex items-center gap-2 flex-wrap">
            <button
              type="button"
              disabled={rows.length === 0}
              onClick={allShownSelected ? onClearSelection : onSelectAll}
              className="px-2 py-0.5 text-[0.7rem] rounded-[var(--radius-xs)] border border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] disabled:opacity-40"
            >
              {allShownSelected ? 'Clear selection' : `Select all ${rows.length} shown`}
            </button>
            <span className="font-mono text-[0.7rem] tabular-nums text-[var(--color-ink-3)]">
              {orderedSelected.length} of {rows.length} shown selected
            </span>
            {/* Select-all never claims to act on the whole tag. */}
            {fetched >= limit && (
              <span className="text-[0.7rem] text-[var(--color-ink-4)]">
                · this tag has more than the {limit} shown
              </span>
            )}
          </div>

          <p className="mt-3 text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
            File these images under another tag
          </p>

          <div className="mt-1.5 flex items-center gap-2 flex-wrap">
            <span className="text-[0.7rem] text-[var(--color-ink-3)]">Destination</span>
            <TagPicker
              value={destTagId}
              onChange={setDestTagId}
              tags={destinationTags}
              excludeIds={[subjectTagId]}
              allowEmpty={false}
              ariaLabel="Destination tag"
            />
          </div>

          <fieldset className="mt-2.5">
            <legend className="text-[0.7rem] text-[var(--color-ink-3)]">
              What happens to{' '}
              <span className="font-mono text-[var(--color-ink-2)]">{subjectShort}</span>
            </legend>
            <div className="mt-1 space-y-1">
              {BATCH_OUTCOMES.map((o) => (
                <label key={o.key} className="flex items-start gap-1.5 cursor-pointer">
                  <input
                    type="radio"
                    name="batch-source-outcome"
                    aria-label={o.label}
                    aria-describedby={`batch-outcome-help-${o.key}`}
                    checked={outcome === o.key}
                    onChange={() => setOutcome(o.key)}
                    className="mt-[0.2rem] h-3 w-3 shrink-0"
                  />
                  <span className="min-w-0">
                    <span className="text-[0.72rem] text-[var(--color-ink-2)]">{o.label}</span>
                    <span
                      id={`batch-outcome-help-${o.key}`}
                      className="block text-[0.68rem] leading-snug text-[var(--color-ink-4)]"
                    >
                      {o.helper}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          {/* Always on screen, never a tooltip: this is the one control where
              the cheap wrong answer poisons a training head, and the operator
              reading it has not read migration 446. */}
          <p className="mt-2 text-[0.68rem] leading-snug text-[var(--color-ink-4)]">
            “Not this tag” is a claim that the subject is not in these photos. If it IS there and
            another tag simply fits better, choose “belongs elsewhere” — a wrong negative poisons
            this tag's classifier, and nothing here can silently undo it.
          </p>

          {destReady && orderedSelected.length > 0 && (
            <p className="mt-2 text-[0.7rem] leading-snug text-[var(--color-ink-2)]">
              <strong className="font-medium tabular-nums">
                {orderedSelected.length} image{orderedSelected.length === 1 ? '' : 's'}
              </strong>{' '}
              become positive on <strong className="font-medium">{destLabel}</strong>
              {outcome === 'keeps' ? (
                <>
                  . They stay positive on <strong className="font-medium">{subjectShort}</strong> —
                  nothing is removed.
                </>
              ) : outcome === 'not-this' ? (
                <>
                  , and negative on <strong className="font-medium">{subjectShort}</strong> —{' '}
                  {subjectShort} loses all {orderedSelected.length} from its contents.
                </>
              ) : (
                <>
                  , and excluded · pruned on{' '}
                  <strong className="font-medium">{subjectShort}</strong> — {subjectShort} loses
                  all {orderedSelected.length} from its contents, without a negative that would
                  poison its head.
                </>
              )}
            </p>
          )}

          <div className="mt-2 flex items-center gap-2 flex-wrap">
            <button
              type="button"
              disabled={!canWrite}
              onClick={() =>
                canWrite &&
                destTagId != null &&
                onBatchFile({ destTagId, imageIds: orderedSelected, outcome })
              }
              className="inline-flex items-center gap-1.5 px-2.5 py-1 text-[0.72rem] rounded-[var(--radius-xs)] border border-[var(--color-copper)] bg-[var(--color-copper)] text-[var(--color-paper)] disabled:opacity-40"
            >
              {batchPending && <Spinner />}
              Write {orderedSelected.length} image{orderedSelected.length === 1 ? '' : 's'}
            </button>
            {/* 300 ids over four sequential calls is otherwise seconds of nothing. */}
            {batchPending && (
              <span className="font-mono text-[0.68rem] tabular-nums text-[var(--color-ink-3)]">
                {batchWritten} of {orderedSelected.length} written to {destLabel}
                {outcome !== 'keeps' && batchWritten === orderedSelected.length
                  ? ` · updating ${subjectShort}…`
                  : ''}
              </span>
            )}
          </div>

          {/* Persists until the selection changes or the mode is left. A
              shortfall that scrolls away in six seconds reads as "it just gave
              me fewer", so this is never a toast. */}
          {batchResult && (
            <div
              className={[
                'mt-2 flex items-start gap-2 text-[0.7rem] leading-snug',
                batchResult.status === 'ok'
                  ? 'text-[var(--color-ink-2)]'
                  : 'text-[var(--color-brick)]',
              ].join(' ')}
            >
              <p className="min-w-0 flex-1">{batchResultLine(batchResult, subjectShort)}</p>
              <button
                type="button"
                onClick={onDismissBatchResult}
                className="shrink-0 text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)]"
              >
                dismiss
              </button>
            </div>
          )}
        </div>
      )}

      {movedOutShown.length > 0 && (
        <div className="mt-3 p-2 rounded-[var(--radius-xs)] bg-[var(--color-inset)]">
          <div className="flex items-center gap-2 flex-wrap">
            <p className="text-[0.7rem] text-[var(--color-ink-3)]">
              {movedOutShown.length} image{movedOutShown.length === 1 ? '' : 's'} moved out of
              this tag in this sitting.
            </p>
            {movedOutShown.length > 1 && (
              <button
                type="button"
                disabled={putBackAllPending}
                onClick={onPutBackAll}
                className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[0.68rem] rounded-[var(--radius-xs)] border border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-copper)] hover:border-[var(--color-copper)] disabled:opacity-40"
              >
                {putBackAllPending && <Spinner size={9} />}
                Put all back
              </button>
            )}
          </div>
          <div className="mt-1.5 flex flex-col gap-1">
            {movedOutShown.slice(0, MOVED_OUT_CHIPS_MAX).map((m) => {
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
            {movedOutShown.length > MOVED_OUT_CHIPS_MAX && (
              <span className="text-[0.68rem] text-[var(--color-ink-4)]">
                +{movedOutShown.length - MOVED_OUT_CHIPS_MAX} more
              </span>
            )}
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
            const isSelected = selectedIds.has(r.image_id);
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
                {/* ONE meaning per click at any moment. In selection mode the
                    tile selects; otherwise it stages a canonical example. The
                    example ring and badge are drawn either way, so a staged
                    example never loses its mark while a batch is built. */}
                <button
                  type="button"
                  onClick={() =>
                    selecting ? onToggleSelect(r.image_id) : onToggleExample(r.image_id)
                  }
                  aria-pressed={selecting ? isSelected : isExample}
                  aria-label={
                    selecting
                      ? `Select image ${r.image_id}`
                      : `Toggle image ${r.image_id} as a canonical example`
                  }
                  className={[
                    'block w-full',
                    /* Selection reads as DIMMING the rest, never a second ring:
                       the copper ring is already the canonical-example mark, and
                       a competing ring would either collide with it or need a
                       hue this page does not have. */
                    selecting && !isSelected ? 'opacity-45' : '',
                  ]
                    .filter(Boolean)
                    .join(' ')}
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
                    workbench scanned by eye must not hide its controls. Both
                    stay live in selection mode — inspecting one image while
                    building a batch is exactly the workflow. */}
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

                {/* The only free corner. */}
                {selecting && isSelected && (
                  <span className="absolute right-1 bottom-1 px-1 py-px text-[0.65rem] leading-none rounded-[var(--radius-xs)] border border-[var(--color-copper)] bg-[var(--color-copper-soft)] text-[var(--color-copper)]">
                    ✓
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

                <OutlierRankBadge row={r} shown={outlierApplied} />
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

/* Every terminal case says what LANDED and what is still selected, because
 * "press Write again" is the recovery and the operator has to know what it will
 * act on. The overlap footnote rides on the success line: a batch moves both
 * tags' centroids, and re-running a pgvector scan per write is not worth it —
 * saying the distances are stale is. */
function batchResultLine(r: BatchFileResult, subjectShort: string): string {
  const n = r.requestedIds.length;
  const u = r.unresolvedIds.length;
  /* The result carries the FULL label (it is data, and a decision is never
   * keyed on display text), but every readout on this page names a tag the way
   * the list and the picker do — one vocabulary. */
  const dest = tagShortLabel(r.destLabel);
  if (r.status === 'dest-failed')
    return (
      `Stopped: ${r.destWrittenIds.length} of ${n} were written positive on ${dest}; ` +
      `the rest failed (${r.message ?? 'unknown error'}). Nothing was changed in ` +
      `${subjectShort}. The ${u} that did not land are still selected — press Write again to finish.`
    );
  if (r.status === 'source-failed')
    return (
      `All ${n} are positive on ${dest}. But ${u} of ${n} are still positive on ` +
      `${subjectShort} — that step failed (${r.message ?? 'unknown error'}). Those ${u} are ` +
      `still selected; pressing Write again re-runs both steps, which is safe.`
    );
  const tail = ' Overlap distances above were computed before this batch.';
  if (r.outcome === 'keeps')
    return (
      `${n} image${n === 1 ? ' is' : 's are'} now positive on ${dest}. ` +
      `They stay positive on ${subjectShort} — nothing was removed.${tail}`
    );
  if (r.outcome === 'not-this')
    return `${n} image${n === 1 ? ' is' : 's are'} now positive on ${dest}, and negative on ${subjectShort}.${tail}`;
  return `${n} image${n === 1 ? ' is' : 's are'} now positive on ${dest}, and excluded · pruned on ${subjectShort}.${tail}`;
}
