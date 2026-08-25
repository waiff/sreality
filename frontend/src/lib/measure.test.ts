/* The frontend half of the per-m² measure: which basis a figure is on.
 *
 * `ppm2Basis` is a hand-written mirror of migration 425's
 * `measure_price_per_m2_basis(category_main, category_type)`. Every RELATION
 * publishes `price_per_m2_basis` — all six, the two Browse read models
 * included — so every row surface reads the published token through
 * `ppm2BasisFromToken` instead. The mirror survives for the one caller with no
 * column to read: the estimation trace panel, whose rounds are a JSONB filter
 * spec. The SQL truth table below is therefore the SPEC, not a convenience: if
 * these cases ever disagree with Postgres, that panel is wrong.
 */
import { describe, expect, it } from 'vitest';
import { pipelineViewFilters } from './filters';
import {
  PPM2_BASIS_TOKEN,
  PPM2_UNIT,
  PPM2_VALUE_LABEL,
  areaKindOf,
  ppm2Basis,
  ppm2BasisFromToken,
  ppm2BasisOfCohort,
  MIXED_BASIS_HINT,
  PRICE_PERIOD_UNIT,
  mixedBasisCause,
  pricePeriodOfCohort,
  type Ppm2RowBasis,
} from './measure';

/* Migration 425's own resolution order, case for case. */
describe('ppm2Basis — mirror of measure_price_per_m2_basis', () => {
  it('resolves the three capital category_types to the sale basis', () => {
    // The allowlist is enumerated in SQL precisely because live category_type
    // has FOUR values, not two: drazba (auction) and podil (co-ownership share)
    // are real, and both are capital transactions.
    expect(ppm2Basis('byt', 'prodej')).toBe('sale');
    expect(ppm2Basis('byt', 'drazba')).toBe('sale');
    expect(ppm2Basis('byt', 'podil')).toBe('sale');
    expect(ppm2Basis('dum', 'prodej')).toBe('sale');
    expect(ppm2Basis('komercni', 'prodej')).toBe('sale');
  });

  it('resolves pronajem to the rent basis for every category_main', () => {
    expect(ppm2Basis('byt', 'pronajem')).toBe('rent');
    expect(ppm2Basis('komercni', 'pronajem')).toBe('rent');
  });

  it('resolves capital pozemek to the land basis', () => {
    expect(ppm2Basis('pozemek', 'prodej')).toBe('land');
    expect(ppm2Basis('pozemek', 'drazba')).toBe('land');
    expect(ppm2Basis('pozemek', 'podil')).toBe('land');
  });

  /* RENT-FIRST, and it matters: pozemek + pronajem is ~1 845 live properties
   * and a MONTHLY figure. Letting category_main win would file a rent under a
   * capital label — the exact confusion this program exists to end. */
  it('gives a RENTED plot the rent basis, not the land basis', () => {
    expect(ppm2Basis('pozemek', 'pronajem')).toBe('rent');
  });

  /* Anything outside the vocabulary is a visible gap, never a guess. */
  it('withholds a basis for a null or unknown category_type', () => {
    expect(ppm2Basis('byt', null)).toBeNull();
    expect(ppm2Basis('byt', undefined)).toBeNull();
    expect(ppm2Basis('byt', 'aukce')).toBeNull();
    expect(ppm2Basis(null, null)).toBeNull();
  });

  it('never returns the cohort-only "mixed" state for a single row', () => {
    const combos: Array<[string | null, string | null]> = [
      ['byt', 'prodej'], ['byt', 'pronajem'], ['pozemek', 'prodej'],
      ['pozemek', 'pronajem'], ['dum', 'podil'], [null, 'drazba'],
    ];
    for (const [main, type] of combos) {
      expect(ppm2Basis(main, type)).not.toBe('mixed');
    }
  });
});

describe('ppm2BasisFromToken — the published column onto the render union', () => {
  it('maps every migration-425 token', () => {
    expect(ppm2BasisFromToken(PPM2_BASIS_TOKEN.sale)).toBe('sale');
    expect(ppm2BasisFromToken(PPM2_BASIS_TOKEN.rent)).toBe('rent');
    expect(ppm2BasisFromToken(PPM2_BASIS_TOKEN.land)).toBe('land');
    expect(ppm2BasisFromToken('mixed')).toBe('mixed');
  });

  it('pins the token spellings themselves', () => {
    // These strings are a cross-territory contract with the SQL function;
    // renaming one in TS alone would silently unlabel every detail page.
    expect(PPM2_BASIS_TOKEN).toEqual({
      sale: 'sale_capital_czk_m2',
      rent: 'rent_monthly_czk_m2',
      land: 'land_capital_czk_m2',
    });
  });

  it('withholds a basis for null, undefined and unknown tokens', () => {
    expect(ppm2BasisFromToken(null)).toBeNull();
    expect(ppm2BasisFromToken(undefined)).toBeNull();
    expect(ppm2BasisFromToken('czk_m2')).toBeNull();
    expect(ppm2BasisFromToken('')).toBeNull();
  });
});

/* The two resolvers must agree on every row-shaped input, because one reads the
 * server's label and the other re-derives it — that is the whole drift risk. */
describe('ppm2Basis and ppm2BasisFromToken agree', () => {
  it('resolves the same basis from the inputs and from the published token', () => {
    const rows: Array<[string | null, string | null]> = [
      ['byt', 'prodej'], ['byt', 'drazba'], ['byt', 'podil'],
      ['byt', 'pronajem'], ['komercni', 'pronajem'],
      ['pozemek', 'prodej'], ['pozemek', 'pronajem'],
      ['dum', 'prodej'], ['byt', null], ['byt', 'aukce'],
    ];
    for (const [main, type] of rows) {
      const derived = ppm2Basis(main, type);
      const token = derived == null ? null : PPM2_BASIS_TOKEN[derived];
      expect(ppm2BasisFromToken(token)).toBe(derived);
    }
  });
});

describe('ppm2BasisOfCohort', () => {
  /* Rule 22: category_type is nullable and NULL means "no constraint" — the
   * Browse "Vše" pill and the Pipeline view both produce it. The canonical
   * producer of the 'mixed' state is pipelineViewFilters(), and a mixed cohort
   * must never be given one blanket unit. */
  it('calls the Pipeline view cohort mixed', () => {
    expect(ppm2BasisOfCohort(pipelineViewFilters())).toBe('mixed');
  });

  it('calls any deal-type-unconstrained cohort mixed', () => {
    expect(ppm2BasisOfCohort({ categoryMain: ['byt'], categoryType: null })).toBe('mixed');
  });

  it('resolves a single-deal-type cohort', () => {
    expect(ppm2BasisOfCohort({ categoryMain: ['byt'], categoryType: 'pronajem' })).toBe('rent');
    expect(ppm2BasisOfCohort({ categoryMain: ['byt', 'dum'], categoryType: 'prodej' })).toBe('sale');
    expect(ppm2BasisOfCohort({ categoryMain: ['byt'], categoryType: 'drazba' })).toBe('sale');
  });

  it('separates land only when the cohort is exactly pozemek', () => {
    expect(ppm2BasisOfCohort({ categoryMain: ['pozemek'], categoryType: 'prodej' })).toBe('land');
    // Plot area and floor area are different denominators, so a cohort holding
    // both spans two bases even though both are capital Kč/m².
    expect(ppm2BasisOfCohort({ categoryMain: ['pozemek', 'dum'], categoryType: 'prodej' })).toBe('mixed');
    // An empty categoryMain means EVERY category, pozemek included.
    expect(ppm2BasisOfCohort({ categoryMain: [], categoryType: 'prodej' })).toBe('mixed');
  });

  it('rents a plot on the rent basis, like a row does', () => {
    expect(ppm2BasisOfCohort({ categoryMain: ['pozemek'], categoryType: 'pronajem' })).toBe('rent');
  });

  it('withholds a basis for an unknown deal type', () => {
    expect(ppm2BasisOfCohort({ categoryMain: ['byt'], categoryType: 'aukce' })).toBeNull();
  });
});

describe('the unit vocabulary', () => {
  /* Only the rent basis carries a period. This is the 300x difference the whole
   * program is about: a sale median runs ~91 535 Kč/m², a rent median ~319. */
  it('gives the rent basis a monthly period and the capital bases none', () => {
    expect(PPM2_UNIT.rent).toBe('Kč/m²/měs');
    expect(PPM2_UNIT.sale).toBe('Kč/m²');
    expect(PPM2_UNIT.land).toBe('Kč/m² pozemku');
  });

  /* THREE bases, THREE strings — pairwise, not just rent-vs-sale. `land` was
   * spelled exactly like `sale` until W8, which is the one way this assertion
   * can pass while the vocabulary is wrong: a plot rate and a floor rate are
   * different denominators and must not share a suffix. */
  it('gives all three bases mutually distinct units', () => {
    const all: Ppm2RowBasis[] = ['sale', 'rent', 'land'];
    expect(new Set(all.map((b) => PPM2_UNIT[b])).size).toBe(all.length);
    expect(new Set(all.map((b) => PPM2_VALUE_LABEL[b])).size).toBe(all.length);
  });

  it('spells the value labels the way the growth choropleth always did', () => {
    expect(PPM2_VALUE_LABEL.rent).toBe('Nájem Kč/m²/měs');
    expect(PPM2_VALUE_LABEL.sale).toBe('Cena Kč/m²');
  });

  it('names a unit for every row basis', () => {
    const all: Ppm2RowBasis[] = ['sale', 'rent', 'land'];
    for (const b of all) {
      expect(PPM2_UNIT[b]).toBeTruthy();
      expect(PPM2_VALUE_LABEL[b]).toBeTruthy();
    }
  });
});

describe('areaKindOf', () => {
  /* Option A: area_m2 stays polymorphic and the READER says which it is. */
  it('calls a pozemek area a plot and everything else usable', () => {
    expect(areaKindOf('pozemek')).toBe('plot');
    expect(areaKindOf('byt')).toBe('usable');
    expect(areaKindOf('dum')).toBe('usable');
    expect(areaKindOf(null)).toBe('usable');
  });
});


/* The ABSOLUTE price's period. Deliberately a different function from
 * ppm2BasisOfCohort: a capital sale price and a capital land price pool fine,
 * a monthly rent and a capital sum never do. */
describe('pricePeriodOfCohort', () => {
  it('is monthly for a rent cohort and capital for every capital deal type', () => {
    expect(pricePeriodOfCohort('pronajem')).toBe('monthly');
    expect(pricePeriodOfCohort('prodej')).toBe('capital');
    expect(pricePeriodOfCohort('drazba')).toBe('capital');
    expect(pricePeriodOfCohort('podil')).toBe('capital');
  });

  it('does NOT go mixed just because the cohort spans pozemek and byt', () => {
    // The per-m² basis IS mixed there (plot area vs floor area) — the absolute
    // price is not: both asks are plain capital Kč.
    expect(ppm2BasisOfCohort({ categoryMain: ['byt', 'pozemek'], categoryType: 'prodej' }))
      .toBe('mixed');
    expect(pricePeriodOfCohort('prodej')).toBe('capital');
  });

  it('is mixed for the deal=any cohort and undecidable for an unknown type', () => {
    expect(pricePeriodOfCohort(null)).toBe('mixed');
    expect(pricePeriodOfCohort('barter')).toBeNull();
  });

  it('names a unit for both decidable periods, and marks the rent one', () => {
    expect(PRICE_PERIOD_UNIT.capital).toBe('Kč');
    expect(PRICE_PERIOD_UNIT.monthly).toContain('měs');
  });
});

/* WHY a cohort is mixed decides what the operator is told to do about it. */
describe('mixedBasisCause', () => {
  it('blames the deal type only when the cohort has not fixed one', () => {
    expect(mixedBasisCause({ categoryMain: [], categoryType: null })).toBe('deal');
    expect(mixedBasisCause({ categoryMain: ['byt'], categoryType: null })).toBe('deal');
  });

  it('blames the denominator once a single deal type is chosen', () => {
    // The unactionable case the copy used to hit: "choose one deal type" to an
    // operator who already has.
    expect(mixedBasisCause({ categoryMain: [], categoryType: 'prodej' }))
      .toBe('denominator');
    expect(mixedBasisCause({ categoryMain: ['byt', 'pozemek'], categoryType: 'prodej' }))
      .toBe('denominator');
  });

  it('gives each cause its own instruction', () => {
    expect(MIXED_BASIS_HINT.deal).not.toBe(MIXED_BASIS_HINT.denominator);
    expect(MIXED_BASIS_HINT.deal).toContain('nabídky');
    expect(MIXED_BASIS_HINT.denominator).toContain('pozemk');
  });
});
