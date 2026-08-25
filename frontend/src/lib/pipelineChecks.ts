import type { PipelineCheckRow } from './queries';
import { fmtCount } from './format';

/* Presentation + rollup helpers for the pipeline verification checks
 * (migration 274, pipeline_checks_public). One latest row per check_key; the
 * DB already emits an ok/warn/fail status, so these helpers only humanize the
 * key/value and roll the statuses up for the Health panel's header badge. */

export type PipelineCheckStatus = 'ok' | 'warn' | 'fail';

/* The keys scripts/verify_pipeline.py still emits. Historical rows from retired
 * checks (the dedup decision-engine ones) fall through to the humanizer below. */
const CHECK_LABELS: Record<string, string> = {
  llm_errors: 'LLM errors',
  llm_liveness: 'LLM liveness',
  llm_burn_rate: 'LLM burn rate',
  db_saturation: 'DB saturation',
  worker_liveness: 'Worker liveness',
  dual_write_parity: 'Dual-write parity',
  property_maintenance: 'Property maintenance',
  broker_resolution_freshness: 'Broker resolution',
  ppm2_median_shift: 'Kč/m² median shift',
  ppm2_basis_floor_share: 'Kč/m² price floor',
  area_vs_usable_divergence: 'Area vs usable area',
};

/* The three per-m² plausibility checks emit a share or a ratio, and the number is
 * meaningless without its unit — "20.03" reads as a count. Every older check's value
 * is a bare count/rate whose meaning lives in its own message, so they stay bare. */
const CHECK_VALUE_UNIT: Record<string, string> = {
  ppm2_median_shift: '\u00d7',
  ppm2_basis_floor_share: '%',
  area_vs_usable_divergence: '%',
};

export function pipelineCheckLabel(key: string): string {
  return (
    CHECK_LABELS[key] ??
    key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
  );
}

/* Every surviving check's `value` is a ratio / percentage / count / minutes whose
 * unit lives in the check itself, so we show the bare number (integers compact,
 * decimals to 2 dp). */
export function pipelineCheckValueLabel(value: number | null, checkKey?: string): string {
  if (value == null) return '—';
  const shown = Number.isInteger(value) ? fmtCount(value) : value.toFixed(2);
  return shown + (checkKey ? (CHECK_VALUE_UNIT[checkKey] ?? '') : '');
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
