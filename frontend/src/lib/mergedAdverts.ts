/* The merged-adverts section on the property page and the proposed-splits page:
 * their query keys, their words for a merge's origin, a split's outcome and where
 * a unit landed, and the refresh after a split. Both write through ONE route,
 * `POST /properties/{id}/split` (E919): a row's 'Rozdělit' separates that one
 * advert (back to the property the merge ledger says it came from, or one no
 * merge brought to a new record; offered on every row that would move,
 * `splittable`), and a proposal card states its whole partition in one call. */

import type { QueryClient } from '@tanstack/react-query';

import type { SplitUnit } from '@/lib/api';

import { invalidateBrowseQueries } from '@/lib/browseInvalidation';
import { revalidateCollections } from '@/lib/collectionCache';
import { revalidatePipeline } from '@/lib/pipelineCache';

export const mergedAdvertsKeys = {
  all: ['merged-adverts'] as const,
  listings: (ids: readonly number[]) => ['merged-adverts', 'listings', ids] as const,
  origins: (propertyId: number) => ['merged-adverts', 'origins', propertyId] as const,
};

/* The property page's own reads, keyed on the property id. */
export const propertyKeys = {
  row: (propertyId: number | null) => ['property', propertyId] as const,
  sources: (propertyId: number | null) => ['property-sources', propertyId] as const,
};

/* 1 inzerát · 2–4 inzeráty · 0 / 5+ inzerátů. */
export function inzeratu(n: number): string {
  if (n === 1) return 'inzerát';
  if (n >= 2 && n <= 4) return 'inzeráty';
  return 'inzerátů';
}

/* The adjective the origin line puts before "sloučení". Explicit per source: an
 * unknown one is shown raw, never passed off as the operator's own. */
export function mergeOriginLabel(source: string): string {
  switch (source) {
    case 'operator':
      return 'ruční';
    case 'autodedup':
      return 'automatické (AUTODEDUP)';
    case 'auto':
      return 'automatické (původní engine)';
    default:
      return `„${source}“`;
  }
}

/* Why separating an advert moves nothing; an outcome not listed here is shown raw. */
const UNMOVED: Record<string, string> = {
  not_on_property: 'inzerát už v této nemovitosti není',
  not_merged: 'inzerát je v nemovitosti sám',
  on_origin: 'inzerát už je v nemovitosti, ze které přišel',
  moved_since: 'inzerát se mezitím přesunul jinam',
  origin_moved_on: 'nemovitost, ze které přišel, byla mezitím sloučena jinam; nejdřív rozdělte tam',
  last_native: 'je to poslední vlastní inzerát nemovitosti; oddělte místo něj sloučené inzeráty',
  propose_only: 'engine rozdělení jen navrhuje',
};

export function unmovedReason(outcome: string): string {
  return UNMOVED[outcome] ?? outcome;
}

/* A native split of the advert the header speaks with: what stays behind (rules 18, 22). */
export const STATE_STAYS =
  'Poznámky, štítky, kolekce a karta v pipeline zůstanou u zbylých inzerátů této nemovitosti.';

/* Where a split left one unit, as the link's words: the property it stays on,
 * a new record, the property it came from, or the record two landings joined. */
export function unitLanding(unit: SplitUnit, from: number): string {
  if (unit.merge_group_id) return `sloučeno do #${unit.property_id}`;
  if (unit.moved.some((m) => m.outcome === 'split_native')) return `nová nemovitost #${unit.property_id}`;
  if (unit.moved.length > 0) return `vráceno do #${unit.property_id}`;
  return unit.property_id === from ? `zůstává #${unit.property_id}` : `už v #${unit.property_id}`;
}

/* Read-your-writes after a split (or its undo), for the property page, the
 * proposals page AND every Browse surface. The property page is keyed on the
 * property, so a plain invalidation re-reads the property (a separated canonical
 * advert hands the header to the next one) and its advert list. */
export function refreshAfterSplit(qc: QueryClient): void {
  for (const key of [
    ['property'],
    ['property-sources'],
    ['property-status-events'],
    ['snapshots'],
    mergedAdvertsKeys.all,
    ['autodedup', 'proposed-splits'],
  ]) {
    qc.invalidateQueries({ queryKey: key });
  }
  invalidateBrowseQueries(qc);
  revalidateCollections(qc);
  revalidatePipeline(qc);
}
