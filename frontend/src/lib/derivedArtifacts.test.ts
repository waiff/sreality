import { describe, expect, it } from 'vitest';
import {
  derivedArtifactFreshness,
  derivedArtifactLabel,
  derivedArtifactStatus,
  derivedArtifactValueLabel,
  parseIntervalMs,
  sortDerivedArtifacts,
  summarizeDerivedArtifacts,
} from './derivedArtifacts';
import type { DerivedArtifactRow } from './types';

const NOW = new Date('2026-08-25T12:00:00Z');

const minutesBefore = (n: number): string =>
  new Date(NOW.getTime() - n * 60_000).toISOString();

const artifact = (over: Partial<DerivedArtifactRow> = {}): DerivedArtifactRow => ({
  name: 'llm_cost_hour_rollup',
  producer: 'refresh_llm_cost_rollups',
  host: 'pg_cron',
  cadence: '4,19,34,49 * * * *',
  staleness_budget: '01:00:00',
  complete_through: minutesBefore(6),
  last_succeeded_at: minutesBefore(6),
  last_duration_ms: 240,
  last_rows: 12,
  is_serving: true,
  ...over,
});

describe('parseIntervalMs', () => {
  /* The three budgets the live view actually returns today, verbatim. The
   * server runs IntervalStyle = postgres, so every sub-day budget is HH:MM:SS. */
  it('reads the live budgets exactly as production sends them', () => {
    expect(parseIntervalMs('00:45:00')).toBe(2_700_000); // browse_list
    expect(parseIntervalMs('01:00:00')).toBe(3_600_000); // llm_cost_hour_rollup
    expect(parseIntervalMs('01:30:00')).toBe(5_400_000); // properties_map_mv
  });

  /* Legal values a future registry row can carry that do NOT match HH:MM:SS —
   * Postgres switches to a day/month prefix past 24 h. Getting these wrong is
   * silent: a budget read as 0 shows every row as permanently stale, and one
   * read as Infinity shows a dead artifact as healthy. */
  it('reads the day- and month-scale forms Postgres switches to past 24 h', () => {
    expect(parseIntervalMs('1 day 00:00:00')).toBe(86_400_000);
    expect(parseIntervalMs('2 days 03:00:00')).toBe(183_600_000);
    expect(parseIntervalMs('1 mon')).toBe(30 * 86_400_000);
    expect(parseIntervalMs('1 mon 5 days')).toBe(35 * 86_400_000);
    expect(parseIntervalMs('100:00:00')).toBe(360_000_000); // hours past two digits
  });

  it('also reads the other IntervalStyles, in case the server setting changes', () => {
    expect(parseIntervalMs('45 mins')).toBe(2_700_000); // postgres_verbose
    expect(parseIntervalMs('PT1H')).toBe(3_600_000); // iso_8601
    expect(parseIntervalMs('P1DT2H')).toBe(93_600_000);
  });

  it('returns null rather than guessing at anything it cannot read', () => {
    expect(parseIntervalMs(null)).toBeNull();
    expect(parseIntervalMs(undefined)).toBeNull();
    expect(parseIntervalMs('')).toBeNull();
    expect(parseIntervalMs('soon')).toBeNull();
    expect(parseIntervalMs('every other Tuesday')).toBeNull();
    expect(parseIntervalMs(3_600_000)).toBeNull(); // a number is not an interval
  });
});

describe('derivedArtifactFreshness', () => {
  it('treats age exactly equal to the budget as fresh, and one ms past it as stale', () => {
    const budgetMs = 3_600_000;
    const exactly = artifact({
      last_succeeded_at: new Date(NOW.getTime() - budgetMs).toISOString(),
    });
    const past = artifact({
      last_succeeded_at: new Date(NOW.getTime() - budgetMs - 1).toISOString(),
    });
    expect(derivedArtifactFreshness(exactly, NOW)).toEqual({
      status: 'ok',
      budgetMs,
      ageMs: budgetMs,
    });
    expect(derivedArtifactStatus(past, NOW)).toBe('stale');
  });

  it('reports a registered-but-never-produced artifact as never, not stale', () => {
    const f = derivedArtifactFreshness(artifact({ last_succeeded_at: null }), NOW);
    expect(f.status).toBe('never');
    expect(f.ageMs).toBeNull();
    expect(f.budgetMs).toBe(3_600_000);
  });

  it('never alarms on is_serving = false — nothing is producing it on purpose', () => {
    // Long past its budget AND never succeeded: paused still wins, because an
    // overdue timestamp on a switched-off artifact is the expected state.
    expect(derivedArtifactStatus(artifact({ is_serving: false }), NOW)).toBe('paused');
    expect(
      derivedArtifactStatus(
        artifact({ is_serving: false, last_succeeded_at: minutesBefore(6000) }),
        NOW,
      ),
    ).toBe('paused');
    expect(
      derivedArtifactStatus(artifact({ is_serving: false, last_succeeded_at: null }), NOW),
    ).toBe('paused');
  });

  it('says unknown when the budget did not parse — never a default of 0 or Infinity', () => {
    // A budget defaulted to 0 would mark every row permanently stale; one
    // defaulted to Infinity would mark a dead artifact healthy. Both are
    // silent. 'unknown' is visible and true.
    expect(derivedArtifactStatus(artifact({ staleness_budget: 'soon' }), NOW)).toBe('unknown');
    expect(derivedArtifactStatus(artifact({ staleness_budget: null }), NOW)).toBe('unknown');
    expect(
      derivedArtifactStatus(
        artifact({ staleness_budget: 'soon', last_succeeded_at: minutesBefore(20_000) }),
        NOW,
      ),
    ).toBe('unknown');
  });
});

describe('summarizeDerivedArtifacts / sortDerivedArtifacts', () => {
  const rows = [
    artifact({ name: 'zzz_fresh_mv' }),
    artifact({ name: 'browse_list', staleness_budget: '45 mins', last_succeeded_at: minutesBefore(70) }),
    artifact({ name: 'aaa_never_mv', last_succeeded_at: null }),
    artifact({ name: 'paused_mv', is_serving: false }),
  ];

  it('counts every status and surfaces stale as the worst', () => {
    expect(summarizeDerivedArtifacts(rows, NOW)).toEqual({
      ok: 1, stale: 1, never: 1, paused: 1, unknown: 0, worst: 'stale',
    });
    expect(summarizeDerivedArtifacts([], NOW).worst).toBe('ok');
    // `worst` ranks; it does not alarm. A paused-only registry ranks 'paused',
    // and the panel's badge (which reads stale/never/unknown) stays quiet.
    expect(summarizeDerivedArtifacts([artifact({ is_serving: false })], NOW).worst).toBe('paused');
  });

  it('orders stale first and never second, alphabetically inside a band', () => {
    expect(sortDerivedArtifacts(rows, NOW).map((r) => r.name)).toEqual([
      'browse_list',
      'aaa_never_mv',
      'paused_mv',
      'zzz_fresh_mv',
    ]);
  });
});

describe('labels', () => {
  it('names the artifacts it knows and softens the rest', () => {
    expect(derivedArtifactLabel('llm_cost_hour_rollup')).toBe('LLM cost rollup');
    expect(derivedArtifactLabel('snapshot_churn_24h_mv')).toBe('Snapshot churn 24h mv');
  });

  it('shows a dash rather than a zero for an unknown row count', () => {
    expect(derivedArtifactValueLabel(artifact({ last_rows: null }))).toBe('—');
    expect(derivedArtifactValueLabel(artifact({ last_rows: 0 }))).toBe('0');
  });
});
