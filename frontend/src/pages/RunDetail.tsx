import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { estimationKeys, fetchEstimation } from '@/lib/queries';
import {
  fmtArea,
  fmtCzk,
  fmtRelative,
  fmtAbsolute,
} from '@/lib/format';
import type {
  ComparableUsed,
  Confidence,
  EstimationRun,
  TargetSpecIn,
  TraceStep,
  TraceStepToolCall,
} from '@/lib/types';

const COMPARABLES_TABLE_CAP = 50;

export default function RunDetail() {
  const { id: idParam } = useParams();
  const id = idParam && /^\d+$/.test(idParam) ? Number(idParam) : null;

  const q = useQuery<EstimationRun, Error>({
    queryKey: id != null ? estimationKeys.detail(id) : ['estimations', 'detail', null],
    queryFn: () => fetchEstimation(id as number),
    enabled: id != null,
    staleTime: 30_000,
  });

  if (id == null) {
    return <Empty title="No run requested" body="The URL is missing a run id." />;
  }
  if (q.isLoading) {
    return <Empty title="Loading run" body={`id ${id}`} />;
  }
  if (q.error) {
    return <Empty title="Couldn't load run" body={q.error.message} tone="brick" />;
  }
  const run = q.data;
  if (!run) {
    return (
      <Empty
        title="Run not found"
        body={`No estimation run with id ${id}.`}
      />
    );
  }
  return <RunPage run={run} />;
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */

function RunPage({ run }: { run: EstimationRun }) {
  const filtersUsed = extractFiltersFromTrace(run.trace?.steps ?? []);

  return (
    <div className="px-6 py-8 max-w-3xl mx-auto">
      <Crumbs run={run} />
      <Header run={run} />
      <Hairline />
      {run.status === 'failed' ? (
        <FailureCard run={run} />
      ) : (
        <ResultCard run={run} />
      )}
      <Hairline />
      <SpecCard
        spec={run.input_spec}
        purchasePriceCzk={run.input_purchase_price_czk}
        filtersUsed={filtersUsed}
      />
      <Hairline />
      <ComparablesSection run={run} />
      <Hairline />
      <TimelineSection run={run} />
      <Hairline />
      <FooterBlock run={run} />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Header + crumbs                                                            */
/* -------------------------------------------------------------------------- */

function Crumbs({ run }: { run: EstimationRun }) {
  return (
    <div className="flex items-center justify-between gap-3 flex-wrap">
      <Link
        to="/runs"
        className="inline-flex items-center gap-1.5 text-[0.75rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
      >
        <BackArrow />
        <span>All runs</span>
      </Link>
      {run.parent_run_id != null && (
        <Link
          to={`/runs/${run.parent_run_id}`}
          className="inline-flex items-center gap-1.5 text-[0.75rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
        >
          <span>Rerun of</span>
          <span className="font-mono tabular-nums">#{run.parent_run_id}</span>
        </Link>
      )}
    </div>
  );
}

function Header({ run }: { run: EstimationRun }) {
  return (
    <header className="mt-5 flex items-start justify-between gap-6">
      <div className="min-w-0">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Estimation run
        </p>
        <h1
          className="mt-2 text-[2.4rem] leading-[1.05] tabular-nums"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          #{run.id}
        </h1>
        <p
          className="mt-2 text-sm text-[var(--color-ink-2)] cursor-help flex items-baseline gap-2 flex-wrap"
          title={fmtAbsolute(run.created_at)}
        >
          <span>{fmtRelative(run.created_at)}</span>
          <Sep />
          <SourcePill source={run.source} />
          <Sep />
          <span className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
            {run.mode} mode
          </span>
        </p>
      </div>
      <div className="flex flex-col items-end gap-3 shrink-0">
        <StatusPill status={run.status} />
        <button
          type="button"
          disabled
          title="Rerun dialog lands in U2 step 12."
          className="px-3 py-1.5 text-xs rounded-[var(--radius-sm)] border border-[var(--color-rule-strong)] bg-[var(--color-paper-2)] text-[var(--color-ink-4)] cursor-not-allowed"
        >
          Rerun
        </button>
      </div>
    </header>
  );
}

/* -------------------------------------------------------------------------- */
/* Result + failure cards                                                     */
/* -------------------------------------------------------------------------- */

function ResultCard({ run }: { run: EstimationRun }) {
  const rent = run.estimated_monthly_rent_czk;
  return (
    <section className="border border-[var(--color-rule)] rounded-[var(--radius-lg)] bg-[var(--color-paper-2)] px-6 py-7">
      <p className="text-[0.65rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
        Estimated monthly rent
      </p>
      <p
        className="mt-2 text-[3rem] leading-[1.05] tabular-nums"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        {fmtCzk(rent)}
        <span className="ml-1 text-base font-sans font-normal text-[var(--color-ink-3)] tracking-wide">
          / měs.
        </span>
      </p>

      <div className="mt-3 flex items-baseline gap-x-5 gap-y-1 flex-wrap text-sm font-mono tabular-nums text-[var(--color-ink-2)]">
        {(run.rent_p25_czk != null || run.rent_p75_czk != null) && (
          <span>
            <span className="text-[var(--color-ink-3)] mr-1">p25</span>
            {fmtCzk(run.rent_p25_czk)}
            <span className="mx-2 text-[var(--color-ink-4)]">—</span>
            <span className="text-[var(--color-ink-3)] mr-1">p75</span>
            {fmtCzk(run.rent_p75_czk)}
          </span>
        )}
        {run.gross_yield_pct != null && (
          <span>
            <span className="text-[var(--color-ink-3)] mr-1">yield</span>
            {run.gross_yield_pct}
            <span className="ml-0.5 text-[var(--color-ink-3)]">%</span>
          </span>
        )}
        {run.comparables_used && (
          <span>
            <span className="text-[var(--color-ink-3)] mr-1">n</span>
            {run.comparables_used.length}
          </span>
        )}
      </div>

      {run.confidence && (
        <p className="mt-4">
          <ConfidencePill level={run.confidence} />
        </p>
      )}

      {run.warnings && run.warnings.length > 0 && (
        <WarningsBlock warnings={run.warnings} />
      )}
    </section>
  );
}

function FailureCard({ run }: { run: EstimationRun }) {
  const retryHref =
    run.input_sreality_id != null
      ? `/estimate?from_listing=${run.input_sreality_id}`
      : '/estimate';
  return (
    <section className="border border-[var(--color-brick)]/30 rounded-[var(--radius-lg)] bg-[var(--color-brick-soft)] px-6 py-6">
      <p className="text-[0.65rem] tracking-[0.18em] uppercase text-[var(--color-brick)] font-medium">
        Failed
      </p>
      <p className="mt-2 text-sm text-[var(--color-brick)] leading-relaxed">
        {run.error_message ?? 'The run failed and no error message was recorded.'}
      </p>
      <p className="mt-4">
        <Link
          to={retryHref}
          className="text-sm text-[var(--color-copper)] hover:text-[var(--color-copper-2)] underline-offset-2 hover:underline"
        >
          Try again with adjusted specs →
        </Link>
      </p>
    </section>
  );
}

function ConfidencePill({ level }: { level: Confidence }) {
  const palette =
    level === 'high'
      ? 'bg-[var(--color-sage-soft)] text-[var(--color-sage)] border-[var(--color-sage)]/25'
      : level === 'medium'
        ? 'bg-[var(--color-copper-soft)] text-[var(--color-copper)] border-[var(--color-copper)]/25'
        : 'bg-[var(--color-ochre-soft)] text-[var(--color-ochre)] border-[var(--color-ochre)]/25';
  return (
    <span
      className={[
        'inline-flex items-center px-2.5 py-1 text-[0.7rem] tracking-[0.14em] uppercase rounded-[var(--radius-sm)] border',
        palette,
      ].join(' ')}
    >
      {level} confidence
    </span>
  );
}

function WarningsBlock({ warnings }: { warnings: string[] }) {
  return (
    <div className="mt-5 px-4 py-3 rounded-[var(--radius-md)] bg-[var(--color-ochre-soft)] border border-[var(--color-ochre)]/20">
      <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ochre)] font-medium">
        Warnings ({warnings.length})
      </p>
      <ul className="mt-2 space-y-1 text-sm text-[var(--color-ink-2)]">
        {warnings.map((w, i) => (
          <li key={i} className="flex gap-2">
            <span className="text-[var(--color-ochre)] mt-0.5" aria-hidden>·</span>
            <span>{w}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Spec card                                                                  */
/* -------------------------------------------------------------------------- */

interface FiltersUsed {
  radius_m?: number;
  area_band_pct?: number;
  disposition_match?: string;
  max_age_days?: number;
  active_only?: boolean;
  has_balcony?: boolean | null;
  has_lift?: boolean | null;
  has_parking?: boolean | null;
}

function extractFiltersFromTrace(steps: TraceStep[]): FiltersUsed | null {
  const findStep = steps.find(
    (s): s is TraceStepToolCall =>
      s.kind === 'tool_call' && s.tool === 'find_comparables',
  );
  const filters = findStep?.input?.filters as
    | Record<string, unknown>
    | undefined;
  if (!filters) return null;
  return filters as FiltersUsed;
}

function SpecCard({
  spec,
  purchasePriceCzk,
  filtersUsed,
}: {
  spec: TargetSpecIn | null;
  purchasePriceCzk: number | null;
  filtersUsed: FiltersUsed | null;
}) {
  if (!spec) {
    return (
      <SectionFrame label="Input spec">
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">
          No spec recorded for this run.
        </p>
      </SectionFrame>
    );
  }

  return (
    <SectionFrame label="Input spec">
      <dl className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-x-6 gap-y-4">
        <KV label="Coords">
          <span className="font-mono tabular-nums">
            {spec.lat?.toFixed(5)}, {spec.lng?.toFixed(5)}
          </span>
        </KV>
        <KV label="Area">
          <span className="font-mono tabular-nums">{fmtArea(spec.area_m2)}</span>
        </KV>
        <KV label="Disposition">
          <span className="font-mono tabular-nums">
            {spec.disposition ?? '—'}
          </span>
        </KV>
        <KV label="Floor">
          <span className="font-mono tabular-nums">
            {spec.floor != null ? String(spec.floor) : '—'}
          </span>
        </KV>
        <KV label="Purchase price">
          <span className="font-mono tabular-nums">{fmtCzk(purchasePriceCzk)}</span>
        </KV>
        {spec.exclude_ids.length > 0 && (
          <KV label="Excluded">
            <span className="font-mono tabular-nums">
              {spec.exclude_ids.length} listing{spec.exclude_ids.length === 1 ? '' : 's'}
            </span>
          </KV>
        )}
      </dl>

      {filtersUsed && (
        <details className="mt-5 group">
          <summary className="cursor-pointer list-none flex items-center justify-between gap-4">
            <span className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
              Search parameters used
            </span>
            <span className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] group-open:hidden">Show</span>
            <span className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] hidden group-open:inline">Hide</span>
          </summary>
          <dl className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-x-6 gap-y-4">
            <KV label="Radius">
              <span className="font-mono tabular-nums">
                {filtersUsed.radius_m != null
                  ? `${filtersUsed.radius_m.toLocaleString('cs-CZ')} m`
                  : '—'}
              </span>
            </KV>
            <KV label="Area band">
              <span className="font-mono tabular-nums">
                {filtersUsed.area_band_pct != null
                  ? `±${Math.round(filtersUsed.area_band_pct * 100)}%`
                  : '—'}
              </span>
            </KV>
            <KV label="Match">
              <span className="font-mono tabular-nums">
                {filtersUsed.disposition_match ?? '—'}
              </span>
            </KV>
            <KV label="Max age">
              <span className="font-mono tabular-nums">
                {filtersUsed.max_age_days != null
                  ? `${filtersUsed.max_age_days} days`
                  : '—'}
              </span>
            </KV>
            <KV label="Active only">
              <span className="font-mono tabular-nums">
                {filtersUsed.active_only == null
                  ? '—'
                  : filtersUsed.active_only ? 'yes' : 'no'}
              </span>
            </KV>
            <KV label="Balcony">
              <FilterBoolValue v={filtersUsed.has_balcony} />
            </KV>
            <KV label="Lift">
              <FilterBoolValue v={filtersUsed.has_lift} />
            </KV>
            <KV label="Parking">
              <FilterBoolValue v={filtersUsed.has_parking} />
            </KV>
          </dl>
        </details>
      )}
    </SectionFrame>
  );
}

function FilterBoolValue({ v }: { v: boolean | null | undefined }) {
  if (v == null) return <span className="text-[var(--color-ink-4)]">any</span>;
  return (
    <span className="font-mono tabular-nums">{v ? 'yes' : 'no'}</span>
  );
}

/* -------------------------------------------------------------------------- */
/* Comparables — table now, map slot reserved for U2 step 9                   */
/* -------------------------------------------------------------------------- */

function ComparablesSection({ run }: { run: EstimationRun }) {
  const list = run.comparables_used ?? [];
  return (
    <SectionFrame
      label="Comparables"
      counter={list.length > 0 ? `${list.length}` : undefined}
    >
      <PlaceholderMap />
      {list.length === 0 ? (
        <p className="mt-4 text-sm text-[var(--color-ink-3)]">
          No comparables were recorded for this run.
        </p>
      ) : (
        <ComparablesTable list={list} />
      )}
    </SectionFrame>
  );
}

function PlaceholderMap() {
  return (
    <div className="mt-3 h-60 rounded-[var(--radius-md)] border border-dashed border-[var(--color-rule)] flex items-center justify-center text-[0.78rem] text-[var(--color-ink-3)] bg-[var(--color-paper-2)]">
      Map of comparables — wired in U2 step 9
    </div>
  );
}

function ComparablesTable({ list }: { list: ComparableUsed[] }) {
  const shown = list.slice(0, COMPARABLES_TABLE_CAP);
  const truncated = list.length > COMPARABLES_TABLE_CAP;
  return (
    <div className="mt-5">
      <details open className="group">
        <summary className="cursor-pointer list-none flex items-center justify-between gap-4 mb-3">
          <span className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
            Audit trail
          </span>
          <span className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] group-open:hidden">Show</span>
          <span className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] hidden group-open:inline">Hide</span>
        </summary>
        <div className="border border-[var(--color-rule)] rounded-[var(--radius-md)] overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)] bg-[var(--color-paper-2)]">
                <th className="px-3 py-2 font-medium">Listing</th>
                <th className="px-3 py-2 font-medium">Snapshot</th>
                <th className="px-3 py-2 font-medium">Captured</th>
                <th className="px-3 py-2 font-medium text-right">Age</th>
                <th className="px-3 py-2 font-medium text-center">Verified</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((c) => (
                <tr key={`${c.sreality_id}-${c.snapshot_id ?? 'null'}`} className="border-t border-[var(--color-rule-soft)]">
                  <td className="px-3 py-2">
                    <Link
                      to={`/listing/${c.sreality_id}`}
                      className="font-mono tabular-nums text-[var(--color-copper)] hover:text-[var(--color-copper-2)] underline-offset-2 hover:underline"
                    >
                      {c.sreality_id}
                    </Link>
                  </td>
                  <td className="px-3 py-2 font-mono tabular-nums text-[var(--color-ink-3)]">
                    {c.snapshot_id ?? '—'}
                  </td>
                  <td
                    className="px-3 py-2 text-[var(--color-ink-2)] cursor-help"
                    title={c.snapshot_date ? fmtAbsolute(c.snapshot_date) : ''}
                  >
                    {c.snapshot_date ? fmtRelative(c.snapshot_date) : '—'}
                  </td>
                  <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-ink-2)]">
                    {c.data_age_days != null ? `${c.data_age_days} d` : '—'}
                  </td>
                  <td className="px-3 py-2 text-center">
                    {c.verified_during_estimate ? (
                      <span className="text-[var(--color-sage)]" aria-label="verified">✓</span>
                    ) : (
                      <span className="text-[var(--color-ink-4)]" aria-label="not verified">·</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {truncated && (
          <p className="mt-2 text-[0.78rem] text-[var(--color-ink-3)]">
            Showing first {COMPARABLES_TABLE_CAP} of {list.length}.
          </p>
        )}
      </details>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Timeline placeholder — full component lands in U2 step 8                   */
/* -------------------------------------------------------------------------- */

function TimelineSection({ run }: { run: EstimationRun }) {
  const trace = run.trace;
  return (
    <SectionFrame label="Timeline">
      {trace ? (
        <>
          <p className="mt-3 text-sm text-[var(--color-ink-2)]">{trace.summary}</p>
          <div className="mt-3 px-4 py-3 rounded-[var(--radius-md)] border border-dashed border-[var(--color-rule)] bg-[var(--color-paper-2)]">
            <p className="text-[0.78rem] text-[var(--color-ink-3)]">
              Trace v{trace.version} · {trace.steps.length} step
              {trace.steps.length === 1 ? '' : 's'} ·
              expandable timeline component lands in U2 step 8.
            </p>
            <ul className="mt-2 space-y-1 text-[0.78rem] text-[var(--color-ink-3)]">
              {trace.steps.map((step) => (
                <li key={step.n} className="flex items-baseline gap-2 font-mono tabular-nums">
                  <span className="text-[var(--color-ink-4)] w-4 text-right">{step.n}</span>
                  <span className="text-[var(--color-ink-2)] truncate">
                    {step.kind === 'tool_call'
                      ? `tool · ${step.tool}`
                      : step.kind === 'computation'
                        ? `compute · ${step.label}`
                        : 'reasoning'}
                  </span>
                  <span className="ml-auto text-[var(--color-ink-3)]">
                    {step.duration_ms} ms
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </>
      ) : (
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">
          No trace recorded for this run.
        </p>
      )}
    </SectionFrame>
  );
}

/* -------------------------------------------------------------------------- */
/* Footer (raw JSON disclosure)                                               */
/* -------------------------------------------------------------------------- */

function FooterBlock({ run }: { run: EstimationRun }) {
  return (
    <footer className="space-y-4">
      <dl className="grid grid-cols-2 sm:grid-cols-3 gap-x-6 gap-y-3 text-sm">
        <KV label="Created">
          <span
            className="cursor-help text-[var(--color-ink-2)]"
            title={fmtAbsolute(run.created_at)}
          >
            {fmtRelative(run.created_at)}
          </span>
        </KV>
        <KV label="Run id">
          <span className="font-mono tabular-nums">#{run.id}</span>
        </KV>
        {run.input_sreality_id != null && (
          <KV label="Source listing">
            <Link
              to={`/listing/${run.input_sreality_id}`}
              className="font-mono tabular-nums text-[var(--color-copper)] hover:text-[var(--color-copper-2)] underline-offset-2 hover:underline"
            >
              {run.input_sreality_id}
            </Link>
          </KV>
        )}
        {run.parent_run_id != null && (
          <KV label="Rerun of">
            <Link
              to={`/runs/${run.parent_run_id}`}
              className="font-mono tabular-nums text-[var(--color-copper)] hover:text-[var(--color-copper-2)] underline-offset-2 hover:underline"
            >
              #{run.parent_run_id}
            </Link>
          </KV>
        )}
        {run.rerun_reason && (
          <KV label="Rerun reason" colSpan={2}>
            <span className="text-[var(--color-ink-2)]">{run.rerun_reason}</span>
          </KV>
        )}
      </dl>

      <details className="group">
        <summary className="cursor-pointer list-none flex items-center justify-between gap-4">
          <span className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
            Raw JSON
          </span>
          <span className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] group-open:hidden">Show</span>
          <span className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] hidden group-open:inline">Hide</span>
        </summary>
        <pre className="mt-3 px-3 py-3 text-[0.72rem] font-mono leading-relaxed bg-[var(--color-inset)] border border-[var(--color-rule)] rounded-[var(--radius-md)] overflow-x-auto text-[var(--color-ink-2)] max-h-[480px] overflow-y-auto">
{JSON.stringify(run, null, 2)}
        </pre>
      </details>
    </footer>
  );
}

/* -------------------------------------------------------------------------- */
/* Layout primitives                                                          */
/* -------------------------------------------------------------------------- */

function SectionFrame({
  label,
  counter,
  children,
}: {
  label: string;
  counter?: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <div className="flex items-baseline justify-between">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
          {label}
        </p>
        {counter && (
          <span className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)] font-mono tabular-nums">
            {counter}
          </span>
        )}
      </div>
      {children}
    </section>
  );
}

function Hairline() {
  return <div className="my-7 h-px bg-[var(--color-rule)]" />;
}

function KV({
  label, children, colSpan = 1,
}: {
  label: string;
  children: React.ReactNode;
  colSpan?: 1 | 2;
}) {
  return (
    <div className={colSpan === 2 ? 'col-span-2' : undefined}>
      <dt className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        {label}
      </dt>
      <dd className="mt-1 text-sm text-[var(--color-ink)]">{children}</dd>
    </div>
  );
}

function StatusPill({ status }: { status: EstimationRun['status'] }) {
  const palette =
    status === 'success'
      ? 'bg-[var(--color-sage-soft)] text-[var(--color-sage)] border-[var(--color-sage)]/20'
      : status === 'failed'
        ? 'bg-[var(--color-brick-soft)] text-[var(--color-brick)] border-[var(--color-brick)]/20'
        : status === 'pending' || status === 'running'
          ? 'bg-[var(--color-ochre-soft)] text-[var(--color-ochre)] border-[var(--color-ochre)]/20'
          : 'bg-[var(--color-rule-soft)] text-[var(--color-ink-3)] border-[var(--color-rule)]';
  return (
    <span
      className={[
        'inline-flex items-center px-2.5 py-1 text-[0.7rem] tracking-[0.14em] uppercase rounded-[var(--radius-sm)] border',
        palette,
      ].join(' ')}
    >
      {status}
    </span>
  );
}

function SourcePill({ source }: { source: EstimationRun['source'] }) {
  return (
    <span className="inline-flex items-center px-2 py-0.5 text-[0.65rem] tracking-[0.14em] uppercase rounded-[var(--radius-xs)] bg-[var(--color-rule-soft)] text-[var(--color-ink-3)]">
      {source}
    </span>
  );
}

function Sep() {
  return <span className="text-[var(--color-ink-4)]" aria-hidden>·</span>;
}

function BackArrow() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
      <polyline
        points="5.5,1.5 1.5,5 5.5,8.5"
        stroke="currentColor"
        strokeWidth="1.25"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <line
        x1="1.5" y1="5" x2="9" y2="5"
        stroke="currentColor"
        strokeWidth="1.25"
        strokeLinecap="round"
      />
    </svg>
  );
}

function Empty({
  title, body, tone,
}: {
  title: string;
  body: string;
  tone?: 'brick';
}) {
  return (
    <div className="px-6 py-16 max-w-md mx-auto text-center">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        {title}
      </p>
      <p
        className={[
          'mt-3 text-sm',
          tone === 'brick' ? 'text-[var(--color-brick)]' : 'text-[var(--color-ink-2)]',
        ].join(' ')}
      >
        {body}
      </p>
    </div>
  );
}
