import { useMutation } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { requestModelCompare, type ModelCompareResponse } from '@/lib/api';
import { pushToast } from '@/lib/toast';

/*
 * "Convene the models" — decision support on the /dedup review queue. Hands undecided pair(s) to
 * every connected vision model; verdicts land on /model-testing. Deliberately a SECONDARY affordance
 * (bordered, copper on hover) so it reads as "ask for a second opinion", distinct from the primary
 * filled-copper merge/approve actions the operator commits with. `tone`:
 *   - 'bar'  full-width queue-level action (compare the next N undecided)
 *   - 'chip' compact per-card action (compare just this pair)
 */

// A small "compare / poll" glyph — three ballots of differing height. Icon clarifies, doesn't
// decorate: it reads as "several verdicts, side by side".
function CompareGlyph() {
  return (
    <svg viewBox="0 0 16 16" className="h-3.5 w-3.5 shrink-0" fill="none" aria-hidden="true">
      <rect x="1.5" y="7" width="3" height="7" rx="0.6" fill="currentColor" opacity="0.55" />
      <rect x="6.5" y="3.5" width="3" height="10.5" rx="0.6" fill="currentColor" opacity="0.8" />
      <rect x="11.5" y="9" width="3" height="5" rx="0.6" fill="currentColor" opacity="0.4" />
    </svg>
  );
}

export function ModelCompareButton({
  candidateIds,
  limit,
  label,
  tone = 'chip',
}: {
  candidateIds?: number[];
  limit?: number;
  label: string;
  tone?: 'bar' | 'chip';
}) {
  const mut = useMutation({
    mutationFn: (): Promise<ModelCompareResponse> =>
      requestModelCompare(candidateIds ? { candidate_ids: candidateIds } : { limit }),
    onSuccess: (r) =>
      pushToast(
        'ok',
        `${r.models.length} models convening on ${r.pair_count} pair${r.pair_count === 1 ? '' : 's'} — verdicts land in Model Testing shortly.`,
      ),
    onError: (e: Error) => pushToast('err', e.message),
  });
  const done = mut.data;

  if (tone === 'bar') {
    return (
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-inset)] px-4 py-3">
        <div className="min-w-0 text-sm text-[var(--color-ink-2)]">
          <span className="font-medium text-[var(--color-ink)]">Second opinion</span> — send the
          oldest undecided pairs to every model and compare their verdicts side by side.
          {done ? (
            <>
              {' '}
              <Link
                to={`/model-testing?run=${encodeURIComponent(done.run_label)}`}
                className="text-[var(--color-copper)] underline underline-offset-2 hover:text-[var(--color-copper-2)]"
              >
                View {done.pair_count}-pair result →
              </Link>
            </>
          ) : null}
        </div>
        <button
          type="button"
          onClick={() => mut.mutate()}
          disabled={mut.isPending}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1.5 text-sm text-[var(--color-ink)] transition-colors hover:border-[var(--color-copper)] hover:text-[var(--color-copper)] disabled:opacity-50"
        >
          <CompareGlyph />
          {mut.isPending ? 'Convening…' : label}
        </button>
      </div>
    );
  }

  return (
    <span className="inline-flex items-center gap-2">
      <button
        type="button"
        onClick={() => mut.mutate()}
        disabled={mut.isPending}
        title="Ask every connected model whether these are the same property"
        className="inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-2.5 py-1 text-[0.78rem] text-[var(--color-ink-2)] transition-colors hover:border-[var(--color-copper)] hover:text-[var(--color-copper)] disabled:opacity-50"
      >
        <CompareGlyph />
        {mut.isPending ? 'Convening…' : label}
      </button>
      {done ? (
        <Link
          to={`/model-testing?run=${encodeURIComponent(done.run_label)}`}
          className="text-[0.78rem] text-[var(--color-copper)] underline underline-offset-2 hover:text-[var(--color-copper-2)]"
        >
          View →
        </Link>
      ) : null}
    </span>
  );
}
