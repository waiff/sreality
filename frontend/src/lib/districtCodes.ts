/* The ONE place predicate, browser side (W3 S3).
 *
 * A location chip is a LEVEL plus a RÚIAN CODE, and every surface compiles it
 * the same way:
 *
 *     <level>_id = any(<codes at that level>)
 *
 * plain equality per level, nothing else. This module owns that compilation for
 * the SPA; `api/location_filter.py` owns it for the API and the Watchdog; the
 * two RPC bodies (migration 504) own it in SQL. All three produce the same PLAN
 * for the same chips — `districtCodePlan` here and `district_code_plan` there
 * return the same object, and `tests/fixtures/district_chip_plan.json` is the
 * table both are tested against.
 *
 * WHAT IT REPLACES: five predicates (obec/okres/region equality, a `locality`
 * pair of obec-equality AND `place_search_text ILIKE`, and a legacy name
 * fallback ILIKE-ing across district/place_search_text/okres/region AND-ed with
 * an optional `context` narrow), written out six times across the SPA, the API
 * and two RPC bodies. `listing_location` (migration 501) answers every listing
 * with RÚIAN codes and migration 503 published them, so equality is now enough:
 * `browse_list.obec_id` / `okres_id` / `region_id` already WERE those codes
 * (`admin_boundaries.id` IS the RÚIAN code), and `cast_obce_id` is new.
 *
 * THE SENTINEL. A chip with no code — an old saved preset, or a name the RÚIAN
 * name index cannot place — compiles to `NO_MATCH_CODE` at the obec level.
 * RÚIAN codes are positive, so that arm is false for every row: an unresolvable
 * INCLUDE chip contributes nothing (fail CLOSED — a place filter that quietly
 * becomes "the whole country" is the failure that matters), an unresolvable
 * EXCLUDE chip subtracts nothing. No surface needs an "unmatched chip" branch. */

import type { DistrictChip, LocationLevel } from './filters';

/* level → the code column, on every place-filterable relation: `browse_list`
 * and `properties_map_mv` (codes from `listing_location` since migration 503),
 * `properties_public` (the Watchdog) and `pipeline_board_public` (the board). */
export const DISTRICT_LEVEL_COLUMN = {
  kraj: 'region_id',
  okres: 'okres_id',
  obec: 'obec_id',
  cast_obce: 'cast_obce_id',
} as const;

export type CodeLevel = keyof typeof DISTRICT_LEVEL_COLUMN;

/* Coarsest first, and DETERMINISTIC: the compiled predicate has to be
 * byte-stable so the SPA, the API and the RPC bodies can be diffed. */
export const DISTRICT_LEVEL_ORDER: ReadonlyArray<CodeLevel> = [
  'kraj', 'okres', 'obec', 'cast_obce',
];

/* A street / address / POI pick has no code of its own; `/maps/resolve` stamps
 * it with its CONTAINING obec, so it filters at the obec level. The retired
 * half of that predicate was a `place_search_text` ILIKE; a street chip now
 * means its municipality, nothing narrower, until a street-grain code exists. */
const LEVEL_ALIAS: Partial<Record<LocationLevel, CodeLevel>> = { locality: 'obec' };

/* Positive RÚIAN codes only, so this matches no row at any level. */
export const NO_MATCH_CODE = -1;

export const codeLevel = (level: LocationLevel | null | undefined): CodeLevel | null => {
  if (level == null) return null;
  if (level in DISTRICT_LEVEL_COLUMN) return level as CodeLevel;
  return LEVEL_ALIAS[level] ?? null;
};

/* RÚIAN codes are positive. Anything else — a hand-typed `districts_id=-1` in a
 * URL, a stored 0 — is NOT a resolved chip, and must never be able to spell the
 * sentinel from outside. */
export const isValidCode = (code: number | null | undefined): code is number =>
  code != null && Number.isFinite(code) && code > 0;

export interface DistrictCodePlan {
  include: Partial<Record<CodeLevel, number[]>>;
  exclude: Partial<Record<CodeLevel, number[]>>;
  /* Names of chips that carried no code and fell back to the sentinel. */
  unresolved: string[];
}

export const districtCodePlan = (
  chips: ReadonlyArray<DistrictChip>,
): DistrictCodePlan => {
  const plan: DistrictCodePlan = { include: {}, exclude: {}, unresolved: [] };
  for (const chip of chips) {
    let level = codeLevel(chip.level);
    let code = chip.id ?? null;
    if (level == null || !isValidCode(code)) {
      level = 'obec';
      code = NO_MATCH_CODE;
      plan.unresolved.push(chip.name);
    }
    const side = chip.excluded === true ? plan.exclude : plan.include;
    const codes = (side[level] ??= []);
    if (!codes.includes(code)) codes.push(code);
  }
  return plan;
};

const levelsWithCodes = (
  side: Partial<Record<CodeLevel, number[]>>,
): CodeLevel[] => DISTRICT_LEVEL_ORDER.filter((l) => (side[l]?.length ?? 0) > 0);

const includeArms = (side: Partial<Record<CodeLevel, number[]>>): string[] =>
  levelsWithCodes(side).map(
    (level) => `${DISTRICT_LEVEL_COLUMN[level]}.in.(${side[level]!.join(',')})`,
  );

/* An EXCLUDE subtracts only what it MATCHES, so a row whose code at that level
 * is NULL — an unresolved listing — must SURVIVE it. PostgREST's `not.in.(…)`
 * is SQL `NOT (col IN (…))`, which is NULL (i.e. drops the row) for a NULL col,
 * so each level is spelled `or(col.is.null,col.not.in.(…))` and the levels AND
 * together. That is De Morgan's law over the include arms with NULL read as
 * "did not match" — exactly what the RPC's `not exists(... case ...)` and the
 * in-memory `!hits` already do. Without it "not Prague" silently also meant
 * "and located", and the four renderings of one predicate disagreed. */
const excludeArms = (side: Partial<Record<CodeLevel, number[]>>): string[] =>
  levelsWithCodes(side).map((level) => {
    const col = DISTRICT_LEVEL_COLUMN[level];
    return `or(${col}.is.null,${col}.not.in.(${side[level]!.join(',')}))`;
  });

/* PostgREST `or=(...)` predicate for the location chips, or null when no chips
 * are set. INCLUDE chips OR together, then AND with the exclude arms so an
 * excluded place is subtracted from the cohort. Combined into one `and(...)`
 * tree so PostgREST AND's the groups. */
export const districtsFilterClause = (
  districts: ReadonlyArray<DistrictChip>,
): string | null => {
  if (!districts.length) return null;
  const plan = districtCodePlan(districts);
  const groups: string[] = [];
  const inc = includeArms(plan.include);
  if (inc.length) groups.push(`or(${inc.join(',')})`);
  groups.push(...excludeArms(plan.exclude));
  return groups.length ? `and(${groups.join(',')})` : null;
};

/* The in-memory counterpart, for the surface that loads its rows whole and
 * filters locally (the pipeline board, rule 22). Same plan, same levels, same
 * include/exclude split — there is no second definition to keep in lockstep any
 * more, only a second RENDERING of the one plan. */
export interface DistrictMatchRow {
  obec_id: number | null;
  okres_id: number | null;
  region_id: number | null;
  cast_obce_id: number | null;
}

export const matchesDistricts = (
  row: DistrictMatchRow,
  districts: ReadonlyArray<DistrictChip>,
): boolean => {
  if (!districts.length) return true;
  const plan = districtCodePlan(districts);
  const hits = (side: Partial<Record<CodeLevel, number[]>>): boolean =>
    DISTRICT_LEVEL_ORDER.some((level) => {
      const codes = side[level];
      if (!codes || !codes.length) return false;
      const value = row[DISTRICT_LEVEL_COLUMN[level]];
      return value != null && codes.includes(value);
    });
  const anyInclude = DISTRICT_LEVEL_ORDER.some((l) => (plan.include[l]?.length ?? 0) > 0);
  const included = !anyInclude || hits(plan.include);
  return included && !hits(plan.exclude);
};

/* Czech level names for the chip badge — a chip shows WHAT it selected, not
 * just a string, so "Žižkov" (část obce) reads differently from "Žižkov" the
 * POI that resolved to Praha. */
export const DISTRICT_LEVEL_LABEL: Record<LocationLevel, string> = {
  kraj: 'Kraj',
  okres: 'Okres',
  obec: 'Obec',
  cast_obce: 'Část obce',
  locality: 'Obec',
};

export const districtChipBadge = (chip: DistrictChip): string =>
  chip.level != null && isValidCode(chip.id)
    ? DISTRICT_LEVEL_LABEL[chip.level]
    : 'Nerozpoznáno';

/* Chip identity. A resolved chip IS its (level, code) — two picks that resolve
 * to the same unit are the same chip however Mapy spelled them; an unresolved
 * one falls back to name+context. Used for dedupe, remove and the React key. */
export const districtChipKey = (chip: DistrictChip): string =>
  chip.level != null && isValidCode(chip.id)
    ? `${chip.level}:${chip.id}`
    : `name:${chip.name}::${chip.context ?? ''}`;
