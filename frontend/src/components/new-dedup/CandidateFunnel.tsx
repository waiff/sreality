/* NEW DEDUP · the candidate funnel — "all listings → candidates", the one
 * readout the Wave 2 audit page and the program dashboard both open with
 * (docs/design/new-dedup/PROGRAM.md, W2: "the dashboard's funnel top").
 *
 * IT IS ONE COMPONENT BECAUSE IT IS ONE NUMBER. Two copies of a funnel drift,
 * and the first thing that drifts is which step counts what — at which point
 * the two pages disagree about how many listings the program can even see.
 *
 * EVERY STEP IS SUMMED FROM THE SAME ROWS. `stats.funnel` carries one row per
 * (portal, property type, deal) with the counts already computed by
 * scripts/dedup_candidates_generate.py; this component only adds them up. It
 * derives nothing the lane did not measure, so a step this page cannot fill
 * renders a gap rather than a guess.
 *
 * THE BARS ARE ONE SERIES, so there is no legend and no second hue: each bar is
 * a share of the FIRST step, drawn on a common baseline, with the number itself
 * printed beside it. The bar is the shape of the drop-off; the digits are the
 * value. Nothing here is readable by colour alone.
 *
 * THE LAST STEP IS BROKEN OUT BOTH WAYS — by property type under the step, by
 * rung (path) under the bars — because the wave text asks for the funnel to end
 * "by type and path" (roadmap/new-dedup.md, PR 3). The type split is the rows of
 * `stats.listings_with_candidates` printed as they arrive; the step's headline
 * stays their sum, so the bar keeps its one series.
 */

import type { NewDedupCandidateStats } from '@/lib/api';
import { categoryMainLabel } from '@/lib/enums';
import { fmtCount, fmtPct } from '@/lib/format';

/* A share-of-base bar: thin, rounded at the data end, on a recessive track.
 * `title` gives it the hover reading every mark on a chart owes the reader. */
export function ProportionBar({
  value,
  base,
  title,
}: {
  value: number | null;
  base: number;
  title?: string;
}) {
  const pct = value == null || base <= 0 ? null : Math.min(100, (value / base) * 100);
  return (
    <div
      className="h-1.5 w-full rounded-full bg-[var(--color-inset)] overflow-hidden"
      title={title}
      aria-hidden
    >
      {pct != null && (
        <div
          className="h-full rounded-full bg-[var(--color-copper)]"
          style={{ width: `${Math.max(pct, pct > 0 ? 0.5 : 0)}%` }}
        />
      )}
    </div>
  );
}

interface Step {
  key: string;
  label: string;
  explanation: string;
  count: number;
  /* The split the lane measured for this step, when it measured one. Rendered
   * under the step as printed rows — never derived, never re-summed. */
  breakdown?: { key: string; label: string; count: number }[];
}

/* A listing whose property type the portal never stated: the same em dash the
 * audit page's tables use, rather than the generic label — an unrecorded type is
 * a gap, not a category. */
const typeLabel = (cm: string | null | undefined): string =>
  cm == null ? '—' : categoryMainLabel(cm);

/* The five sets the corpus narrows through, in order. Each label says what the
 * set IS; each explanation says why a listing falls out before the next one. */
function steps(stats: NewDedupCandidateStats): Step[] {
  const f = stats.funnel ?? [];
  const sum = (pick: (r: (typeof f)[number]) => number): number =>
    f.reduce((acc, r) => acc + (pick(r) || 0), 0);

  const byType = stats.listings_with_candidates ?? [];

  return [
    {
      key: 'listings',
      label: 'Listings in the database',
      explanation:
        'Every listing row this run looked at, across all nine portals. The run’s scope decides whether that means every listing ever collected or only the ones still on sale.',
      count: sum((r) => r.listings),
    },
    {
      key: 'with_projection',
      label: 'Known to the location engine',
      explanation:
        'The location engine — the service that turns a listing’s address into a place on the map — has an answer for this listing. Without one, nothing below can be asked.',
      count: sum((r) => r.with_projection),
    },
    {
      key: 'with_town',
      label: 'Placed precisely enough to name a town',
      explanation:
        'The engine’s answer is at least town-grain (an obec — the Czech municipality). Path C compares two listings only when they are in the same town, so a listing that loses its town here can never become a candidate.',
      count: sum((r) => r.with_town),
    },
    {
      key: 'eligible',
      label: 'Has an attribute the rule can compare',
      explanation:
        'On top of a town, the listing states a disposition (2+kk and the like) or a floor area — one of the two things path C compares. A listing with a town and neither attribute is counted in the missing-data table below.',
      count: sum((r) => r.c1_eligible) + sum((r) => r.c3_eligible),
    },
    {
      key: 'paired',
      label: 'Ended up in at least one candidate pair',
      explanation:
        'The run actually found another listing to pair it with. A listing can be perfectly eligible and still land here at zero — that only means nothing else in its town matched it.',
      count: byType.reduce((acc, r) => acc + (r.listings || 0), 0),
      breakdown: byType.map((r) => ({
        key: r.category_main ?? 'unknown',
        label: typeLabel(r.category_main),
        count: r.listings,
      })),
    },
  ];
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

  const rows = steps(stats);
  const base = rows[0]?.count ?? 0;
  const pairs = stats.pairs;

  return (
    <div>
      <ol className="space-y-3">
        {rows.map((s, i) => {
          const pct = base > 0 ? (s.count / base) * 100 : null;
          const previous = i === 0 ? null : rows[i - 1].count;
          const lost = previous == null ? null : previous - s.count;
          return (
            <li key={s.key}>
              <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                <span className="text-sm text-[var(--color-ink)]">{s.label}</span>
                <span className="font-mono tabular-nums text-sm text-[var(--color-ink)]">
                  {fmtCount(s.count)}
                  <span className="ml-2 text-[0.7rem] text-[var(--color-ink-3)]">
                    {fmtPct(pct)}
                  </span>
                </span>
              </div>
              <div className="mt-1">
                <ProportionBar
                  value={s.count}
                  base={base}
                  title={`${s.label}: ${fmtCount(s.count)} (${fmtPct(pct)} of all listings)`}
                />
              </div>
              <p className="mt-1 text-[0.72rem] leading-relaxed text-[var(--color-ink-3)]">
                {s.explanation}
                {lost != null && lost > 0 && (
                  <span className="text-[var(--color-ink-4)]">
                    {' '}
                    Lost at this step: {fmtCount(lost)}.
                  </span>
                )}
              </p>
              {s.breakdown && s.breakdown.length > 0 && (
                <p className="mt-1 flex flex-wrap items-baseline gap-x-4 gap-y-0.5 text-[0.72rem] text-[var(--color-ink-3)]">
                  <span className="text-[var(--color-ink-4)]">By property type:</span>
                  {s.breakdown.map((b) => (
                    <span key={b.key}>
                      {b.label}{' '}
                      <span className="font-mono tabular-nums">{fmtCount(b.count)}</span>
                    </span>
                  ))}
                </p>
              )}
            </li>
          );
        })}
      </ol>

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
