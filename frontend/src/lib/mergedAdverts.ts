/* The merged-adverts section on the listing page: its switches, its query keys,
 * and the one decision it cannot make from what it can see — which merge group a
 * given advert row came in with.
 *
 * SHIPS DARK. Both switches are off, and with them off the page renders exactly
 * what it did before: no section, no extra read, no write affordance
 * (ListingDetail.test pins that). The section is read-only; the unmerge is a
 * production write through the existing admin-gated route, so it has its own
 * switch and can stay off after the section goes on.
 *
 * WHY UNMERGE IS NOT SIMPLY PER ROW. The only undo that exists is
 * `POST /properties/merges/{group}/unmerge`, which reverses a whole merge GROUP,
 * and the only read of the ledger is `GET /properties/merges?survivor_property_id=`
 * — every group this property survived, with how many adverts each moved, but NOT
 * which adverts. So from the page the per-advert question has an exact answer in
 * one shape only: a two-advert property one merge of one advert produced. For
 * one merge that explains every advert but the base, the honest offer is the
 * whole group; for anything else the page says it cannot tell rather than guess.
 * A per-listing ledger read (listing -> its active merge_group_id) would turn
 * every row into the exact case; until one exists, `planRowUnmerge` is the rule. */

import type { QueryClient } from '@tanstack/react-query';

import { listPropertyMerges } from '@/lib/api';
import { invalidateBrowseQueries } from '@/lib/browseInvalidation';
import { revalidateCollections } from '@/lib/collectionCache';
import { revalidatePipeline } from '@/lib/pipelineCache';
import { fetchPropertySources, propertySourcesKey } from '@/lib/queries';
import type { MergeGroup, MergesResponse } from '@/lib/types';

/* The section itself (read-only). Off: the page is unchanged. */
export const MERGED_ADVERTS_SECTION_ENABLED = false;
/* The per-row 'Rozdělit' write, admin sessions only. Off: rows carry no action.
 * An AUTODEDUP merge is never undone from here (planRowUnmerge's 'engine'): this
 * route writes no must-not-link and leaves the engine's own applied-merge record
 * standing, so the engine's own verdict + undo path owns those groups. */
export const MERGED_ADVERTS_UNMERGE_ENABLED = false;

/* The ledger is read only after the operator asks to split — never on page load —
 * and filtered to this property as survivor server-side, so the read is exact and
 * exhaustive whatever the ledger's size. The page cap only bounds a runaway (an
 * API that ignored the filter); hitting it reports "not read to the end". */
export const MERGE_LEDGER_PAGE_SIZE = 200;
export const MERGE_LEDGER_MAX_PAGES = 5;

export const mergedAdvertsKeys = {
  all: ['merged-adverts'] as const,
  listings: (ids: readonly number[]) => ['merged-adverts', 'listings', ids] as const,
  images: (ids: readonly number[]) => ['merged-adverts', 'images', ids] as const,
  groups: (propertyId: number) => ['merged-adverts', 'merge-groups', propertyId] as const,
};

/* 1 inzerát · 2–4 inzeráty · 0 / 5+ inzerátů. */
export function inzeratu(n: number): string {
  if (n === 1) return 'inzerát';
  if (n >= 2 && n <= 4) return 'inzeráty';
  return 'inzerátů';
}

export interface MergeGroupScan {
  /* The ACTIVE groups (not fully undone) whose survivor is this property. */
  groups: MergeGroup[];
  /* Ledger groups read. */
  scanned: number;
  /* True when the read reached the end of this property's ledger — "none found"
   * is then a fact about the property. */
  exhaustive: boolean;
}

/* Read every ledger group this property survived and keep the active ones. The
 * survivor check stays client-side too, so an API that ignored the filter still
 * yields only this property's groups. `list` is injectable for tests. */
export async function findActivePropertyMergeGroups(
  propertyId: number,
  list: (params: {
    limit: number;
    offset: number;
    survivor_property_id: number;
  }) => Promise<MergesResponse> = listPropertyMerges,
): Promise<MergeGroupScan> {
  const groups: MergeGroup[] = [];
  let scanned = 0;
  for (let page = 0; page < MERGE_LEDGER_MAX_PAGES; page++) {
    const res = await list({
      limit: MERGE_LEDGER_PAGE_SIZE,
      offset: scanned,
      survivor_property_id: propertyId,
    });
    const rows = res.data ?? [];
    scanned += rows.length;
    for (const g of rows) {
      if (g.survivor_property_id === propertyId && !g.fully_undone) groups.push(g);
    }
    if (rows.length < MERGE_LEDGER_PAGE_SIZE) return { groups, scanned, exhaustive: true };
  }
  return { groups, scanned, exhaustive: false };
}

/* A group the AUTODEDUP engine wrote: the apply path's own source, or its reason
 * prefix (PROGRAM.md's E38 sketch writes source 'auto' + reason 'autodedup:…'). */
export function isAutodedupMerge(g: MergeGroup): boolean {
  return g.source === 'autodedup' || (g.reason ?? '').startsWith('autodedup');
}

/* The adjective the confirm copy puts before "sloučení". Explicit per source: an
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

export type UnmergePlan =
  /* Two adverts, one merge of one advert joined them: undoing it separates
   * exactly these two, whichever row asked. */
  | { kind: 'pair'; group: MergeGroup }
  /* One merge explains every advert but the base: undoing it is the only undo
   * there is, and it dissolves the whole property back into its originals. */
  | { kind: 'whole-group'; group: MergeGroup }
  /* Several merges, or one that does not account for every advert: which one
   * brought THIS row in is not knowable from the ledger read. */
  | { kind: 'ambiguous'; groups: MergeGroup[] }
  /* The AUTODEDUP engine made (part of) this property. Undoing its group here
   * would write no must-not-link, so the engine could merge it again, and would
   * leave its applied-merge record standing: never offered from this page. */
  | { kind: 'engine'; groups: MergeGroup[] }
  | { kind: 'not-found'; scanned: number; exhaustive: boolean };

export function planRowUnmerge(scan: MergeGroupScan, rowCount: number): UnmergePlan {
  const { groups } = scan;
  if (groups.length === 0) {
    return { kind: 'not-found', scanned: scan.scanned, exhaustive: scan.exhaustive };
  }
  const engine = groups.filter(isAutodedupMerge);
  if (engine.length > 0) return { kind: 'engine', groups: engine };
  if (groups.length === 1 && groups[0].listings_moved === rowCount - 1) {
    return rowCount === 2
      ? { kind: 'pair', group: groups[0] }
      : { kind: 'whole-group', group: groups[0] };
  }
  return { kind: 'ambiguous', groups };
}

/* Read-your-writes after an unmerge, for the listing page AND every Browse
 * surface. The page's source list is re-resolved from scratch rather than
 * invalidated: its query hands the listing's (pre-unmerge) property_id to
 * fetchPropertySources, so a plain refetch would re-read the OLD property when
 * the advert on screen is the one that moved back. Everything else is keyed off
 * the listing row, which is refetched with it. */
export async function refreshAfterUnmerge(
  qc: QueryClient,
  currentListingId: number,
): Promise<void> {
  await qc.fetchQuery({
    queryKey: propertySourcesKey(currentListingId),
    queryFn: () => fetchPropertySources(currentListingId),
    staleTime: 0,
  });
  for (const key of [
    ['listing'],
    ['property-mf'],
    ['property-status-events'],
    ['snapshots'],
    mergedAdvertsKeys.all,
  ]) {
    qc.invalidateQueries({ queryKey: key });
  }
  invalidateBrowseQueries(qc);
  revalidateCollections(qc);
  revalidatePipeline(qc);
}
