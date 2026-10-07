/* lib/mergedAdverts — the words for a merge's origin, why an advert cannot move
 * and where a split left a unit, the property page's letters as ONE split
 * statement, the one receipt after a merge, and the read-your-writes refresh
 * after a split or a merge. */

import { describe, expect, it, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';

import type { MergeResult, SplitUnit } from './api';
import {
  inzeratu,
  mergeOriginLabel,
  mergeReceiptText,
  pushMergeReceipt,
  refreshAfterSplit,
  splitPlan,
  stateStays,
  unitLanding,
  unmovedReason,
} from './mergedAdverts';
import * as toast from './toast';

vi.mock('./toast', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./toast')>();
  let id = 0;
  return { ...actual, pushToast: vi.fn(() => ++id), dismissToast: vi.fn() };
});

describe('inzeratu', () => {
  it('declines the Czech noun by count', () => {
    expect(inzeratu(1)).toBe('inzerát');
    expect(inzeratu(2)).toBe('inzeráty');
    expect(inzeratu(4)).toBe('inzeráty');
    expect(inzeratu(5)).toBe('inzerátů');
    expect(inzeratu(0)).toBe('inzerátů');
  });
});

const merged = (over: Partial<MergeResult> = {}): MergeResult => ({
  merge_group_id: 'g',
  survivor_id: 310481,
  retired_ids: [876074],
  listings_moved: 2,
  pairs_ruled_same: 1,
  rulings_taken_back: 0,
  carried: { notes: 0, pipeline: null, collections: [], tags: [] },
  hidden_for_you: false,
  ...over,
});
const carrying = (
  notes: number,
  collections: string[],
  tags: string[],
  pipeline: string | null = null,
) =>
  mergeReceiptText(merged({ carried: { notes, pipeline, collections, tags } }));

describe('mergeReceiptText', () => {
  it('names the survivor and what moved: notes counted, the rest by name, declined', () => {
    expect(mergeReceiptText(merged())).toBe('Sloučeno do nemovitosti #310481.');
    expect(carrying(2, ['Brno 2+kk'], ['výhled'], 'Prohlídka')).toBe(
      'Sloučeno do nemovitosti #310481. Přesunuto: 2 poznámky, zařazení v pipeline (Prohlídka), ' +
        'kolekce „Brno 2+kk“, štítek „výhled“.',
    );
    expect(carrying(1, ['A', 'B'], ['x', 'y'])).toBe(
      'Sloučeno do nemovitosti #310481. Přesunuto: 1 poznámka, kolekce „A“, „B“, štítky „x“, „y“.',
    );
    expect(carrying(5, [], [])).toBe('Sloučeno do nemovitosti #310481. Přesunuto: 5 poznámek.');
  });

  it('counts the "Různé" rulings taken back and says when the property is hidden', () => {
    expect(mergeReceiptText(merged({ rulings_taken_back: 3, hidden_for_you: true }))).toBe(
      'Sloučeno do nemovitosti #310481. Zrušená rozhodnutí „Různé“: 3. Pro vás je skrytá.',
    );
  });

  it('says a receipt it could not read, never that nothing moved', () => {
    expect(mergeReceiptText(merged({ carried: null, hidden_for_you: null }))).toBe(
      'Sloučeno do nemovitosti #310481. Co se přesunulo, se nepodařilo načíst.',
    );
  });
});

describe('pushMergeReceipt', () => {
  it('replaces the previous receipt, and its action opens the survivor', () => {
    const open = vi.fn();
    pushMergeReceipt(merged({ survivor_id: 7 }), open);
    pushMergeReceipt(merged({ survivor_id: 8 }), open);
    const [first, second] = vi.mocked(toast.pushToast).mock.results.map((r) => r.value);
    expect(toast.dismissToast).toHaveBeenCalledWith(first);
    const [kind, message, ttl, action] = vi.mocked(toast.pushToast).mock.calls[1];
    expect([kind, message, ttl, action?.label]).toEqual([
      'ok',
      'Sloučeno do nemovitosti #8.',
      0,
      'Otevřít #8',
    ]);
    action?.onClick();
    expect(open).toHaveBeenCalledWith(8);
    expect(toast.dismissToast).toHaveBeenLastCalledWith(second);
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
    expect(unmovedReason('shared_origin')).toBe(
      'přišel ze stejné nemovitosti jako inzerát s jiným písmenem a vrátily by se do ní spolu',
    );
    expect(unmovedReason('moved_on')).toBe('moved_on');
  });
});

describe('stateStays', () => {
  it('names the property the operator’s state stays with', () => {
    expect(stateStays(42)).toBe(
      'Poznámky, štítky, kolekce a zařazení v pipeline zůstanou u nemovitosti #42.',
    );
  });
});

describe('splitPlan', () => {
  const ADS = [101, 202, 303, 404];
  const own = (...ids: number[]) => new Set(ids);

  it('every advert under A: nothing leaves', () => {
    const plan = splitPlan(ADS, {}, own(101), 101);
    expect(plan.kept).toEqual({ letter: 'A', listingIds: ADS });
    expect(plan.leaving).toEqual([]);
    expect(plan.statement).toEqual({ adverts: ADS, separate: [], keep_together: false });
  });

  it('two flats: the twins leave as ONE unit, the own advert’s group stays', () => {
    const plan = splitPlan(ADS, { 303: 'B', 404: 'B' }, own(101), 101);
    expect(plan.statement).toEqual({ adverts: ADS, separate: [[303, 404]], keep_together: false });
    expect(plan.kept).toEqual({ letter: 'A', listingIds: [101, 202] });
    expect(plan.leaving).toEqual([{ letter: 'B', listingIds: [303, 404] }]);
  });

  it('one unit per leaving letter, in letter order, ids ascending; the adverts as shown', () => {
    const shown = [101, 404, 202, 303];
    const plan = splitPlan(shown, { 404: 'C', 202: 'B', 303: 'C' }, own(101), 101);
    expect(plan.statement).toEqual({
      adverts: [101, 404, 202, 303],
      separate: [[202], [303, 404]],
      keep_together: false,
    });
    expect(plan.leaving.map((g) => g.letter)).toEqual(['B', 'C']);
  });

  it('the letters do not decide who stays: the own advert’s group does, whatever its letter', () => {
    const plan = splitPlan(ADS, { 101: 'B' }, own(101), 101);
    expect(plan.kept).toEqual({ letter: 'B', listingIds: [101] });
    expect(plan.statement.separate).toEqual([[202, 303, 404]]);
  });

  it('the group with more own adverts stays, even under a later letter', () => {
    const plan = splitPlan(ADS, { 303: 'B', 404: 'B' }, own(101, 303, 404), 101);
    expect(plan.kept.letter).toBe('B');
    expect(plan.statement.separate).toEqual([[101, 202]]);
  });

  it('a tie in own adverts keeps the earliest of the tied letters', () => {
    // A holds none; B and C one each.
    const plan = splitPlan(ADS, { 101: 'B', 303: 'C' }, own(101, 303), 101);
    expect(plan.kept).toEqual({ letter: 'B', listingIds: [101] });
    expect(plan.statement.separate).toEqual([[202, 404], [303]]);
  });

  it('no own advert: the canonical advert’s group stays; an unshown canonical, the earliest letter', () => {
    expect(splitPlan(ADS, { 101: 'B' }, own(), 101).kept.letter).toBe('B');
    expect(splitPlan(ADS, { 101: 'B' }, own(), 999).kept.letter).toBe('A');
    // An own advert the page does not show counts for nothing.
    expect(splitPlan(ADS, { 202: 'B' }, own(999), 202).kept.letter).toBe('B');
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
  it('re-reads the property page, the proposals, the category review and every Browse surface', () => {
    const qc = new QueryClient();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    refreshAfterSplit(qc);

    for (const key of [
      ['property'],
      ['property-sources'],
      ['snapshots'],
      ['curation'],
      ['merged-adverts'],
      ['autodedup', 'proposed-splits'],
      ['autodedup', 'category-splits'],
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
