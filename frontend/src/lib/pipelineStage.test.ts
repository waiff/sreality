/* The stage badge is the reason migration 377 exists, so its fallback ladder is
 * pinned here: operator code first, ordinal second, nothing third — and NEVER
 * `position`, which the live board's three "9." closed stages would render as
 * 5 / 6 / 7. */

import { describe, expect, it } from 'vitest';

import { stageAccent, stageBadge } from './pipelineStage';
import type { PipelineStage } from './types';

const stage = (over: Partial<PipelineStage> & { id: number }): PipelineStage => ({
  key: `s${over.id}`,
  label: `Stage ${over.id}`,
  position: over.id,
  color: null,
  is_terminal: false,
  is_entry: false,
  code: null,
  ...over,
});

/* Shaped like the operator's live board: hand-numbered 1..4, then three closed
 * stages all coded "9", sitting at positions 5, 6 and 7. */
const BOARD: PipelineStage[] = [
  stage({ id: 11, code: '1', color: 'slate', is_entry: true }),
  stage({ id: 12, code: '2', color: 'ochre' }),
  stage({ id: 13, code: '3', color: 'sage' }),
  stage({ id: 14, code: '4', color: 'copper' }),
  stage({ id: 15, code: '9', color: 'sand', is_terminal: true, position: 5 }),
  stage({ id: 16, code: '9', color: 'sand', is_terminal: true, position: 6 }),
  stage({ id: 17, code: '9', color: 'sand', is_terminal: true, position: 7 }),
];

describe('stageBadge', () => {
  it('prefers the operator code, including duplicates across closed stages', () => {
    expect(BOARD.map((s) => stageBadge(s, BOARD))).toEqual(
      ['1', '2', '3', '4', '9', '9', '9'],
    );
  });

  it('falls back to the 1-based ordinal when a stage has no code', () => {
    const uncoded = stage({ id: 99 });
    const stages = [...BOARD, uncoded];
    expect(stageBadge(uncoded, stages)).toBe('8');
  });

  it('is the ordinal, not `position`', () => {
    // A gap-y position sequence (archived stages leave holes) must not leak
    // into the badge.
    const gappy = [stage({ id: 1, position: 4 }), stage({ id: 2, position: 40 })];
    expect(gappy.map((s) => stageBadge(s, gappy))).toEqual(['1', '2']);
  });

  it('renders no badge for a stage missing from the live list', () => {
    // Archived stage still referenced by a card, or the list not loaded yet:
    // no badge beats a wrong number.
    expect(stageBadge(stage({ id: 999 }), BOARD)).toBeNull();
    expect(stageBadge(stage({ id: 11 }), [])).toBeNull();
  });
});

describe('stageAccent', () => {
  it('maps a stage colour onto the shared tag palette', () => {
    expect(stageAccent(stage({ id: 1, color: 'teal' }))).toEqual({
      fg: 'var(--color-tag-teal)',
      soft: 'var(--color-tag-teal-soft)',
    });
  });

  it('falls back to copper — the deal-tracking accent — never to grey', () => {
    // The board used to fall back to --color-rule-strong while the listing
    // header fell back to copper; one uncoloured stage rendered two ways.
    expect(stageAccent(null).fg).toBe('var(--color-copper)');
    expect(stageAccent(stage({ id: 1 })).fg).toBe('var(--color-copper)');
  });
});
