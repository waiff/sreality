import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  fetchDedupCostByCategory,
  fetchDedupEngineFlow,
  fetchDedupFunnelResolutions,
  fetchDedupQueueSnapshot,
} from '@/lib/queries';
import {
  assembleFunnel,
  matrixHasData,
  summarizeCapture,
  CATEGORY_MAIN_LABELS,
  CATEGORY_MAIN_ORDER,
  CATEGORY_TYPE_LABELS,
  CATEGORY_TYPE_ORDER,
  type CategoryMatrix,
  type FunnelKind,
  type FunnelStepView,
  type FunnelWindow,
} from '@/lib/dedupFunnel';
import { fmtCount, fmtUsd } from '@/lib/format';

/* The full free/paid dedup funnel — every resolve_pair step with its
 * pair/property counts, so it's obvious how much the free steps capture
 * before any paid vision runs. Deep-linked from /costs (#funnel-…).
 *
 * Two number kinds, deliberately distinct (never compare across them):
 *   pairs resolved  = distinct pairs from dedup_pair_audit (matview, 15 min)
 *   evaluations     = work counters from dedup_engine_runs (re-scans repeat)
 * Paid-step $ figures come from dedup_llm_cost_by_category — the SAME
 * source the /costs category card reads, so the two tabs always agree. */

const KIND_STYLE: Record<FunnelKind, { label: string; cls: string }> = {
  free: { label: 'FREE', cls: 'bg-[var(--color-sage-soft)] text-[var(--color-sage)]' },
  paid: { label: 'PAID', cls: 'bg-[var(--color-ochre-soft)] text-[var(--color-ochre)]' },
  manual: { label: 'MANUAL', cls: 'bg-[var(--color-rule-soft)] text-[var(--color-ink-3)]' },
};

export default function DedupFunnel() {
  const [win, setWin] = useState<FunnelWindow>(30);

  const resolutions = useQuery({
    queryKey: ['dedup-funnel-resolutions'],
    queryFn: fetchDedupFunnelResolutions,
    staleTime: 5 * 60_000,
  });
  const flow = useQuery({
    queryKey: ['dedup-engine-flow'],
    queryFn: fetchDedupEngineFlow,
    staleTime: 5 * 60_000,
  });
  const queue = useQuery({
    queryKey: ['dedup-queue-snapshot'],
    queryFn: fetchDedupQueueSnapshot,
    staleTime: 60_000,
  });
  const cost = useQuery({
    queryKey: ['dedup-cost-by-category'],
    queryFn: fetchDedupCostByCategory,
    staleTime: 5 * 60_000,
  });

  const steps = useMemo(
    () =>
      resolutions.data && queue.data && cost.data
        ? assembleFunnel(resolutions.data, flow.data ?? null, queue.data, cost.data, win)
        : null,
    [resolutions.data, flow.data, queue.data, cost.data, win],
  );
  const capture = useMemo(() => (steps ? summarizeCapture(steps) : null), [steps]);

  // Deep links from /costs land on #funnel-<step>; scroll once data is up.
  useEffect(() => {
    if (!steps) return;
    const hash = window.location.hash.slice(1);
    if (hash.startsWith('funnel')) {
      document.getElementById(hash)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, [steps]);

  const anyError = resolutions.error || flow.error || queue.error || cost.error;

  return (
    <section
      id="funnel"
      className="scroll-mt-4 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4 mt-4"
    >
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h3 className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
            Funnel · free vs paid capture
          </h3>
          <p className="mt-0.5 text-xs text-[var(--color-ink-3)] leading-snug max-w-3xl">
            Every resolve_pair step in engine order.{' '}
            <span className="text-[var(--color-ink-2)]">Pairs</span> are distinct resolved pairs
            (audit log, refreshed ≤ 15 min);{' '}
            <span className="text-[var(--color-ink-2)]">evaluations</span> are work counters —
            re-scans of the same group count each time. Paid-step $ equals the{' '}
            <Link to="/costs" className="underline decoration-[var(--color-rule-strong)] underline-offset-2 hover:text-[var(--color-copper-2)]">
              LLM costs
            </Link>{' '}
            category card (same source).
          </p>
        </div>
        <span className="inline-flex rounded-[var(--radius-sm)] border border-[var(--color-rule)] overflow-hidden">
          {([7, 30] as const).map((w) => (
            <button
              key={w}
              type="button"
              onClick={() => setWin(w)}
              className="px-1.5 py-0.5 text-[0.6rem] font-medium transition-colors"
              style={
                win === w
                  ? { background: 'var(--color-copper)', color: 'var(--color-paper-3)' }
                  : { color: 'var(--color-ink-3)' }
              }
            >
              {w} d
            </button>
          ))}
        </span>
      </div>

      {anyError ? (
        <p className="mt-3 text-sm text-[var(--color-brick)]">
          Funnel data failed to load: {(anyError as Error).message}
        </p>
      ) : !steps || !capture ? (
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">Loading funnel…</p>
      ) : (
        <>
          <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-2">
            <CaptureStat label={`Resolved free · ${win} d`} value={fmtCount(capture.freeResolved)} tone="sage" />
            <CaptureStat label={`Resolved paid · ${win} d`} value={fmtCount(capture.paidResolved)} tone="ochre" />
            <CaptureStat label={`Resolved manually · ${win} d`} value={fmtCount(capture.manualResolved)} />
            <CaptureStat label={`Paid vision spend · ${win} d`} value={fmtUsd(capture.paidCost)} tone="ochre" />
          </div>

          <div className="mt-3 space-y-1.5">
            {steps.map((s) => (
              <StepRow key={s.def.id} step={s} win={win} />
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function CaptureStat({ label, value, tone }: { label: string; value: string; tone?: 'sage' | 'ochre' }) {
  const color =
    tone === 'sage' ? 'text-[var(--color-sage)]' : tone === 'ochre' ? 'text-[var(--color-ochre)]' : 'text-[var(--color-ink)]';
  return (
    <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper)] px-3 py-2">
      <div className="text-[0.62rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">{label}</div>
      <div className={`font-mono tabular-nums text-lg ${color}`}>{value}</div>
    </div>
  );
}

function StepRow({ step, win }: { step: FunnelStepView; win: FunnelWindow }) {
  const [open, setOpen] = useState(false);
  const kind = KIND_STYLE[step.def.kind];
  const resolved = step.merged + step.dismissed;
  const expandable = matrixHasData(step.categories);

  return (
    <div
      id={step.def.anchor}
      className="scroll-mt-4 rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper)]"
    >
      <button
        type="button"
        onClick={() => expandable && setOpen((o) => !o)}
        aria-expanded={open}
        disabled={!expandable}
        className="w-full text-left px-3 py-2 flex items-center gap-3 flex-wrap disabled:cursor-default"
      >
        <span className={`shrink-0 rounded-[3px] px-1.5 py-0.5 text-[0.58rem] font-semibold tracking-[0.08em] ${kind.cls}`}>
          {kind.label}
        </span>
        <span className="shrink-0 text-sm text-[var(--color-ink)]">{step.def.label}</span>
        <span className="hidden md:inline text-[0.7rem] text-[var(--color-ink-4)] leading-snug flex-1 min-w-[12rem]">
          {step.def.description}
        </span>
        <span className="ml-auto flex items-center gap-x-3 gap-y-0.5 flex-wrap font-mono tabular-nums text-[0.72rem]">
          {resolved > 0 || step.def.auditStage || step.def.id === 'operator' ? (
            <>
              <Num label="merged" value={step.merged} tone={step.merged > 0 ? 'sage' : undefined} />
              <Num label="dismissed" value={step.dismissed} tone={step.dismissed > 0 ? 'brick' : undefined} />
              <Num label="properties" value={step.properties} />
              <Num label="listings" value={step.listings} />
            </>
          ) : null}
          {step.evaluations != null ? <Num label="evaluations" value={step.evaluations} muted /> : null}
          {step.extras.map((e) => (
            <Num key={e.label} label={e.label} value={e.value} muted />
          ))}
          {step.def.kind === 'paid' ? (
            <span className="text-[var(--color-ochre)]">
              {fmtUsd(step.cost)}
              <span className="text-[var(--color-ink-4)]"> · {fmtCount(step.calls)} calls · {win} d</span>
            </span>
          ) : null}
          {expandable ? (
            <span className="text-[var(--color-ink-4)]">{open ? '▾' : '▸'}</span>
          ) : null}
        </span>
      </button>
      {open && expandable ? (
        <div className="px-3 pb-2.5">
          <CategoryBreakdown matrix={step.categories} paid={step.def.kind === 'paid'} />
        </div>
      ) : null}
    </div>
  );
}

function Num({ label, value, tone, muted }: { label: string; value: number; tone?: 'sage' | 'brick'; muted?: boolean }) {
  const color =
    tone === 'sage'
      ? 'text-[var(--color-sage)]'
      : tone === 'brick'
        ? 'text-[var(--color-brick)]'
        : muted
          ? 'text-[var(--color-ink-3)]'
          : 'text-[var(--color-ink)]';
  return (
    <span className={color}>
      {fmtCount(value)}
      <span className="text-[var(--color-ink-4)]"> {label}</span>
    </span>
  );
}

function CategoryBreakdown({ matrix, paid }: { matrix: CategoryMatrix; paid: boolean }) {
  return (
    <div className="overflow-x-auto">
      <table className="text-[0.72rem] font-mono tabular-nums">
        <thead>
          <tr className="text-left text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)] font-sans">
            <th className="py-1 pr-4 font-medium">Kategorie</th>
            {CATEGORY_TYPE_ORDER.map((ct) => (
              <th key={ct} className="py-1 pr-4 font-medium text-right">
                {CATEGORY_TYPE_LABELS[ct]}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {CATEGORY_MAIN_ORDER.map((cm) => {
            const row = matrix[cm];
            const empty = !Object.values(row).some((c) => Object.values(c).some((v) => (v ?? 0) > 0));
            if (empty) return null;
            return (
              <tr key={cm} className="border-t border-[var(--color-rule-soft)]">
                <td className="py-1 pr-4 font-sans text-[var(--color-ink-2)]">{CATEGORY_MAIN_LABELS[cm]}</td>
                {CATEGORY_TYPE_ORDER.map((ct) => {
                  const c = row[ct] ?? {};
                  const has = Object.values(c).some((v) => (v ?? 0) > 0);
                  return (
                    <td key={ct} className="py-1 pr-4 text-right">
                      {!has ? (
                        <span className="text-[var(--color-ink-4)]">—</span>
                      ) : paid ? (
                        <>
                          {fmtUsd(c.cost ?? 0)}
                          <span className="text-[var(--color-ink-4)]"> · {fmtCount(c.calls ?? 0)} calls · {fmtCount(c.listings ?? 0)} inz.</span>
                        </>
                      ) : (
                        <>
                          {fmtCount(c.pairs ?? 0)}
                          <span className="text-[var(--color-ink-4)]"> párů{c.properties ? ` · ${fmtCount(c.properties)} nem.` : ''}</span>
                        </>
                      )}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
