/* lib/mergedAdverts — the words for a merge's origin, the property page's
 * letters as the split's statement and as a link, the one receipt slot after a
 * merge or a split, and the read-your-writes refresh after either. */

import { describe, expect, it, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';

import type { MergeResult, SplitResult } from './api';
import {
  choicesParam,
  inzeratu,
  lettersParam,
  mergeOriginLabel,
  mergeReceiptText,
  parseLetters,
  pushMergeReceipt,
  pushSplitReceipt,
  refreshAfterSplit,
  splitPath,
  splitPlan,
  splitReceiptText,
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

describe('splitPlan', () => {
  const ADS = [101, 202, 303, 404];
  const own = (...ids: number[]) => new Set(ids);

  it('every advert under A: nothing leaves', () => {
    const plan = splitPlan(ADS, {}, own(101), 101);
    expect(plan.kept).toEqual({ letter: 'A', listingIds: ADS });
    expect(plan.leaving).toEqual([]);
    expect(plan.letters).toEqual({ 101: 'A', 202: 'A', 303: 'A', 404: 'A' });
  });

  it('two flats: the twins leave as ONE unit, the own advert’s group stays', () => {
    const plan = splitPlan(ADS, { 303: 'B', 404: 'B' }, own(101), 101);
    expect(plan.letters).toEqual({ 101: 'A', 202: 'A', 303: 'B', 404: 'B' });
    expect(plan.kept).toEqual({ letter: 'A', listingIds: [101, 202] });
    expect(plan.leaving).toEqual([{ letter: 'B', listingIds: [303, 404] }]);
  });

  it('one group per leaving letter, in letter order, ids ascending; every advert named', () => {
    const shown = [101, 404, 202, 303];
    const plan = splitPlan(shown, { 404: 'C', 202: 'B', 303: 'C' }, own(101), 101);
    expect(plan.letters).toEqual({ 101: 'A', 202: 'B', 303: 'C', 404: 'C' });
    expect(plan.leaving).toEqual([
      { letter: 'B', listingIds: [202] },
      { letter: 'C', listingIds: [303, 404] },
    ]);
  });

  it('the letters do not decide who stays: the own advert’s group does, whatever its letter', () => {
    const plan = splitPlan(ADS, { 101: 'B' }, own(101), 101);
    expect(plan.kept).toEqual({ letter: 'B', listingIds: [101] });
    expect(plan.leaving).toEqual([{ letter: 'A', listingIds: [202, 303, 404] }]);
  });

  it('the group with more own adverts stays, even under a later letter', () => {
    const plan = splitPlan(ADS, { 303: 'B', 404: 'B' }, own(101, 303, 404), 101);
    expect(plan.kept.letter).toBe('B');
    expect(plan.leaving).toEqual([{ letter: 'A', listingIds: [101, 202] }]);
  });

  it('a tie in own adverts keeps the earliest of the tied letters', () => {
    // A holds none; B and C one each.
    const plan = splitPlan(ADS, { 101: 'B', 303: 'C' }, own(101, 303), 101);
    expect(plan.kept).toEqual({ letter: 'B', listingIds: [101] });
    expect(plan.leaving.map((g) => g.letter)).toEqual(['A', 'C']);
  });

  it('no own advert: the canonical advert’s group stays; an unshown canonical, the earliest letter', () => {
    expect(splitPlan(ADS, { 101: 'B' }, own(), 101).kept.letter).toBe('B');
    expect(splitPlan(ADS, { 101: 'B' }, own(), 999).kept.letter).toBe('A');
    // An own advert the page does not show counts for nothing.
    expect(splitPlan(ADS, { 202: 'B' }, own(999), 202).kept.letter).toBe('B');
  });
});

describe('lettersParam / parseLetters / splitPath', () => {
  it('writes the letters ids ascending and reads back only `id:A–Z` parts', () => {
    expect(lettersParam({ 303: 'B', 101: 'A', 202: 'A' })).toBe('101:A,202:A,303:B');
    expect(parseLetters('101:A, 202:B,303:b,x:C,404,505:AB')).toEqual({ 101: 'A', 202: 'B' });
    expect(parseLetters(null)).toEqual({});
  });

  it('links the property page with the letters, the one place a review page sends a split', () => {
    expect(splitPath(42, { 202: 'B', 101: 'A' })).toBe('/property/42?letters=101%3AA%2C202%3AB');
  });
});

const split = (over: Partial<SplitResult> = {}): SplitResult => ({
  property_id: 42,
  call_id: 'c',
  letters: [
    { letter: 'A', listing_ids: [1], property_id: 42, lands: 'kept', joined: null },
    { letter: 'B', listing_ids: [2], property_id: 43, lands: 'origin', joined: null },
    { letter: 'C', listing_ids: [3, 4], property_id: 90, lands: 'new', joined: 'g' },
  ],
  curation: [
    { item: 'note:1', kind: 'note', label: 'Sousedi', letter: 'B', property_id: 43, copies: [] },
    {
      item: 'pipeline', kind: 'pipeline', label: 'Prohlídka', letter: 'B', property_id: 43,
      copies: [{ letter: 'C', property_id: 90 }],
    },
    { item: 'tag:7', kind: 'tag', label: 'výhled', letter: 'A', property_id: 42, copies: [] },
    { item: 'fold:9', kind: 'collection', label: 'Brno', letter: 'C', property_id: 90, copies: [], why: 'fold' },
  ],
  rulings: { different: 5, same: 1, taken_back: 1 },
  ...over,
});

describe('splitReceiptText / pushSplitReceipt', () => {
  it('says where each letter landed, what of mine went where and was copied, and the rulings', () => {
    expect(splitReceiptText(split())).toBe(
      'Rozděleno: A zůstává v #42; B → #43; C → nová #90. ' +
        'Do B: poznámka „Sousedi“, zařazení v pipeline (Prohlídka). Do C: kolekce „Brno“. ' +
        'Kopie do C: zařazení v pipeline (Prohlídka). „Různé“ zapsáno u 5 dvojic. ' +
        '„Stejné“ zapsáno u 1 dvojice (sloučení písmene C). Zrušená rozhodnutí „Různé“: 1.',
    );
    expect(
      splitReceiptText(split({ curation: [], rulings: { different: 1, same: 0, taken_back: 0 } })),
    ).toBe('Rozděleno: A zůstává v #42; B → #43; C → nová #90. „Různé“ zapsáno u 1 dvojice.');
  });

  it('names a fold not re-made and a copy not made apart from what went where', () => {
    const text = splitReceiptText(
      split({
        curation: [
          {
            item: 'dismissal', kind: 'dismissal', label: null, letter: 'B', property_id: 43,
            copies: [{ letter: 'C', property_id: 90, skipped: 'card' }],
          },
          { item: 'fold:9', kind: 'collection', label: 'Brno', letter: 'C', property_id: 90, copies: [], why: 'fold', skipped: 'gone' },
        ],
        rulings: { different: 5, same: 0, taken_back: 0 },
      }),
    );
    expect(text).toBe(
      'Rozděleno: A zůstává v #42; B → #43; C → nová #90. Do B: skrytí z vašeho Browse. ' +
        'Neobnoveno: kolekce „Brno“. Kopie nevytvořena: skrytí z vašeho Browse (do C). ' +
        '„Různé“ zapsáno u 5 dvojic.',
    );
  });

  it('writes the picks for the preview in one order, nothing for none', () => {
    expect(choicesParam({})).toBe('');
    expect(
      choicesParam({ pipeline: { to: 'B', copies: ['A'] }, 'note:11': { to: 'C', copies: [] } }),
    ).toBe('{"note:11":{"to":"C","copies":[]},"pipeline":{"to":"B","copies":["A"]}}');
  });

  it('shares the merge receipt’s one slot and opens the first letter that left', () => {
    const open = vi.fn();
    pushMergeReceipt(merged({ survivor_id: 7 }), open);
    pushSplitReceipt(split(), open);
    const [merge, splitToast] = vi.mocked(toast.pushToast).mock.results.slice(-2).map((r) => r.value);
    expect(toast.dismissToast).toHaveBeenCalledWith(merge);
    const [kind, , ttl, action] = vi.mocked(toast.pushToast).mock.calls.at(-1)!;
    expect([kind, ttl, action?.label]).toEqual(['ok', 0, 'Otevřít #43']);
    action?.onClick();
    expect(open).toHaveBeenCalledWith(43);
    expect(toast.dismissToast).toHaveBeenLastCalledWith(splitToast);
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
