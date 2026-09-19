/* AUTODEDUP · the ochre notice a carried-over cluster verdict wears (rule E58).
 *
 * A CLUSTER VERDICT IS A STATEMENT ABOUT A SET OF ADVERTS, not about a key. The
 * key is the smallest listing id in the group, so two clustering passes over one
 * cohort produce the same keys — and promoting g5 re-stamped 836 of g4's 870
 * groups under the operator's 224 confirmations. 203 still named the same
 * adverts; 21 did not (11 grown by a bridge, 10 absorbed into another key), and
 * nothing on any surface said so.
 *
 * SO THE GROUP READS AS UNREVIEWED AND SAYS WHY. The earlier ruling is not
 * hidden and not silently inherited: it is shown as what it is — a ruling about
 * a different set — with the adverts that arrived and the ones that left named
 * by id, and the ordinary verdict buttons underneath to rule again. Ochre, not
 * red: nothing is broken and nothing was lost, the question simply changed.
 */

import { type AutodedupStaleVerdict } from '@/lib/api';

const IDS = 'font-mono tabular-nums';

function idList(ids: number[]): string {
  return ids.map((id) => `#${id}`).join(', ');
}

export function staleVerdictText(stale: AutodedupStaleVerdict): string {
  const where = stale.generation ? `v ${stale.generation}` : 'dříve';
  const parts: string[] = [];
  if (stale.added.length > 0) parts.push(`přibyly ${idList(stale.added)}`);
  if (stale.removed.length > 0) parts.push(`ubyly ${idList(stale.removed)}`);
  /* Both sides empty is possible — a detail read with no member list resolved —
   * and the notice still has to say that the ruling was taken on another set. */
  const moved = parts.length > 0 ? `: ${parts.join(', ')}` : '.';
  return `Potvrzeno ${where} pro jinou sestavu inzerátů${moved}`;
}

export default function StaleVerdictNotice({
  stale,
}: {
  stale: AutodedupStaleVerdict | null;
}) {
  if (!stale) return null;
  return (
    <p
      role="note"
      data-testid="stale-verdict-notice"
      className="rounded-[var(--radius-sm)] border border-[var(--color-ochre)] bg-[var(--color-ochre-soft)] px-3 py-2 text-[0.72rem] leading-relaxed text-[var(--color-ink-2)]"
    >
      <span className="text-[var(--color-ink)]">{staleVerdictText(stale)}</span>{' '}
      <span className="text-[var(--color-ink-3)]">
        (verdikt <span className="lowercase">{stale.verdict}</span>
        {stale.decided_at ? `, ${stale.decided_at.slice(0, 10)}` : ''}). Skupina je
        vedena jako neposouzená — posuďte ji prosím znovu.
      </span>
      {stale.added.length > 0 && (
        <>
          {' '}
          <span className={`${IDS} text-[var(--color-ink-3)]`}>
            nové: {idList(stale.added)}
          </span>
        </>
      )}
    </p>
  );
}
