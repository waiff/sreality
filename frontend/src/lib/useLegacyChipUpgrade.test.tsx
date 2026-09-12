/* The stored-chip compatibility reader (W3 S3).
 *
 * A chip is a level plus a RÚIAN code, and a chip with no code matches NOTHING.
 * A preset or URL written before codes existed therefore has to be resolved on
 * the way into the query — once, in memory, never by rewriting the stored blob. */
import { renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach } from 'vitest';

import type { DistrictChip } from './filters';
import { useLegacyChipUpgrade } from './useLegacyChipUpgrade';

const resolveChipNames = vi.hoisted(() => vi.fn());
vi.mock('./maps', () => ({ resolveChipNames }));

beforeEach(() => {
  resolveChipNames.mockReset();
});

describe('useLegacyChipUpgrade', () => {
  it('resolves a name-only chip to every level that name means', async () => {
    resolveChipNames.mockResolvedValue([
      [{ level: 'obec', id: 586846 }, { level: 'okres', id: 3707 }],
    ]);
    const onUpgrade = vi.fn();
    const chips: DistrictChip[] = [{ name: 'Jihlava', context: null }];
    renderHook(() => useLegacyChipUpgrade(chips, onUpgrade));

    await waitFor(() => expect(onUpgrade).toHaveBeenCalledTimes(1));
    expect(onUpgrade.mock.calls[0][0]).toEqual([
      { name: 'Jihlava', context: null, level: 'obec', id: 586846 },
      { name: 'Jihlava', context: null, level: 'okres', id: 3707 },
    ]);
    // The caller's array is untouched — the stored blob is never rewritten.
    expect(chips).toEqual([{ name: 'Jihlava', context: null }]);
  });

  it('keeps the exclude flag when it upgrades a chip', async () => {
    resolveChipNames.mockResolvedValue([[{ level: 'cast_obce', id: 490017 }]]);
    const onUpgrade = vi.fn();
    renderHook(() =>
      useLegacyChipUpgrade(
        [{ name: 'Modřany', context: 'Praha', excluded: true }],
        onUpgrade,
      ),
    );
    await waitFor(() => expect(onUpgrade).toHaveBeenCalled());
    expect(onUpgrade.mock.calls[0][0]).toEqual([
      { name: 'Modřany', context: 'Praha', excluded: true, level: 'cast_obce', id: 490017 },
    ]);
  });

  it('leaves an already-coded chip alone and never calls the resolver', async () => {
    const onUpgrade = vi.fn();
    renderHook(() =>
      useLegacyChipUpgrade(
        [{ name: 'Praha', context: null, level: 'obec', id: 554782 }],
        onUpgrade,
      ),
    );
    await Promise.resolve();
    expect(resolveChipNames).not.toHaveBeenCalled();
    expect(onUpgrade).not.toHaveBeenCalled();
  });

  it('keeps an unresolvable chip visible and warns once', async () => {
    resolveChipNames.mockResolvedValue([[]]);
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const onUpgrade = vi.fn();
    renderHook(() => useLegacyChipUpgrade([{ name: 'U Kulaťáku', context: null }], onUpgrade));

    await waitFor(() => expect(warn).toHaveBeenCalledTimes(1));
    // Nothing changed, so no state write — the chip stays on screen and, under
    // the one predicate, matches nothing.
    expect(onUpgrade).not.toHaveBeenCalled();
    warn.mockRestore();
  });

  it('does not retry a name it already attempted', async () => {
    resolveChipNames.mockResolvedValue([[]]);
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const chips: DistrictChip[] = [{ name: 'U Kulaťáku', context: null }];
    const { rerender } = renderHook(() => useLegacyChipUpgrade(chips, vi.fn()));
    await waitFor(() => expect(resolveChipNames).toHaveBeenCalledTimes(1));
    rerender();
    rerender();
    expect(resolveChipNames).toHaveBeenCalledTimes(1);
  });

  it('survives a resolver outage without dropping the filter', async () => {
    resolveChipNames.mockRejectedValue(new Error('503'));
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const onUpgrade = vi.fn();
    renderHook(() => useLegacyChipUpgrade([{ name: 'Brno', context: null }], onUpgrade));
    await waitFor(() => expect(warn).toHaveBeenCalled());
    expect(onUpgrade).not.toHaveBeenCalled();
    warn.mockRestore();
  });
});
