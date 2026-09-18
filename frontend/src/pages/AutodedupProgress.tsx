/* AUTODEDUP · Progress — the autonomous dedup program's ledger page.
 *
 * WHAT THIS IS FOR. The program runs unattended for a long time, so the thing
 * that makes it legible is not a funnel or a score: it is the ITERATION LOG —
 * what was tried, on what sample, with which tools, what it measured, and what
 * it cost. One row of `autodedup.iterations` per iteration, newest first, each
 * one collapsible so the page stays a list you can scan and a story you can
 * open. It is the first UI the program ships (PROGRAM.md §12, W1), ahead of the
 * validation views, for exactly that reason.
 *
 * NOTHING HERE IS A PRODUCTION CHANGE. The whole trial is shadow mode (ruling
 * D4): the engine decides and stores inside its own schema and merges nothing.
 * The page says so in the lede rather than leaving the reader to infer it.
 *
 * THE STRIP CARRIES THE GATES, NOT ONLY THE TOTALS (PROGRAM.md §12). Spend is
 * meaningless without the cap it is spent against, so the strip prints both, and
 * the mode — SHADOW, for the life of this program (D4/E39) — is a chip, not a
 * sentence buried in the lede. The caps and the mode are program CONSTANTS from
 * ruling D2/E39, not measurements, so they render with no backend at all and are
 * never subject to the "not yet" idiom below.
 *
 * "NOT YET" IS A REAL ANSWER. Until migration 528 is applied there is no store
 * to read, and until a lane has written a row there is nothing to total. Both
 * cases print words. A zero would read as "we ran it and it measured zero",
 * which is the one wrong answer on a progress page. A MEASURED $0.00 — the
 * census and export iterations really are free — is a different thing and is
 * printed as money.
 */

import { type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';

import {
  getAutodedupIterations,
  getAutodedupStats,
  type AutodedupIteration,
  type AutodedupReasonRollup,
} from '@/lib/api';
import { useReasonLabels } from '@/components/autodedup/VerdictNotes';
import AgreementPanel from '@/components/autodedup/AgreementPanel';
import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';
import { Chevron, useCollapsed } from '@/components/settings/SectionChrome';
import { useInfiniteList, type InfiniteListPage } from '@/lib/useInfiniteList';
import { fmtAbsolute, fmtCount, fmtUsd } from '@/lib/format';

/* The one phrase for "not measured yet" — never a zero, never a dash that
 * could be read as "none". Same word the NEW DEDUP dashboard uses. */
const NOT_YET = 'not yet';

const PAGE_SIZE = 25;

/* Ruling D2 / E31: $25 is the hard cap on any one run, $200 the whole program's
 * budget. Constants of the program, not figures the store reports. */
const PROGRAM_CAP_USD = 200;
const RUN_CAP_USD = 25;

/* Ruling D4 / E39: shadow mode is the default AND the only mode for W0–W7. */
const MODE = 'SHADOW';

const TH = 'py-1 pr-3 font-medium whitespace-nowrap align-top';
const HEAD = 'text-left text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]';
const TD = 'py-1 pr-3 align-top';
const ROW = 'border-t border-[var(--color-rule-soft)]';

/* ------------------------------------------------------------------ status */

type IterationStatus = AutodedupIteration['status'];

/* Three token colours and one neutral: a finished iteration is sage, a failed
 * one brick, one still in flight ochre. `skipped` is deliberately NOT a
 * semantic colour — it is an absence of work, not an outcome. */
const STATUS_CLASS: Record<IterationStatus, string> = {
  done: 'border-[var(--color-sage)] bg-[var(--color-sage-soft)] text-[var(--color-sage)]',
  failed: 'border-[var(--color-brick)] bg-[var(--color-brick-soft)] text-[var(--color-brick)]',
  running: 'border-[var(--color-ochre)] bg-[var(--color-ochre-soft)] text-[var(--color-ochre)]',
  skipped: 'border-[var(--color-rule)] bg-[var(--color-paper)] text-[var(--color-ink-3)]',
};

function StatusBadge({ status }: { status: IterationStatus }) {
  const cls = STATUS_CLASS[status] ?? STATUS_CLASS.skipped;
  return (
    <span
      className={`rounded-[var(--radius-xs)] border px-1.5 py-0.5 text-[0.6rem] tracking-[0.1em] uppercase ${cls}`}
    >
      {status}
    </span>
  );
}

/* --------------------------------------------------------------- KPI tiles */

function Tile({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-4 py-3">
      <div className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        {label}
      </div>
      <div className="mt-1 text-xl font-mono tabular-nums text-[var(--color-ink)]">{value}</div>
      <div className="mt-1 text-[0.68rem] leading-relaxed text-[var(--color-ink-3)]">{hint}</div>
    </div>
  );
}

/* ------------------------------------------------------- key/value rendering */

/* Rates, not counts. `fmtCount` is the cs-CZ integer formatter and caps at three
 * fraction digits, so a measured ECE of 0.0004 would print as "0" — the one
 * answer this page must never fabricate. Counts still go through it (thousands
 * separators); anything fractional keeps four significant digits. */
const czRate = new Intl.NumberFormat('cs-CZ', { maximumSignificantDigits: 4 });

/* A jsonb blob the lane wrote, rendered without inventing structure. A nested
 * object or array is pretty-printed verbatim, because guessing at its shape here
 * is how a page starts lying about what the lane measured. */
function renderValue(value: unknown): ReactNode {
  if (value == null) return <span className="text-[var(--color-ink-3)]">{NOT_YET}</span>;
  if (typeof value === 'number') {
    return Number.isInteger(value) ? fmtCount(value) : czRate.format(value);
  }
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if (typeof value === 'string') return value;
  return (
    <pre className="whitespace-pre-wrap break-words font-mono text-[0.7rem] text-[var(--color-ink-2)]">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function KeyValues({
  title,
  blob,
}: {
  title: string;
  blob: Record<string, unknown> | null | undefined;
}) {
  const entries = Object.entries(blob ?? {});
  return (
    <div>
      <h3 className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        {title}
      </h3>
      {entries.length === 0 ? (
        <p className="mt-1 text-[0.72rem] text-[var(--color-ink-3)]">{NOT_YET}</p>
      ) : (
        <div className="mt-1 overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="sr-only">
              <tr className={HEAD}>
                <th className={TH}>Name</th>
                <th className={TH}>Value</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(([k, v]) => (
                <tr key={k} className={ROW}>
                  <td className={`${TD} text-[var(--color-ink-2)] whitespace-nowrap`}>{k}</td>
                  <td className={`${TD} font-mono tabular-nums text-[0.78rem]`}>
                    {renderValue(v)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------- artifact links */

/* The jsonb key is the lane's own name for the thing; some of those names are
 * machine keys ("run", "gh") that mean nothing on a page. A URL that points at
 * an Actions run IS an Actions run whatever the key spells, so the label is read
 * off the destination and the key is the fallback rather than the answer. */
const ACTIONS_RUN_RE = /^https?:\/\/(www\.)?github\.com\/[^/]+\/[^/]+\/actions\/runs\/\d+/i;
const ACTIONS_ARTIFACT_RE = /^https?:\/\/(www\.)?github\.com\/.*\/artifacts\/\d+/i;

export function artifactLabel(key: string, url: string): string {
  if (ACTIONS_ARTIFACT_RE.test(url)) return 'Artifact download';
  if (ACTIONS_RUN_RE.test(url)) return 'Actions run';
  return key;
}

/* Two jsonb blobs are "the same" when they carry the same keys with the same
 * values — compared on SORTED keys, because the lane builds the two dicts in
 * different places and a key order is not a difference. */
export function sameBlob(
  a: Record<string, unknown> | null | undefined,
  b: Record<string, unknown> | null | undefined,
): boolean {
  const ea = Object.entries(a ?? {}).sort(([x], [y]) => x.localeCompare(y));
  const eb = Object.entries(b ?? {}).sort(([x], [y]) => x.localeCompare(y));
  if (ea.length === 0 || ea.length !== eb.length) return false;
  return JSON.stringify(ea) === JSON.stringify(eb);
}

/* ------------------------------------------------------------------- a card */

function IterationCard({ row }: { row: AutodedupIteration }) {
  /* Same mechanism and same storage scheme as the settings pages and the
   * candidate audit — one twisty, one localStorage key namespace. */
  const [open, toggle] = useCollapsed(`autodedup-progress.${row.id}`, false);
  const bodyId = `autodedup-iteration-${row.id}`;
  const tools = row.tools ?? [];
  /* The column is unconstrained jsonb written by the lane; only an http(s)
   * string is a link. A nested object would otherwise reach `href` as
   * "[object Object]". */
  const artifacts = Object.entries(row.artifacts ?? {}).filter(
    (entry): entry is [string, string] =>
      typeof entry[1] === 'string' && /^https?:\/\//i.test(entry[1]),
  );
  const identical = sameBlob(row.sample_stats, row.metrics);

  return (
    <li className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4">
      <h2>
        <button
          type="button"
          onClick={toggle}
          aria-expanded={open}
          aria-controls={bodyId}
          className="flex w-full items-center gap-2 text-left hover:text-[var(--color-ink)]"
        >
          <Chevron open={open} />
          <span className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-1.5 py-0.5 font-mono text-[0.68rem] text-[var(--color-ink-2)]">
            {row.wave}
          </span>
          <span className="text-sm text-[var(--color-ink)]">{row.title}</span>
          <StatusBadge status={row.status} />
        </button>
      </h2>

      {/* The when stays visible when folded: a `running` row with a stale
        * start time is itself the signal that a lane died (PROGRAM.md §12). */}
      <p className="mt-1.5 pl-[22px] text-[0.7rem] text-[var(--color-ink-3)]">
        {row.started_at ? fmtAbsolute(row.started_at) : NOT_YET} →{' '}
        {row.finished_at ? fmtAbsolute(row.finished_at) : NOT_YET}
        {' · '}
        <span className="font-mono tabular-nums">
          {row.cost_usd == null ? NOT_YET : fmtUsd(row.cost_usd)}
        </span>
      </p>

      <div id={bodyId} className="mt-3 space-y-3" hidden={!open}>
        {row.approach && (
          <p className="text-[0.78rem] leading-relaxed text-[var(--color-ink-2)] max-w-[52rem]">
            {row.approach}
          </p>
        )}

        {tools.length > 0 && (
          <ul className="flex flex-wrap gap-1.5">
            {tools.map((t) => (
              <li
                key={t}
                className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-1.5 py-0.5 font-mono text-[0.65rem] text-[var(--color-ink-2)]"
              >
                {t}
              </li>
            ))}
          </ul>
        )}

        {/* A lane that measured nothing new writes its sample back as its
          * metrics; printing the same table twice reads as two findings. */}
        <div className={`grid gap-4 ${identical ? '' : 'sm:grid-cols-2'}`}>
          <KeyValues title="Sample" blob={row.sample_stats} />
          {identical ? (
            <p className="text-[0.68rem] text-[var(--color-ink-3)]">
              Metrics are identical to the sample — this iteration measured the cohort, not a
              result.
            </p>
          ) : (
            <KeyValues title="Metrics" blob={row.metrics} />
          )}
        </div>

        {(artifacts.length > 0 || row.run_id != null) && (
          <ul className="flex flex-wrap gap-3 text-[0.72rem]">
            {row.run_id != null && (
              <li className="text-[var(--color-ink-3)]">
                Actions run <span className="font-mono tabular-nums">{row.run_id}</span>
              </li>
            )}
            {artifacts.map(([name, url]) => (
              <li key={name}>
                <a
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
                >
                  {artifactLabel(name, url)}
                </a>
              </li>
            ))}
          </ul>
        )}

        {row.notes && (
          <p className="text-[0.72rem] leading-relaxed text-[var(--color-ink-3)] max-w-[52rem]">
            {row.notes}
          </p>
        )}
      </div>
    </li>
  );
}

/* ---------------------------------------------------------------- the page */

interface IterationsPage extends InfiniteListPage<AutodedupIteration> {
  store_ready: boolean;
}

/* "DŮVODY OPERÁTORA" — the reason histogram (migration 533), pair and cluster
 * side by side and NEVER summed: the same chip means a discriminator one edge
 * missed on a pair, and a wrong proposal on a cluster. This table is the
 * feature-gap readout of §9 — the column to compare against the judge's
 * `unit_discriminator` — which is why it sits with the counts and not in a
 * drawer. Rows are ordered by the total so the loudest gap reads first. */
export function reasonRows(
  rollups: ReadonlyArray<AutodedupReasonRollup>,
): Array<{ reason: string; pair: number; cluster: number; total: number }> {
  const by = new Map<string, { reason: string; pair: number; cluster: number; total: number }>();
  for (const r of rollups) {
    const row = by.get(r.reason) ?? { reason: r.reason, pair: 0, cluster: 0, total: 0 };
    if (r.kind === 'cluster') row.cluster += r.n;
    else row.pair += r.n;
    row.total = row.pair + row.cluster;
    by.set(r.reason, row);
  }
  return [...by.values()].sort((a, b) => b.total - a.total || a.reason.localeCompare(b.reason));
}

function ReasonTable({ rollups }: { rollups: ReadonlyArray<AutodedupReasonRollup> }) {
  const label = useReasonLabels();
  const rows = reasonRows(rollups);
  return (
    <section className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4">
      <h2 className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        Důvody operátora
      </h2>
      <p className="mt-1 text-[0.72rem] text-[var(--color-ink-3)] max-w-[46rem]">
        Co operátor při kontrole viděl — evidence, kterou model neměl. Dvojice a skupiny se
        nesčítají: stejný důvod znamená u dvojice chybějící rozlišovací znak, u skupiny špatný
        návrh.
      </p>
      {rows.length === 0 ? (
        <p className="mt-2 text-[0.72rem] text-[var(--color-ink-3)]">
          {NOT_YET} — u žádného verdiktu není uveden důvod.
        </p>
      ) : (
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-[0.72rem]">
            <thead>
              <tr className="text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
                <th className="py-1 pr-3 text-left font-medium">Důvod</th>
                <th className="py-1 pr-3 text-right font-medium">Dvojice</th>
                <th className="py-1 pr-3 text-right font-medium">Skupiny</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.reason} className="border-t border-[var(--color-rule-soft)]">
                  <td className="py-1 pr-3">{label(row.reason)}</td>
                  <td className="py-1 pr-3 text-right font-mono tabular-nums">
                    {row.pair === 0 ? '—' : fmtCount(row.pair)}
                  </td>
                  <td className="py-1 pr-3 text-right font-mono tabular-nums">
                    {row.cluster === 0 ? '—' : fmtCount(row.cluster)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export default function AutodedupProgress() {
  const stats = useQuery({
    queryKey: ['autodedup', 'stats'],
    queryFn: getAutodedupStats,
  });

  const list = useInfiniteList<AutodedupIteration, IterationsPage>({
    queryKey: ['autodedup', 'iterations'],
    queryFn: async (cursor) => {
      const page = await getAutodedupIterations({
        limit: PAGE_SIZE,
        after: (cursor as number | null) ?? null,
      });
      return {
        rows: page.data?.items ?? [],
        nextCursor: page.data?.next_after_id ?? undefined,
        store_ready: page.store_ready,
      };
    },
    pageSize: PAGE_SIZE,
    getRowId: (row) => row.id,
  });

  const rows = list.rows;
  const s = stats.data?.data ?? null;
  /* Either read is enough to know the schema is missing; the list is the one
   * whose answer the body below is waiting on, so it speaks first. */
  const storeReady = list.firstPage?.store_ready ?? stats.data?.store_ready ?? null;

  /* The wave of the NEWEST iteration — the program's own definition of "where
   * we are". The per-wave rollup comes back ordered by each wave's latest pass,
   * so its first row is the same answer a moment earlier, before the list
   * lands; it is a fallback, not a second opinion. */
  const currentWave = rows.length > 0 ? rows[0].wave : (s?.waves[0]?.wave ?? null);

  /* A wave whose newest iteration finished; the rest are still open. Computed
   * from the rollup already on the page, so the strip and the cards below
   * cannot disagree. */
  const wavesClosed = s ? s.waves.filter((w) => w.last_status === 'done').length : null;

  /* Distinguish "the read has not landed" from "the read landed and there is
   * nothing to report" — only the second is NOT_YET. */
  const pending = stats.isPending;

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">AUTODEDUP · Progress</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)] leading-relaxed max-w-[52rem]">
          An <em>autonomous</em> engine that looks for the same real-world property advertised on
          several portals, or re-posted later, and decides by itself whether the two are one. It is
          being trialled on four small blocks — two towns, one Prague quarter, and a deliberately
          hard negative-control set drawn from Praha: address points that carry listings on more
          than one floor, i.e. the same building, different unit — before it is allowed near
          anything else. Which blocks those are is the operator's choice and is recorded on each
          iteration below, not in this paragraph. The whole trial runs in{' '}
          <strong>shadow mode</strong>: it decides, stores its decisions in a schema of its own and{' '}
          <strong>merges nothing</strong>. No listing, property or merge in the live database is
          touched by any row on this page.
        </p>
        <p className="mt-2 text-[0.78rem] leading-relaxed text-[var(--color-ink-3)] max-w-[52rem]">
          Below is the program's own log, one entry per iteration, newest first: what was tried, on
          what sample, with which tools, what it measured, and what it cost. Spend is read back from
          the recorded model calls after the fact, never forecast — so an iteration that has not
          been billed yet says so instead of showing a zero.
        </p>
      </header>

      {/* The mode is a constant of this program, not a reading — it renders
        * before any query lands and never says "not yet" (D4/E39). */}
      <p className="mt-4 flex items-center gap-2 text-[0.7rem] text-[var(--color-ink-3)]">
        <span className="rounded-[var(--radius-xs)] border border-[var(--color-ochre)] bg-[var(--color-ochre-soft)] px-1.5 py-0.5 tracking-[0.12em] uppercase text-[var(--color-ochre)]">
          {MODE}
        </span>
        merges nothing — every decision stays inside the program's own schema.
      </p>

      <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Tile
          label="Iterations"
          value={pending ? '…' : s == null ? NOT_YET : fmtCount(s.n_iterations)}
          hint="Entries written to the program log so far."
        />
        <Tile
          label="Spent so far"
          value={
            pending
              ? '…'
              : s == null
                ? NOT_YET
                : `${fmtUsd(s.total_cost_usd)} of ${fmtUsd(PROGRAM_CAP_USD)}`
          }
          hint={`Measured from the recorded model calls, not projected. ${fmtUsd(
            RUN_CAP_USD,
          )} is the hard cap on any single run.`}
        />
        <Tile
          label="Waves closed"
          value={
            pending ? '…' : s == null ? NOT_YET : `${wavesClosed} of ${s.waves.length}`
          }
          hint="Waves whose newest iteration finished; the rest are still open."
        />
        <Tile
          label="Current wave"
          value={pending ? '…' : currentWave == null ? NOT_YET : currentWave}
          hint="The wave the newest iteration belongs to."
        />
      </div>

      {/* THE D6 GATE, LIVE — read from the verdicts and judgements that exist,
        * not from a lane's end-of-wave report. It renders with no argument: the
        * newest clustering pass is the server's own answer (E54). */}
      <AgreementPanel />

      {s && <ReasonTable rollups={s.engine?.verdict_reasons ?? []} />}

      {stats.error && <ErrorBanner message={(stats.error as Error).message} />}
      {list.error && <ErrorBanner message={list.error.message} />}

      {list.isLoading && (
        <p className="mt-6 flex items-center gap-2 text-sm text-[var(--color-ink-3)]">
          <Spinner /> Loading the program log…
        </p>
      )}

      {storeReady === false && (
        <p className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4 text-sm text-[var(--color-ink-2)]">
          Schema not migrated yet — the program's store does not exist in this database, so there is
          nothing to show.
        </p>
      )}

      {/* Never next to an ErrorBanner: "nothing has been recorded" is a claim
        * about the store, and a failed read is exactly the case where the page
        * does not know. */}
      {storeReady !== false && !list.isLoading && !list.isError && rows.length === 0 && (
        <p className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4 text-sm text-[var(--color-ink-2)]">
          No iteration has been recorded yet. The first lane run writes the first entry.
        </p>
      )}

      {rows.length > 0 && (
        <ul className="mt-6 space-y-3">
          {rows.map((row) => (
            <IterationCard key={row.id} row={row} />
          ))}
        </ul>
      )}

      {list.hasNextPage && (
        <div className="mt-4">
          <button
            type="button"
            onClick={list.fetchNextPage}
            disabled={list.isFetchingNextPage}
            className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-3 py-1.5 text-sm text-[var(--color-ink-2)] hover:text-[var(--color-ink)] disabled:opacity-50"
          >
            {list.isFetchingNextPage ? 'Loading…' : 'Load more'}
          </button>
        </div>
      )}
    </div>
  );
}
