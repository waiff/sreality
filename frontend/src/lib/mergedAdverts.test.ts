/* lib/mergedAdverts — the ledger scan, and the one rule that decides what a
 * row's 'Rozdělit' may honestly offer. */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';

import {
  MERGE_LEDGER_MAX_PAGES,
  MERGE_LEDGER_PAGE_SIZE,
  findActivePropertyMergeGroups,
  inzeratu,
  mergeOriginLabel,
  planRowUnmerge,
  refreshAfterUnmerge,
  type MergeGroupScan,
} from './mergedAdverts';
import * as queries from './queries';
import type { MergeGroup, MergesResponse } from './types';

vi.mock('./queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./queries')>();
  return {
    ...actual,
    fetchPropertySources: vi.fn(async () => ({ property_id: 9, sources: [] })),
  };
});

function group(over: Partial<MergeGroup> = {}): MergeGroup {
  return {
    merge_group_id: 'g-1',
    merged_at: '2026-09-20T10:00:00Z',
    survivor_property_id: 42,
    retired_count: 1,
    listings_moved: 1,
    source: 'operator',
    reason: 'manual_link',
    fully_undone: false,
    ...over,
  };
}

function scan(groups: MergeGroup[], over: Partial<MergeGroupScan> = {}): MergeGroupScan {
  return { groups, scanned: 200, exhaustive: false, ...over };
}

describe('inzeratu', () => {
  it('declines the Czech noun by count', () => {
    expect(inzeratu(1)).toBe('inzerát');
    expect(inzeratu(2)).toBe('inzeráty');
    expect(inzeratu(4)).toBe('inzeráty');
    expect(inzeratu(5)).toBe('inzerátů');
    expect(inzeratu(0)).toBe('inzerátů');
  });
});

describe('planRowUnmerge', () => {
  it('a two-advert property one single-advert merge made is the exact case', () => {
    const g = group();
    expect(planRowUnmerge(scan([g]), 2)).toEqual({ kind: 'pair', group: g });
  });

  it('one merge that made a bigger property is never undone whole from a row', () => {
    const g = group({ listings_moved: 2, retired_count: 2 });
    expect(planRowUnmerge(scan([g]), 3)).toEqual({ kind: 'ambiguous', groups: [g] });
  });

  it('several merges cannot be told apart from a row', () => {
    const a = group({ merge_group_id: 'a' });
    const b = group({ merge_group_id: 'b' });
    expect(planRowUnmerge(scan([a, b]), 3)).toEqual({ kind: 'ambiguous', groups: [a, b] });
  });

  it('one merge that does not account for every advert is ambiguous, not a guess', () => {
    const g = group({ listings_moved: 1 });
    expect(planRowUnmerge(scan([g]), 3).kind).toBe('ambiguous');
  });

  it('offers a merge of any origin alike — the origin is information only', () => {
    for (const source of ['operator', 'auto', 'autodedup'] as const) {
      const g = group({ source });
      expect(planRowUnmerge(scan([g]), 2)).toEqual({ kind: 'pair', group: g });
    }
  });

  it('says whether "none found" is about the property or about the window', () => {
    expect(planRowUnmerge(scan([], { exhaustive: true, scanned: 37 }), 2)).toEqual({
      kind: 'not-found',
      scanned: 37,
      exhaustive: true,
    });
    expect(planRowUnmerge(scan([], { exhaustive: false, scanned: 1000 }), 2)).toEqual({
      kind: 'not-found',
      scanned: 1000,
      exhaustive: false,
    });
  });
});

describe('findActivePropertyMergeGroups', () => {
  const page = (rows: MergeGroup[]): MergesResponse => ({ data: rows, total: rows.length });
  const filler = (n: number, prefix: string): MergeGroup[] =>
    Array.from({ length: n }, (_, i) =>
      group({ merge_group_id: `${prefix}${i}`, survivor_property_id: 1_000 + i }),
    );

  it('reads this property’s groups server-filtered, exhaustively, keeping the active ones', async () => {
    // However old the merge, one filtered read finds it — no newest-N window.
    const mine = group({ merge_group_id: 'mine', merged_at: '2019-01-01T00:00:00Z' });
    const undone = group({ merge_group_id: 'undone', fully_undone: true });
    const list = vi.fn().mockResolvedValueOnce(page([undone, mine]));

    const res = await findActivePropertyMergeGroups(42, list);

    expect(list).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledWith({
      limit: MERGE_LEDGER_PAGE_SIZE,
      offset: 0,
      survivor_property_id: 42,
    });
    expect(res).toEqual({ groups: [mine], scanned: 2, exhaustive: true });
  });

  it('pages on while a page comes back full, and still keeps only this property', async () => {
    // An API that ignored the filter must not leak another property's groups.
    const mine = group({ merge_group_id: 'mine' });
    const list = vi
      .fn()
      .mockResolvedValueOnce(page(filler(MERGE_LEDGER_PAGE_SIZE, 'x')))
      .mockResolvedValueOnce(page([mine]));

    const res = await findActivePropertyMergeGroups(42, list);

    expect(list).toHaveBeenNthCalledWith(2, {
      limit: MERGE_LEDGER_PAGE_SIZE,
      offset: MERGE_LEDGER_PAGE_SIZE,
      survivor_property_id: 42,
    });
    expect(res).toEqual({ groups: [mine], scanned: MERGE_LEDGER_PAGE_SIZE + 1, exhaustive: true });
  });

  it('never reads past the page cap, and says it did not read to the end', async () => {
    const list = vi.fn().mockResolvedValue(page(filler(MERGE_LEDGER_PAGE_SIZE, 'x')));

    const res = await findActivePropertyMergeGroups(42, list);

    expect(list).toHaveBeenCalledTimes(MERGE_LEDGER_MAX_PAGES);
    expect(res).toEqual({
      groups: [],
      scanned: MERGE_LEDGER_MAX_PAGES * MERGE_LEDGER_PAGE_SIZE,
      exhaustive: false,
    });
  });

  it('treats an empty page as the end of the ledger', async () => {
    const list = vi.fn().mockResolvedValue(page([]));
    const res = await findActivePropertyMergeGroups(42, list);
    expect(res).toEqual({ groups: [], scanned: 0, exhaustive: true });
  });
});

describe('mergeOriginLabel', () => {
  it('names each ledger source explicitly — only the operator’s merges are "ruční"', () => {
    expect(mergeOriginLabel('operator')).toBe('ruční');
    expect(mergeOriginLabel('autodedup')).toBe('automatické (AUTODEDUP)');
    expect(mergeOriginLabel('auto')).toBe('automatické (původní engine)');
    expect(mergeOriginLabel('something_new')).toBe('„something_new“');
  });
});

describe('refreshAfterUnmerge', () => {
  beforeEach(() => vi.mocked(queries.fetchPropertySources).mockClear());

  it('re-resolves the page’s sources from the listing alone, then refreshes every surface', async () => {
    const qc = new QueryClient();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    await refreshAfterUnmerge(qc, 105053);

    // ONE argument: no remembered property_id — the advert on screen may be
    // the one that just moved back to its own property.
    expect(queries.fetchPropertySources).toHaveBeenCalledWith(105053);
    expect(qc.getQueryData(queries.propertySourcesKey(105053))).toEqual({
      property_id: 9,
      sources: [],
    });
    for (const key of [
      ['listing'],
      ['property-mf'],
      ['property-status-events'],
      ['snapshots'],
      ['merged-adverts'],
      ['cards'],
      ['map'],
      ['table'],
      ['stats'],
      ['browse-count'],
    ]) {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: key });
    }
  });
});
