/* AUTODEDUP · WHY the operator ruled that way — reason chips plus one note.
 *
 * THE FEATURE-GAP LOOP, NOT A COMMENT FIELD (PROGRAM.md §9). The verdict says
 * WHAT was decided; these say what the operator SAW. A chip like "Jiný půdorys"
 * on a pair the engine merged names a discriminator the feature set missed, and
 * the histogram of the codes is directly comparable with the judge's
 * `unit_discriminator`. The free-text note is the half a vocabulary cannot
 * hold; it rides beside the codes rather than instead of them.
 *
 * THE VOCABULARY IS SERVED, NEVER HARD-CODED HERE. `GET /autodedup/verdict-reasons`
 * is the one registry (`autodedup/verdict_reasons.py`); a list copied into the
 * browser would be a second one, and it would drift the first time a review
 * session names a shape the server already knows. Until the read lands the note
 * input still works — a missing registry must not block the note.
 *
 * COLLAPSED BY DEFAULT, because the residual queue is a scroll: a reason picker
 * open on every row pushes the next pair off the screen, which is the one thing
 * a review queue may not do. The pair page and the group dialog, where exactly
 * one subject is on screen, open it.
 *
 * WHAT "DIRTY" MEANS HERE. Chips and note hydrate from the STORED verdict, so a
 * row ruled last week reads back its own evidence. Editing them after a verdict
 * is stored is not itself a write — it arms "Uložit poznámku", which re-posts
 * the SAME verdict with the new annotation through the same endpoint and the
 * same optimistic overlay. A note that saved itself on every keystroke would
 * write a verdict per character.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import {
  getAutodedupVerdictReasons,
  type AutodedupVerdictReason,
  type AutodedupVerdictRow,
} from '@/lib/api';

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

/* One query for the whole page — react-query dedupes it across every row, and
 * the vocabulary does not change inside a session. */
export function useVerdictReasons(): AutodedupVerdictReason[] {
  const q = useQuery({
    queryKey: ['autodedup', 'verdict-reasons'],
    queryFn: getAutodedupVerdictReasons,
    staleTime: Infinity,
    retry: false,
  });
  return q.data ?? [];
}

export function useReasonLabels(): (code: string) => string {
  const reasons = useVerdictReasons();
  const byCode = useMemo(() => {
    const out: Record<string, string> = {};
    for (const r of reasons) out[r.code] = r.label;
    return out;
  }, [reasons]);
  /* An unknown code renders as its code — a verdict stored before a label was
   * renamed still shows something true rather than an empty chip. */
  return useCallback((code: string) => byCode[code] ?? code, [byCode]);
}

/* The stored reasons as read-only chips, shown wherever a verdict badge is. */
export function ReasonChips({ codes }: { codes: ReadonlyArray<string> | null | undefined }) {
  const label = useReasonLabels();
  if (!codes || codes.length === 0) return null;
  return (
    <>
      {codes.map((code) => (
        <span
          key={code}
          className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-1.5 py-0.5 text-[0.6rem] text-[var(--color-ink-3)]"
        >
          {label(code)}
        </span>
      ))}
    </>
  );
}

export default function VerdictNotes({
  value,
  onChange,
  defaultOpen = false,
  dirty = false,
  onSave,
  pending = false,
  label = 'důvod / poznámka',
  showReasons = true,
}: {
  value: VerdictAnnotation;
  onChange: (next: VerdictAnnotation) => void;
  defaultOpen?: boolean;
  /* The stored verdict and the controls disagree — the save button is armed. */
  dirty?: boolean;
  onSave?: () => void;
  pending?: boolean;
  label?: string;
  /* OFF where the ruling has no row to carry chips. A candidate split writes
   * only pair rows, and §9 keeps reason chips off a pairwise fan-out — the
   * server answers 400 — so offering the chips there would be offering a
   * control whose clicks are refused. The note still travels. */
  showReasons?: boolean;
}) {
  /* The hook runs unconditionally — the registry read is shared and cached, and
   * a conditional hook is a re-render hazard; only the CHIPS are withheld. */
  const registry = useVerdictReasons();
  const reasons = showReasons ? registry : [];
  const [open, setOpen] = useState(defaultOpen);

  /* A row that arrives already annotated opens itself: hiding the operator's own
   * evidence behind a toggle is the write-only failure one level down. */
  useEffect(() => {
    if (value.reasons.length > 0 || value.note !== '') setOpen(true);
  }, [value.reasons.length, value.note]);

  const toggle = (code: string) => {
    const next = value.reasons.includes(code)
      ? value.reasons.filter((c) => c !== code)
      : [...value.reasons, code];
    onChange({ ...value, reasons: next });
  };

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
    <div className="space-y-1.5">
      {reasons.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {reasons.map((reason) => {
            const on = value.reasons.includes(reason.code);
            return (
              <button
                key={reason.code}
                type="button"
                aria-pressed={on}
                onClick={() => toggle(reason.code)}
                className={[
                  'rounded-[var(--radius-xs)] border px-1.5 py-0.5 text-[0.62rem] transition-colors',
                  on
                    ? 'border-[var(--color-ink-3)] bg-[var(--color-paper-3)] text-[var(--color-ink)]'
                    : 'border-[var(--color-rule)] bg-[var(--color-paper)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]',
                ].join(' ')}
              >
                {reason.label}
              </button>
            );
          })}
        </div>
      )}
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
    </div>
  );
}
