import { describe, expect, it } from 'vitest';
import { splitTagLabel } from './tagLabel';

describe('splitTagLabel', () => {
  it('lifts the family out of the name so the name gets the width', () => {
    expect(splitTagLabel('interier - koupelna')).toEqual({
      family: 'interier', name: 'koupelna',
    });
    expect(splitTagLabel('podklad - letecký snímek s ohraničením subjektu')).toEqual({
      family: 'podklad', name: 'letecký snímek s ohraničením subjektu',
    });
  });

  it('keeps the family, because two tags differ ONLY by it', () => {
    // exterier = photographed from the street, interier = from inside. Drop the
    // prefix and these two buttons read identically — the exact mislabel this
    // programme exists to prevent.
    const ext = splitTagLabel('exterier - domovní vchod');
    const int = splitTagLabel('interier - domovní vchod / chodba');
    expect(ext.family).toBe('exterier');
    expect(int.family).toBe('interier');
    expect(ext.family).not.toBe(int.family);
  });

  it('tolerates the taxonomy\'s missing space and unfamilied labels', () => {
    expect(splitTagLabel('interier -vstupní chodba'))
      .toEqual({ family: 'interier', name: 'vstupní chodba' });
    expect(splitTagLabel('garáž')).toEqual({ family: null, name: 'garáž' });
    expect(splitTagLabel('technické zařízení / místnost'))
      .toEqual({ family: null, name: 'technické zařízení / místnost' });
  });
});
