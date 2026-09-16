/* AUTODEDUP · the operator's verdict, on a pair or on a group.
 *
 * FOUR ANSWERS, ALWAYS THE SAME FOUR (§12). "Same", "different", "same
 * building, different unit" and "unsure" are the stored vocabulary; only the
 * WORDING changes per surface, because "Confirm" reads right over a proposed
 * group and "This IS a duplicate" reads right over a pair the engine rejected.
 * The stored value never changes with the wording — that is why the labels are
 * a prop and the values are not.
 *
 * THE THIRD ANSWER IS NOT A SOFTER "DIFFERENT". `same_building_different_unit`
 * is the negative control this whole program is calibrated against, and folding
 * it into "different" would destroy exactly the signal the trial needs.
 *
 * TWO-STEP ON A NEGATIVE PAIR VERDICT. A negative verdict on a PAIR writes a
 * permanent must-not-link server-side — it outlives every recalibration — so it
 * takes a second, deliberate click. A cluster verdict records an opinion about
 * the group and nothing permanent, so it does not arm.
 */

import { useEffect, useState } from 'react';

import type { AutodedupVerdictRow, AutodedupVerdictValue } from '@/lib/api';

export const VERDICT_VALUES: ReadonlyArray<AutodedupVerdictValue> = [
  'same',
  'different',
  'same_building_different_unit',
  'unsure',
];

/* The permanent ones — the two that also mean "never link these again". */
export const NEGATIVE_VERDICTS: ReadonlyArray<AutodedupVerdictValue> = [
  'different',
  'same_building_different_unit',
];

export const GROUP_LABELS: Record<AutodedupVerdictValue, string> = {
  same: 'Confirm',
  different: 'Not the same',
  same_building_different_unit: 'Same building, different unit',
  unsure: 'Unsure',
};

export const PAIR_LABELS: Record<AutodedupVerdictValue, string> = {
  same: 'This IS a duplicate',
  different: 'Correctly separate',
  same_building_different_unit: 'Same building, different unit',
  unsure: 'Unsure',
};

const TONE: Record<AutodedupVerdictValue, string> = {
  same: 'border-[var(--color-sage)] text-[var(--color-sage)] hover:bg-[var(--color-sage-soft)]',
  different: 'border-[var(--color-brick)] text-[var(--color-brick)] hover:bg-[var(--color-brick-soft)]',
  same_building_different_unit:
    'border-[var(--color-ochre)] text-[var(--color-ochre)] hover:bg-[var(--color-ochre-soft)]',
  unsure: 'border-[var(--color-rule-strong)] text-[var(--color-ink-3)] hover:bg-[var(--color-paper)]',
};

const SELECTED: Record<AutodedupVerdictValue, string> = {
  same: 'bg-[var(--color-sage-soft)]',
  different: 'bg-[var(--color-brick-soft)]',
  same_building_different_unit: 'bg-[var(--color-ochre-soft)]',
  unsure: 'bg-[var(--color-paper)]',
};

export default function VerdictButtons({
  kind,
  verdict,
  onVerdict,
  pending = false,
  labels,
}: {
  kind: 'pair' | 'cluster';
  /* The stored verdict, or null when nobody has ruled yet. */
  verdict: AutodedupVerdictRow | null;
  onVerdict: (value: AutodedupVerdictValue) => void;
  pending?: boolean;
  labels?: Record<AutodedupVerdictValue, string>;
}) {
  const words = labels ?? (kind === 'cluster' ? GROUP_LABELS : PAIR_LABELS);
  const [armed, setArmed] = useState<AutodedupVerdictValue | null>(null);

  /* Disarm as soon as a verdict lands, so a stored answer never leaves a
   * primed second click behind it. */
  useEffect(() => {
    setArmed(null);
  }, [verdict?.verdict, verdict?.decided_at]);

  const click = (value: AutodedupVerdictValue) => {
    const needsConfirm = kind === 'pair' && NEGATIVE_VERDICTS.includes(value);
    if (needsConfirm && armed !== value) {
      setArmed(value);
      return;
    }
    setArmed(null);
    onVerdict(value);
  };

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {VERDICT_VALUES.map((value) => {
        const isStored = verdict?.verdict === value;
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
            {isArmed ? 'Click again to confirm' : words[value]}
          </button>
        );
      })}
      {verdict && (
        <span className="text-[0.65rem] text-[var(--color-ink-3)]">
          {/* Who ruled and when — the session's own audit trail, and the thing
            * that tells a second reviewer the group was already seen. */}
          {words[verdict.verdict] ?? verdict.verdict} · {verdict.decided_by}
        </span>
      )}
    </div>
  );
}
