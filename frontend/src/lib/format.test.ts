import { describe, expect, it } from 'vitest';
import { fmtPct, fmtPP } from './format';

const NBSP = ' ';

describe('fmtPct', () => {
  it('uses the Czech comma decimal and a non-breaking space before the sign', () => {
    // The variant this replaced emitted "3.5%" — an English decimal point and
    // no space — on an otherwise Czech UI.
    expect(fmtPct(3.5)).toBe(`3,5${NBSP}%`);
    expect(fmtPct(12)).toBe(`12,0${NBSP}%`);
  });

  it('pads to a fixed decimal count so a column of values stays aligned', () => {
    expect(fmtPct(4)).toBe(`4,0${NBSP}%`);
    expect(fmtPct(4.25)).toBe(`4,3${NBSP}%`);
  });

  it('honours an explicit digit count', () => {
    expect(fmtPct(4.25, { digits: 2 })).toBe(`4,25${NBSP}%`);
    expect(fmtPct(4.25, { digits: 0 })).toBe(`4${NBSP}%`);
  });

  /* cs-CZ renders negatives with an ASCII hyphen-minus, not U+2212. Pinned
   * because a delta and its arrow must agree, and because a future switch to
   * a typographic minus would be a visible change, not an invisible one. */
  it('signs only when asked, and only positives (negatives carry their own minus)', () => {
    expect(fmtPct(3.5, { signed: true })).toBe(`+3,5${NBSP}%`);
    expect(fmtPct(-3.5, { signed: true })).toBe(`-3,5${NBSP}%`);
    expect(fmtPct(-3.5)).toBe(`-3,5${NBSP}%`);
    expect(fmtPct(0, { signed: true })).toBe(`0,0${NBSP}%`);
  });

  it('renders an em-dash for absent or non-finite input', () => {
    expect(fmtPct(null)).toBe('—');
    expect(fmtPct(undefined)).toBe('—');
    expect(fmtPct(NaN)).toBe('—');
    expect(fmtPct(Infinity)).toBe('—');
  });
});

describe('fmtPP', () => {
  it('labels percentage POINTS, always signed', () => {
    expect(fmtPP(0.9)).toBe(`+0,90${NBSP}pp`);
    expect(fmtPP(-0.9)).toBe(`-0,90${NBSP}pp`);
  });

  it('em-dashes absent input', () => {
    expect(fmtPP(null)).toBe('—');
    expect(fmtPP(NaN)).toBe('—');
  });
});
