/* lib/mergedAdverts — the words for a merge's origin and a detach's outcome, and
 * the read-your-writes refresh after a detach. */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';

import { detachOutcomeNote, inzeratu, mergeOriginLabel, refreshAfterDetach } from './mergedAdverts';
import * as queries from './queries';

vi.mock('./queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./queries')>();
  return {
    ...actual,
    fetchPropertySources: vi.fn(async () => ({ property_id: 9, sources: [] })),
  };
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
    expect(detachOutcomeNote('moved_on')).toBe('Nic se nepřesunulo — moved_on.');
  });
});

describe('refreshAfterDetach', () => {
  beforeEach(() => vi.mocked(queries.fetchPropertySources).mockClear());

  it('re-resolves the page’s sources from the listing alone, then refreshes every surface', async () => {
    const qc = new QueryClient();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    await refreshAfterDetach(qc, 105053);

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
