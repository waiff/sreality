/* lib/mergedAdverts — the words for a merge's origin and a detach's outcome, and
 * the read-your-writes refresh after a detach. */

import { describe, expect, it, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';

import { detachOutcomeNote, inzeratu, mergeOriginLabel, refreshAfterDetach } from './mergedAdverts';

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

describe('detachOutcomeNote', () => {
  it('says why nothing moved; an unknown outcome is shown raw', () => {
    expect(detachOutcomeNote('moved_since')).toBe(
      'Nic se nepřesunulo — inzerát se mezitím přesunul jinam.',
    );
    expect(detachOutcomeNote('origin_moved_on')).toMatch(/byla mezitím sloučena jinam/);
    expect(detachOutcomeNote('last_native')).toMatch(/poslední vlastní inzerát nemovitosti/);
    expect(detachOutcomeNote('moved_on')).toBe('Nic se nepřesunulo — moved_on.');
  });
});

describe('refreshAfterDetach', () => {
  it('re-reads the property page, the proposals and every Browse surface', () => {
    const qc = new QueryClient();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    refreshAfterDetach(qc);

    for (const key of [
      ['property'],
      ['property-sources'],
      ['property-status-events'],
      ['snapshots'],
      ['merged-adverts'],
      ['autodedup', 'proposed-splits'],
      ['autodedup', 'rulings'],
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
