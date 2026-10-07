/* The merged-adverts section on the property page, the proposed-splits page and
 * the category review: their query keys, their words for a merge's origin, a
 * split's outcome and where a unit landed, and the refresh after a split or a
 * merge. Splits write through ONE route, `POST /properties/{id}/split` (E919),
 * and each states a whole partition in one call: the property page and the
 * category review as letters over the adverts (`splitPlan` — every letter group
 * but the one keeping the record leaves as one property), a proposal card as
 * ticks. A merge, from Browse or the Rulings page, answers with one toast
 * (`pushMergeReceipt`, MS15). */

import type { QueryClient } from '@tanstack/react-query';

import type { MergeCarried, MergeResult, SplitStatement, SplitUnit } from '@/lib/api';
import { distinctUnits, unitOf, type UnitMap } from '@/components/autodedup/UnitSplit';

import { invalidateBrowseQueries } from '@/lib/browseInvalidation';
import { revalidateCollections } from '@/lib/collectionCache';
import { czPlural } from '@/lib/format';
import { revalidatePipeline } from '@/lib/pipelineCache';
import { autodedupKeys } from '@/lib/autodedupKeys';
import { dismissToast, pushToast } from '@/lib/toast';

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
  return czPlural(n, 'inzerát', 'inzeráty', 'inzerátů');
}

const quoted = (names: readonly string[]): string => names.map((n) => `„${n}“`).join(', ');
const NOTHING_CARRIED: MergeCarried = { notes: 0, pipeline: null, collections: [], tags: [] };

/* The one toast after a merge (MS15): the surviving property, only the acting
 * account's MOVED items (notes as a count, the rest by name), the "Různé"
 * rulings it took back (MS12) and whether the property is hidden from the
 * account (MS13); an unread receipt is said, never shown as nothing moved. */
export function mergeReceiptText(r: MergeResult): string {
  const { notes, pipeline, collections, tags } = r.carried ?? NOTHING_CARRIED;
  const moved = [
    notes > 0 ? `${notes} ${czPlural(notes, 'poznámka', 'poznámky', 'poznámek')}` : null,
    pipeline != null ? `zařazení v pipeline (${pipeline})` : null,
    collections.length > 0 ? `kolekce ${quoted(collections)}` : null,
    tags.length > 0 ? `${tags.length === 1 ? 'štítek' : 'štítky'} ${quoted(tags)}` : null,
  ].filter((part): part is string => part != null);
  return [
    `Sloučeno do nemovitosti #${r.survivor_id}.`,
    r.carried == null ? 'Co se přesunulo, se nepodařilo načíst.' : null,
    moved.length > 0 ? `Přesunuto: ${moved.join(', ')}.` : null,
    r.rulings_taken_back > 0 ? `Zrušená rozhodnutí „Různé“: ${r.rulings_taken_back}.` : null,
    r.hidden_for_you ? 'Pro vás je skrytá.' : null,
  ]
    .filter((part) => part != null)
    .join(' ');
}

let receiptToast: number | null = null;

/* Pushes the receipt with "Otevřít #S", replacing the previous receipt so
 * merges in a row never stack; it stays until dismissed or opened. */
export function pushMergeReceipt(r: MergeResult, open: (propertyId: number) => void): void {
  if (receiptToast != null) dismissToast(receiptToast);
  const id = pushToast('ok', mergeReceiptText(r), 0, {
    label: `Otevřít #${r.survivor_id}`,
    onClick: () => {
      dismissToast(id);
      open(r.survivor_id);
    },
  });
  receiptToast = id;
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
  not_merged: 'inzerát je v nemovitosti sám',
  on_origin: 'inzerát už je v nemovitosti, ze které přišel',
  moved_since: 'inzerát se mezitím přesunul jinam',
  origin_moved_on: 'nemovitost, ze které přišel, byla mezitím sloučena jinam; nejdřív rozdělte tam',
  last_native: 'je to poslední vlastní inzerát nemovitosti; oddělte místo něj sloučené inzeráty',
  propose_only: 'engine rozdělení jen navrhuje',
  shared_origin: 'přišel ze stejné nemovitosti jako inzerát s jiným písmenem a vrátily by se do ní spolu',
};

export function unmovedReason(outcome: string): string {
  return UNMOVED[outcome] ?? outcome;
}

/* Whether an advert's row says why it cannot leave: never of a lone advert
 * (nothing to leave) nor of the last own one (the letters keep its group on the
 * property). */
export function saysUnmoved(a: { splittable: boolean; detach_outcome: string | null }): boolean {
  return !a.splittable && !['not_merged', 'last_native'].includes(a.detach_outcome ?? '');
}

/* What a split never moves: the operator's state is the property record's (rules 18, 22). */
export function stateStays(propertyId: number): string {
  return `Poznámky, štítky, kolekce a zařazení v pipeline zůstanou u nemovitosti #${propertyId}.`;
}

export interface PlanGroup {
  letter: string;
  /* Ascending. */
  listingIds: number[];
}

export interface SplitPlan {
  /* Every advert shown, each leaving group one unit; no reason, no confirm. */
  statement: SplitStatement;
  kept: PlanGroup;
  /* Letter order. */
  leaving: PlanGroup[];
}

/* The letters as the ONE split statement: the property page's, and the category
 * review's (E937), whose letters start one per category. The letters say which
 * adverts are one property; they do not say which one stays: the server keeps
 * the record with the unit not sent in `separate`, and refuses (`cannot_move`
 * `last_native`) when that unit holds none of the property's own adverts while
 * another does. So the group with the most own adverts (no merge brought them)
 * stays, the earliest letter on a tie, the canonical advert's group when none is
 * own; every other group leaves as one unit. `keep_together` is false: the
 * statement rules nothing among the adverts that stay. */
export function splitPlan(
  adverts: readonly number[],
  units: UnitMap,
  own: ReadonlySet<number>,
  canonicalListingId: number | null,
): SplitPlan {
  const groups: PlanGroup[] = distinctUnits(
    adverts.map((id) => ({ listing_id: id })),
    units,
  ).map((letter) => ({
    letter,
    listingIds: adverts.filter((id) => unitOf(units, id) === letter).sort((a, b) => a - b),
  }));
  const owned = (g: PlanGroup) => g.listingIds.filter((id) => own.has(id)).length;
  let kept: PlanGroup | undefined;
  for (const g of groups) if (owned(g) > (kept ? owned(kept) : 0)) kept = g;
  kept ??=
    groups.find((g) => canonicalListingId != null && g.listingIds.includes(canonicalListingId)) ??
    groups[0] ??
    { letter: 'A', listingIds: [] };
  const leaving = groups.filter((g) => g !== kept);
  return {
    statement: {
      adverts: [...adverts],
      separate: leaving.map((g) => g.listingIds),
      keep_together: false,
    },
    kept,
    leaving,
  };
}

/* Where a split left one unit, as the link's words: the property it stays on,
 * a new record, the property it came from, or the record two landings joined. */
export function unitLanding(unit: SplitUnit, from: number): string {
  if (unit.merge_group_id) return `sloučeno do #${unit.property_id}`;
  if (unit.moved.some((m) => m.outcome === 'split_native')) return `nová nemovitost #${unit.property_id}`;
  if (unit.moved.length > 0) return `vráceno do #${unit.property_id}`;
  return unit.property_id === from ? `zůstává #${unit.property_id}` : `už v #${unit.property_id}`;
}

/* Read-your-writes after a split (or its undo) or a merge, for the property
 * page, the proposals page, the category review AND every Browse surface. The
 * property page is keyed on the property, so a plain invalidation re-reads the
 * property (a separated canonical advert hands the header to the next one) and
 * its advert list; `curation` re-reads notes and tags, which a merge moves. */
export function refreshAfterSplit(qc: QueryClient): void {
  for (const key of [
    ['property'],
    ['property-sources'],
    ['snapshots'],
    ['curation'],
    mergedAdvertsKeys.all,
    autodedupKeys.proposedSplits,
    autodedupKeys.categorySplits,
  ]) {
    qc.invalidateQueries({ queryKey: key });
  }
  invalidateBrowseQueries(qc);
  revalidateCollections(qc);
  revalidatePipeline(qc);
}
