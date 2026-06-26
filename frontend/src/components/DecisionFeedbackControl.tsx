import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import {
  setDecisionFeedback,
  deleteDecisionFeedback,
  type DecisionFeedbackInput,
} from '@/lib/api';
import type { DecisionFeedback } from '@/lib/types';

/* The "this dedup decision was wrong" control — one shared component on BOTH the Decision
 * history feed and the Needs-review queue (the operator's ask: flag + note, filterable,
 * a labelled corpus for improving the flow). Pair-keyed (left/right sreality_id), so the
 * flag follows the pair across both surfaces. Civic-archive: brick = "wrong", a native
 * <select> for the single-choice direction, commit-on-Save. */

type Direction = '' | 'should_merge' | 'should_dismiss' | 'unsure';

const DIRECTIONS: { id: Direction; label: string }[] = [
  { id: '', label: 'Směr neurčen' },
  { id: 'should_merge', label: 'Mělo se sloučit' },
  { id: 'should_dismiss', label: 'Mělo se zamítnout' },
  { id: 'unsure', label: 'Nejisté' },
];
const DIRECTION_LABEL: Record<string, string> = Object.fromEntries(
  DIRECTIONS.filter((d) => d.id).map((d) => [d.id, d.label]),
);

export default function DecisionFeedbackControl({
  leftSrealityId,
  rightSrealityId,
  categoryMain,
  feedback,
}: {
  leftSrealityId: number | null;
  rightSrealityId: number | null;
  categoryMain?: string | null;
  feedback: DecisionFeedback | null | undefined;
}) {
  const qc = useQueryClient();
  const flagged = !!feedback?.is_incorrect;
  const [open, setOpen] = useState(false);
  const [direction, setDirection] = useState<Direction>(
    (feedback?.expected_outcome as Direction) ?? '',
  );
  const [note, setNote] = useState(feedback?.note ?? '');

  // Re-sync the draft when the saved flag changes (e.g. after invalidation).
  useEffect(() => {
    setDirection((feedback?.expected_outcome as Direction) ?? '');
    setNote(feedback?.note ?? '');
  }, [feedback?.expected_outcome, feedback?.note]);

  const canFlag = leftSrealityId != null && rightSrealityId != null;

  const save = useMutation({
    mutationFn: (body: DecisionFeedbackInput) => setDecisionFeedback(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['dedup'] });
      setOpen(false);
    },
  });
  const remove = useMutation({
    mutationFn: () =>
      deleteDecisionFeedback(leftSrealityId as number, rightSrealityId as number),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['dedup'] });
      setOpen(false);
    },
  });

  if (!canFlag) return null;

  const onSave = () =>
    save.mutate({
      left_sreality_id: leftSrealityId as number,
      right_sreality_id: rightSrealityId as number,
      is_incorrect: true,
      expected_outcome: direction || null,
      note: note.trim() || null,
      category_main: categoryMain ?? null,
    });

  const busy = save.isPending || remove.isPending;

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          title={flagged ? 'Upravit označení' : 'Označit toto rozhodnutí jako nesprávné'}
          className={[
            'inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[var(--radius-xs)] border text-[0.68rem] transition-colors',
            flagged
              ? 'border-[var(--color-brick)] bg-[var(--color-brick-soft)] text-[var(--color-brick)]'
              : 'border-[var(--color-rule)] text-[var(--color-ink-4)] hover:text-[var(--color-brick)] hover:border-[var(--color-brick)]',
          ].join(' ')}
        >
          <span aria-hidden>⚑</span>
          {flagged ? 'Nesprávné' : 'Označit jako nesprávné'}
        </button>
        {flagged && feedback?.expected_outcome && (
          <span className="text-[0.68rem] text-[var(--color-ink-4)]">
            {DIRECTION_LABEL[feedback.expected_outcome] ?? feedback.expected_outcome}
          </span>
        )}
        {flagged && feedback?.note && !open && (
          <span className="min-w-0 flex-1 truncate text-[0.7rem] text-[var(--color-ink-3)] italic">
            „{feedback.note}“
          </span>
        )}
      </div>

      {open && (
        <div className="flex flex-col gap-2 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/40 bg-[var(--color-brick-soft)]/40 px-2.5 py-2">
          <label className="flex items-center gap-2 text-[0.72rem] text-[var(--color-ink-3)]">
            Co se mělo stát:
            <select
              value={direction}
              onChange={(e) => setDirection(e.target.value as Direction)}
              disabled={busy}
              className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-1.5 py-0.5 text-[0.72rem] text-[var(--color-ink)] focus:outline-none focus:border-[var(--color-brick)]"
            >
              {DIRECTIONS.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.label}
                </option>
              ))}
            </select>
          </label>
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            disabled={busy}
            rows={2}
            placeholder="Co je špatně? (poznámka pro pozdější vylepšení dedup pravidel)"
            className="w-full resize-y rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-[0.74rem] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-brick)]"
          />
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onSave}
              disabled={busy}
              className="px-2 py-0.5 rounded-[var(--radius-xs)] border border-[var(--color-brick)] bg-[var(--color-brick-soft)] text-[0.72rem] text-[var(--color-brick)] hover:bg-[var(--color-brick)] hover:text-[var(--color-paper)] transition-colors disabled:opacity-50"
            >
              {save.isPending ? 'Ukládám…' : flagged ? 'Uložit změny' : 'Označit'}
            </button>
            {flagged && (
              <button
                type="button"
                onClick={() => remove.mutate()}
                disabled={busy}
                className="text-[0.72rem] text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)] underline decoration-dotted underline-offset-2 disabled:opacity-50"
              >
                {remove.isPending ? 'Odebírám…' : 'Odebrat označení'}
              </button>
            )}
            <button
              type="button"
              onClick={() => setOpen(false)}
              disabled={busy}
              className="ml-auto text-[0.72rem] text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)]"
            >
              Zavřít
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
