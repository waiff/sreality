import { describe, expect, it } from 'vitest';
import {
  assessPublicationGate,
  PUBLICATION_OLDEST_DANGER_HOURS,
} from './publicationHealth';
import type { PublicationGateRow } from './queries';

const NOW = Date.parse('2026-07-05T13:34:00Z');
const hoursAgo = (h: number): string => new Date(NOW - h * 3_600_000).toISOString();

function row(
  unpublished: number,
  activeTotal: number,
  oldest: string | null = null,
): PublicationGateRow {
  return { unpublished, active_total: activeTotal, oldest_unpublished_at: oldest };
}

describe('assessPublicationGate', () => {
  it('ok when nothing is unpublished', () => {
    const h = assessPublicationGate(row(0, 446_000, null), NOW);
    expect(h.status).toBe('ok');
    expect(h.ratioPct).toBe(0);
    expect(h.oldestAgeHours).toBeNull();
  });

  it('ok when the backlog is a tiny, fresh share (the live steady state)', () => {
    // Real reading: 50 / 446_043 = 0.011 %, minutes old.
    const h = assessPublicationGate(row(50, 446_043, hoursAgo(0.2)), NOW);
    expect(h.status).toBe('ok');
    expect(h.ratioPct).toBeCloseTo(0.0112, 3);
  });

  it('warn once the ratio crosses 2 %', () => {
    const h = assessPublicationGate(row(3_000, 100_000, hoursAgo(1)), NOW); // 3 %
    expect(h.status).toBe('warn');
  });

  it('danger once the ratio crosses 5 %', () => {
    const h = assessPublicationGate(row(6_000, 100_000, hoursAgo(1)), NOW); // 6 %
    expect(h.status).toBe('danger');
  });

  it('danger on a numerically-small backlog that is older than the age backstop', () => {
    // Ratio well under 2 % but the oldest has waited past the 6h backstop -> stall.
    const h = assessPublicationGate(
      row(20, 446_000, hoursAgo(PUBLICATION_OLDEST_DANGER_HOURS + 1)),
      NOW,
    );
    expect(h.ratioPct).toBeLessThan(1);
    expect(h.oldestAgeHours).toBeGreaterThan(PUBLICATION_OLDEST_DANGER_HOURS);
    expect(h.status).toBe('danger');
  });

  it('stays ok for a small backlog still inside the age backstop', () => {
    const h = assessPublicationGate(row(20, 446_000, hoursAgo(1)), NOW);
    expect(h.status).toBe('ok');
  });

  it('ratioPct is null when active_total is 0 (avoids divide-by-zero)', () => {
    const h = assessPublicationGate(row(5, 0, hoursAgo(1)), NOW);
    expect(h.ratioPct).toBeNull();
    // No ratio + fresh age -> cannot judge severity, stays ok.
    expect(h.status).toBe('ok');
  });

  it('is null-safe on a malformed oldest timestamp', () => {
    const h = assessPublicationGate(row(10, 100, 'not-a-date'), NOW);
    expect(h.oldestAgeHours).toBeNull();
  });
});
