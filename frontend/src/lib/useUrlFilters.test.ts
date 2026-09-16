/* The filter state that lives in the URL.
 *
 * Pins the one property everything else rests on: read(write(x)) === x for
 * every state, including the two that are easy to get wrong — a value that
 * happens to equal the default (written nowhere, read back anyway) and a value
 * the operator CLEARED whose default is not empty (written as an empty key,
 * because "no floor" and "never touched" are different filters).
 */

import { describe, expect, it } from 'vitest';

import { readUrlFilters, writeUrlFilters } from './useUrlFilters';

interface F {
  generation: string;
  block: string;
  min_score: string;
  sort: string;
}

const DEFAULTS: F = { generation: 'g1', block: '', min_score: '0.2', sort: 'weakest' };

const roundTrip = (state: F): F =>
  readUrlFilters(writeUrlFilters(new URLSearchParams(), state, DEFAULTS), DEFAULTS);

describe('url filter state', () => {
  it('is the defaults when the query string is empty', () => {
    expect(readUrlFilters(new URLSearchParams(), DEFAULTS)).toEqual(DEFAULTS);
  });

  it('writes only what differs from the default', () => {
    const sp = writeUrlFilters(new URLSearchParams(), { ...DEFAULTS, block: '563510' }, DEFAULTS);
    expect(sp.toString()).toBe('block=563510');
  });

  it('round-trips a filled filter', () => {
    const state = { generation: 'g2', block: '563510', min_score: '0.5', sort: 'largest' };
    expect(roundTrip(state)).toEqual(state);
  });

  it('round-trips a value CLEARED against a non-empty default', () => {
    /* "no score floor" is a filter; dropping the key would silently restore
     * 0.20 on the next reload, i.e. a different queue from the one shared. */
    const state = { ...DEFAULTS, min_score: '' };
    const sp = writeUrlFilters(new URLSearchParams(), state, DEFAULTS);
    expect(sp.get('min_score')).toBe('');
    expect(roundTrip(state)).toEqual(state);
  });

  it('drops a key the operator set back to its default', () => {
    const before = writeUrlFilters(new URLSearchParams(), { ...DEFAULTS, sort: 'newest' }, DEFAULTS);
    expect(before.get('sort')).toBe('newest');
    const after = writeUrlFilters(before, DEFAULTS, DEFAULTS);
    expect(after.has('sort')).toBe(false);
  });

  it('leaves a param it does not own alone', () => {
    const sp = writeUrlFilters(
      new URLSearchParams('highlight=42'),
      { ...DEFAULTS, block: '7' },
      DEFAULTS,
    );
    expect(sp.get('highlight')).toBe('42');
    expect(sp.get('block')).toBe('7');
  });
});
