/* The card-size table's invariants.
 *
 * jsdom cannot lay anything out, so these are deliberately NOT assertions about
 * how the board looks — the geometry was measured in a real browser. What they
 * pin is that every size is COMPLETE and DISTINCT (a half-applied step is the
 * failure mode: a wider column still drawing the old thumbnail, a taller card
 * over the old drop-zone floor) and that a stored preference survives, or is
 * rejected, exactly as intended. */

import { beforeEach, describe, expect, it } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import {
  PIPELINE_CARD_GEOMETRY,
  PIPELINE_CARD_SIZE_LABELS,
  PIPELINE_CARD_SIZES,
  usePipelineCardSize,
} from './pipelineCardSize';

const KEY = 'sreality.pipeline.cardSize';

describe('pipeline card geometry', () => {
  it('gives every size a complete entry', () => {
    for (const size of PIPELINE_CARD_SIZES) {
      const geo = PIPELINE_CARD_GEOMETRY[size];
      for (const [field, value] of Object.entries(geo)) {
        if (field === 'stacked') continue;
        expect(value, `${size}.${field}`).toMatch(/\S/);
      }
      expect(PIPELINE_CARD_SIZE_LABELS[size].label).toMatch(/\S/);
      expect(PIPELINE_CARD_SIZE_LABELS[size].title).toMatch(/\S/);
    }
  });

  it('gives each size its own column, thumbnail and drop-zone floor', () => {
    const distinct = (pick: (s: (typeof PIPELINE_CARD_SIZES)[number]) => string) =>
      new Set(PIPELINE_CARD_SIZES.map(pick)).size;
    expect(distinct((s) => PIPELINE_CARD_GEOMETRY[s].column)).toBe(3);
    expect(distinct((s) => PIPELINE_CARD_GEOMETRY[s].thumb)).toBe(3);
    expect(distinct((s) => PIPELINE_CARD_GEOMETRY[s].dropZoneMin)).toBe(3);
    expect(distinct((s) => PIPELINE_CARD_GEOMETRY[s].overlay)).toBe(3);
  });

  /* The app-wide "one step up doubles the photo" convention Browse's --card-min
   * follows (11.5rem → 23rem). md is that step for this board, so it is worth a
   * test rather than a comment: 3rem → 6rem. */
  it('doubles the thumbnail from sm to md, exactly', () => {
    expect(PIPELINE_CARD_GEOMETRY.sm.thumb).toContain('h-12 w-12');
    expect(PIPELINE_CARD_GEOMETRY.md.thumb).toContain('h-24 w-24');
  });

  /* Only lg changes the card's DESIGN — the photo leaves the text's row. */
  it('stacks the photo above the text at lg only', () => {
    expect(PIPELINE_CARD_GEOMETRY.sm.stacked).toBe(false);
    expect(PIPELINE_CARD_GEOMETRY.md.stacked).toBe(false);
    expect(PIPELINE_CARD_GEOMETRY.lg.stacked).toBe(true);
  });

  /* Whatever the size, the floor from #1399 stays a real target: a stage with
   * no cards must never collapse to a header-high sliver again. */
  it('keeps every drop-zone floor at or above the 18rem three-row minimum', () => {
    for (const size of PIPELINE_CARD_SIZES) {
      const rem = Number(
        /min-h-\[(\d+(?:\.\d+)?)rem\]/.exec(PIPELINE_CARD_GEOMETRY[size].dropZoneMin)?.[1],
      );
      expect(rem, size).toBeGreaterThanOrEqual(18);
    }
  });
});

describe('usePipelineCardSize', () => {
  beforeEach(() => localStorage.clear());

  it('defaults to the board everyone already knows', () => {
    const { result } = renderHook(() => usePipelineCardSize());
    expect(result.current.value).toBe('sm');
  });

  it('remembers a choice across mounts', () => {
    const first = renderHook(() => usePipelineCardSize());
    act(() => first.result.current.set('lg'));
    expect(localStorage.getItem(KEY)).toBe('lg');
    const second = renderHook(() => usePipelineCardSize());
    expect(second.result.current.value).toBe('lg');
  });

  /* A key left behind by an older build — or hand-edited — must not render a
   * size the geometry table has no entry for. */
  it('falls back when the stored value is not a known size', () => {
    localStorage.setItem(KEY, 'enormous');
    const { result } = renderHook(() => usePipelineCardSize());
    expect(result.current.value).toBe('sm');
  });
});
