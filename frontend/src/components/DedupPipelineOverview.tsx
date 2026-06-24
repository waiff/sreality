import { lazy, Suspense } from 'react';
import { useQuery } from '@tanstack/react-query';

import { getDedupPipelineOverview, type DedupPipelineOverview } from '@/lib/api';
import { fmtCount, fmtRelative } from '@/lib/format';

// Lazy so recharts stays out of the /dedup entry chunk (the app's chart convention).
const DedupPipelineTimeline = lazy(() => import('@/components/DedupPipelineTimeline'));

/* The dedup funnel at a glance — four stages on a vertical spine that reads
 * top→bottom (photos tagged → listings eligible → candidate pairs → decisions).
 * Each stage shows its cumulative count and a copper "movement" chip = work done in
 * the last 24h, so a stalled stage stands out (idle → quiet, not alarming). The
 * footer is the engine's heartbeat (last run + CLIP routing). Civic-archive:
 * borders-only depth, copper = flow, tabular numerals. Polls so it ticks live. */

const POLL_MS = 60_000;

type Stage = {
  key: string;
  label: string;
  total: number;
  delta: number | null; // null = a population (no throughput), not a flow
  sub: string;
};

function Movement({ delta }: { delta: number | null }) {
  if (delta == null) return null;
  const moving = delta > 0;
  return (
    <span
      className={[
        'inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[var(--radius-xs)] border text-[0.68rem] tabular-nums whitespace-nowrap',
        moving
          ? 'text-[var(--color-copper)] border-[var(--color-copper)] bg-[var(--color-copper-soft)]'
          : 'text-[var(--color-ink-4)] border-[var(--color-rule)]',
      ].join(' ')}
      title="New in the last 24 hours"
    >
      {moving ? `+${fmtCount(delta)}` : '0'} / 24h
    </span>
  );
}

function Node({ stage, last }: { stage: Stage; last: boolean }) {
  const flowing = stage.delta == null ? stage.total > 0 : stage.delta > 0;
  return (
    <li className="relative flex gap-3 pb-5 last:pb-0">
      {!last && (
        <span
          aria-hidden="true"
          className="absolute left-[6px] top-5 bottom-0 w-px bg-[var(--color-rule)]"
        />
      )}
      <span
        aria-hidden="true"
        className={[
          'relative z-10 mt-1 shrink-0 h-3.5 w-3.5 rounded-full border-2 bg-[var(--color-paper)]',
          flowing ? 'border-[var(--color-copper)]' : 'border-[var(--color-rule-strong)]',
        ].join(' ')}
      />
      <div className="min-w-0 flex-1 flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm text-[var(--color-ink)]">{stage.label}</div>
          <div className="text-[0.72rem] text-[var(--color-ink-4)] truncate">{stage.sub}</div>
        </div>
        <div className="shrink-0 flex items-baseline gap-2">
          <Movement delta={stage.delta} />
          <span
            className="text-xl tabular-nums text-[var(--color-ink)]"
            style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
          >
            {fmtCount(stage.total)}
          </span>
        </div>
      </div>
    </li>
  );
}

function stagesFrom(d: DedupPipelineOverview): Stage[] {
  return [
    {
      key: 'tag',
      label: 'Tag & embed (CLIP)',
      total: d.tagging.total,
      delta: d.tagging.delta_24h,
      sub: `${fmtCount(d.tagging.embeddings)} embeddings`,
    },
    {
      key: 'eligible',
      label: 'Eligible listings',
      total: d.eligible.total,
      delta: null,
      sub: `${fmtCount(d.eligible.flagged_location)} no street · ${fmtCount(d.eligible.flagged_disposition)} no disposition`,
    },
    {
      key: 'candidates',
      label: 'Candidate pairs',
      total: d.candidates.total,
      delta: d.candidates.delta_24h,
      sub: 'awaiting your review',
    },
    {
      key: 'decisions',
      label: 'Decisions',
      total: d.decisions.total,
      delta: d.decisions.delta_24h,
      sub: `${fmtCount(d.decisions.merged)} merged · ${fmtCount(d.decisions.dismissed)} dismissed`,
    },
  ];
}

export default function DedupPipelineOverview() {
  const q = useQuery({
    queryKey: ['dedup', 'pipeline-overview'],
    queryFn: getDedupPipelineOverview,
    refetchInterval: POLL_MS,
  });
  const d = q.data?.data;

  return (
    <section className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4">
      <div className="flex items-center justify-between gap-3 mb-3">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Pipeline
        </p>
        {d?.last_run && (
          <p className="text-[0.7rem] text-[var(--color-ink-4)] tabular-nums">
            last run {fmtRelative(d.last_run.started_at)}
          </p>
        )}
      </div>

      {q.isLoading ? (
        <p className="text-sm text-[var(--color-ink-3)]">Loading…</p>
      ) : !d ? (
        <p className="text-sm text-[var(--color-ink-3)]">
          {q.error ? `Couldn’t load: ${(q.error as Error).message}` : 'No data yet.'}
        </p>
      ) : (
        <>
          <ol className="relative">
            {stagesFrom(d).map((s, i, arr) => (
              <Node key={s.key} stage={s} last={i === arr.length - 1} />
            ))}
          </ol>

          {d.last_run && (
            <div className="mt-3 pt-3 border-t border-[var(--color-rule)] text-[0.72rem] text-[var(--color-ink-3)] tabular-nums flex flex-wrap gap-x-3 gap-y-1">
              <span>
                Last engine run: {fmtCount(d.last_run.auto_merged)} merged ·{' '}
                {fmtCount(d.last_run.queued)} queued
              </span>
              <span className="text-[var(--color-ink-4)]">
                CLIP {fmtCount(d.last_run.clip_classified)} tagged ·{' '}
                {fmtCount(d.last_run.routed_haiku)}→Haiku /{' '}
                {fmtCount(d.last_run.routed_sonnet)}→Sonnet ·{' '}
                {fmtCount(d.last_run.vision_calls)} vision
              </span>
            </div>
          )}

          <Suspense fallback={null}>
            <DedupPipelineTimeline />
          </Suspense>
        </>
      )}
    </section>
  );
}
