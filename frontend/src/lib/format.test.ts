import { describe, expect, it } from 'vitest';
import { fmtArea, fmtMeasuredPricePerM2, fmtPct, fmtPP } from './format';

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

/* The per-m² renderer is where the north star is enforced at the last inch: a
 * number reaches the screen only WITH its basis label. */
describe('fmtMeasuredPricePerM2', () => {
  it('gives each basis its own suffix, with a non-breaking space before it', () => {
    expect(fmtMeasuredPricePerM2(91_535, 'sale')).toBe(`91\u00a0535${NBSP}Kč/m²`);
    expect(fmtMeasuredPricePerM2(319, 'rent')).toBe(`319${NBSP}Kč/m²/měs`);
    expect(fmtMeasuredPricePerM2(2_450, 'land')).toBe(`2\u00a0450${NBSP}Kč/m²`);
  });

  /* The one distinction that carries all the weight: the same numeral under two
   * bases must not render the same string. 319 Kč/m² (a capital price) and
   * 319 Kč/m²/měs (a monthly rent) are 300x apart in meaning. */
  it('never renders a rent and a sale figure identically', () => {
    expect(fmtMeasuredPricePerM2(319, 'rent')).not.toBe(
      fmtMeasuredPricePerM2(319, 'sale'),
    );
  });

  /* A mixed cohort has no unit, so it gets no number — printing one would be
   * exactly the category error the measure exists to prevent. */
  it('renders the gap for a mixed basis, however good the number is', () => {
    expect(fmtMeasuredPricePerM2(91_535, 'mixed')).toBe('—');
  });

  it('renders the gap for a null basis and for a null measure', () => {
    // NULL measure = below its basis floor, or basis undecidable. The server
    // withheld it on purpose; the cell shows the gap rather than guessing.
    expect(fmtMeasuredPricePerM2(null, 'sale')).toBe('—');
    expect(fmtMeasuredPricePerM2(undefined, 'sale')).toBe('—');
    expect(fmtMeasuredPricePerM2(91_535, null)).toBe('—');
  });

  it('rounds to whole crowns', () => {
    expect(fmtMeasuredPricePerM2(318.62, 'rent')).toBe(`319${NBSP}Kč/m²/měs`);
  });
});

describe('fmtArea', () => {
  it('is unchanged without an area kind', () => {
    expect(fmtArea(62)).toBe(`62${NBSP}m²`);
    expect(fmtArea(null)).toBe('—');
  });

  /* area_m2 is polymorphic (Option A): the same column is floor area for a byt
   * and PLOT area for a pozemek. A surface with room says which. */
  it('names the plot denominator when asked', () => {
    expect(fmtArea(1_200, 'plot')).toBe(`1\u00a0200${NBSP}m²${NBSP}pozemku`);
    expect(fmtArea(62, 'usable')).toBe(`62${NBSP}m²`);
  });
});
