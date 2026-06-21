/* Pure-function tests for the document-title rules: formatTitle (the "LR:"
 * prefix scheme) and browseTitleSummary (filters → "disposition · area · city").
 * No jsdom — both are pure transforms. */
import { describe, expect, it } from 'vitest';

import { formatTitle, APP_NAME } from './pageTitle';
import { DEFAULT_FILTERS, browseTitleSummary, type ListingFilters } from './filters';
import type { DistrictChip } from './filters';
import { fmtArea } from './format';

const chip = (name: string, excluded = false): DistrictChip => ({
  name,
  context: null,
  excluded,
});

const withFilters = (over: Partial<ListingFilters>): ListingFilters => ({
  ...DEFAULT_FILTERS,
  ...over,
});

describe('formatTitle', () => {
  it('prefixes a segment with "LR: "', () => {
    expect(formatTitle('Estimations')).toBe('LR: Estimations');
  });

  it('falls back to the bare app name when there is no segment', () => {
    expect(formatTitle(null)).toBe(APP_NAME);
    expect(formatTitle(undefined)).toBe(APP_NAME);
    expect(formatTitle('   ')).toBe(APP_NAME);
  });

  it('trims surrounding whitespace', () => {
    expect(formatTitle('  Health  ')).toBe('LR: Health');
  });
});

describe('browseTitleSummary', () => {
  it('returns null for the default (unfiltered) Browse', () => {
    expect(browseTitleSummary(DEFAULT_FILTERS)).toBeNull();
  });

  it('summarises disposition · area · city', () => {
    const s = browseTitleSummary(
      withFilters({
        dispositions: ['2+kk'],
        areaMin: 60,
        areaMax: 90,
        districts: [chip('Praha 9')],
      }),
    );
    expect(s).toBe(`2+kk · 60–${fmtArea(90)} · Praha 9`);
  });

  it('omits empty segments and collapses the separators', () => {
    expect(browseTitleSummary(withFilters({ dispositions: ['3+1'] }))).toBe('3+1');
    expect(browseTitleSummary(withFilters({ districts: [chip('Brno')] }))).toBe('Brno');
  });

  it('renders single area bounds with ≥ / ≤', () => {
    expect(browseTitleSummary(withFilters({ areaMin: 50 }))).toBe(`≥ ${fmtArea(50)}`);
    expect(browseTitleSummary(withFilters({ areaMax: 120 }))).toBe(`≤ ${fmtArea(120)}`);
  });

  it('joins dispositions with ", " and caps the long list with +N', () => {
    expect(
      browseTitleSummary(withFilters({ dispositions: ['1+kk', '2+kk', '3+kk'] })),
    ).toBe('1+kk, 2+kk, 3+kk');
    expect(
      browseTitleSummary(
        withFilters({ dispositions: ['1+kk', '2+kk', '3+kk', '4+kk'] }),
      ),
    ).toBe('1+kk, 2+kk +2');
  });

  it('shows one city name + "+N" and ignores exclude chips', () => {
    expect(
      browseTitleSummary(withFilters({ districts: [chip('Praha'), chip('Brno')] })),
    ).toBe('Praha +1');
    expect(
      browseTitleSummary(
        withFilters({ districts: [chip('Praha'), chip('Brno', true)] }),
      ),
    ).toBe('Praha');
  });
});
