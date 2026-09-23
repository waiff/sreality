import { describe, expect, it } from 'vitest';
import { splitCardPlace } from './cardPlace';

describe('splitCardPlace', () => {
  it('peels the town off a street label', () => {
    expect(splitCardPlace('Radkovská 262/8, Jihlava', 'Jihlava')).toEqual({
      head: 'Radkovská 262/8',
      town: 'Jihlava',
    });
  });

  it('peels the town off a part-of-town label', () => {
    expect(splitCardPlace('Závodí, Beroun', 'Beroun')).toEqual({
      head: 'Závodí',
      town: 'Beroun',
    });
  });

  it('only strips the TRAILING town, not one inside the street name', () => {
    expect(splitCardPlace('Brno-Sever, Brno', 'Brno')).toEqual({
      head: 'Brno-Sever',
      town: 'Brno',
    });
  });

  it('has no head when the label is the bare town', () => {
    expect(splitCardPlace('Humpolec', 'Humpolec')).toEqual({
      head: null,
      town: 'Humpolec',
    });
  });

  it('keeps a label whole when it does not end with the town', () => {
    expect(splitCardPlace('DE', null)).toEqual({ head: 'DE', town: null });
    expect(splitCardPlace('Sadová', 'Praha')).toEqual({ head: 'Sadová', town: 'Praha' });
  });

  it('still reports the town when the label is missing', () => {
    expect(splitCardPlace(null, 'Praha')).toEqual({ head: null, town: 'Praha' });
  });

  it('is empty for a property with neither', () => {
    expect(splitCardPlace(null, null)).toEqual({ head: null, town: null });
    expect(splitCardPlace('  ', '  ')).toEqual({ head: null, town: null });
  });
});
