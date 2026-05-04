/* Estimation trace renderer.
 *
 * Reads `trace.steps` and renders one row per step, dispatching on
 * `step.kind`. The renderer map is keyed by TraceStepKind so adding
 * the U4 agent's `reasoning` row is a single entry, not a switch
 * change.
 *
 * Today's deterministic mode produces 4 steps; the agent track will
 * produce 20+. The component's layout (vertical line, dots, expand
 * per step) is sized for either.
 */

import { useState, type FC, type ReactNode } from 'react';
import type {
  Trace,
  TraceStep,
  TraceStepComputation,
  TraceStepKind,
  TraceStepReasoning,
  TraceStepToolCall,
} from '@/lib/types';
import { fmtCount, fmtCzk } from '@/lib/format';

interface Props {
  trace: Trace | null;
}

export default function Timeline({ trace }: Props) {
  if (!trace || trace.steps.length === 0) {
    return (
      <div className="text-sm text-[var(--color-ink-3)] italic">
        No steps recorded.
      </div>
    );
  }

  const expandDefaults = computeDefaultExpansion(trace.steps);

  return (
    <div>
      {trace.summary && (
        <p className="mb-5 text-[0.85rem] text-[var(--color-ink-2)] leading-relaxed">
          {trace.summary}
        </p>
      )}
      <ol className="relative pl-7">
        <span
          aria-hidden
          className="absolute left-[10px] top-1 bottom-1 w-px bg-[var(--color-rule)]"
        />
        {trace.steps.map((step) => (
          <StepRow
            key={step.n}
            step={step}
            defaultExpanded={expandDefaults.has(step.n)}
          />
        ))}
      </ol>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Default-expansion heuristic — last step plus anything that took >500ms.    */
/* -------------------------------------------------------------------------- */

function computeDefaultExpansion(steps: TraceStep[]): Set<number> {
  const out = new Set<number>();
  if (steps.length === 0) return out;
  out.add(steps[steps.length - 1].n);
  for (const s of steps) {
    if (s.duration_ms > 500) out.add(s.n);
  }
  return out;
}

/* -------------------------------------------------------------------------- */
/* Per-step row                                                               */
/* -------------------------------------------------------------------------- */

interface StepRendererProps {
  step: TraceStep;
}

const RENDERERS: Record<TraceStepKind, FC<StepRendererProps>> = {
  tool_call: ToolCallBody,
  computation: ComputationBody,
  reasoning: ReasoningBody,
};

function StepRow({
  step,
  defaultExpanded,
}: {
  step: TraceStep;
  defaultExpanded: boolean;
}) {
  const [open, setOpen] = useState(defaultExpanded);
  const Body = RENDERERS[step.kind];
  const headline = stepHeadline(step);

  return (
    <li className="relative pb-5 last:pb-0">
      <span
        aria-hidden
        className="absolute -left-[26px] top-1.5 w-[9px] h-[9px] rounded-full bg-[var(--color-paper)] border-2 border-[var(--color-copper)]"
      />
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="w-full flex items-baseline gap-3 text-left group"
      >
        <span className="font-mono tabular-nums text-[0.7rem] text-[var(--color-ink-4)] w-5 shrink-0">
          {step.n}
        </span>
        <KindBadge kind={step.kind} />
        <span className="flex-1 min-w-0 text-sm text-[var(--color-ink)] group-hover:text-[var(--color-copper)] transition-colors truncate">
          {headline}
        </span>
        <span className="font-mono tabular-nums text-[0.7rem] text-[var(--color-ink-3)] shrink-0">
          {fmtDuration(step.duration_ms)}
        </span>
        <Chevron open={open} />
      </button>
      {open && (
        <div className="mt-2 ml-[60px] text-sm">
          <Body step={step} />
        </div>
      )}
    </li>
  );
}

function stepHeadline(step: TraceStep): string {
  if (step.kind === 'tool_call') return step.tool;
  if (step.kind === 'computation') return step.label;
  return 'agent step';
}

/* -------------------------------------------------------------------------- */
/* Kind badge                                                                 */
/* -------------------------------------------------------------------------- */

const KIND_LABELS: Record<TraceStepKind, string> = {
  tool_call: 'tool',
  computation: 'compute',
  reasoning: 'reason',
};

function KindBadge({ kind }: { kind: TraceStepKind }) {
  const tone =
    kind === 'tool_call'
      ? 'bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
      : kind === 'computation'
        ? 'bg-[var(--color-sage-soft)] text-[var(--color-sage)]'
        : 'bg-[var(--color-ochre-soft)] text-[var(--color-ochre)]';
  return (
    <span
      className={[
        'shrink-0 inline-block px-1.5 py-px text-[0.6rem] tracking-[0.14em] uppercase rounded-[var(--radius-xs)] font-medium',
        tone,
      ].join(' ')}
    >
      {KIND_LABELS[kind]}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Per-kind bodies                                                            */
/* -------------------------------------------------------------------------- */

function ToolCallBody({ step }: { step: TraceStep }) {
  const s = step as TraceStepToolCall;
  return (
    <div className="space-y-3">
      <DetailGroup label="Tool">
        <span className="font-mono text-[0.78rem] text-[var(--color-ink-2)]">
          {s.tool}
        </span>
      </DetailGroup>
      <DetailGroup label="Input">
        <SummaryGrid summary={s.input} />
      </DetailGroup>
      <DetailGroup label="Output summary">
        <SummaryGrid summary={s.output_summary} />
      </DetailGroup>
    </div>
  );
}

function ComputationBody({ step }: { step: TraceStep }) {
  const s = step as TraceStepComputation;
  return (
    <DetailGroup label={s.label}>
      <SummaryGrid summary={s.output_summary} />
    </DetailGroup>
  );
}

function ReasoningBody({ step }: { step: TraceStep }) {
  const s = step as TraceStepReasoning;
  return (
    <DetailGroup label="Reasoning">
      <p className="text-[0.78rem] text-[var(--color-ink-3)] italic">
        Reserved for the U4 agent track. Today's deterministic runs do not
        produce reasoning steps.
      </p>
      {Object.keys(s.output_summary ?? {}).length > 0 && (
        <div className="mt-2">
          <SummaryGrid summary={s.output_summary} />
        </div>
      )}
    </DetailGroup>
  );
}

/* -------------------------------------------------------------------------- */
/* Detail rendering                                                           */
/* -------------------------------------------------------------------------- */

function DetailGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <p className="text-[0.6rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
        {label}
      </p>
      <div className="mt-1.5">{children}</div>
    </div>
  );
}

function SummaryGrid({ summary }: { summary: Record<string, unknown> | undefined }) {
  if (!summary || Object.keys(summary).length === 0) {
    return <p className="text-[0.78rem] text-[var(--color-ink-4)] italic">empty</p>;
  }
  const entries = Object.entries(summary);
  return (
    <dl className="grid grid-cols-[max-content_minmax(0,_1fr)] gap-x-4 gap-y-1">
      {entries.map(([k, v]) => (
        <SummaryRow key={k} k={k} v={v} />
      ))}
    </dl>
  );
}

function SummaryRow({ k, v }: { k: string; v: unknown }) {
  return (
    <>
      <dt className="font-mono text-[0.72rem] text-[var(--color-ink-3)] whitespace-nowrap">
        {k}
      </dt>
      <dd className="text-[0.78rem] text-[var(--color-ink)] min-w-0 break-words">
        <SummaryValue k={k} v={v} />
      </dd>
    </>
  );
}

function SummaryValue({ k, v }: { k: string; v: unknown }) {
  if (v == null) return <span className="text-[var(--color-ink-4)]">null</span>;
  if (typeof v === 'boolean') {
    return <span className="font-mono tabular-nums">{String(v)}</span>;
  }
  if (typeof v === 'number') {
    return (
      <span className="font-mono tabular-nums">
        {looksMonetary(k) ? fmtCzk(v) : fmtCount(v)}
      </span>
    );
  }
  if (typeof v === 'string') {
    return <span>{v}</span>;
  }
  if (Array.isArray(v)) {
    if (v.length === 0) return <span className="text-[var(--color-ink-4)]">[]</span>;
    if (v.every(isPrimitive)) {
      return <span className="font-mono tabular-nums">{v.map(String).join(', ')}</span>;
    }
    return <pre className="font-mono text-[0.72rem] text-[var(--color-ink-2)] whitespace-pre-wrap bg-[var(--color-inset)] px-2 py-1 rounded-[var(--radius-xs)] border border-[var(--color-rule)] overflow-x-auto">{safeStringify(v)}</pre>;
  }
  if (typeof v === 'object') {
    return <pre className="font-mono text-[0.72rem] text-[var(--color-ink-2)] whitespace-pre-wrap bg-[var(--color-inset)] px-2 py-1 rounded-[var(--radius-xs)] border border-[var(--color-rule)] overflow-x-auto">{safeStringify(v)}</pre>;
  }
  return <span>{String(v)}</span>;
}

function isPrimitive(x: unknown): boolean {
  return x == null || ['string', 'number', 'boolean'].includes(typeof x);
}

function looksMonetary(key: string): boolean {
  const k = key.toLowerCase();
  return (
    k.endsWith('_czk') ||
    k.startsWith('rent_') ||
    k.startsWith('price_') ||
    k.endsWith('_price') ||
    k.endsWith('_rent')
  );
}

function safeStringify(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

/* -------------------------------------------------------------------------- */
/* Chrome                                                                     */
/* -------------------------------------------------------------------------- */

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="9"
      height="9"
      viewBox="0 0 9 9"
      aria-hidden
      className="shrink-0 text-[var(--color-ink-4)] transition-transform"
      style={{ transform: open ? 'rotate(90deg)' : 'rotate(0deg)' }}
    >
      <polyline
        points="2,1.5 6,4.5 2,7.5"
        stroke="currentColor"
        strokeWidth="1.25"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
