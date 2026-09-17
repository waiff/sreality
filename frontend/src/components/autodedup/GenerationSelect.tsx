/* AUTODEDUP · WHICH PASS the validation views are reading.
 *
 * WHAT WAS WRONG. Every validation view defaulted to generation `g1`: the
 * page's own constant, the API's `Query` default, the block vocabulary behind
 * the filter. `g1` is the first hand-prior pass — it over-merged developer units
 * and was superseded by `g2` and `g3` — so the queue kept handing the operator
 * groups the live engine no longer proposes, with nothing on screen saying so.
 * Reviewing a retired pass is worse than reviewing nothing: the verdicts are
 * real, they land as permanent must-not-links and as calibration labels, and
 * they are about edges the current engine never drew.
 *
 * WHAT IT IS NOW. "Which generation" is a fact of the store, not a constant:
 * the empty value means "the newest pass", resolved server-side, and the picker
 * offers the passes that actually exist with how much each holds. An older one
 * stays selectable — that is how a past review is re-examined — but the page
 * says out loud that it is not the current one (`GenerationNotice`).
 *
 * FAILURE IS QUIET, NOT FATAL. If the vocabulary read fails the select still
 * renders with "nejnovější" and whatever the URL carried: losing the list of
 * passes must never cost the operator the queue.
 */

import { useQuery } from '@tanstack/react-query';

import { getAutodedupGenerations, type AutodedupGenerationRollup } from '@/lib/api';
import { fmtCount } from '@/lib/format';

export interface GenerationVocabulary {
  items: AutodedupGenerationRollup[];
  /* The pass the engine wrote last — what the empty filter resolves to. */
  latest: string | null;
  isPending: boolean;
}

export function useAutodedupGenerations(): GenerationVocabulary {
  const q = useQuery({
    queryKey: ['autodedup', 'generations'],
    queryFn: getAutodedupGenerations,
    /* The set of passes changes only when the lane rebuilds a clustering. */
    staleTime: 5 * 60_000,
    retry: false,
  });
  return {
    items: q.data?.data?.items ?? [],
    latest: q.data?.data?.latest ?? null,
    isPending: q.isPending,
  };
}

/* "g3 · 1 204 skupin". The count is what makes the choice legible: a pass with
 * nine clusters is a probe, not the queue anyone should be reviewing. */
export function generationLabel(row: AutodedupGenerationRollup): string {
  return `${row.generation} · ${fmtCount(row.n_clusters)} skupin`;
}

export default function GenerationSelect({
  value,
  onChange,
  label = 'Generation',
  labelClassName,
  controlClassName,
}: {
  /* '' means "the newest pass" — the parameter is then left off the wire and the
   * server resolves it, so a bookmarked queue follows the engine forward. */
  value: string;
  onChange: (next: string) => void;
  label?: string;
  labelClassName?: string;
  controlClassName?: string;
}) {
  const { items, latest, isPending } = useAutodedupGenerations();
  /* A generation a link carries that the vocabulary does not list stays
   * selected rather than snapping the operator onto another pass behind their
   * back — the same rule the block picker follows. */
  const unknown = value !== '' && !items.some((g) => g.generation === value) ? value : null;

  return (
    <label className="block">
      <span className={labelClassName}>{label}</span>
      <select
        className={controlClassName}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">
          {isPending ? 'nejnovější (načítám…)' : latest ? `nejnovější (${latest})` : 'nejnovější'}
        </option>
        {unknown && <option value={unknown}>{unknown}</option>}
        {items.map((g) => (
          <option key={g.generation} value={g.generation}>
            {generationLabel(g)}
          </option>
        ))}
      </select>
    </label>
  );
}

/* Said out loud, and undoable in one click: a queue of a superseded pass looks
 * exactly like the live one, and every minute spent on it is spent on proposals
 * the engine has already stopped making. Renders nothing while the two agree —
 * and nothing at all until both are known, because "unknown" must not read as
 * "stale". */
export function GenerationNotice({
  generation,
  latest,
  onLatest,
}: {
  /* The pass the view actually read, as the server echoed it back. */
  generation: string | null;
  latest: string | null;
  onLatest: () => void;
}) {
  if (!generation || !latest || generation === latest) return null;
  return (
    <p
      role="status"
      className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-4 py-2 text-[0.78rem] text-[var(--color-ink-2)]"
    >
      <span>
        Prohlížíte starší generaci <strong className="text-[var(--color-ink)]">{generation}</strong>{' '}
        — nejnovější je <strong className="text-[var(--color-ink)]">{latest}</strong>. Tyto návrhy
        engine už nedělá.
      </span>
      <button
        type="button"
        onClick={onLatest}
        className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-2 py-0.5 text-[0.72rem] text-[var(--color-ink-2)] hover:text-[var(--color-ink)]"
      >
        Zobrazit nejnovější ({latest})
      </button>
    </p>
  );
}
