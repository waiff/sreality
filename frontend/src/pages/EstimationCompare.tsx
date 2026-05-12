/* Side-by-side comparison view for 2–3 estimation runs.
 *
 * Phase 7 slice 2. Read from `?ids=12,15,18` in the URL — the source of
 * truth for which runs are being compared. Entry points: the "Compare
 * with parent" link on EstimationDetail when run.parent_run_id is set,
 * and the multi-select + Compare button on EstimationList.
 *
 * No new backend endpoint — fetches each run individually via the
 * existing GET /estimations/{id}. That keeps the page composable: any
 * link with `?ids=...` works without server-side support.
 *
 * Trace summaries follow the shape produced by
 * api/estimation_runs._agent_summary_line:
 *   "agent <provider>/<skill> after <N> iter(s) (<stop_reason>) cost $..."
 * — we surface the iteration count and stop reason from there.
 */

import { Link, useSearchParams } from 'react-router-dom';
import { useQueries, type UseQueryResult } from '@tanstack/react-query';
import {
  estimationKeys,
  fetchEstimation,
} from '@/lib/queries';
import { fmtCzk, fmtRelative } from '@/lib/format';
import type { EstimationRun } from '@/lib/types';

const MAX_RUNS = 3;

export default function EstimationCompare() {
  const [params] = useSearchParams();
  const rawIds = params.get('ids') ?? '';
  const ids = parseIds(rawIds).slice(0, MAX_RUNS);

  const queries = useQueries({
    queries: ids.map((id) => ({
      queryKey: estimationKeys.detail(id),
      queryFn: () => fetchEstimation(id),
      staleTime: 30_000,
    })),
  }) as UseQueryResult<EstimationRun, Error>[];

  return (
    <div className="px-6 py-8 max-w-6xl mx-auto">
      <header>
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Compare
        </p>
        <h1
          className="mt-1.5 text-[2.1rem] leading-tight"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          {ids.length === 0
            ? 'Pick runs to compare'
            : ids.length === 1
              ? 'One run selected'
              : `Comparing ${ids.length} runs`}
        </h1>
        <p className="mt-2 text-sm text-[var(--color-ink-2)]">
          Same listing across different skills, providers, or modes.
          Use the multi-select on the{' '}
          <Link
            to="/estimations"
            className="text-[var(--color-copper)] hover:underline underline-offset-2"
          >
            estimations list
          </Link>{' '}
          to add runs.
        </p>
      </header>

      {ids.length === 0 && (
        <div className="mt-10 px-5 py-8 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-sm text-[var(--color-ink-3)]">
          No run ids supplied. Append <span className="font-mono">?ids=…,…</span>{' '}
          (up to {MAX_RUNS}) to the URL, or click "Compare with parent" on a
          re-run's detail page.
        </div>
      )}

      {ids.length >= 1 && (
        <div className="mt-8 overflow-x-auto">
          <div
            className="grid gap-4 min-w-fit"
            style={{
              gridTemplateColumns: `9rem repeat(${ids.length}, minmax(14rem, 1fr))`,
            }}
          >
            <HeaderRow ids={ids} queries={queries} />
            <DataRow label="When">
              {queries.map((q, i) => (
                <Cell key={ids[i]}>
                  {q.data ? fmtRelative(q.data.created_at) : '…'}
                </Cell>
              ))}
            </DataRow>
            <DataRow label="Mode">
              {queries.map((q, i) => (
                <Cell key={ids[i]}>
                  {q.data ? <Chip>{q.data.mode}</Chip> : '…'}
                </Cell>
              ))}
            </DataRow>
            <DataRow label="Provider">
              {queries.map((q, i) => (
                <Cell key={ids[i]}>
                  {q.data ? (
                    q.data.provider ? (
                      <Chip>{q.data.provider}</Chip>
                    ) : (
                      <Muted>—</Muted>
                    )
                  ) : (
                    '…'
                  )}
                </Cell>
              ))}
            </DataRow>
            <DataRow label="Skill">
              {queries.map((q, i) => (
                <Cell key={ids[i]}>
                  {q.data ? (
                    q.data.skill_name ? (
                      <Chip>{q.data.skill_name}</Chip>
                    ) : (
                      <Muted>—</Muted>
                    )
                  ) : (
                    '…'
                  )}
                </Cell>
              ))}
            </DataRow>
            <DataRow label="Status">
              {queries.map((q, i) => (
                <Cell key={ids[i]}>
                  {q.data ? <StatusBadge run={q.data} /> : '…'}
                </Cell>
              ))}
            </DataRow>
            <DataRow label="Estimate">
              {queries.map((q, i) => (
                <Cell key={ids[i]}>
                  {q.data ? <EstimateCell run={q.data} /> : '…'}
                </Cell>
              ))}
            </DataRow>
            <DataRow label="Rent range">
              {queries.map((q, i) => (
                <Cell key={ids[i]}>
                  {q.data ? <RangeCell run={q.data} /> : '…'}
                </Cell>
              ))}
            </DataRow>
            <DataRow label="Confidence">
              {queries.map((q, i) => (
                <Cell key={ids[i]}>
                  {q.data?.confidence ?? <Muted>—</Muted>}
                </Cell>
              ))}
            </DataRow>
            <DataRow label="LLM cost">
              {queries.map((q, i) => (
                <Cell key={ids[i]}>
                  {q.data ? (
                    q.data.cost_usd_total != null ? (
                      <span className="font-mono tabular-nums">
                        ${q.data.cost_usd_total.toFixed(4)}
                      </span>
                    ) : (
                      <Muted>—</Muted>
                    )
                  ) : (
                    '…'
                  )}
                </Cell>
              ))}
            </DataRow>
            <DataRow label="Comparables">
              {queries.map((q, i) => (
                <Cell key={ids[i]}>
                  {q.data ? (
                    <span className="font-mono tabular-nums">
                      {q.data.comparables_used?.length ?? 0}
                    </span>
                  ) : (
                    '…'
                  )}
                </Cell>
              ))}
            </DataRow>
            <DataRow label="Loop summary">
              {queries.map((q, i) => (
                <Cell key={ids[i]}>
                  {q.data ? (
                    <span className="text-[0.78rem] text-[var(--color-ink-3)]">
                      {summariseAgentLine(q.data.trace?.summary ?? null)}
                    </span>
                  ) : (
                    '…'
                  )}
                </Cell>
              ))}
            </DataRow>
            <DataRow label="Error">
              {queries.map((q, i) => (
                <Cell key={ids[i]}>
                  {q.data?.error_message ? (
                    <span className="text-[0.78rem] text-[var(--color-brick)]">
                      {q.data.error_message}
                    </span>
                  ) : (
                    <Muted>—</Muted>
                  )}
                </Cell>
              ))}
            </DataRow>
          </div>
        </div>
      )}
    </div>
  );
}

function parseIds(raw: string): number[] {
  const seen = new Set<number>();
  const out: number[] = [];
  for (const tok of raw.split(',')) {
    const n = Number(tok.trim());
    if (Number.isInteger(n) && n > 0 && !seen.has(n)) {
      seen.add(n);
      out.push(n);
    }
  }
  return out;
}

function HeaderRow({
  ids,
  queries,
}: {
  ids: number[];
  queries: UseQueryResult<EstimationRun, Error>[];
}) {
  return (
    <>
      <div />
      {ids.map((id, i) => {
        const q = queries[i];
        return (
          <Link
            key={id}
            to={`/estimation/${id}`}
            className="px-3 py-2 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] hover:border-[var(--color-copper)] transition-colors"
          >
            <p className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
              Run #{id}
            </p>
            <p className="mt-1 text-[0.85rem] text-[var(--color-ink)] truncate">
              {q.data
                ? q.data.input_url ??
                  `sreality_id ${q.data.input_sreality_id ?? '—'}`
                : q.error
                  ? <span className="text-[var(--color-brick)]">Failed to load</span>
                  : 'Loading…'}
            </p>
          </Link>
        );
      })}
    </>
  );
}

function DataRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <>
      <div className="px-3 py-2 text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        {label}
      </div>
      {children}
    </>
  );
}

function Cell({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-3 py-2 text-[0.85rem] text-[var(--color-ink-2)] border-b border-[var(--color-rule-soft)]">
      {children}
    </div>
  );
}

function Chip({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center px-2 py-0.5 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[0.75rem] font-mono">
      {children}
    </span>
  );
}

function Muted({ children }: { children: React.ReactNode }) {
  return <span className="text-[var(--color-ink-4)]">{children}</span>;
}

function StatusBadge({ run }: { run: EstimationRun }) {
  const cls =
    run.status === 'success'
      ? 'bg-[var(--color-sage-soft)] text-[var(--color-sage)] border-[var(--color-sage)]/25'
      : run.status === 'failed'
        ? 'bg-[var(--color-brick-soft)] text-[var(--color-brick)] border-[var(--color-brick)]/25'
        : 'bg-[var(--color-copper-soft)] text-[var(--color-copper)] border-[var(--color-copper)]/25';
  return (
    <span
      className={[
        'inline-flex items-center px-2 py-0.5 rounded-[var(--radius-sm)] border text-[0.75rem]',
        cls,
      ].join(' ')}
    >
      {run.status}
    </span>
  );
}

function EstimateCell({ run }: { run: EstimationRun }) {
  const kind = run.estimate_kind ?? 'rent';
  if (kind === 'sale') {
    return run.estimated_sale_price_czk != null ? (
      <span className="font-mono tabular-nums">
        {fmtCzk(run.estimated_sale_price_czk)}
      </span>
    ) : (
      <Muted>—</Muted>
    );
  }
  return run.estimated_monthly_rent_czk != null ? (
    <span className="font-mono tabular-nums">
      {fmtCzk(run.estimated_monthly_rent_czk)} / mo
    </span>
  ) : (
    <Muted>—</Muted>
  );
}

function RangeCell({ run }: { run: EstimationRun }) {
  const kind = run.estimate_kind ?? 'rent';
  const lo = kind === 'sale' ? run.sale_p25_czk : run.rent_p25_czk;
  const hi = kind === 'sale' ? run.sale_p75_czk : run.rent_p75_czk;
  if (lo == null || hi == null) return <Muted>—</Muted>;
  return (
    <span className="font-mono tabular-nums text-[0.78rem]">
      {fmtCzk(lo)} – {fmtCzk(hi)}
    </span>
  );
}

/* Trim trace.summary down to the per-iteration tail we care about
 * (iter count + stop reason). Lossy on purpose — the full summary
 * lives on the run-detail page. */
function summariseAgentLine(summary: string | null): string {
  if (!summary) return '—';
  // "agent provider/skill after N iter(s) (stop_reason) ..." → keep the iter + stop_reason part
  const m = summary.match(/after\s+\d+\s+iters?\s+\([^)]+\)/);
  return m?.[0] ?? summary;
}
