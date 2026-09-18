/* AUTODEDUP · one candidate group, whole — the drawer the groups queue has, here.
 *
 * WHAT IT ADDS TO THE CARD is what a developer project makes necessary: the
 * advert's OWN WORDS. Five units in one building share the render set, the price
 * band, the disposition and the storey count, and differ only in what the ad
 * says — "byt č. 14", "ve 4. NP", "68 m²". The card cannot carry that text (it
 * would drag a TOASTed description per member of every card on the page), so the
 * dialog reads it for the one card being opened, scrubbed (E28).
 *
 * AND THE EVIDENCE: every scored pair among these adverts, with the score, the
 * zone, the certificate and WHY IT WASN'T MERGED — the field that turns a review
 * session into design feedback. A pair inside an already-merged group is shown
 * as evidence and marked as such: it is not one of this card's questions.
 *
 * BLIND MODE TRAVELS IN. The judge column is withheld until the operator has
 * ruled on this card, exactly as the queue withholds it, and the drill-down link
 * carries `?blind=1` — a pair page that showed the transcript on arrival is the
 * hole in the blinding.
 */

import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import { getAutodedupCandidate, type AutodedupJudgementRow } from '@/lib/api';
import Dialog from '@/components/Dialog';
import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';
import { fmtCount } from '@/lib/format';
import { fmtScore } from '@/components/autodedup/EvidenceChips';
import { JudgeChip } from '@/components/autodedup/PairCard';
import MemberRow from '@/components/autodedup/MemberRow';
import { SplitRow, type SplitControls } from '@/components/autodedup/UnitSplit';
import { pairHref } from '@/components/autodedup/filterState';

const TH = 'py-1 pr-3 text-left font-medium whitespace-nowrap align-top';
const TD = 'py-1 pr-3 align-top';

export default function CandidateDialog({
  candidateKey,
  generation,
  split,
  blind,
  onClose,
}: {
  candidateKey: string;
  generation: string;
  split: SplitControls;
  blind: boolean;
  onClose: () => void;
}) {
  const detail = useQuery({
    queryKey: ['autodedup', 'candidate', candidateKey, generation],
    queryFn: () => getAutodedupCandidate(candidateKey, generation),
  });
  const data = detail.data?.data ?? null;
  const titleId = `autodedup-candidate-${candidateKey}`;

  const judgeByPair: Record<string, AutodedupJudgementRow> = {};
  for (const j of data?.judgements ?? []) judgeByPair[`${j.listing_lo}:${j.listing_hi}`] = j;

  const unitCount = data?.candidate.n_units ?? 2;

  return (
    <Dialog open onClose={onClose} labelledBy={titleId} className="w-[64rem] max-w-full p-5">
      <h2 id={titleId} className="text-lg">
        Kandidát <span className="font-mono">#{candidateKey}</span>
      </h2>
      {detail.isPending && (
        <p className="mt-4 flex items-center gap-2 text-sm text-[var(--color-ink-3)]">
          <Spinner /> Načítám skupinu…
        </p>
      )}
      {detail.error && <ErrorBanner message={(detail.error as Error).message} />}
      {data && (
        <div className="mt-4 space-y-5">
          <p className="text-[0.75rem] text-[var(--color-ink-3)]">
            {fmtCount(data.candidate.size)} inzerátů ve {fmtCount(data.candidate.n_units)}{' '}
            jednotkách · {fmtCount(data.candidate.n_pairs)} dvojic k rozhodnutí · engine je{' '}
            <strong>nesloučil</strong>
          </p>
          <ul className="space-y-3">
            {data.members.map((m) => (
              <MemberRow
                key={m.listing_id}
                member={m}
                split={split}
                count={unitCount}
                /* The lock is named on the row itself here: in a vertical list
                 * the bracket the card draws around a merged group is gone, and
                 * a locked letter with no explanation reads as a broken control. */
                badge={
                  m.unit_lock != null
                    ? `už sloučeno · skupina #${m.unit_lock} — celá skupina má jedno písmeno`
                    : undefined
                }
                unitLabel={
                  m.unit_lock != null ? `Jednotka skupiny #${m.unit_lock}` : undefined
                }
                /* Moving ONE advert of a merged group is a statement about that
                 * group, and this route refuses it — so the control says so by
                 * not moving. The card's group-level select is how the whole
                 * locked unit changes letter. */
                unitDisabled={m.unit_lock != null}
              />
            ))}
          </ul>

          <SplitRow
            members={data.members}
            split={split}
            generation={generation}
            notesOpen
            alwaysOpen
            saveLabel="Uložit rozhodnutí"
            mergeBackLabel="Uložit rozhodnutí"
            showReasons={false}
          />

          <section>
            <h3 className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
              Dvojice
            </h3>
            <div className="mt-1 overflow-x-auto">
              <table className="w-full text-[0.72rem]">
                <thead>
                  <tr className="text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
                    <th className={TH}>Dvojice</th>
                    <th className={TH}>Skóre</th>
                    <th className={TH}>Pásmo</th>
                    <th className={TH}>Proč nesloučeno</th>
                    <th className={TH}>Soudce</th>
                    <th className={TH}></th>
                  </tr>
                </thead>
                <tbody>
                  {data.pairs.map((p) => {
                    const judge = judgeByPair[`${p.listing_lo}:${p.listing_hi}`];
                    return (
                      <tr
                        key={`${p.listing_lo}:${p.listing_hi}`}
                        className="border-t border-[var(--color-rule-soft)]"
                      >
                        <td className={`${TD} font-mono tabular-nums`}>
                          {p.listing_lo} · {p.listing_hi}
                          {p.residual === false && (
                            <span className="ml-1 font-sans text-[0.6rem] text-[var(--color-ink-4)]">
                              uvnitř sloučené skupiny
                            </span>
                          )}
                        </td>
                        <td className={`${TD} font-mono tabular-nums`}>{fmtScore(p.score)}</td>
                        <td className={TD}>{p.zone ?? '—'}</td>
                        <td className={TD}>{p.why_not_merged ?? '—'}</td>
                        <td className={TD}>
                          {blind ? (
                            <span className="text-[var(--color-ink-4)]">skryto</span>
                          ) : judge ? (
                            <JudgeChip judgement={judge} />
                          ) : (
                            '—'
                          )}
                        </td>
                        <td className={TD}>
                          <Link
                            to={pairHref(p.listing_lo, p.listing_hi, generation, blind)}
                            className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
                          >
                            Evidence
                          </Link>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      )}
      <div className="mt-5">
        <button
          type="button"
          onClick={onClose}
          className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-3 py-1.5 text-sm text-[var(--color-ink-2)] hover:text-[var(--color-ink)]"
        >
          Close
        </button>
      </div>
    </Dialog>
  );
}
