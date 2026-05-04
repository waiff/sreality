import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { estimationKeys, fetchEstimation } from '@/lib/queries';
import { fmtCzk, fmtRelative, fmtAbsolute } from '@/lib/format';
import type { EstimationRun } from '@/lib/types';

/* Placeholder run-detail page. Step 7 of U2 fills out the full audit
 * surface (header, result card, spec card, comparables, timeline, footer).
 * For now this lets the post-submit redirect from /estimate land on a real
 * page that confirms the row was written. */

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
    return (
      <Empty
        title="Couldn't load run"
        body={q.error.message}
        tone="brick"
      />
    );
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
  return <RunPlaceholder run={run} />;
}

function RunPlaceholder({ run }: { run: EstimationRun }) {
  return (
    <div className="px-6 py-8 max-w-3xl mx-auto">
      <Link
        to="/estimate"
        className="inline-flex items-center gap-1.5 text-[0.75rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
      >
        <span>← New estimate</span>
      </Link>

      <header className="mt-5 flex items-start justify-between gap-6">
        <div>
          <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
            Estimation run
          </p>
          <h1
            className="mt-2 text-[1.9rem] leading-tight"
            style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
          >
            #{run.id}
          </h1>
          <p
            className="mt-2 text-sm text-[var(--color-ink-2)] cursor-help"
            title={fmtAbsolute(run.created_at)}
          >
            {fmtRelative(run.created_at)} · source{' '}
            <span className="font-mono tabular-nums">{run.source}</span> ·{' '}
            mode <span className="font-mono">{run.mode}</span>
          </p>
        </div>
        <StatusPill status={run.status} />
      </header>

      <div className="my-7 h-px bg-[var(--color-rule)]" />

      {run.status === 'success' ? (
        <ResultPreview run={run} />
      ) : run.status === 'failed' ? (
        <FailureBlock message={run.error_message ?? 'Run failed.'} />
      ) : (
        <p className="text-sm text-[var(--color-ink-2)]">
          Status: {run.status}
        </p>
      )}

      <div className="my-7 h-px bg-[var(--color-rule)]" />

      <p className="text-[0.78rem] text-[var(--color-ink-3)]">
        Full audit view (spec card, comparables map, trace timeline) lands in
        the next step. For now this is a thin confirmation that the row was
        persisted.
      </p>
    </div>
  );
}

function ResultPreview({ run }: { run: EstimationRun }) {
  return (
    <div>
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
        Estimated rent
      </p>
      <p
        className="mt-2 text-[2.2rem] leading-tight tabular-nums"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        {fmtCzk(run.estimated_monthly_rent_czk)}
        <span className="text-base font-sans font-normal text-[var(--color-ink-3)] tracking-wide ml-1">
          / měs.
        </span>
      </p>
      {(run.rent_p25_czk != null || run.rent_p75_czk != null) && (
        <p className="mt-1 text-sm font-mono tabular-nums text-[var(--color-ink-2)]">
          p25 {fmtCzk(run.rent_p25_czk)} — p75 {fmtCzk(run.rent_p75_czk)}
        </p>
      )}
      {run.gross_yield_pct != null && (
        <p className="mt-1 text-sm text-[var(--color-ink-2)]">
          Gross yield <span className="font-mono tabular-nums">{run.gross_yield_pct}</span>%
        </p>
      )}
      {run.confidence && (
        <p className="mt-3">
          <span className="px-2 py-0.5 text-[0.65rem] tracking-[0.14em] uppercase rounded-[var(--radius-xs)] bg-[var(--color-copper-soft)] text-[var(--color-copper)]">
            {run.confidence} confidence
          </span>
        </p>
      )}
    </div>
  );
}

function FailureBlock({ message }: { message: string }) {
  return (
    <div className="px-3 py-3 rounded-[var(--radius-md)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)]">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-brick)] font-medium">
        Failed
      </p>
      <p className="mt-2 text-sm text-[var(--color-brick)]">{message}</p>
    </div>
  );
}

function StatusPill({ status }: { status: EstimationRun['status'] }) {
  const palette = (() => {
    switch (status) {
      case 'success':
        return 'bg-[var(--color-sage-soft)] text-[var(--color-sage)] border-[var(--color-sage)]/20';
      case 'failed':
        return 'bg-[var(--color-brick-soft)] text-[var(--color-brick)] border-[var(--color-brick)]/20';
      case 'pending':
      case 'running':
        return 'bg-[var(--color-ochre-soft)] text-[var(--color-ochre)] border-[var(--color-ochre)]/20';
      default:
        return 'bg-[var(--color-rule-soft)] text-[var(--color-ink-3)] border-[var(--color-rule)]';
    }
  })();
  return (
    <span
      className={[
        'shrink-0 inline-flex items-center px-2.5 py-1 text-[0.7rem] tracking-wide rounded-[var(--radius-sm)] border',
        palette,
      ].join(' ')}
    >
      {status}
    </span>
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
