/* The compatibility reader for stored location chips (W3 S3).
 *
 * A chip is a level plus a RÚIAN code. Chips saved before codes existed — a
 * preset written pre-migration-172, a hand-typed `?districts=Brno` URL — carry
 * a name and nothing else, and under the one code predicate a chip with no code
 * matches NOTHING (`districtCodes.NO_MATCH_CODE`, fail closed).
 *
 * So they are resolved ONCE, here, on the way into the query: the names go to
 * `/maps/resolve-names`, which reads the same RÚIAN name index the Watchdog's
 * in-process reader uses, and the chips are replaced IN MEMORY. The stored blob
 * is never rewritten — a preset stores the operator's full blob, and rewriting
 * it behind their back would make a filter mean something they never saved. (If
 * they re-save the preset afterwards, they save the resolved chips, which is
 * the only time the stored shape changes.)
 *
 * One name can answer at several levels ("Jihlava" is an obec AND an okres);
 * all of them are kept, so the cohort is the union — the closest equality has
 * to the ILIKE-across-four-columns this replaces. A name the index cannot place
 * keeps its chip (the operator still sees what they filtered on) and matches
 * nothing, with ONE console line saying so.
 *
 * Convergence: every name-only chip is either replaced or marked resolved after
 * one attempt, and every attempted key is remembered, so this can never loop. */

import { useEffect, useRef } from 'react';

import { codeLevel } from './districtCodes';
import type { DistrictChip } from './filters';
import { resolveChipNames } from './maps';

const isLegacy = (chip: DistrictChip): boolean =>
  codeLevel(chip.level) == null || chip.id == null;

const legacyKey = (chip: DistrictChip): string =>
  `${chip.name}::${chip.context ?? ''}`;

export const useLegacyChipUpgrade = (
  districts: ReadonlyArray<DistrictChip>,
  onUpgrade: (next: DistrictChip[]) => void,
): void => {
  const attempted = useRef<Set<string>>(new Set());
  useEffect(() => {
    const pending = districts.filter(
      (c) => isLegacy(c) && !attempted.current.has(legacyKey(c)),
    );
    if (!pending.length) return;
    let cancelled = false;
    for (const chip of pending) attempted.current.add(legacyKey(chip));
    void (async () => {
      let matches: Awaited<ReturnType<typeof resolveChipNames>>;
      try {
        matches = await resolveChipNames(
          pending.map((c) => ({ name: c.name, context: c.context })),
        );
      } catch {
        console.warn(
          `location chips: could not resolve ${pending.length} name-only chip(s) `
          + `(${pending.map((c) => c.name).join(', ')}) — they match nothing`,
        );
        return;
      }
      if (cancelled) return;
      const byKey = new Map(pending.map((c, i) => [legacyKey(c), matches[i] ?? []]));
      const unresolved: string[] = [];
      const next: DistrictChip[] = [];
      for (const chip of districts) {
        const hits = isLegacy(chip) ? byKey.get(legacyKey(chip)) : undefined;
        if (hits == null) {
          next.push(chip);
          continue;
        }
        if (!hits.length) {
          unresolved.push(chip.name);
          next.push(chip);
          continue;
        }
        for (const hit of hits) {
          next.push({
            name: chip.name,
            context: chip.context,
            ...(chip.excluded === true ? { excluded: true } : {}),
            level: hit.level,
            id: hit.id,
          });
        }
      }
      if (unresolved.length) {
        console.warn(
          `location chips: ${unresolved.length} saved chip(s) (${unresolved.join(', ')}) `
          + 'are not in the RÚIAN name index — they match nothing',
        );
      }
      const changed =
        next.length !== districts.length
        || next.some((c, i) => c.level !== districts[i]?.level || c.id !== districts[i]?.id);
      if (changed) onUpgrade(next);
    })();
    return () => {
      cancelled = true;
    };
  }, [districts, onUpgrade]);
};
