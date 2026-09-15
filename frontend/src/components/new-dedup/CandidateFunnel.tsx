/* NEW DEDUP · the candidate funnel — "all listings → candidates", the one
 * readout the Wave 2 audit page and the program dashboard both open with
 * (docs/design/new-dedup/PROGRAM.md, W2: "the dashboard's funnel top").
 *
 * IT IS ONE COMPONENT BECAUSE IT IS ONE NUMBER. Two copies of a funnel drift,
 * and the first thing that drifts is which step counts what — at which point
 * the two pages disagree about how many listings the program can even see.
 *
 * W16 — IT IS ALSO ONE VOCABULARY. The chain comes from `stats.waterfall`: rows
 * the lane stamped with the SHARED step keys (location_data/location_steps.py),
 * the same keys, kinds and columns `location_audit_waterfall` writes hourly for
 * `/new-dedup/pin-audit`. The step names and the sentences under them come from
 * `lib/locationSteps.ts`, which is where a step is worded for both languages and
 * both pages at once. Nothing is spelled here.
 *
 * THIS COMPONENT DOES NO ARITHMETIC. Counts, losses and shares are computed once
 * by the lane; a browser that re-derived a step would be a second definition of
 * the same question — the exact fault this wave removed. Before W16 the steps
 * were summed here and "Lost at this step" was subtracted here, which is how the
 * town step came to report one loss that was really three unlike things added
 * together (judged-but-not-located, ABROAD, and a Czech point with no town).
 *
 * ABROAD IS AN ANSWER. `located_foreign` and `located_no_town` are SPLITS of
 * "has a location", printed indented under it and never as a drop. The town
 * step's loss is exactly those two rows.
 *
 * TWO HONEST DIFFERENCES FROM THE AUDIT PAGE, both printed rather than hidden:
 * a run can be scoped to active listings only, and a run's numbers are frozen at
 * the moment it counted them while the audit page refreshes hourly. That is the
 * reading that was missing when two identical predicates looked like a
 * contradiction.
 *
 * A RUN THAT PREDATES W16 carries no `waterfall`, so the chain renders as one
 * line saying so — a gap, never a zero.
 *
 * THE BARS ARE ONE SERIES, so there is no legend and no second hue: each bar is
 * the share of the first step the lane measured, drawn on a common baseline,
 * with the number itself printed beside it. Nothing here is readable by colour
 * alone.
 */

import ProportionBar from '@/components/new-dedup/ProportionBar';
import type { NewDedupCandidateStats } from '@/lib/api';
import { categoryMainLabel } from '@/lib/enums';
import { fmtAbsolute, fmtCount, fmtPct } from '@/lib/format';
import { stepLabel, stepNote } from '@/lib/locationSteps';
import { groupWaterfall } from '@/lib/locationWaterfall';

/* A listing whose property type the portal never stated: the same em dash the
 * audit page's tables use, rather than the generic label — an unrecorded type is
 * a gap, not a category. */
const typeLabel = (cm: string | null | undefined): string =>
  cm == null ? '—' : categoryMainLabel(cm);

const SCOPE_TEXT: Record<string, string> = {
  all: 'every listing ever collected, active or delisted',
  active: 'only listings active today',
};

/* The one line that makes this chain comparable with the hourly audit page:
 * WHICH listings the run looked at, and WHEN it counted them. */
function AsOf({ stats }: { stats: NewDedupCandidateStats }) {
  const scope = stats.scope ? (SCOPE_TEXT[stats.scope] ?? stats.scope) : null;
  if (!scope && !stats.computed_at) return null;
  return (
    <p className="mb-3 text-[0.72rem] leading-relaxed text-[var(--color-ink-3)]">
      {scope ? <>Scope: {scope}. </> : null}
      {stats.computed_at ? (
        <>
          Counted {fmtAbsolute(stats.computed_at)} and frozen with the run — the location
          audit page asks the same questions of the live database every hour, so the two
          differ by time as well as by scope.
        </>
      ) : null}
    </p>
  );
}

export default function CandidateFunnel({
  stats,
  emptyText = 'No run has produced these numbers yet.',
}: {
  stats: NewDedupCandidateStats | null;
  emptyText?: string;
}) {
  if (!stats) {
    return <p className="text-sm text-[var(--color-ink-3)]">{emptyText}</p>;
  }

  const chain = groupWaterfall(stats.waterfall ?? []);
  const byType = stats.listings_with_candidates ?? [];
  const pairs = stats.pairs;

  return (
    <div>
      <AsOf stats={stats} />

      {chain.length === 0 ? (
        <p className="text-sm text-[var(--color-ink-3)]">
          This run predates the shared steps, so it carries no chain. Its other numbers are
          unaffected; re-running the generator fills this in.
        </p>
      ) : (
        <ol className="space-y-3">
          {chain.map(({ row, splits }) => (
            <li key={row.step_key}>
              <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                <span className="text-sm text-[var(--color-ink)]">
                  {stepLabel(row.step_key, 'en')}
                </span>
                <span className="font-mono tabular-nums text-sm text-[var(--color-ink)]">
                  {fmtCount(row.n)}
                  <span className="ml-2 text-[0.7rem] text-[var(--color-ink-3)]">
                    {fmtPct(row.share_pct)}
                  </span>
                </span>
              </div>
              <div className="mt-1">
                <ProportionBar
                  pct={row.share_pct}
                  title={`${stepLabel(row.step_key, 'en')}: ${fmtCount(row.n)} (${fmtPct(
                    row.share_pct,
                  )} of all listings)`}
                />
              </div>
              <p className="mt-1 text-[0.72rem] leading-relaxed text-[var(--color-ink-3)]">
                {stepNote(row.step_key, 'en')}
                {row.lost != null && row.lost > 0 && (
                  <span className="text-[var(--color-ink-4)]">
                    {' '}
                    Lost at this step: {fmtCount(row.lost)}.
                  </span>
                )}
              </p>

              {splits.length > 0 && (
                <ul className="mt-1.5 space-y-1 pl-4 border-l border-[var(--color-rule-soft)]">
                  {splits.map((s) => (
                    <li key={s.step_key} data-testid={`funnel-split-${s.step_key}`}>
                      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                        <span className="text-[0.78rem] text-[var(--color-ink-2)]">
                          · {stepLabel(s.step_key, 'en')}
                        </span>
                        <span className="font-mono tabular-nums text-[0.78rem] text-[var(--color-ink-2)]">
                          {fmtCount(s.n)}
                          <span className="ml-2 text-[0.68rem] text-[var(--color-ink-3)]">
                            {fmtPct(s.share_pct)}
                          </span>
                        </span>
                      </div>
                      <p className="text-[0.7rem] leading-relaxed text-[var(--color-ink-3)]">
                        {stepNote(s.step_key, 'en')}
                      </p>
                    </li>
                  ))}
                </ul>
              )}

              {row.step_key === 'paired' && byType.length > 0 && (
                <p className="mt-1 flex flex-wrap items-baseline gap-x-4 gap-y-0.5 text-[0.72rem] text-[var(--color-ink-3)]">
                  <span className="text-[var(--color-ink-4)]">By property type:</span>
                  {byType.map((b) => (
                    <span key={b.category_main ?? 'unknown'}>
                      {typeLabel(b.category_main)}{' '}
                      <span className="font-mono tabular-nums">{fmtCount(b.listings)}</span>
                    </span>
                  ))}
                </p>
              )}
            </li>
          ))}
        </ol>
      )}

      {stats.partial ? (
        <p className="mt-4 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-3 py-2 text-[0.72rem] leading-relaxed text-[var(--color-ink-2)]">
          <strong className="font-medium text-[var(--color-ink)]">
            This run covered only part of the country, so the last step is not comparable with
            the steps above it.
          </strong>{' '}
          Every step down to “{stepLabel('eligible', 'en')}” counts the whole database,
          because that is a fact about the listings. The pair count below it was produced over{' '}
          {stats.only && stats.only.length > 0
            ? `${fmtCount(stats.only.length)} town${stats.only.length === 1 ? '' : 's'}`
            : 'a limited set of towns'}{' '}
          only. The lane leaves that step's drop blank here rather than printing a misleading
          subtraction; a full run is what makes it mean something.
        </p>
      ) : null}

      <div className="mt-4 pt-3 border-t border-[var(--color-rule-soft)] flex flex-wrap items-baseline gap-x-6 gap-y-1">
        <span className="text-sm text-[var(--color-ink)]">
          Candidate pairs found:{' '}
          <strong className="font-mono tabular-nums font-medium">
            {fmtCount(pairs?.total ?? null)}
          </strong>
        </span>
        <span className="text-[0.72rem] text-[var(--color-ink-3)]">
          on the disposition rung (C1):{' '}
          <span className="font-mono tabular-nums">{fmtCount(pairs?.C1 ?? null)}</span>
        </span>
        <span className="text-[0.72rem] text-[var(--color-ink-3)]">
          on the area rung (C3):{' '}
          <span className="font-mono tabular-nums">{fmtCount(pairs?.C3 ?? null)}</span>
        </span>
      </div>
      <p className="mt-2 text-[0.72rem] leading-relaxed text-[var(--color-ink-3)]">
        A <em>pair</em> is two listings the rule thinks might be the same property; a{' '}
        <em>rung</em> is which attributes it compared them on. Every pair sits on exactly one
        rung — C1 when both listings state a disposition, C3 when at least one does not and the
        rule falls back to floor area. C1 checks the floor area too, but as a wide sanity check
        rather than the match itself. Being a candidate is not a decision that they are the same
        property; it is only the shortlist the later levels look at.
      </p>
    </div>
  );
}
