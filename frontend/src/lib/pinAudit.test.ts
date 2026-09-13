/* The two reads that carry W7-b's state (migration 518).
 *
 * The nav badge is an ALARM: it must count the listings the system finished and
 * could not place, and never the ones it simply has not reached yet — otherwise
 * it climbs every time the scrapers do their job and the operator learns to
 * ignore it. And every list read is scoped to exactly one state, because a total
 * over both answers no question anyone asked.
 *
 * The stub records the PostgREST chain instead of hitting a server: every method
 * returns the same recorder and the recorder is thenable, so the builder's real
 * shape (which method is called with what) is what the assertions read.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

type Call = [string, unknown[]];

const stub = vi.hoisted(() => {
  const calls: Call[] = [];
  const target = {
    then: (resolve: (v: unknown) => void) =>
      resolve({ data: [], error: null, count: 7 }),
  };
  const proxy: Record<string, unknown> = new Proxy(target, {
    get(t, prop) {
      if (prop === 'then') return t.then;
      return (...args: unknown[]) => {
        calls.push([String(prop), args]);
        return proxy;
      };
    },
  }) as unknown as Record<string, unknown>;
  return { calls, proxy };
});

vi.mock('@/lib/supabase', () => ({
  supabase: {
    from: (relation: string) => {
      stub.calls.push(['from', [relation]]);
      return stub.proxy;
    },
  },
  isSupabaseConfigured: () => true,
}));

import {
  EMPTY_PIN_AUDIT_FILTERS,
  fetchPinAuditPage,
  fetchPinAuditTotal,
  summaryRowMatches,
  type PinAuditSummaryRow,
} from '@/lib/pinAudit';

const eqCalls = (): Call[] => stub.calls.filter(([m]) => m === 'eq');
const inCalls = (): Call[] => stub.calls.filter(([m]) => m === 'in');

describe('pinAudit reads', () => {
  beforeEach(() => {
    stub.calls.length = 0;
  });

  it('counts only the unresolved rows for the nav badge', async () => {
    const n = await fetchPinAuditTotal();
    expect(n).toBe(7);
    expect(eqCalls()).toContainEqual(['eq', ['state', 'unresolved']]);
  });

  it('defaults the page to the issue, not to the waiting room', () => {
    expect(EMPTY_PIN_AUDIT_FILTERS.state).toBe('unresolved');
  });

  it('scopes every list read to one state', async () => {
    await fetchPinAuditPage(
      { ...EMPTY_PIN_AUDIT_FILTERS, state: 'pending' },
      { field: 'last_seen_at', direction: 'desc' },
      null,
    );
    expect(eqCalls()).toContainEqual(['eq', ['state', 'pending']]);
  });

  it('never carries the quality buckets into pending — there is no verdict yet', async () => {
    await fetchPinAuditPage(
      {
        ...EMPTY_PIN_AUDIT_FILTERS,
        state: 'pending',
        qualities: ['active_no_claims'],
      },
      { field: 'last_seen_at', direction: 'desc' },
      null,
    );
    expect(inCalls().some(([, args]) => args[0] === 'quality')).toBe(false);
  });

  it('does send them under unresolved', async () => {
    await fetchPinAuditPage(
      { ...EMPTY_PIN_AUDIT_FILTERS, qualities: ['active_no_claims'] },
      { field: 'last_seen_at', direction: 'desc' },
      null,
    );
    expect(inCalls()).toContainEqual(['in', ['quality', ['active_no_claims']]]);
  });

  it('matches the summary payload the same way the SQL does', () => {
    const row: PinAuditSummaryRow = {
      state: 'pending',
      source: 'sreality',
      category_main: 'byt',
      quality: 'active_no_claims',
      sibling_has_pin: false,
      n: 3,
      refreshed_at: null,
    };
    expect(summaryRowMatches(row, EMPTY_PIN_AUDIT_FILTERS)).toBe(false);
    expect(
      summaryRowMatches(row, { ...EMPTY_PIN_AUDIT_FILTERS, state: 'pending' }),
    ).toBe(true);
    /* A quality chip left over from the other state cannot hide a pending row. */
    expect(
      summaryRowMatches(row, {
        ...EMPTY_PIN_AUDIT_FILTERS,
        state: 'pending',
        qualities: ['delisted_unresolved'],
      }),
    ).toBe(true);
  });
});
