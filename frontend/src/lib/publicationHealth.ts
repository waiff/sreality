import type { PublicationGateRow } from './queries';

/* Health of the dedup-aware publication gate (migration 273), derived from the
 * publication_gate_health_public view. New properties are hidden from Browse
 * until the dedup engine evaluates them; the gate has NO auto-publish timeout,
 * so a rising unpublished backlog is the only signal that dedup has stalled.
 * ONE source of truth for the Health page's "Publikace" panel colour + the copy
 * that explains it, so the thresholds are testable and can't drift per render. */

export type PublicationGateStatus = 'ok' | 'warn' | 'danger';

/* Unpublished-vs-active ratio thresholds (percent) plus an absolute-age backstop:
 * even a tiny backlog that is HOURS old is a stall (the gate never times out). */
export const PUBLICATION_RATIO_WARN_PCT = 2;
export const PUBLICATION_RATIO_DANGER_PCT = 5;
export const PUBLICATION_OLDEST_DANGER_HOURS = 6;

export interface PublicationGateHealth {
  unpublished: number;
  activeTotal: number;
  oldestUnpublishedAt: string | null;
  /* unpublished / active_total as a percent; null when active_total is 0. */
  ratioPct: number | null;
  /* age of the oldest still-unpublished property, in hours; null when none. */
  oldestAgeHours: number | null;
  status: PublicationGateStatus;
}

export function assessPublicationGate(
  row: PublicationGateRow,
  now: number = Date.now(),
): PublicationGateHealth {
  const unpublished = row.unpublished ?? 0;
  const activeTotal = row.active_total ?? 0;
  const oldestUnpublishedAt = row.oldest_unpublished_at ?? null;

  const ratioPct = activeTotal > 0 ? (unpublished / activeTotal) * 100 : null;

  let oldestAgeHours: number | null = null;
  if (oldestUnpublishedAt != null) {
    const t = new Date(oldestUnpublishedAt).getTime();
    if (!Number.isNaN(t)) oldestAgeHours = Math.max(0, (now - t) / 3_600_000);
  }

  // Nothing waiting -> healthy. Otherwise the ratio drives it, with the age
  // backstop able to escalate a numerically-small-but-old backlog to danger.
  let status: PublicationGateStatus = 'ok';
  if (unpublished > 0) {
    const ratioDanger = ratioPct != null && ratioPct > PUBLICATION_RATIO_DANGER_PCT;
    const ageDanger =
      oldestAgeHours != null && oldestAgeHours > PUBLICATION_OLDEST_DANGER_HOURS;
    const ratioWarn = ratioPct != null && ratioPct > PUBLICATION_RATIO_WARN_PCT;
    if (ratioDanger || ageDanger) status = 'danger';
    else if (ratioWarn) status = 'warn';
  }

  return {
    unpublished,
    activeTotal,
    oldestUnpublishedAt,
    ratioPct,
    oldestAgeHours,
    status,
  };
}
