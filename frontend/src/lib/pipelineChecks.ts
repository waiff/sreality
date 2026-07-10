import type { PipelineCheckRow } from './queries';
import { fmtCount } from './format';

/* Presentation + rollup helpers for the dedup pipeline verification checks
 * (migration 274, pipeline_checks_public). One latest row per check_key; the
 * DB already emits an ok/warn/fail status, so these helpers only humanize the
 * key/value and roll the statuses up for the Health panel's header badge. */

export type PipelineCheckStatus = 'ok' | 'warn' | 'fail';

const CHECK_LABELS: Record<string, string> = {
  street_debt: 'Street debt',
  geo_debt: 'Geo debt',
  eligibility_funnel: 'Eligibility funnel',
  merge_latency: 'Merge latency',
  engine_health: 'Engine health',
  llm_errors: 'LLM errors',
  merge_precision_sample: 'Merge precision (sample)',
};

export function pipelineCheckLabel(key: string): string {
  return (
    CHECK_LABELS[key] ??
    key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
  );
}

/* street_debt / geo_debt count SUSPECT PAIRS; every other check's `value` is a
 * ratio / percentage / minutes whose unit lives in the check itself, so we show
 * the bare number (integers compact, decimals to 2 dp). */
const PAIR_COUNT_CHECKS = new Set(['street_debt', 'geo_debt']);

export function pipelineCheckValueLabel(key: string, value: number | null): string {
  if (value == null) return '—';
  if (PAIR_COUNT_CHECKS.has(key)) return `${fmtCount(value)} pairs`;
  return Number.isInteger(value) ? fmtCount(value) : value.toFixed(2);
}

/* Anything the DB didn't stamp warn/fail is treated as ok, so an unknown future
 * status never renders as a false alarm. */
export function normalizePipelineStatus(status: string): PipelineCheckStatus {
  return status === 'fail' || status === 'warn' ? status : 'ok';
}

export interface PipelineChecksSummary {
  ok: number;
  warn: number;
  fail: number;
  worst: PipelineCheckStatus;
}

export function summarizePipelineChecks(rows: PipelineCheckRow[]): PipelineChecksSummary {
  const acc: Record<PipelineCheckStatus, number> = { ok: 0, warn: 0, fail: 0 };
  for (const r of rows) acc[normalizePipelineStatus(r.status)] += 1;
  const worst: PipelineCheckStatus = acc.fail > 0 ? 'fail' : acc.warn > 0 ? 'warn' : 'ok';
  return { ok: acc.ok, warn: acc.warn, fail: acc.fail, worst };
}

// fail first, then warn, then ok; stable by check_key inside a status band.
const STATUS_ORDER: Record<PipelineCheckStatus, number> = { fail: 0, warn: 1, ok: 2 };

export function sortPipelineChecks(rows: PipelineCheckRow[]): PipelineCheckRow[] {
  return [...rows].sort(
    (a, b) =>
      STATUS_ORDER[normalizePipelineStatus(a.status)] -
        STATUS_ORDER[normalizePipelineStatus(b.status)] ||
      a.check_key.localeCompare(b.check_key),
  );
}
