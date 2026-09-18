/* AUTODEDUP · the validation session's own two controls.
 *
 * THE STRIP answers the question the operator actually asked — "how many do I
 * need to do?" — and it answers it twice, because there are two honest answers.
 * The whole generation says how much of the queue carries a ruling at all. The
 * SAMPLE says how far through the first 100 of the seeded random order they are,
 * and that is the number the program's gate (D6) is measured on: an error rate
 * counted on the weakest-edge queue is an error rate about the weakest edges.
 *
 * THE SAMPLE IS NOT NARROWED BY THE FILTER BAR, and the strip says so in words
 * rather than letting "37 / 100" quietly mean something different on every page.
 *
 * BLIND MODE is the other half of the same measurement. An agreement number
 * between an operator who has just read "judge: same property 0.93" and the
 * judge that wrote it is not an independent check of anything — it measures how
 * persuasive the chip is. So the judge is hidden until the operator has recorded
 * their own verdict on that pair, and revealed the moment they have: the review
 * stays blind, the LEARNING does not.
 */

import { useQuery } from '@tanstack/react-query';

import { getAutodedupValidationProgress } from '@/lib/api';
import { fmtCount } from '@/lib/format';

export interface ValidationStripProps {
  surface: 'groups' | 'residual';
  /* The pass the QUEUE read, so the strip and the rows count one generation.
   * Null before the first page lands — the read is skipped rather than asked
   * about a pass nobody named. */
  generation: string | null;
  seed: string;
  /* Whether the page is IN the seeded order. The sample half only renders then:
   * a "37 / 100" over a weakest-edge queue would be a progress bar for a walk
   * the operator is not taking. */
  sampleOrder: boolean;
  /* The residual view's display floor — the cohort its sample is drawn from. */
  minScore?: number | null;
}

const NOUNS: Record<'groups' | 'residual', { one: string; many: string }> = {
  groups: { one: 'skupina', many: 'skupin' },
  residual: { one: 'dvojice', many: 'dvojic' },
};

const noun = (surface: 'groups' | 'residual', n: number): string =>
  n === 1 ? NOUNS[surface].one : NOUNS[surface].many;

export default function ValidationStrip({
  surface,
  generation,
  seed,
  sampleOrder,
  minScore,
}: ValidationStripProps) {
  const q = useQuery({
    queryKey: ['autodedup', 'validation-progress', surface, generation, seed, minScore ?? null],
    queryFn: () =>
      getAutodedupValidationProgress({
        surface,
        generation,
        seed,
        min_score: surface === 'residual' ? (minScore ?? null) : null,
      }),
    enabled: generation != null,
  });

  const data = q.data?.data ?? null;
  /* An un-migrated store or a read that has not landed says nothing at all —
   * a strip of zeros would read as "you have reviewed none of 0", which is the
   * one answer that is never true. */
  if (!data) return null;

  const { sample, total } = data;
  const sampleDone = Math.min(sample.n_reviewed, sample.n);
  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-4 py-2 text-[0.72rem] text-[var(--color-ink-2)]">
      <span>
        <span className="text-[var(--color-ink-4)]">Zkontrolováno: </span>
        <span className="font-mono tabular-nums">
          {fmtCount(total.n_reviewed)} / {fmtCount(total.n)}
        </span>{' '}
        {noun(surface, total.n)}
        {surface === 'groups' && total.n_reviewed > 0 && (
          <span className="text-[var(--color-ink-4)]">
            {' '}· z toho {fmtCount(total.n_not_same)} jiných než „stejné“
          </span>
        )}
      </span>

      {sampleOrder && (
        <span
          className="rounded-[var(--radius-xs)] border border-[var(--color-ochre)] bg-[var(--color-ochre-soft)] px-2 py-0.5 text-[var(--color-ochre)]"
          data-testid="validation-sample"
        >
          Náhodný vzorek: <span className="font-mono tabular-nums">{fmtCount(sampleDone)} / {fmtCount(sample.n)}</span>{' '}
          zkontrolováno
          {surface === 'groups' && (
            <>
              {' · '}
              <span className="font-mono tabular-nums">{fmtCount(sample.n_not_same)}</span> jiných
              než „stejné“
            </>
          )}
        </span>
      )}

      {sampleOrder && (
        <span className="text-[var(--color-ink-4)]">
          semínko <span className="font-mono">{data.seed}</span> — vzorek je prvních{' '}
          {fmtCount(data.sample_size)} v tomto pořadí nad celou generací, filtry ho nemění
        </span>
      )}
    </div>
  );
}

/* One checkbox, in the words the operator uses. NOT a button: it is a mode that
 * stays on for a whole session, and its state has to be readable at a glance
 * from anywhere on the page. */
export function BlindToggle({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <label className="mt-3 flex w-fit items-center gap-2 text-[0.72rem] text-[var(--color-ink-2)]">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="accent-[var(--color-copper-2)]"
      />
      <span>
        naslepo (skrýt verdikt soudce)
        <span className="text-[var(--color-ink-4)]">
          {' '}— verdikt soudce se ukáže až po uložení vlastního verdiktu
        </span>
      </span>
    </label>
  );
}
