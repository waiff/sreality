/* AUTODEDUP · the operator's verdict, on a pair or on a group.
 *
 * THREE ANSWERS, ALWAYS THE SAME THREE (D39). "Stejné", "Různé" and "Nevím".
 * The engine consumes every negative identically — one permanent must-not-link,
 * one negative calibration label (§9) — so the finer breakdown migration 532
 * offered ("same building, different unit", "same project, different unit")
 * bought no decision and cost the operator clicks. It is gone from the page and
 * the wording is ONE vocabulary on every surface, because "different or same"
 * is the whole question this queue asks.
 *
 * THE STORE KEEPS ITS HISTORY. A ruling taken under the older vocabulary is
 * still in `autodedup.verdicts` and nothing rewrites it, so a stored finer value
 * DISPLAYS as "Různé" and presses that button — `displayVerdict` is the one
 * place that mapping lives.
 *
 * THE ANNOTATION TRAVELS WITH THE CLICK. The chips and the note live one level
 * up (a page-level draft, beside the verdict overlay), because the same
 * annotation has to reach the POST this component fires AND the "Uložit
 * poznámku" re-post that `VerdictNotes` fires — two writers of one value. So
 * this component only carries it through: `onVerdict(value, annotation)`. Both
 * are OPTIONAL: a verdict saves with neither.
 *
 * TWO-STEP ON A NEGATIVE PAIR VERDICT. A negative verdict on a PAIR writes a
 * permanent must-not-link server-side — it outlives every recalibration — so it
 * takes a second, deliberate click. A cluster verdict records an opinion about
 * the group and nothing permanent, so it does not arm.
 */

import { useEffect, useState } from 'react';

import type { AutodedupVerdictRow, AutodedupVerdictValue } from '@/lib/api';
import { ReasonChips, annotationInput, EMPTY_ANNOTATION, type VerdictAnnotation } from './VerdictNotes';

/* What the page OFFERS — a subset of what the store may hold. */
export type OfferedVerdict = Extract<AutodedupVerdictValue, 'same' | 'different' | 'unsure'>;

export const VERDICT_VALUES: ReadonlyArray<OfferedVerdict> = ['same', 'different', 'unsure'];

/* The permanent ones — the verdicts that also mean "never link these again".
 * The two finer values are here because the STORE still holds them, not because
 * the page offers them. */
export const NEGATIVE_VERDICTS: ReadonlyArray<AutodedupVerdictValue> = [
  'different',
  'same_building_different_unit',
  'same_project_different_unit',
];

/* Every stored value, as one of the three the page speaks. */
export function displayVerdict(value: AutodedupVerdictValue): OfferedVerdict {
  if (value === 'same' || value === 'unsure') return value;
  return 'different';
}

export const VERDICT_LABELS: Record<OfferedVerdict, string> = {
  same: 'Stejné',
  different: 'Různé',
  unsure: 'Nevím',
};

const TONE: Record<OfferedVerdict, string> = {
  same: 'border-[var(--color-sage)] text-[var(--color-sage)] hover:bg-[var(--color-sage-soft)]',
  different: 'border-[var(--color-brick)] text-[var(--color-brick)] hover:bg-[var(--color-brick-soft)]',
  unsure: 'border-[var(--color-rule-strong)] text-[var(--color-ink-3)] hover:bg-[var(--color-paper)]',
};

const SELECTED: Record<OfferedVerdict, string> = {
  same: 'bg-[var(--color-sage-soft)]',
  different: 'bg-[var(--color-brick-soft)]',
  unsure: 'bg-[var(--color-paper)]',
};

export default function VerdictButtons({
  kind,
  verdict,
  onVerdict,
  pending = false,
  annotation = EMPTY_ANNOTATION,
}: {
  kind: 'pair' | 'cluster';
  /* The stored verdict, or null when nobody has ruled yet. */
  verdict: AutodedupVerdictRow | null;
  onVerdict: (
    value: AutodedupVerdictValue,
    annotation: { reasons: string[]; note: string | null },
  ) => void;
  pending?: boolean;
  /* The chips and the note as they stand on screen, sent with the verdict. */
  annotation?: VerdictAnnotation;
}) {
  const [armed, setArmed] = useState<OfferedVerdict | null>(null);

  /* Disarm as soon as a verdict lands, so a stored answer never leaves a
   * primed second click behind it. */
  useEffect(() => {
    setArmed(null);
  }, [verdict?.verdict, verdict?.decided_at]);

  const click = (value: OfferedVerdict) => {
    const needsConfirm = kind === 'pair' && value === 'different';
    if (needsConfirm && armed !== value) {
      setArmed(value);
      return;
    }
    setArmed(null);
    onVerdict(value, annotationInput(annotation));
  };

  /* A ruling taken under the older vocabulary presses the button it MEANS. */
  const stored = verdict ? displayVerdict(verdict.verdict) : null;

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {VERDICT_VALUES.map((value) => {
        const isStored = stored === value;
        const isArmed = armed === value;
        return (
          <button
            key={value}
            type="button"
            /* NOT disabled while the write is in flight: disabling the button
             * that was just clicked blurs it, and focus lands on <body> — a
             * keyboard operator loses their place in the queue after every
             * verdict. `aria-busy` says the same thing without moving focus. */
            aria-busy={pending}
            aria-pressed={isStored}
            onClick={() => click(value)}
            className={[
              'rounded-[var(--radius-sm)] border px-2 py-1 text-[0.7rem] transition-colors',
              pending ? 'opacity-60' : '',
              TONE[value],
              isStored ? SELECTED[value] : '',
              isArmed ? 'ring-1 ring-[var(--color-focus)]' : '',
            ].join(' ')}
          >
            {isArmed ? 'Klikněte znovu pro potvrzení' : VERDICT_LABELS[value]}
          </button>
        );
      })}
      {verdict && stored && (
        <span className="flex flex-wrap items-center gap-1 text-[0.65rem] text-[var(--color-ink-3)]">
          {/* Who ruled and when — the session's own audit trail, and the thing
            * that tells a second reviewer the group was already seen. */}
          {VERDICT_LABELS[stored]} · {verdict.decided_by}
          {/* WHAT THEY SAW, beside what they decided — when they said it: the
            * chips and the note are optional everywhere, so a verdict taken
            * without them renders exactly as it was taken. */}
          <ReasonChips codes={verdict.reasons} />
        </span>
      )}
    </div>
  );
}
