/* lib/mergedAdverts — the words for a merge's origin, why an advert cannot move
 * and where a split left a unit, and the read-your-writes refresh after a split. */

import { describe, expect, it, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';

import type { SplitUnit } from './api';
import { inzeratu, mergeOriginLabel, refreshAfterSplit, unitLanding, unmovedReason } from './mergedAdverts';

describe('inzeratu', () => {
  it('declines the Czech noun by count', () => {
    expect(inzeratu(1)).toBe('inzerát');
    expect(inzeratu(2)).toBe('inzeráty');
    expect(inzeratu(4)).toBe('inzeráty');
    expect(inzeratu(5)).toBe('inzerátů');
    expect(inzeratu(0)).toBe('inzerátů');
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

describe('unmovedReason', () => {
  it('says why an advert cannot move; an unknown outcome is shown raw', () => {
    expect(unmovedReason('moved_since')).toBe('inzerát se mezitím přesunul jinam');
    expect(unmovedReason('origin_moved_on')).toMatch(/byla mezitím sloučena jinam/);
    expect(unmovedReason('last_native')).toMatch(/poslední vlastní inzerát nemovitosti/);
    expect(unmovedReason('moved_on')).toBe('moved_on');
  });
});

describe('unitLanding', () => {
  const unit = (over: Partial<SplitUnit>): SplitUnit => ({
    unit: 'B',
    role: 'separated',
    listing_ids: [2],
    property_id: 20,
    moved: [],
    merge_group_id: null,
    ...over,
  });
  it('names where a split left the unit', () => {
    expect(unitLanding(unit({ property_id: 10 }), 10)).toBe('zůstává #10');
    expect(unitLanding(unit({ moved: [{ listing_id: 2, outcome: 'detached', from: 10, to: 20 }] }), 10)).toBe(
      'vráceno do #20',
    );
    expect(
      unitLanding(unit({ property_id: 90, moved: [{ listing_id: 2, outcome: 'split_native', from: 10, to: 90 }] }), 10),
    ).toBe('nová nemovitost #90');
    expect(unitLanding(unit({ merge_group_id: 'g' }), 10)).toBe('sloučeno do #20');
    expect(unitLanding(unit({}), 10)).toBe('už v #20');
  });
});

describe('refreshAfterSplit', () => {
  it('re-reads the property page, the proposals and every Browse surface', () => {
    const qc = new QueryClient();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    refreshAfterSplit(qc);

    for (const key of [
      ['property'],
      ['property-sources'],
      ['property-status-events'],
      ['snapshots'],
      ['merged-adverts'],
      ['autodedup', 'proposed-splits'],
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
