import { useEffect, useState } from 'react';
import type { NewDedupTag } from '@/lib/api';
import Spinner from '@/components/Spinner';

/* Deleting a tag CASCADES: every image_tag_labels row under it dies with it
 * (toolkit.tag_annotations.remove_tag DELETEs them, then the tag).
 *
 * The honest number is NOT the raw row count. Most tags carry ~1,300
 * manufactured `backfill_442` rows that are worthless, alongside a few dozen
 * real human decisions that are the only ground truth this system has for the
 * tag. "1,440 annotations" buries the number that matters, so the human count
 * is the headline and the manufactured remainder is a quieter second line.
 *
 * A modal rather than TaxonomyManageModal's inline strip, because these numbers
 * do not fit in a 22rem sidebar row. That other surface is untouched. */
interface Props {
  /* Every number in the confirm comes from this row — no new route, no new
   * field. human_count is the headline; backfill_count and machine_count are
   * the manufactured remainder. */
  tag: NewDedupTag;
  /* statusByTag.get(tag.id)?.version ?? null */
  definitionVersion: number | null;
  /* versions.length, or null while that query has not answered — the version
   * list is a SEPARATE per-tag query from the page-level status that supplies
   * definitionVersion, so it can still be in flight (or failed) when the confirm
   * opens. "v2 and all 0 saved versions" is a self-contradiction in the one
   * dialog whose whole job is stating the loss accurately, so an unknown count
   * says so instead of printing a zero. */
  savedVersionCount: number | null;
  /* The page's `dirty`. Adds the "they go too" line. */
  hasUnsavedDraft: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  pending: boolean;
  /* Rendered inside the modal, never toasted. */
  error: string | null;
}

export default function TagDeleteConfirm({
  tag,
  definitionVersion,
  savedVersionCount,
  hasUnsavedDraft,
  onCancel,
  onConfirm,
  pending,
  error,
}: Props) {
  /* The gate's PRESENCE is the signal. A tag with no human decisions gets no
   * checkbox and a live button, so the checkbox never decays into ritual. */
  const [acknowledged, setAcknowledged] = useState(false);
  const needsGate = tag.human_count > 0;
  const total = tag.human_count + tag.machine_count + tag.backfill_count;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !pending) onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel, pending]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-[var(--color-ink)]/40 px-4 pt-[10vh]"
      onClick={() => !pending && onCancel()}
      role="presentation"
    >
      <div
        className="flex max-h-[78vh] w-full max-w-lg flex-col overflow-y-auto rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)] p-5 shadow-lg"
        onClick={(e) => e.stopPropagation()}
        // eslint-disable-next-line no-restricted-syntax -- W6b migrates this dialog
        role="dialog"
        aria-modal="true"
        aria-label="Delete tag"
      >
        <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
          This cannot be undone from here
        </p>
        <h2
          className="mt-1 text-lg leading-tight"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          Delete <span className="font-mono text-base">{tag.label}</span>?
        </h2>

        {/* The largest type in the modal, because this number IS the decision. */}
        <div className="mt-4">
          <p className="font-mono text-2xl leading-none tabular-nums text-[var(--color-brick)]">
            {tag.human_count} human decision{tag.human_count === 1 ? '' : 's'}
          </p>
          <p className="mt-1.5 text-[0.78rem] leading-snug text-[var(--color-ink-2)]">
            Every positive, negative and exclusion a person made for this tag. This is the only
            ground truth the system has for it, and deleting the tag deletes all of it.
          </p>
        </div>

        <div className="mt-3 space-y-1 text-[0.72rem] leading-snug text-[var(--color-ink-3)]">
          {(tag.backfill_count > 0 || tag.machine_count > 0) && (
            <p>
              Also deleted:{' '}
              {tag.backfill_count > 0 && (
                <>
                  <span className="font-mono tabular-nums">{tag.backfill_count}</span> rows
                  manufactured by migration 442's backfill — not decisions anybody made
                </>
              )}
              {tag.backfill_count > 0 && tag.machine_count > 0 && ', and '}
              {tag.machine_count > 0 && (
                <>
                  <span className="font-mono tabular-nums">{tag.machine_count}</span> unreviewed
                  machine rows
                </>
              )}
              .
            </p>
          )}
          <p>
            <span className="font-mono tabular-nums">{total}</span> row
            {total === 1 ? '' : 's'} in all.
          </p>
          <p>
            <span className="font-mono tabular-nums">{tag.positive_count}</span> image
            {tag.positive_count === 1 ? '' : 's'} stop being positive on this tag. The images
            themselves are untouched.
          </p>
          {definitionVersion != null && (
            <p>
              Its written definition (v{definitionVersion}) and{' '}
              {savedVersionCount == null ? (
                <>every saved version of it</>
              ) : savedVersionCount === 1 ? (
                <>its 1 saved version</>
              ) : (
                <>all {savedVersionCount} saved versions</>
              )}{' '}
              go with it.
            </p>
          )}
        </div>

        {hasUnsavedDraft && (
          <p className="mt-2 text-[0.72rem] leading-snug text-[var(--color-brick)]">
            You have unsaved changes to this definition. They go too.
          </p>
        )}

        <div className="mt-3 space-y-1 text-[0.68rem] leading-snug text-[var(--color-ink-4)]">
          {/* Verified against migration 446: the trigger fires on DELETE, and
              tag_label is denormalised onto the event row. No restore button is
              promised, because none exists. */}
          <p>
            Every deleted decision is still recorded in <code>image_tag_label_events</code>, with
            this tag's label copied onto the row — that table carries no foreign keys precisely
            so a cascade cannot erase the record of its own deletion. Recovering them is a
            hand-written SQL job, not a button here.
          </p>
          <p>
            Other tags' definitions that point at this one keep the reference; it just stops
            resolving to a name.
          </p>
        </div>

        {error && (
          <p className="mt-3 text-[0.72rem] leading-snug text-[var(--color-brick)]">{error}</p>
        )}

        {needsGate && (
          <label className="mt-3 flex items-start gap-1.5 text-[0.72rem] text-[var(--color-ink-2)] cursor-pointer">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(e) => setAcknowledged(e.target.checked)}
              className="mt-0.5 h-3.5 w-3.5 shrink-0"
            />
            <span>
              I am deleting {tag.human_count} human decision
              {tag.human_count === 1 ? '' : 's'}.
            </span>
          </label>
        )}

        <div className="mt-4 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={pending}
            className="px-2.5 py-1 text-xs rounded-[var(--radius-xs)] border border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={pending || (needsGate && !acknowledged)}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs rounded-[var(--radius-xs)] border border-[var(--color-brick)] bg-[var(--color-brick-soft)] text-[var(--color-brick)] disabled:opacity-40"
          >
            {pending && <Spinner size={10} />}
            Delete tag
          </button>
        </div>
      </div>
    </div>
  );
}
