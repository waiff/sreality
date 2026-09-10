/* NEW DEDUP · Dashboard — the program's front page.
 *
 * This replaces the Wave 0 placeholder. It is W1's one unbuilt bullet — the
 * "dashboard skeleton (funnel + cost table)" — carried into Wave 2 and shipped
 * with the candidate audit page, because until a candidate generation had run
 * there was nothing for a funnel to count (PROGRAM.md, ledger 2026-09-10 (a)).
 *
 * THREE THINGS, IN THE ORDER SOMEONE ARRIVING WANTS THEM:
 *   1. WHERE THE PROGRAM IS — the waves, and which one is being built.
 *   2. WHAT IT CAN SEE — the same funnel component the Candidates page opens
 *      with, over the newest finished candidate run. One component, so the two
 *      pages can never disagree about how many listings the program can reach.
 *   3. WHAT IT WILL COST — the two spend knobs that are set, beside the actual
 *      spend, which does not exist yet and therefore says "not yet".
 *
 * "NOT YET" IS A REAL ANSWER HERE. Levels 3 and 4 have not run, so there is no
 * spend to report. The cell says so in words rather than showing a zero, which
 * would read as "we ran it and it was free".
 */

import { type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import {
  getNewDedupCandidateOverview,
  listNewDedupSettings,
  type NewDedupSetting,
} from '@/lib/api';
import CandidateFunnel from '@/components/new-dedup/CandidateFunnel';
import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';
import { ROUTES } from '@/lib/routes';
import { fmtAbsolute, fmtCount, fmtUsd } from '@/lib/format';

/* ------------------------------------------------------------------ chrome */

const TH = 'py-1.5 pr-3 font-medium whitespace-nowrap';
const HEAD = 'text-left text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]';
const TD = 'py-1.5 pr-3';
const ROW = 'border-t border-[var(--color-rule-soft)]';

/* The one phrase this page uses for "we have not measured this yet". Never a
 * zero, never a dash that could be read as "none". */
const NOT_YET = 'not yet';

function Card({ title, lede, children }: { title: string; lede: string; children: ReactNode }) {
  return (
    <section className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4">
      <h2 className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
        {title}
      </h2>
      <p className="mt-2 text-[0.78rem] leading-relaxed text-[var(--color-ink-2)] max-w-[52rem]">
        {lede}
      </p>
      <div className="mt-3">{children}</div>
    </section>
  );
}

/* ------------------------------------------------------------- wave strip */

type WaveState = 'done' | 'building' | 'ahead';

/* The program's own wave list, mirrored from docs/design/new-dedup/PROGRAM.md
 * § Waves. The design doc is the source of truth and is named on the page, so a
 * reader who suspects this strip is stale knows exactly what to check. Only two
 * states here are claims: W0/W1 done (Gate 1 CLOSED on 2026-09-09, recorded in
 * the ledger of 2026-09-10 (a)) and W2 building (that ledger entry opens it).
 * Nothing here is a percentage or a date — those would be invented. */
const WAVES: { code: string; title: string; state: WaveState }[] = [
  { code: 'W0', title: 'Backup, teardown, scaffolding', state: 'done' },
  { code: 'W1', title: 'Shared prerequisites + labeling', state: 'done' },
  { code: 'W2', title: 'Level 0 — candidate selection', state: 'building' },
  { code: 'W3', title: 'Linear probe, full retag, path B', state: 'ahead' },
  { code: 'W4', title: 'Level 2 — perceptual hash', state: 'ahead' },
  { code: 'W5', title: 'Level 3 — embeddings', state: 'ahead' },
  { code: 'W6', title: 'Level 4 — vision', state: 'ahead' },
  { code: 'W7', title: 'Level 1 — exact attributes', state: 'ahead' },
  { code: 'W8', title: 'End-to-end approval + production', state: 'ahead' },
];

const WAVE_STATE_LABEL: Record<WaveState, string> = {
  done: 'closed',
  building: 'being built',
  ahead: 'not started',
};

const WAVE_STATE_CLASS: Record<WaveState, string> = {
  done: 'border-[var(--color-sage)] bg-[var(--color-sage-soft)] text-[var(--color-sage)]',
  building: 'border-[var(--color-copper)] bg-[var(--color-copper-soft)] text-[var(--color-copper-2)]',
  ahead: 'border-[var(--color-rule)] bg-[var(--color-paper)] text-[var(--color-ink-3)]',
};

function WaveStrip() {
  return (
    <ol className="flex flex-wrap gap-2">
      {WAVES.map((w) => (
        <li
          key={w.code}
          className={`rounded-[var(--radius-sm)] border px-2.5 py-1.5 ${WAVE_STATE_CLASS[w.state]}`}
        >
          <div className="flex items-baseline gap-2">
            <span className="font-mono text-[0.72rem] font-medium">{w.code}</span>
            <span className="text-[0.6rem] tracking-[0.1em] uppercase">
              {WAVE_STATE_LABEL[w.state]}
            </span>
          </div>
          <div className="text-[0.72rem] text-[var(--color-ink-2)] max-w-[13rem]">{w.title}</div>
        </li>
      ))}
    </ol>
  );
}

/* ------------------------------------------------------------- cost table */

/* The two spend knobs that exist today. `key` is read from the settings
 * registry — if the registry has not got it, the cell says "not yet" rather
 * than a hardcoded number that would silently disagree with the real setting. */
const COST_ROWS: {
  level: string;
  what: string;
  key: string;
  render: (s: NewDedupSetting | undefined) => string;
}[] = [
  {
    level: 'L3 · Embeddings',
    what: 'Daily spend cap on the rented GPUs that turn photographs into vectors',
    key: 'l3_runpod_daily_cost_cap_usd',
    render: (s) => (typeof s?.value === 'number' ? `${fmtUsd(s.value)} / day` : NOT_YET),
  },
  {
    level: 'L4 · Vision',
    what: 'Which vision model reviews the hardest pairs',
    key: 'l4_vision_model',
    render: (s) => (typeof s?.value === 'string' && s.value ? s.value : NOT_YET),
  },
];

function CostTable({ settings }: { settings: NewDedupSetting[] | null }) {
  const byKey = new Map((settings ?? []).map((s) => [s.key, s]));
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className={HEAD}>
            <th className={TH}>Level</th>
            <th className={TH}>What is set</th>
            <th className={TH}>Setting</th>
            <th className={TH}>Spent so far</th>
            <th className={TH}>Projected</th>
          </tr>
        </thead>
        <tbody>
          {COST_ROWS.map((r) => (
            <tr key={r.key} className={ROW}>
              <td className={`${TD} whitespace-nowrap`}>{r.level}</td>
              <td className={`${TD} text-[var(--color-ink-2)]`}>{r.what}</td>
              <td className={`${TD} font-mono text-[0.78rem]`}>
                {settings == null ? NOT_YET : r.render(byKey.get(r.key))}
              </td>
              {/* Neither level has run, so neither has a bill. A zero here would
                * read as a measured result. */}
              <td className={`${TD} text-[var(--color-ink-3)]`}>{NOT_YET}</td>
              <td className={`${TD} text-[var(--color-ink-3)]`}>{NOT_YET}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ---------------------------------------------------------------- the page */

export default function NewDedupDashboard() {
  const overview = useQuery({
    queryKey: ['new-dedup', 'candidates', 'overview', null],
    queryFn: () => getNewDedupCandidateOverview(null),
  });
  const settings = useQuery({
    queryKey: ['new-dedup', 'settings'],
    queryFn: listNewDedupSettings,
  });

  const data = overview.data?.data;
  const generation = data?.generation ?? null;

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">NEW DEDUP</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)] leading-relaxed max-w-[52rem]">
          The rebuilt deduplication program: the work of recognising when the same real-world
          property is advertised on several portals at once. It is being built{' '}
          <em>simulation-first</em> — every level is measured against real data and signed off by
          you before it is ever allowed to merge anything. Nothing on this page changes any
          listing; the full plan, wave by wave, is <code>docs/design/new-dedup/PROGRAM.md</code> in
          the repo, which is also the source of truth for the strip below.
        </p>
      </header>

      <div className="mt-5 space-y-4">
        <Card
          title="Where the program is"
          lede="The program is built in waves, each one ending at a gate you either accept or refuse. A wave is not started until the wave before it has been signed off."
        >
          <WaveStrip />
        </Card>

        <Card
          title="What the program can see"
          lede="How many listings survive each narrowing on the way to becoming a candidate — a pair of listings worth comparing. This is the same readout the Candidates page opens with, over the newest finished candidate run."
        >
          {overview.isPending && (
            <p className="flex items-center gap-2 text-sm text-[var(--color-ink-3)]">
              <Spinner /> Loading the funnel…
            </p>
          )}
          {overview.error && <ErrorBanner message={(overview.error as Error).message} />}
          {data && (
            <>
              <CandidateFunnel
                stats={data.stats}
                emptyText={
                  data.store_ready
                    ? 'No candidate run has finished yet, so there is nothing to count.'
                    : 'The candidate store has not been created yet, so there is nothing to count.'
                }
              />
              <p className="mt-3 text-[0.72rem] text-[var(--color-ink-3)]">
                {generation ? (
                  <>
                    From run #{generation.id}, finished {fmtAbsolute(generation.completed_at)}.{' '}
                  </>
                ) : null}
                <Link
                  to={ROUTES.newDedupCandidates.build()}
                  className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
                >
                  Open the full candidate audit
                </Link>
                .
              </p>
            </>
          )}
        </Card>

        <Card
          title="What it will cost"
          lede="The two spend knobs that are set today, beside what has actually been spent. Levels 3 and 4 have not run, so both amounts say “not yet” rather than showing a zero — a zero would read as a measured result."
        >
          {settings.error && <ErrorBanner message={(settings.error as Error).message} />}
          <CostTable settings={settings.data?.data ?? null} />
          <p className="mt-3 text-[0.72rem] leading-relaxed text-[var(--color-ink-3)]">
            Every knob, with its explanation and what it has been calibrated against, lives on{' '}
            <Link
              to={ROUTES.newDedupSettings.build()}
              className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
            >
              Settings
            </Link>
            . Candidate pairs recorded so far:{' '}
            <span className="font-mono tabular-nums">
              {data?.stats ? fmtCount(data.stats.pairs?.total ?? null) : NOT_YET}
            </span>
            .
          </p>
        </Card>
      </div>
    </div>
  );
}
