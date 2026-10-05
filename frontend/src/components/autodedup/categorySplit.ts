/* AUTODEDUP · the category review's statements and sentences (E937), pure.
 *
 * The server says which ads can be one property (the SIDES, rule 15's one
 * definition) and which side is the survivor of a split (`kept`: most of the
 * property's own ads, the side `toolkit/property_split._keeper` leaves the
 * property's number and curation with).
 * This file turns one card into the two statements its buttons send
 * (`POST /properties/{id}/split`, E919) and the sentence each confirm shows. */

import type { CategorySplit, CategorySplitAdvert, CategorySplitSide, SplitStatement } from '@/lib/api';
import { categoryMainLabel, categoryTypeLabel } from '@/lib/enums';
import { unmovedReason } from '@/lib/mergedAdverts';

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

export function keptSide(item: CategorySplit): CategorySplitSide | undefined {
  return item.groups.find((g) => g.kept) ?? item.groups[0];
}

export interface CategoryPlan {
  statement: SplitStatement;
  /* Each side that leaves: the ads the split moves, and the ones it cannot. */
  leaving: { side: CategorySplitSide; movers: CategorySplitAdvert[]; stuck: CategorySplitAdvert[] }[];
  /* Sides with no ad the split can move: the split is not offered. */
  blocked: CategorySplitSide[];
}

const everyAd = (item: CategorySplit) => item.groups.flatMap((g) => g.adverts).map((a) => a.listing_id);

/* "Rozdělit podle kategorií": every side but the kept one leaves as one unit.
 * Only the ads the split can move are named: `keep_together` is false, so the
 * server would refuse the whole statement over one it cannot move. */
export function categorySplitPlan(item: CategorySplit): CategoryPlan {
  const leaving = item.groups
    .filter((g) => !g.kept)
    .map((side) => ({
      side,
      movers: side.adverts.filter((a) => a.splittable),
      stuck: side.adverts.filter((a) => !a.splittable),
    }));
  return {
    statement: {
      adverts: everyAd(item),
      separate: leaving.filter((l) => l.movers.length > 0).map((l) => l.movers.map((a) => a.listing_id)),
      keep_together: false,
    },
    leaving,
    blocked: leaving.filter((l) => l.movers.length === 0).map((l) => l.side),
  };
}

/* "Ponechat jako jednu nemovitost": nothing leaves, every pair is ruled "stejné". */
export function keepStatement(item: CategorySplit): SplitStatement {
  return { adverts: everyAd(item), separate: [], keep_together: true };
}

const tag = (a: CategorySplitAdvert) => `#${a.listing_id} (${a.source})`;

export function splitSentence(item: CategorySplit, plan: CategoryPlan): string {
  const kept = keptSide(item);
  const parts = plan.leaving
    .filter((l) => l.movers.length > 0)
    .map((l) => `oddělit ${sideLabel(l.side)} jako jednu nemovitost: ${l.movers.map(tag).join(' + ')}`);
  /* It stays in the kept unit, so the statement rules it "different" from the
   * ads of its own side that leave: the operator reads that before confirming. */
  for (const l of plan.leaving) {
    for (const a of l.stuck) {
      parts.push(
        `${tag(a)} oddělit nejde (${unmovedReason(a.detach_outcome ?? '')}), zůstane — ` +
          's oddělenými inzeráty se mu zapíše „různé“',
      );
    }
  }
  if (kept) {
    parts.push(
      `na #${item.property_id} zůstane ${sideLabel(kept)} ` +
        `(${kept.adverts.map((a) => `#${a.listing_id}`).join(', ')}) ` +
        'i se záznamem nemovitosti (poznámky, štítky, karta v pipeline)',
    );
  }
  return `Plán: ${parts.join(' · ')}`;
}

export function keepSentence(item: CategorySplit): string {
  return (
    `Plán: potvrdit jako jednu nemovitost — všechny inzeráty zůstanou na #${item.property_id} ` +
    'a mezi stranami se zapíše „stejné“'
  );
}
