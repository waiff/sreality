import { describe, expect, it } from 'vitest';

import { engineLine } from './engineView';

const APART: Parameters<typeof engineLine>[0] = {
  engine_view: 'apart',
  zone: null,
  score: null,
  certificate: null,
  decision: null,
  why_not_merged: null,
};

describe('engineLine', () => {
  it('says the pair has no stored row rather than leaving it blank', () => {
    expect(engineLine(APART)).toBe('odděleně · pár bez uloženého řádku');
  });

  it('names a dissolved closure even when the engine stored no row for the pair (E926)', () => {
    const why = 'vaše rozhodnutí „stejné“ spojují prodej s pronájmem';
    expect(engineLine({ ...APART, why_not_merged: why })).toBe(`odděleně · pár bez uloženého řádku — ${why}`);
  });

  it('reads the stored pair, then the reason', () => {
    expect(
      engineLine({ ...APART, zone: 'merge', score: 0.97, decision: 'model', why_not_merged: 'důvod' }),
    ).toBe('odděleně · pár: merge 0.97 · model — důvod');
  });
});
