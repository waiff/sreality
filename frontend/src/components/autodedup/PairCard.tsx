/* AUTODEDUP · one pair, compared.
 *
 * THE one comparison component (§12: "build exactly one new comparison
 * component, PairCard, used by both verdict views"). The residual list renders
 * a stack of these; the group drawer and the pair page render the same card for
 * a single edge, so an edge can never look like one thing in a list and another
 * on a detail page.
 *
 * WHAT IT SHOWS, IN THE ORDER THE QUESTION IS ASKED: the two adverts side by
 * side (photos first — a human answers most of these from the photos alone),
 * then the attribute diff, then WHY THE ENGINE DID NOT MERGE THEM, then the
 * model's own breakdown, then the judge, then the four answers. "Why not" sits
 * above the machinery on purpose: it is the field that turns a review session
 * into design feedback.
 */

import { Link } from 'react-router-dom';

import type {
  AutodedupContribution,
  AutodedupJudgementRow,
  AutodedupMember,
  AutodedupVerdictRow,
  AutodedupVerdictValue,
  AutodedupZone,
} from '@/lib/api';
import { type RoutePath } from '@/lib/routes';
import AttrDiffTable, { memberDiffRows } from './AttrDiffTable';
import EvidenceChips, { Chip, fmtScore } from './EvidenceChips';
import ListingMini from './ListingMini';
import VerdictButtons from './VerdictButtons';

/* The judge's four verdicts, in the operator's words. `insufficient_evidence`
 * is an answer, not a failure — it is what the judge says when the digests
 * genuinely do not settle the question. */
const JUDGE_WORDS: Record<AutodedupJudgementRow['verdict'], string> = {
  same_property: 'judge: same property',
  different_property: 'judge: different property',
  same_building_different_unit: 'judge: same building, different unit',
  insufficient_evidence: 'judge: not enough evidence',
};

export function JudgeChip({ judgement }: { judgement: AutodedupJudgementRow }) {
  const tone = judgement.verdict === 'same_property' ? 'good' : 'warn';
  return (
    <Chip tone={tone} title={`${judgement.tier} tier · ${judgement.model}`}>
      {JUDGE_WORDS[judgement.verdict]}
      {judgement.confidence != null && (
        <span className="font-mono tabular-nums"> {fmtScore(judgement.confidence)}</span>
      )}
    </Chip>
  );
}

export interface PairCardProps {
  lo: AutodedupMember;
  hi: AutodedupMember;
  score?: number | null;
  zone?: AutodedupZone | null;
  certificate?: string | null;
  families?: number | string[] | null;
  guardVeto?: string | null;
  whyNotMerged?: string | null;
  contributions?: AutodedupContribution[];
  judgement?: AutodedupJudgementRow | null;
  verdict?: AutodedupVerdictRow | null;
  onVerdict?: (value: AutodedupVerdictValue) => void;
  pending?: boolean;
  /* Cover photos above the fold are decoded eagerly — see ListingMini. */
  eager?: boolean;
  /* The full-evidence page for this pair, when the surface is not already it. */
  evidenceHref?: RoutePath | null;
  labels?: Record<AutodedupVerdictValue, string>;
  onlyDiffs?: boolean;
  /* QUEUE GRAIN (see ListingMini). A residual row keeps the covers as 160px
   * thumbnails so the attribute diff, the reason and the four answers are all
   * on screen at once; the full-size photos are the pair page's job. */
  dense?: boolean;
}

export default function PairCard({
  lo,
  hi,
  score,
  zone,
  certificate,
  families,
  guardVeto,
  whyNotMerged,
  contributions,
  judgement,
  verdict,
  onVerdict,
  pending = false,
  eager = false,
  evidenceHref,
  labels,
  onlyDiffs = false,
  dense = false,
}: PairCardProps) {
  const top = (contributions ?? []).slice(0, 5);
  /* The queue's judge summary carries no evidence lists; the pair page's full
   * transcript does. Absent is empty, never a rendered "null". */
  const keyEvidence = judgement?.key_evidence ?? [];
  const contraEvidence = judgement?.contradicting_evidence ?? [];
  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-4 py-3 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <EvidenceChips
          zone={zone}
          score={score}
          certificate={certificate}
          families={families}
          guardVeto={guardVeto}
        />
        {judgement && <JudgeChip judgement={judgement} />}
        {evidenceHref && (
          <Link
            to={evidenceHref}
            className="ml-auto text-[0.7rem] text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
          >
            Full evidence
          </Link>
        )}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <ListingMini member={lo} eager={eager} dense={dense} />
        <ListingMini member={hi} eager={eager} dense={dense} />
      </div>

      <AttrDiffTable
        rows={memberDiffRows(lo, hi)}
        onlyDiffs={onlyDiffs}
        captionA={`#${lo.listing_id}`}
        captionB={`#${hi.listing_id}`}
      />

      {whyNotMerged && (
        <p className="rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper)] px-3 py-2 text-[0.72rem] leading-relaxed text-[var(--color-ink-2)]">
          <span className="text-[var(--color-ink-4)]">Why it wasn't merged: </span>
          {whyNotMerged}
        </p>
      )}

      {top.length > 0 && (
        <div>
          <h4 className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
            Top contributions
          </h4>
          <ul className="mt-1 space-y-0.5">
            {top.map((c) => (
              <li
                key={c.name}
                className="flex items-baseline justify-between gap-3 text-[0.7rem]"
              >
                <span className="font-mono text-[var(--color-ink-2)]">{c.name}</span>
                <span className="font-mono tabular-nums text-[var(--color-ink-3)]">
                  {/* Value and contribution are different quantities and are
                    * never merged into one number: a feature can be large and
                    * weigh nothing. An absent feature says so. */}
                  {c.present ? fmtScore(c.value) : 'absent'}
                  {c.contribution != null && (
                    <span className="ml-2 text-[var(--color-ink-4)]">
                      {c.contribution > 0 ? '+' : ''}
                      {fmtScore(c.contribution)}
                    </span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {judgement && (keyEvidence.length > 0 || contraEvidence.length > 0) && (
        <div className="grid gap-3 sm:grid-cols-2 text-[0.7rem]">
          <ul className="space-y-0.5 text-[var(--color-ink-2)]">
            {keyEvidence.map((e) => (
              <li key={e}>+ {e}</li>
            ))}
          </ul>
          <ul className="space-y-0.5 text-[var(--color-brick)]">
            {contraEvidence.map((e) => (
              <li key={e}>− {e}</li>
            ))}
          </ul>
        </div>
      )}

      {onVerdict && (
        <VerdictButtons
          kind="pair"
          verdict={verdict ?? null}
          onVerdict={onVerdict}
          pending={pending}
          labels={labels}
        />
      )}
    </div>
  );
}
