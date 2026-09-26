/* deriveSplit — the stored split, read back from the pair rulings. The newest
 * row per pair is the ruling (migration 573), so a withdrawal (a newer `unsure`)
 * hides the older word it withdrew rather than letting it show through. */

import { describe, expect, it } from 'vitest';

import type { AutodedupVerdictRow } from '@/lib/api';
import { deriveSplit } from './UnitSplit';

const members = [{ listing_id: 11 }, { listing_id: 12 }, { listing_id: 13 }];

const row = (
  lo: number,
  hi: number,
  verdict: AutodedupVerdictRow['verdict'],
  decided_at: string,
): AutodedupVerdictRow => ({
  kind: 'pair',
  listing_lo: lo,
  listing_hi: hi,
  verdict,
  note: null,
  decided_by: 'operator@example.com',
  decided_at,
});

describe('deriveSplit', () => {
  it('reads a stored split: a negative pair puts two adverts in two units', () => {
    expect(deriveSplit(members, [row(11, 13, 'different', '2026-09-20T10:00:00Z')])).toEqual({
      units: { 11: 'A', 12: 'B', 13: 'C' },
    });
  });

  it('a withdrawn ruling is no ruling: the newest unsure hides the older same', () => {
    /* Newest first, as the server sends them. */
    const verdicts = [
      row(11, 12, 'unsure', '2026-09-26T10:00:00Z'),
      row(11, 12, 'same', '2026-09-20T10:00:00Z'),
    ];
    expect(deriveSplit(members, verdicts)).toBeNull();
  });

  it('a flip reads as its newest word', () => {
    const verdicts = [
      row(11, 12, 'same', '2026-09-26T10:00:00Z'),
      row(11, 12, 'different', '2026-09-20T10:00:00Z'),
    ];
    expect(deriveSplit(members, verdicts)).toEqual({ units: { 11: 'A', 12: 'A', 13: 'B' } });
  });
});
