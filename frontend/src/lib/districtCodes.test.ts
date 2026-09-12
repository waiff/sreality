/* The SPA half of the ONE place predicate (W3 S3).
 *
 * The chip sets come from `tests/fixtures/district_chip_plan.json` — the SAME
 * file `tests/test_one_place_predicate.py` compiles with `api/location_filter`
 * and checks the two RPC bodies against. A chip set that plans differently here
 * than there is exactly how Browse and the Watchdog came to answer one saved
 * filter two ways; one table read by both languages makes that a red test. */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import {
  DISTRICT_LEVEL_ORDER,
  NO_MATCH_CODE,
  districtCodePlan,
  districtChipBadge,
  districtChipKey,
  districtsFilterClause,
  matchesDistricts,
  type DistrictMatchRow,
} from './districtCodes';
import type { DistrictChip } from './filters';

interface PlanFixture {
  include: Record<string, number[]>;
  exclude: Record<string, number[]>;
  unresolved: string[];
}

interface Case {
  name: string;
  chips: DistrictChip[];
  plan: PlanFixture;
  postgrest: string | null;
  rows: Array<{ row: Partial<DistrictMatchRow>; matches: boolean }>;
}

const TABLE = JSON.parse(
  readFileSync(
    join(process.cwd(), '..', 'tests', 'fixtures', 'district_chip_plan.json'),
    'utf-8',
  ),
) as { no_match_code: number; cases: Case[] };

const row = (over: Partial<DistrictMatchRow>): DistrictMatchRow => ({
  obec_id: null,
  okres_id: null,
  region_id: null,
  cast_obce_id: null,
  ...over,
});

/* Object key order is not part of the plan; compare level→codes as entries. */
const normalise = (side: Partial<Record<string, number[]>>): Record<string, number[]> =>
  Object.fromEntries(
    DISTRICT_LEVEL_ORDER.flatMap((l) => (side[l]?.length ? [[l, side[l]!]] : [])),
  );

describe('the one place predicate — the shared table', () => {
  it('agrees with the API on the sentinel', () => {
    expect(NO_MATCH_CODE).toBe(TABLE.no_match_code);
  });

  for (const c of TABLE.cases) {
    it(`plans "${c.name}" the way the API does`, () => {
      const plan = districtCodePlan(c.chips);
      expect(normalise(plan.include)).toEqual(normalise(c.plan.include));
      expect(normalise(plan.exclude)).toEqual(normalise(c.plan.exclude));
      expect(plan.unresolved).toEqual(c.plan.unresolved);
    });

    it(`compiles "${c.name}" to the pinned PostgREST predicate`, () => {
      expect(districtsFilterClause(c.chips)).toBe(c.postgrest);
    });

    it(`filters rows for "${c.name}" the same way`, () => {
      for (const r of c.rows) {
        expect(matchesDistricts(row(r.row), c.chips)).toBe(r.matches);
      }
    });
  }
});

describe('the compiled predicate reads no text column', () => {
  it('never emits an ilike or a place_search_text arm', () => {
    for (const c of TABLE.cases) {
      const clause = districtsFilterClause(c.chips) ?? '';
      expect(clause).not.toContain('ilike');
      expect(clause).not.toContain('place_search_text');
      expect(clause).not.toContain('district.');
    }
  });
});

describe('chip identity and badge', () => {
  it('keys a resolved chip on its unit, not its spelling', () => {
    const a: DistrictChip = { name: 'Praha', context: null, level: 'obec', id: 554782 };
    const b: DistrictChip = {
      name: 'Praha, hlavní město', context: 'Praha', level: 'obec', id: 554782,
    };
    expect(districtChipKey(a)).toBe(districtChipKey(b));
  });

  it('keys an unresolved chip on name + context', () => {
    const a: DistrictChip = { name: 'Brno', context: null };
    const b: DistrictChip = { name: 'Brno', context: 'Jihomoravský kraj' };
    expect(districtChipKey(a)).not.toBe(districtChipKey(b));
  });

  it('says a chip carries no code rather than looking like a live filter', () => {
    expect(districtChipBadge({ name: 'Brno', context: null })).toBe('Nerozpoznáno');
    expect(
      districtChipBadge({ name: 'Žižkov', context: null, level: 'cast_obce', id: 490067 }),
    ).toBe('Část obce');
    expect(
      districtChipBadge({ name: 'Pezinská', context: null, level: 'locality', id: 535419 }),
    ).toBe('Obec');
  });
});
