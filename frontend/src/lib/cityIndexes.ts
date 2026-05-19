/* Phase QUAL — shared helpers for presenting curated-city indexes.
 *
 * One source of truth for: the "pinned" short list of indexes that
 * appear at the top of any selector, the picker's per-category
 * grouping, and the popup's sort order. Both `CityIndexRulesPicker`
 * (the sidebar rule chooser) and `ListingMap`'s "Color by" dropdown +
 * city popup pull from here so the user sees the same priority order
 * everywhere.
 */

import type { CityIndexDefinition } from './queries';

/** Czech category label shown in `<optgroup>` headers. */
export const CATEGORY_LABELS: Record<CityIndexDefinition['category'], string> = {
  overall: 'Celkové',
  health_env: 'Zdraví a prostředí',
  material_edu: 'Práce a vzdělání',
  services_relations: 'Služby a vztahy',
  sub_index: 'Ostatní indexy',
};

/* Operator-curated short list of the indexes that get reached for
 * most often. They render at the top of every selector as a
 * "Připnuté" group with each label prefixed by `- ` so the pinning
 * is visually obvious. Adding / removing an entry is a one-line
 * edit; unknown slugs are silently ignored if the seed hasn't
 * loaded them. Order in the array is the display order. */
export const PINNED_SLUGS: readonly string[] = [
  'celkove_hodnoceni',     // Celkové hodnocení
  'prirustek_obyvatel',    // Index přírůstku obyvatelstva
  'stehovani_mladych',     // Index stěhování mladých
  'pracovni_mista',        // Index nabídky pracovních míst
  'nezamestnanost',        // Index nezaměstnanosti
  'silnicni_sit',          // Index silniční sítě
  'zeleznicni_doprava',    // Index železniční dopravy
];

/** Czech label first; fall back to the English one if `label_cs` is
 *  missing (shouldn't happen post-seed, but the registry typing keeps
 *  `label_en` optional / nullable so we stay defensive). */
export function indexLabel(d: CityIndexDefinition): string {
  return d.label_cs || d.label_en || d.index_name;
}

/** One option group in a `<select>`. `prefix` is prepended to each
 *  option's visible label, used to mark the pinned set with `- `. */
export interface IndexOptionGroup {
  label: string;
  defs: ReadonlyArray<CityIndexDefinition>;
  prefix: string;
}

/** Build the option-group list for a `<select>` rendering all index
 *  definitions: pinned-first under a "Připnuté" optgroup with each
 *  label prefixed by `- `, then the remaining definitions grouped by
 *  `category` in the canonical order. Definitions with no matching
 *  category fall through silently (current data has no such rows). */
export function groupForPicker(
  defs: ReadonlyArray<CityIndexDefinition>,
): IndexOptionGroup[] {
  const byName = new Map(defs.map((d) => [d.index_name, d]));
  const pinnedDefs = PINNED_SLUGS
    .map((slug) => byName.get(slug))
    .filter((d): d is CityIndexDefinition => d != null);
  const pinnedSet = new Set(pinnedDefs.map((d) => d.index_name));

  const groups: IndexOptionGroup[] = [];
  if (pinnedDefs.length > 0) {
    groups.push({ label: 'Připnuté', defs: pinnedDefs, prefix: '- ' });
  }

  const order: CityIndexDefinition['category'][] = [
    'overall', 'health_env', 'material_edu', 'services_relations', 'sub_index',
  ];
  for (const cat of order) {
    const inCat = defs.filter(
      (d) => d.category === cat && !pinnedSet.has(d.index_name),
    );
    if (inCat.length > 0) {
      groups.push({ label: CATEGORY_LABELS[cat], defs: inCat, prefix: '' });
    }
  }
  return groups;
}

/** Sort the definitions so the pinned set comes first in
 *  `PINNED_SLUGS` order, then everything else by category and the
 *  registry's `sort_order`. Used by the city-pin popup, which caps
 *  the list at 8 rows and wants the headline indexes guaranteed to
 *  be inside that cap. The optional `highlighted` argument bumps a
 *  specific index to the very top — used when the operator picked a
 *  color-by index and the popup wants to lead with that one's value. */
export function pinnedFirst(
  defs: ReadonlyArray<CityIndexDefinition>,
  highlighted: CityIndexDefinition | null = null,
): CityIndexDefinition[] {
  const pinnedRank = new Map<string, number>();
  PINNED_SLUGS.forEach((slug, i) => pinnedRank.set(slug, i));
  const categoryRank: Record<string, number> = {
    overall: 0, health_env: 1, material_edu: 2, services_relations: 3, sub_index: 4,
  };
  return [...defs].sort((a, b) => {
    if (highlighted) {
      if (a.index_name === highlighted.index_name) return -1;
      if (b.index_name === highlighted.index_name) return 1;
    }
    const ap = pinnedRank.get(a.index_name);
    const bp = pinnedRank.get(b.index_name);
    if (ap != null && bp != null) return ap - bp;
    if (ap != null) return -1;
    if (bp != null) return 1;
    const ac = categoryRank[a.category] ?? 99;
    const bc = categoryRank[b.category] ?? 99;
    if (ac !== bc) return ac - bc;
    return a.sort_order - b.sort_order;
  });
}
