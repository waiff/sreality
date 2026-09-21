/* AUTODEDUP · "Shoda operátor × soudce" — the D6 gate, live.
 *
 * WHAT THIS NUMBER DECIDES. D6 says the program stops if the GOLD judge and the
 * operator agree on less than 0.95 of a ~200-pair session. Until this panel
 * existed that number was something a lane would compute once, at the end; here
 * it moves while the operator works, so a session can be stopped, extended or
 * re-aimed on the evidence rather than on its last day.
 *
 * WHAT IS COMPARED. One binary question — ONE PROPERTY, or not — because the two
 * vocabularies are not the same vocabulary: the operator has five verdicts, the
 * judge four, and only that distinction is common to both. `unsure` is not a
 * label and is dropped; `insufficient_evidence` is not a judgement and is
 * counted separately, because a judge that abstains on half the sample is a fact
 * about the judge, not agreement about the pairs.
 *
 * IMPLIED PAIRS ARE INCLUDED, and the panel says so. Confirming a group is a
 * statement about every pair inside it, and 103 confirmed groups carry far more
 * pair-grade evidence than the handful of pairs ruled on one at a time. An
 * explicit verdict on a pair always wins over the implication, and the split of
 * the two is printed so the number can never quietly become a number about
 * groups.
 *
 * NOT-YET IS A REAL ANSWER. No comparable pair means "nothing to compare",
 * printed in words. A zero here would read as "the operator and the judge never
 * agree", which is the one wrong answer on a page that gates a program.
 */

import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import {
  getAutodedupAgreement,
  type AutodedupAgreementStats,
  type AutodedupAgreementTier,
  type AutodedupDisagreement,
} from '@/lib/api';
import { ROUTES, withQuery } from '@/lib/routes';
import { fmtCount } from '@/lib/format';
import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';

const NOT_YET = 'not yet';

const pct = new Intl.NumberFormat('cs-CZ', {
  style: 'percent',
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

export const fmtPct = (n: number | null | undefined): string => (n == null ? '—' : pct.format(n));

/* The judge's four verdicts and the operator's three (D39), each in its own
 * words — a disagreement table that prints `same_building_different_unit` twice
 * in two different vocabularies is a table nobody reads. The judge keeps its
 * finer verdict because it is the JUDGE's vocabulary; the operator's column
 * says "Různé" for every negative, including the two finer values a ruling
 * taken before D39 still carries in the store. */
const JUDGE_WORDS: Record<string, string> = {
  same_property: 'stejná nemovitost',
  different_property: 'jiná nemovitost',
  same_building_different_unit: 'stejná budova, jiná jednotka',
  insufficient_evidence: 'nedostatek důkazů',
};

const OPERATOR_WORDS: Record<string, string> = {
  same: 'Stejné',
  different: 'Různé',
  same_building_different_unit: 'Různé',
  same_project_different_unit: 'Různé',
  unsure: 'Nevím',
};

const TIER_WORDS: Record<string, string> = {
  gold: 'gold (tři hlasy)',
  vision: 'vision',
  text: 'text',
};

const TH = 'py-1 pr-3 text-left font-medium whitespace-nowrap align-top';
const TD = 'py-1 pr-3 align-top';

function Interval({ stats }: { stats: AutodedupAgreementStats }) {
  if (stats.n === 0 || stats.ci_low == null || stats.ci_high == null) {
    return <span className="text-[var(--color-ink-3)]">{NOT_YET}</span>;
  }
  return (
    <span className="font-mono tabular-nums">
      {fmtPct(stats.ci_low)} – {fmtPct(stats.ci_high)}
    </span>
  );
}

function pairHref(row: AutodedupDisagreement, generation: string | null) {
  return withQuery(ROUTES.autodedupPair.build({ lo: row.listing_lo, hi: row.listing_hi }), {
    generation: generation || null,
  });
}

export default function AgreementPanel({ generation }: { generation?: string | null }) {
  const q = useQuery({
    queryKey: ['autodedup', 'agreement', generation ?? null],
    queryFn: () => getAutodedupAgreement(generation ?? null),
  });
  const data = q.data?.data ?? null;
  const gold: AutodedupAgreementTier | undefined = data?.tiers.find((t) => t.tier === 'gold');
  const gate = data?.gate;
  /* The gate is PASSED, FAILED or NOT YET MEASURED, and the third is the state
   * this program is actually in: a point estimate over 40 pairs does not decide
   * a 200-pair gate, so the panel says which of the three it is instead of
   * painting a colour on an unfinished number. */
  const goldMet =
    gold && gate && gold.agreement != null && gold.n >= gate.target_n
      ? gold.agreement >= gate.bar
      : null;

  return (
    <section className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4">
      <h2 className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        Shoda operátor × soudce
      </h2>
      <p className="mt-1 text-[0.72rem] leading-relaxed text-[var(--color-ink-3)] max-w-[46rem]">
        Jedna binární otázka — <strong>jedna nemovitost, nebo ne</strong> — položená operátorovi i
        modelu, na dvojicích, kde se vyjádřili oba. Verdikty operátora jsou jednak přímé verdikty
        na dvojici, jednak dvojice <strong>odvozené</strong> z potvrzených skupin (potvrzení
        skupiny je tvrzení o každé dvojici v ní); přímý verdikt má přednost. „Nejisté“ se nepočítá
        a soudcovo „nedostatek důkazů“ se vylučuje a vykazuje zvlášť.
      </p>

      {q.isPending && (
        <p className="mt-3 flex items-center gap-2 text-sm text-[var(--color-ink-3)]">
          <Spinner /> Počítám shodu…
        </p>
      )}
      {q.error && <ErrorBanner message={(q.error as Error).message} />}

      {data && (
        <>
          <div className="mt-3 flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <span className="text-xl font-mono tabular-nums text-[var(--color-ink)]">
              {data.overall.n === 0 ? NOT_YET : fmtPct(data.overall.agreement)}
            </span>
            <span className="text-[0.72rem] text-[var(--color-ink-3)]">
              celkem · {fmtCount(data.overall.n_agree)} z {fmtCount(data.overall.n)} dvojic ·
              95% interval <Interval stats={data.overall} />
            </span>
          </div>

          {/* THE BAR, beside the number rather than in a document nobody has
            * open: D6 is measured on the GOLD tier and on ~200 pairs, and the
            * panel prints how far off both of those the session is. */}
          {gate && (
            <p className="mt-2 text-[0.72rem] text-[var(--color-ink-2)]">
              <span className="text-[var(--color-ink-4)]">Hranice D6: </span>
              shoda s tierem <span className="font-mono">{gate.tier}</span> ≥{' '}
              <span className="font-mono tabular-nums">{fmtPct(gate.bar)}</span> na ~
              {fmtCount(gate.target_n)} dvojicích, jinak se program zastaví. Zatím{' '}
              <span className="font-mono tabular-nums">
                {fmtCount(gold?.n ?? 0)} / {fmtCount(gate.target_n)}
              </span>{' '}
              dvojic se zlatým verdiktem
              {goldMet === null
                ? ' — vzorek ještě nestačí na rozhodnutí.'
                : goldMet
                  ? ' — hranice splněna.'
                  : ' — hranice NENÍ splněna.'}
            </p>
          )}

          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-[0.72rem]">
              <thead>
                <tr className="text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
                  <th className={TH}>Tier</th>
                  <th className={TH}>Dvojic</th>
                  <th className={TH}>Shoda</th>
                  <th className={TH}>95% interval</th>
                  <th className={TH} title={'Operátor řekl „Stejné“, soudce „jiná“'}>
                    Soudce jiná
                  </th>
                  <th className={TH} title={'Operátor řekl „Různé“, soudce „stejná“'}>
                    Soudce stejná
                  </th>
                  <th className={TH}>Přímé / odvozené</th>
                </tr>
              </thead>
              <tbody>
                {data.tiers.map((t) => (
                  <tr key={t.tier} className="border-t border-[var(--color-rule-soft)]">
                    <td className={TD}>{TIER_WORDS[t.tier] ?? t.tier}</td>
                    <td className={`${TD} font-mono tabular-nums`}>{fmtCount(t.n)}</td>
                    <td className={`${TD} font-mono tabular-nums`}>
                      {t.n === 0 ? NOT_YET : fmtPct(t.agreement)}
                    </td>
                    <td className={TD}>
                      <Interval stats={t} />
                    </td>
                    <td className={`${TD} font-mono tabular-nums`}>
                      {fmtCount(t.n_judge_different_operator_same)}
                    </td>
                    <td className={`${TD} font-mono tabular-nums`}>
                      {fmtCount(t.n_judge_same_operator_different)}
                    </td>
                    <td className={`${TD} font-mono tabular-nums`}>
                      {fmtCount(t.n_explicit)} / {fmtCount(t.n_implied)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <p className="mt-2 text-[0.68rem] leading-relaxed text-[var(--color-ink-3)]">
            Vyloučeno pro „nedostatek důkazů“:{' '}
            <span className="font-mono tabular-nums">
              {fmtCount(data.n_insufficient_evidence)}
            </span>{' '}
            dvojic. Odvozené dvojice se berou jen ze skupin do{' '}
            <span className="font-mono tabular-nums">{fmtCount(data.max_cluster_size)}</span>{' '}
            inzerátů
            {data.n_clusters_over_cap > 0 && (
              <>
                {' '}— <span className="font-mono tabular-nums">
                  {fmtCount(data.n_clusters_over_cap)}
                </span>{' '}
                potvrzených skupin je větších a do čísla nevstupuje
              </>
            )}
            .
          </p>

          <h3 className="mt-4 text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
            Neshody
          </h3>
          {data.disagreements.length === 0 ? (
            <p className="mt-1 text-[0.72rem] text-[var(--color-ink-3)]">
              {data.overall.n === 0
                ? `${NOT_YET} — žádná dvojice, kde se vyjádřili oba.`
                : 'Žádná neshoda na porovnávaných dvojicích.'}
            </p>
          ) : (
            <div className="mt-1 overflow-x-auto">
              <table className="w-full text-[0.72rem]">
                <thead>
                  <tr className="text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
                    <th className={TH}>Dvojice</th>
                    <th className={TH}>Operátor</th>
                    <th className={TH}>Soudce</th>
                    <th className={TH}>Tier</th>
                    <th className={TH}>Zdroj</th>
                    <th className={TH}></th>
                  </tr>
                </thead>
                <tbody>
                  {data.disagreements.map((row) => (
                    <tr
                      key={`${row.listing_lo}:${row.listing_hi}`}
                      className="border-t border-[var(--color-rule-soft)]"
                    >
                      <td className={`${TD} font-mono tabular-nums`}>
                        {row.listing_lo} · {row.listing_hi}
                      </td>
                      <td className={TD}>
                        {OPERATOR_WORDS[row.operator_verdict] ?? row.operator_verdict}
                      </td>
                      <td className={TD}>{JUDGE_WORDS[row.judge_verdict] ?? row.judge_verdict}</td>
                      <td className={TD}>{row.judge_tier}</td>
                      <td className={TD}>
                        {row.operator_source === 'implied' ? 'odvozeno ze skupiny' : 'přímý'}
                      </td>
                      <td className={TD}>
                        <Link
                          to={pairHref(row, data.generation)}
                          className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
                        >
                          Důkazy
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {data.n_disagreements > data.disagreements.length && (
                <p className="mt-1 text-[0.68rem] text-[var(--color-ink-3)]">
                  Zobrazeno {fmtCount(data.disagreements.length)} z{' '}
                  {fmtCount(data.n_disagreements)} neshod.
                </p>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}
