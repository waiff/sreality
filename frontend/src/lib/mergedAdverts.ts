/* The merged-adverts section on the property page: its query keys, its words for
 * a merge's origin and a detach's outcome, and the refresh after a detach. A row's
 * 'Rozdělit' is exact for any property size and any merge origin: it detaches that
 * one advert back to the property the merge ledger says it came from
 * (`GET /properties/{id}/origins`), offered only on a row that has one. The
 * proposed-splits page detaches the same way, advert by advert. */

import type { QueryClient } from '@tanstack/react-query';

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

/* Why a detach moved nothing; an outcome not listed here is shown raw. */
const UNMOVED: Record<string, string> = {
  not_on_property: 'inzerát už v této nemovitosti není',
  not_merged: 'inzerát sem nepřivedlo žádné platné sloučení',
  on_origin: 'inzerát už je v nemovitosti, ze které přišel',
  moved_since: 'inzerát se mezitím přesunul jinam',
  origin_moved_on: 'nemovitost, ze které přišel, byla mezitím sloučena jinam; nejdřív rozdělte tam',
};

export function detachOutcomeNote(outcome: string): string {
  return `Nic se nepřesunulo — ${UNMOVED[outcome] ?? outcome}.`;
}

/* Read-your-writes after a detach, for the property page, the proposals page
 * AND every Browse surface. The property page is keyed on the property, so a
 * plain invalidation re-reads the property (a detached canonical advert hands the
 * header to the next one) and its advert list. */
export function refreshAfterDetach(qc: QueryClient): void {
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
