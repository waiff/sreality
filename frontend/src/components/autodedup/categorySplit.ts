/* AUTODEDUP · the category review's letters, statements and sentences (E937), pure.
 *
 * The server says which ads can be one property (the SIDES, rule 15's one
 * definition). A side can still bundle two flats, so every ad carries a LETTER,
 * as on the property page: one letter is one property. The letters start one
 * per side, so an untouched card states exactly the split by category, and they
 * become the split statement through the property page's own `splitPlan`
 * (`POST /properties/{id}/split`, E919). */

import type { CategorySplit, CategorySplitAdvert, CategorySplitSide, SplitStatement } from '@/lib/api';
import { UNIT_LETTERS, type UnitMap } from '@/components/autodedup/UnitSplit';
import { categoryMainLabel, categoryTypeLabel } from '@/lib/enums';
import { splitPlan, unmovedReason, type SplitPlan } from '@/lib/mergedAdverts';

/* A request per ten properties: one property can hold thirty ads, and each ad
 * brings its text and twelve photos. */
export const PROPERTIES_PER_PAGE = 10;

/* The ids a link names (`?properties=12664,9737`), in its order and each once;
 * a part that is not an id is dropped. */
export function parsePropertyIds(raw: string | null): number[] {
  const ids: number[] = [];
  for (const part of (raw ?? '').split(',')) {
    const id = part.trim();
    if (/^\d{1,15}$/.test(id) && !ids.includes(Number(id))) ids.push(Number(id));
  }
  return ids;
}

/* A side in the Browse filters' Czech words: "Prodej · byt". */
export function sideLabel(side: CategorySplitSide): string {
  if (!side.category_type) return 'neznámá kategorie';
  const mains = side.category_main.map((m) => categoryMainLabel(m).toLowerCase()).join(' + ');
  return `${categoryTypeLabel(side.category_type)} · ${mains}`;
}

/* `split`: one category is left, whether a split did it or it never was mixed. */
export type CardState = 'open' | 'split' | 'kept';

export function cardState(item: CategorySplit): CardState {
  if (!item.mixed) return 'split';
  return item.confirmed ? 'kept' : 'open';
}

const everyAd = (item: CategorySplit) => item.groups.flatMap((g) => g.adverts);

/* Never a side of its own (a contentless record, or a category unknown): it
 * takes no letter and goes with the group that stays. */
export const rides = (a: CategorySplitAdvert): boolean => a.empty || a.unknown;

/* The letters an untouched card starts from: one per side, in display order. */
export function defaultLetters(item: CategorySplit): UnitMap {
  const units: UnitMap = {};
  item.groups.forEach((g, i) => {
    for (const a of g.adverts) if (!rides(a)) units[a.listing_id] = UNIT_LETTERS[i] ?? 'A';
  });
  return units;
}

export interface LetterPlan {
  plan: SplitPlan;
  /* The ads of a leaving group the split cannot move. One is enough to withhold
   * the split: it would stay behind, ruled "různé" from its own group. */
  stuck: CategorySplitAdvert[];
}

/* The letters as the split statement, through `splitPlan` (`own`: no merge brought
 * the ad). The riders take the letter of the group that stays, read off the
 * lettered ads first, so every ad is named in `adverts` and none rides out. */
export function letterPlan(item: CategorySplit, units: UnitMap): LetterPlan {
  const ads = everyAd(item);
  const own = new Set(ads.filter((a) => a.origin_property_id == null).map((a) => a.listing_id));
  const lettered = ads.filter((a) => !rides(a)).map((a) => a.listing_id);
  const stays = splitPlan(lettered, units, own, item.canonical_listing_id).kept.letter;
  const all: UnitMap = { ...units };
  for (const a of ads) if (rides(a)) all[a.listing_id] = stays;
  const plan = splitPlan(ads.map((a) => a.listing_id), all, own, item.canonical_listing_id);
  const leaving = new Set(plan.leaving.flatMap((g) => g.listingIds));
  return { plan, stuck: ads.filter((a) => leaving.has(a.listing_id) && !a.splittable) };
}

/* Why "Rozdělit podle písmen" is not offered, or null when it is. */
export function splitWithheld({ plan, stuck }: LetterPlan): string | null {
  if (plan.leaving.length === 0) return 'Všechny inzeráty mají stejné písmeno, není co rozdělit.';
  if (stuck.length === 0) return null;
  const each = stuck.map((a) => `#${a.listing_id} nejde oddělit (${unmovedReason(a.detach_outcome ?? '')})`);
  return `Rozdělit nelze: ${each.join('; ')}.`;
}

/* The letters as they stand, "A zůstává (6) · B odejde (2)". */
export function lettersLine(plan: SplitPlan): string {
  return [plan.kept, ...plan.leaving]
    .sort((a, b) => a.letter.localeCompare(b.letter))
    .map((g) => `${g.letter} ${g === plan.kept ? 'zůstává' : 'odejde'} (${g.listingIds.length})`)
    .join(' · ');
}

/* "Ponechat jako jednu nemovitost": nothing leaves, every pair is ruled "stejné". */
export function keepStatement(item: CategorySplit): SplitStatement {
  return { adverts: everyAd(item).map((a) => a.listing_id), separate: [], keep_together: true };
}

/* The clash in the side headings' words, "Prodej · byt × Pronájem · byt". */
export function clashLabel(item: CategorySplit, lo: number, hi: number): string | null {
  const sideOf = (id: number) => item.groups.find((g) => g.adverts.some((a) => a.listing_id === id));
  const [a, b] = [sideOf(lo), sideOf(hi)];
  return a && b ? `${sideLabel(a)} × ${sideLabel(b)}` : null;
}

export function keepSentence(item: CategorySplit): string {
  return (
    `Plán: potvrdit jako jednu nemovitost — všechny inzeráty zůstanou na #${item.property_id} ` +
    'a mezi stranami se zapíše „stejné“'
  );
}
