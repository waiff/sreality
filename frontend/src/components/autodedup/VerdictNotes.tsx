/* AUTODEDUP · the operator's note beside a verdict.
 *
 * The verdict says WHAT was decided; the note says what the operator SAW, in
 * their own words. The page asks two answers and a note, nothing more (binary
 * verdicts, D39): the reason-chip picker of migration 533 is gone from every
 * surface. A verdict stored with reason codes keeps them — a note saved over it
 * re-posts them unchanged, and the rulings history still reads them.
 *
 * COLLAPSED BY DEFAULT, because a review queue is a scroll: an open note box on
 * every row pushes the next pair off the screen. The pair page and the group
 * dialog, where exactly one subject is on screen, open it.
 *
 * WHAT "DIRTY" MEANS HERE. The note hydrates from the STORED verdict, so a row
 * ruled last week reads back its own words. Editing it after a verdict is stored
 * is not itself a write — it arms "Uložit poznámku", which re-posts the SAME
 * verdict with the new note through the same endpoint and the same optimistic
 * overlay. A note that saved itself on every keystroke would write a verdict
 * per character.
 */

import { useCallback, useEffect, useState } from 'react';

import type { AutodedupVerdictRow } from '@/lib/api';

export interface VerdictAnnotation {
  reasons: string[];
  note: string;
}

export const EMPTY_ANNOTATION: VerdictAnnotation = { reasons: [], note: '' };

/* The annotation a STORED verdict carries. `note` is normalised to '' so the
 * input is never switched between controlled and uncontrolled. */
export function storedAnnotation(verdict: AutodedupVerdictRow | null | undefined): VerdictAnnotation {
  if (!verdict) return EMPTY_ANNOTATION;
  return { reasons: [...(verdict.reasons ?? [])], note: verdict.note ?? '' };
}

export function sameAnnotation(a: VerdictAnnotation, b: VerdictAnnotation): boolean {
  return (
    a.note.trim() === b.note.trim() &&
    a.reasons.length === b.reasons.length &&
    a.reasons.every((code, i) => code === b.reasons[i])
  );
}

/* What travels on the wire: the note only when it says something, so an empty
 * input never overwrites a stored note with the empty string by accident. */
export function annotationInput(value: VerdictAnnotation): { reasons: string[]; note: string | null } {
  return { reasons: value.reasons, note: value.note.trim() === '' ? null : value.note.trim() };
}

/* PAGE-LEVEL DRAFTS, keyed the way the verdict overlay is. The draft wins while
 * it exists; where it does not, the stored verdict is what the controls show —
 * so a reload shows the ruling that exists rather than a blank slate, and a save
 * leaves the draft equal to the stored row (hence not dirty). */
export function useVerdictAnnotations() {
  const [drafts, setDrafts] = useState<Record<string, VerdictAnnotation>>({});

  const annotationOf = useCallback(
    (key: string, stored: AutodedupVerdictRow | null | undefined): VerdictAnnotation =>
      drafts[key] ?? storedAnnotation(stored),
    [drafts],
  );
  const setAnnotation = useCallback((key: string, next: VerdictAnnotation) => {
    setDrafts((all) => ({ ...all, [key]: next }));
  }, []);
  const isDirty = useCallback(
    (key: string, stored: AutodedupVerdictRow | null | undefined): boolean =>
      stored != null && key in drafts && !sameAnnotation(drafts[key], storedAnnotation(stored)),
    [drafts],
  );

  return { annotationOf, setAnnotation, isDirty };
}

export default function VerdictNotes({
  value,
  onChange,
  defaultOpen = false,
  dirty = false,
  onSave,
  pending = false,
  label = 'poznámka',
}: {
  value: VerdictAnnotation;
  onChange: (next: VerdictAnnotation) => void;
  defaultOpen?: boolean;
  /* The stored verdict and the note disagree — the save button is armed. */
  dirty?: boolean;
  onSave?: () => void;
  pending?: boolean;
  label?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);

  /* A row that arrives with a note opens itself: hiding the operator's own
   * words behind a toggle is the write-only failure one level down. */
  useEffect(() => {
    if (value.note !== '') setOpen(true);
  }, [value.note]);

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="text-[0.65rem] text-[var(--color-ink-3)] underline decoration-dotted underline-offset-2 hover:text-[var(--color-ink)]"
      >
        + {label}
      </button>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <input
        type="text"
        value={value.note}
        placeholder="Poznámka"
        aria-label="Poznámka"
        onChange={(e) => onChange({ ...value, note: e.target.value })}
        className="min-w-[14rem] flex-1 rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-2 py-1 text-[0.7rem] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)]"
      />
      {dirty && onSave && (
        <button
          type="button"
          aria-busy={pending}
          onClick={onSave}
          className={`rounded-[var(--radius-xs)] border border-[var(--color-rule-strong)] bg-[var(--color-paper-2)] px-2 py-1 text-[0.65rem] text-[var(--color-ink)] hover:bg-[var(--color-paper-3)] ${
            pending ? 'opacity-60' : ''
          }`}
        >
          Uložit poznámku
        </button>
      )}
    </div>
  );
}
