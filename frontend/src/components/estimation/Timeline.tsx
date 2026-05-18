/* Estimation trace renderer.
 *
 * Two layouts, switched on `trace.version`:
 *
 *   v1 (and v2 deterministic runs)
 *     Flat ordered list of steps, dispatching on `step.kind` via the
 *     RENDERERS map. The Timeline reads top-to-bottom like a log.
 *
 *   v2 with a `comparable_selection_summary` computation step
 *     Agent runs append a final computation step whose
 *     `output_summary.rounds` describes the per-iteration cohort
 *     selection ladder. We pluck it out and render:
 *       - a top "Strategy" panel with a filter-delta table and the
 *         final-picks count
 *       - the remaining trace steps grouped into collapsible
 *         "Round N" blocks, each with its cohort-diff sub-panel
 *
 * The leaf renderers (ToolCallBody / ComputationBody / ReasoningBody)
 * and the SummaryGrid helpers are shared across both layouts.
 */

import { createContext, useContext, useState, type FC, type ReactNode } from 'react';
import type {
  Trace,
  TraceStep,
  TraceStepComputation,
  TraceStepKind,
  TraceStepReasoning,
  TraceStepToolCall,
} from '@/lib/types';
import { fmtCount, fmtCzk } from '@/lib/format';
import { useTracePayload } from '@/lib/queries';

interface Props {
  trace: Trace | null;
  /**
   * Estimation run id. When supplied, tool_call rows expose a
   * "Show full payload" expander that fetches from
   * GET /estimations/{runId}/trace/{n}/payload. Omit for previews or
   * any context where the payload side-table isn't reachable.
   */
  runId?: number;
}

const RunIdContext = createContext<number | null>(null);

const SELECTION_SUMMARY_LABEL = 'comparable_selection_summary';
const FCR_TOOL_NAME = 'find_comparables_relaxed';

interface SelectionRoundFilters {
  radius_m: number | null;
  area_band_pct: number | null;
  disposition_match: string | null;
  max_age_days: number | null;
  min_results: number | null;
  active_only?: boolean | null;
  population?: string | null;
  floor_band?: number | null;
  condition_match?: string[] | null;
  building_type_match?: string[] | null;
  energy_rating_match?: string[] | null;
  has_balcony?: boolean | null;
  has_lift?: boolean | null;
  has_parking?: boolean | null;
  min_price_czk?: number | null;
  max_price_czk?: number | null;
  category_main?: string | null;
  category_type?: string | null;
  category_sub_cb?: number | null;
  locality_district_id?: number | null;
  locality_region_id?: number | null;
  include_unreliable?: boolean | null;
  furnished?: string | null;
  terrace?: boolean | null;
  cellar?: boolean | null;
  garage?: boolean | null;
  ownership?: string | null;
  min_estate_area?: number | null;
  max_estate_area?: number | null;
  min_usable_area?: number | null;
  max_usable_area?: number | null;
  min_parking_lots?: number | null;
  building_condition_level_min?: number | null;
  apartment_condition_level_min?: number | null;
}

interface SelectionRound {
  n: number;
  filters: SelectionRoundFilters;
  cohort_size: number;
  cohort_ids: number[];
  added_ids: number[];
  removed_ids: number[];
  n_relaxations: number;
  reasoning: string;
}

interface SelectionSummary {
  n_rounds: number;
  rounds: SelectionRound[];
  final_filters: SelectionRoundFilters | null;
  final_comparable_ids: number[];
  stop_reason: string | null;
}

export default function Timeline({ trace, runId }: Props) {
  if (!trace || trace.steps.length === 0) {
    return (
      <div className="text-sm text-[var(--color-ink-3)] italic">
        No steps recorded.
      </div>
    );
  }

  const summary = pickSelectionSummary(trace.steps);
  const toolStats = aggregateToolStats(trace.steps);
  const body =
    summary && summary.rounds.length > 0 ? (
      <V2Layout trace={trace} summary={summary} />
    ) : (
      <FlatLayout trace={trace} />
    );

  return (
    <RunIdContext.Provider value={runId ?? null}>
      {toolStats.length > 0 && (
        <div className="mb-7">
          <ToolRuntimeSummary stats={toolStats} />
        </div>
      )}
      {body}
    </RunIdContext.Provider>
  );
}

/* -------------------------------------------------------------------------- */
/* Tool runtime summary — per-run rollup of tool_call durations               */
/* -------------------------------------------------------------------------- */

interface ToolStat {
  tool: string;
  count: number;
  total_ms: number;
  max_ms: number;
}

function aggregateToolStats(steps: TraceStep[]): ToolStat[] {
  const byTool = new Map<string, ToolStat>();
  for (const step of steps) {
    if (step.kind !== 'tool_call') continue;
    const tool = (step as TraceStepToolCall).tool;
    if (!tool) continue;
    const ms = step.duration_ms ?? 0;
    const existing = byTool.get(tool);
    if (existing) {
      existing.count += 1;
      existing.total_ms += ms;
      if (ms > existing.max_ms) existing.max_ms = ms;
    } else {
      byTool.set(tool, { tool, count: 1, total_ms: ms, max_ms: ms });
    }
  }
  return [...byTool.values()].sort((a, b) => b.total_ms - a.total_ms);
}

function ToolRuntimeSummary({ stats }: { stats: ToolStat[] }) {
  const grandTotalMs = stats.reduce((sum, s) => sum + s.total_ms, 0);
  const grandCalls = stats.reduce((sum, s) => sum + s.count, 0);
  return (
    <details className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]">
      <summary className="cursor-pointer select-none px-3 py-2 flex items-baseline gap-3">
        <span className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
          Tool runtime
        </span>
        <span className="text-[0.78rem] text-[var(--color-ink-2)] tabular-nums">
          {fmtCount(grandCalls)} call{grandCalls === 1 ? '' : 's'}
          {' · '}
          {fmtDuration(grandTotalMs)} total
        </span>
      </summary>
      <div className="px-3 pb-3 pt-1 overflow-x-auto">
        <table className="w-full text-[0.8rem]">
          <thead>
            <tr className="text-left text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
              <th scope="col" className="py-1.5 pr-4 font-medium">Tool</th>
              <th scope="col" className="py-1.5 px-2 text-right font-medium">Calls</th>
              <th scope="col" className="py-1.5 px-2 text-right font-medium">Total</th>
              <th scope="col" className="py-1.5 pl-2 text-right font-medium">Longest</th>
            </tr>
          </thead>
          <tbody>
            {stats.map((s) => (
              <tr key={s.tool} className="border-t border-[var(--color-rule-soft)]">
                <td className="py-1.5 pr-4 font-mono text-[var(--color-ink-2)] truncate">
                  {s.tool}
                </td>
                <td className="py-1.5 px-2 text-right tabular-nums text-[var(--color-ink-2)]">
                  {fmtCount(s.count)}
                </td>
                <td className="py-1.5 px-2 text-right tabular-nums text-[var(--color-ink-2)]">
                  {fmtDuration(s.total_ms)}
                </td>
                <td className="py-1.5 pl-2 text-right tabular-nums text-[var(--color-ink-2)]">
                  {fmtDuration(s.max_ms)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

/* -------------------------------------------------------------------------- */
/* Selection-summary extraction                                               */
/* -------------------------------------------------------------------------- */

function pickSelectionSummary(steps: TraceStep[]): SelectionSummary | null {
  for (const step of steps) {
    if (step.kind !== 'computation') continue;
    if ((step as TraceStepComputation).label !== SELECTION_SUMMARY_LABEL) continue;
    const out = step.output_summary as Partial<SelectionSummary> | undefined;
    if (!out || !Array.isArray(out.rounds)) return null;
    return {
      n_rounds: out.n_rounds ?? out.rounds.length,
      rounds: out.rounds as SelectionRound[],
      final_filters: (out.final_filters ?? null) as SelectionRoundFilters | null,
      final_comparable_ids: Array.isArray(out.final_comparable_ids)
        ? (out.final_comparable_ids as number[])
        : [],
      stop_reason:
        typeof out.stop_reason === 'string' ? out.stop_reason : null,
    };
  }
  return null;
}

/* -------------------------------------------------------------------------- */
/* V2 layout: strategy panel + per-round groups                               */
/* -------------------------------------------------------------------------- */

function V2Layout({
  trace,
  summary,
}: {
  trace: Trace;
  summary: SelectionSummary;
}) {
  const groups = groupStepsByRound(trace.steps);

  return (
    <div className="space-y-7">
      {trace.summary && (
        <p className="text-[0.85rem] text-[var(--color-ink-2)] leading-relaxed">
          {trace.summary}
        </p>
      )}

      <StrategyPanel summary={summary} />

      <div>
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium mb-3">
          Selection rounds
        </p>
        <div className="space-y-2">
          {groups.map((group, idx) => (
            <RoundBlock
              key={group.label}
              group={group}
              round={
                group.roundN > 0 ? summary.rounds[group.roundN - 1] : null
              }
              isLast={idx === groups.length - 1}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Strategy panel: filter-delta table + final picks                           */
/* -------------------------------------------------------------------------- */

const EM_DASH = '—';

function fmtNumberUnit(v: unknown, unit: string): string {
  return typeof v === 'number' ? `${fmtCount(v)} ${unit}` : EM_DASH;
}

function fmtBool(v: unknown): string {
  if (v === true) return 'yes';
  if (v === false) return 'no';
  return EM_DASH;
}

function fmtString(v: unknown): string {
  return typeof v === 'string' && v ? v : EM_DASH;
}

function fmtList(v: unknown): string {
  if (Array.isArray(v) && v.length > 0) return v.join(', ');
  return EM_DASH;
}

function fmtCzkPrice(v: unknown): string {
  return typeof v === 'number' ? fmtCzk(v) : EM_DASH;
}

function filterValueEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i += 1) {
      if (a[i] !== b[i]) return false;
    }
    return true;
  }
  return false;
}

const FILTER_ROWS: Array<{
  key: keyof SelectionRoundFilters;
  label: string;
  fmt: (v: unknown) => string;
}> = [
  { key: 'radius_m', label: 'Radius', fmt: (v) => fmtNumberUnit(v, 'm') },
  { key: 'area_band_pct', label: 'Area band', fmt: (v) => (typeof v === 'number' ? `±${Math.round(v * 100)}%` : EM_DASH) },
  { key: 'disposition_match', label: 'Disposition', fmt: fmtString },
  { key: 'max_age_days', label: 'Max age', fmt: (v) => (typeof v === 'number' ? `${v} d` : EM_DASH) },
  { key: 'min_results', label: 'Min results', fmt: (v) => (typeof v === 'number' ? String(v) : EM_DASH) },
  { key: 'active_only', label: 'Active only', fmt: fmtBool },
  { key: 'population', label: 'Population', fmt: fmtString },
  { key: 'floor_band', label: 'Floor band', fmt: (v) => (typeof v === 'number' ? `±${v}` : EM_DASH) },
  { key: 'condition_match', label: 'Condition', fmt: fmtList },
  { key: 'building_type_match', label: 'Building type', fmt: fmtList },
  { key: 'energy_rating_match', label: 'Energy rating', fmt: fmtList },
  { key: 'has_balcony', label: 'Has balcony*', fmt: fmtBool },
  { key: 'has_lift', label: 'Has lift', fmt: fmtBool },
  { key: 'has_parking', label: 'Has parking*', fmt: fmtBool },
  { key: 'min_price_czk', label: 'Min price', fmt: fmtCzkPrice },
  { key: 'max_price_czk', label: 'Max price', fmt: fmtCzkPrice },
  { key: 'category_main', label: 'Category', fmt: fmtString },
  { key: 'category_type', label: 'Type', fmt: fmtString },
  { key: 'category_sub_cb', label: 'Sub-category', fmt: (v) => (typeof v === 'number' ? String(v) : EM_DASH) },
  { key: 'locality_district_id', label: 'District id', fmt: (v) => (typeof v === 'number' ? String(v) : EM_DASH) },
  { key: 'locality_region_id', label: 'Region id', fmt: (v) => (typeof v === 'number' ? String(v) : EM_DASH) },
  { key: 'include_unreliable', label: 'Include unreliable', fmt: fmtBool },
  { key: 'furnished', label: 'Furnished', fmt: fmtString },
  { key: 'terrace', label: 'Terrace', fmt: fmtBool },
  { key: 'cellar', label: 'Cellar', fmt: fmtBool },
  { key: 'garage', label: 'Garage', fmt: fmtBool },
  { key: 'ownership', label: 'Ownership', fmt: fmtString },
  { key: 'min_estate_area', label: 'Min estate area', fmt: (v) => fmtNumberUnit(v, 'm²') },
  { key: 'max_estate_area', label: 'Max estate area', fmt: (v) => fmtNumberUnit(v, 'm²') },
  { key: 'min_usable_area', label: 'Min usable area', fmt: (v) => fmtNumberUnit(v, 'm²') },
  { key: 'max_usable_area', label: 'Max usable area', fmt: (v) => fmtNumberUnit(v, 'm²') },
  { key: 'min_parking_lots', label: 'Min parking lots', fmt: (v) => (typeof v === 'number' ? String(v) : EM_DASH) },
  { key: 'building_condition_level_min', label: 'Min building condition', fmt: (v) => (typeof v === 'number' ? String(v) : EM_DASH) },
  { key: 'apartment_condition_level_min', label: 'Min apartment condition', fmt: (v) => (typeof v === 'number' ? String(v) : EM_DASH) },
];

function StrategyPanel({ summary }: { summary: SelectionSummary }) {
  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-4 py-4">
      <div className="flex items-baseline justify-between gap-4 flex-wrap">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
          Strategy
        </p>
        <p className="text-[0.78rem] text-[var(--color-ink-3)]">
          <span className="font-mono tabular-nums text-[var(--color-ink)]">
            {summary.n_rounds}
          </span>
          {' '}
          {summary.n_rounds === 1 ? 'strategy tried' : 'strategies tried'} ·{' '}
          <span className="font-mono tabular-nums text-[var(--color-ink)]">
            {summary.final_comparable_ids.length}
          </span>
          {' '}comparables kept
          {summary.stop_reason && (
            <>
              {' '}·{' '}
              <span className="font-mono tabular-nums text-[var(--color-ink-2)]">
                {summary.stop_reason}
              </span>
            </>
          )}
        </p>
      </div>

      <div className="mt-4 overflow-x-auto">
        <FilterDeltaTable rounds={summary.rounds} />
      </div>
    </div>
  );
}

function FilterDeltaTable({ rounds }: { rounds: SelectionRound[] }) {
  return (
    <table className="min-w-full text-sm border-collapse">
      <thead>
        <tr>
          <th
            scope="col"
            className="text-left text-[0.6rem] tracking-[0.16em] uppercase font-medium text-[var(--color-ink-4)] pr-4 pb-2"
          >
            Filter
          </th>
          {rounds.map((r) => (
            <th
              key={r.n}
              scope="col"
              className="text-right text-[0.6rem] tracking-[0.16em] uppercase font-medium text-[var(--color-ink-4)] px-2 pb-2"
            >
              Round {r.n}
            </th>
          ))}
          <th
            scope="col"
            className="text-right text-[0.6rem] tracking-[0.16em] uppercase font-medium text-[var(--color-copper)] pl-3 pb-2"
          >
            Final
          </th>
        </tr>
      </thead>
      <tbody>
        {FILTER_ROWS.map(({ key, label, fmt }) => (
          <tr key={key} className="border-t border-[var(--color-rule-soft)]">
            <th
              scope="row"
              className="text-left text-[0.78rem] text-[var(--color-ink-3)] pr-4 py-1.5 font-normal"
            >
              {label}
            </th>
            {rounds.map((r, idx) => {
              const v = r.filters[key];
              const prev = idx === 0 ? undefined : rounds[idx - 1].filters[key];
              const changed = idx > 0 && !filterValueEqual(v, prev);
              return (
                <td
                  key={r.n}
                  className={[
                    'text-right font-mono tabular-nums text-[0.78rem] px-2 py-1.5',
                    changed
                      ? 'text-[var(--color-copper)]'
                      : 'text-[var(--color-ink)]',
                  ].join(' ')}
                >
                  {fmt(v)}
                </td>
              );
            })}
            <td className="text-right font-mono tabular-nums text-[0.78rem] pl-3 py-1.5 text-[var(--color-copper)] font-semibold">
              {fmt(rounds[rounds.length - 1].filters[key])}
            </td>
          </tr>
        ))}
        <tr className="border-t border-[var(--color-rule-soft)]">
          <th
            scope="row"
            className="text-left text-[0.78rem] text-[var(--color-ink-3)] pr-4 py-1.5 font-normal"
          >
            Cohort
          </th>
          {rounds.map((r) => (
            <td
              key={r.n}
              className="text-right font-mono tabular-nums text-[0.78rem] px-2 py-1.5 text-[var(--color-ink)]"
            >
              {r.cohort_size}
            </td>
          ))}
          <td className="text-right font-mono tabular-nums text-[0.78rem] pl-3 py-1.5 text-[var(--color-copper)] font-semibold">
            {rounds[rounds.length - 1].cohort_size}
          </td>
        </tr>
      </tbody>
    </table>
  );
}

/* -------------------------------------------------------------------------- */
/* Step → round grouping                                                      */
/* -------------------------------------------------------------------------- */

interface StepGroup {
  label: string;          // "Round 1", "Round 2"
  roundN: number;         // 1-indexed; 0 means "before any round" (only for runs that emit no FCR)
  steps: TraceStep[];
}

function groupStepsByRound(steps: TraceStep[]): StepGroup[] {
  const groups: StepGroup[] = [];
  let current: StepGroup | null = null;
  let roundCounter = 0;

  for (const step of steps) {
    if (
      step.kind === 'computation' &&
      (step as TraceStepComputation).label === SELECTION_SUMMARY_LABEL
    ) {
      continue;
    }

    const isFCR =
      step.kind === 'tool_call' &&
      (step as TraceStepToolCall).tool === FCR_TOOL_NAME;

    if (isFCR) {
      roundCounter += 1;
      const next: StepGroup = {
        label: `Round ${roundCounter}`,
        roundN: roundCounter,
        steps: [],
      };
      // Carry any setup steps (reasoning/etc that landed in the
      // not-yet-started current group) into round 1 so the first
      // round's "why" is co-located with its tool call.
      if (current && current.roundN === 0 && current.steps.length > 0) {
        next.steps.push(...current.steps);
        current = null;
      }
      groups.push(next);
      current = next;
    }

    if (current == null) {
      current = { label: 'Before round 1', roundN: 0, steps: [] };
      groups.push(current);
    }
    current.steps.push(step);
  }

  // If we accumulated only a "Before round 1" bucket (agent never
  // called find_comparables_relaxed) keep it but rename it for
  // clarity.
  if (groups.length === 1 && groups[0].roundN === 0) {
    groups[0] = { ...groups[0], label: 'Agent steps' };
  }

  return groups;
}

/* -------------------------------------------------------------------------- */
/* Round block                                                                */
/* -------------------------------------------------------------------------- */

function RoundBlock({
  group,
  round,
  isLast,
}: {
  group: StepGroup;
  round: SelectionRound | null;
  isLast: boolean;
}) {
  const [open, setOpen] = useState(isLast || group.roundN === 0);

  return (
    <details
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
      className="group rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]"
    >
      <summary className="cursor-pointer list-none px-4 py-3 flex items-baseline justify-between gap-4 flex-wrap">
        <div className="flex items-baseline gap-3 min-w-0">
          <span className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
            {group.label}
          </span>
          {round && (
            <span className="text-[0.78rem] font-mono tabular-nums text-[var(--color-ink-2)] truncate">
              radius {fmtCount(round.filters.radius_m ?? 0)} m
              {' · '}±{Math.round((round.filters.area_band_pct ?? 0) * 100)}%
              {' · '}cohort {round.cohort_size}
            </span>
          )}
        </div>
        {round && <CohortDiffChip round={round} />}
      </summary>
      <div className="px-4 pb-5 pt-1 space-y-4">
        {round && <RoundDetails round={round} />}
        <StepList steps={group.steps} />
      </div>
    </details>
  );
}

function CohortDiffChip({ round }: { round: SelectionRound }) {
  const added = round.added_ids.length;
  const removed = round.removed_ids.length;
  if (round.n === 1) {
    return (
      <span className="text-[0.7rem] font-mono tabular-nums text-[var(--color-ink-3)]">
        first cohort
      </span>
    );
  }
  return (
    <span className="text-[0.7rem] font-mono tabular-nums flex items-baseline gap-3">
      {added > 0 && (
        <span className="text-[var(--color-sage)]">+{added}</span>
      )}
      {removed > 0 && (
        <span className="text-[var(--color-brick)]">−{removed}</span>
      )}
      {added === 0 && removed === 0 && (
        <span className="text-[var(--color-ink-3)]">no change</span>
      )}
    </span>
  );
}

function RoundDetails({ round }: { round: SelectionRound }) {
  return (
    <div className="space-y-3">
      {round.reasoning && (
        <div>
          <p className="text-[0.6rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
            Reasoning
          </p>
          <p className="mt-1 text-[0.85rem] text-[var(--color-ink-2)] italic leading-relaxed">
            {round.reasoning}
          </p>
        </div>
      )}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {round.added_ids.length > 0 && (
          <IdsBlock label="Added this round" ids={round.added_ids} tone="sage" />
        )}
        {round.removed_ids.length > 0 && (
          <IdsBlock label="Dropped this round" ids={round.removed_ids} tone="brick" />
        )}
      </div>
    </div>
  );
}

function IdsBlock({
  label,
  ids,
  tone,
}: {
  label: string;
  ids: number[];
  tone: 'sage' | 'brick';
}) {
  const max = 12;
  const shown = ids.slice(0, max);
  const remaining = ids.length - shown.length;
  const toneClass =
    tone === 'sage'
      ? 'text-[var(--color-sage)] border-[var(--color-sage)]/30 bg-[var(--color-sage-soft)]'
      : 'text-[var(--color-brick)] border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)]';
  return (
    <div>
      <p className="text-[0.6rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
        {label}
      </p>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        {shown.map((id) => (
          <span
            key={id}
            className={[
              'inline-block px-1.5 py-0.5 text-[0.7rem] font-mono tabular-nums rounded-[var(--radius-xs)] border',
              toneClass,
            ].join(' ')}
          >
            {id}
          </span>
        ))}
        {remaining > 0 && (
          <span className="text-[0.7rem] font-mono tabular-nums text-[var(--color-ink-3)] self-center">
            +{remaining} more
          </span>
        )}
      </div>
    </div>
  );
}

function StepList({ steps }: { steps: TraceStep[] }) {
  if (steps.length === 0) return null;
  const expandDefaults = computeDefaultExpansion(steps);
  return (
    <ol className="relative pl-7 mt-1">
      <span
        aria-hidden
        className="absolute left-[10px] top-1 bottom-1 w-px bg-[var(--color-rule)]"
      />
      {steps.map((step) => (
        <StepRow
          key={step.n}
          step={step}
          defaultExpanded={expandDefaults.has(step.n)}
        />
      ))}
    </ol>
  );
}

/* -------------------------------------------------------------------------- */
/* Flat layout (v1 + v2 deterministic)                                        */
/* -------------------------------------------------------------------------- */

function FlatLayout({ trace }: { trace: Trace }) {
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
  return 'reasoning';
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
  const runId = useContext(RunIdContext);
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
      {runId != null && <FullPayloadDrillDown runId={runId} stepN={s.n} />}
    </div>
  );
}

function FullPayloadDrillDown({ runId, stepN }: { runId: number; stepN: number }) {
  const [open, setOpen] = useState(false);
  const query = useTracePayload(runId, stepN, open);

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="inline-flex items-baseline gap-1.5 text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
      >
        <Chevron open={open} />
        {open ? 'Hide full payload' : 'Show full payload'}
      </button>
      {open && (
        <div className="mt-2">
          {query.isLoading && (
            <p className="text-[0.78rem] text-[var(--color-ink-4)] italic">
              loading payload…
            </p>
          )}
          {query.isError && (
            <p className="text-[0.78rem] text-[var(--color-brick)] italic">
              {query.error?.status === 404
                ? 'No payload captured for this step.'
                : `Could not load payload: ${query.error?.message ?? 'unknown error'}`}
            </p>
          )}
          {query.data && (
            <pre className="font-mono text-[0.72rem] text-[var(--color-ink-2)] whitespace-pre-wrap bg-[var(--color-inset)] px-2 py-1.5 rounded-[var(--radius-xs)] border border-[var(--color-rule)] overflow-x-auto max-h-[28rem] overflow-y-auto">
              {safeStringify(query.data.full_output)}
            </pre>
          )}
        </div>
      )}
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
  const text =
    typeof (s.output_summary as { text?: unknown })?.text === 'string'
      ? ((s.output_summary as { text: string }).text)
      : '';
  return (
    <DetailGroup label="Reasoning">
      {text ? (
        <p className="text-[0.85rem] text-[var(--color-ink-2)] italic leading-relaxed whitespace-pre-wrap">
          {text}
        </p>
      ) : (
        <p className="text-[0.78rem] text-[var(--color-ink-3)] italic">
          No reasoning text recorded.
        </p>
      )}
      {Object.keys(s.output_summary ?? {}).filter((k) => k !== 'text').length > 0 && (
        <div className="mt-2">
          <SummaryGrid
            summary={dropKey(s.output_summary, 'text')}
          />
        </div>
      )}
    </DetailGroup>
  );
}

function dropKey(
  obj: Record<string, unknown> | undefined,
  key: string,
): Record<string, unknown> {
  if (!obj) return {};
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(obj)) {
    if (k !== key) out[k] = v;
  }
  return out;
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
