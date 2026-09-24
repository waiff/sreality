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
 * and the only read of the ledger is `GET /properties/merges` — every group in the
 * system, newest first, with its survivor and how many adverts it moved, but NOT
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
/* The per-row 'Rozdělit' write, admin sessions only. Off: rows carry no action. */
export const MERGED_ADVERTS_UNMERGE_ENABLED = false;

/* The ledger is read newest first, a page at a time, only after the operator asks
 * to split — never on page load. A merge older than this window is reported as
 * "not found here", not searched for across the whole ledger (six figures of
 * events; one full scan per click is not a page-load cost this page may pay). */
export const MERGE_LEDGER_PAGE_SIZE = 200;
export const MERGE_LEDGER_MAX_PAGES = 5;

export const mergedAdvertsKeys = {
  all: ['merged-adverts'] as const,
  listings: (ids: readonly number[]) => ['merged-adverts', 'listings', ids] as const,
  images: (ids: readonly number[]) => ['merged-adverts', 'images', ids] as const,
  /* The row count is part of the key: the ledger read stops early once the groups
   * found account for every advert but one, so a scan for 3 rows and one for 2
   * are different reads. */
  groups: (propertyId: number, rowCount: number) =>
    ['merged-adverts', 'merge-groups', propertyId, rowCount] as const,
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
  /* Ledger groups read, of any property. */
  scanned: number;
  /* True when the read reached the end of the ledger — "none found" is then a
   * fact about the property, not about the window. */
  exhaustive: boolean;
}

/* Page the ledger newest-first for this property's active groups. Stops early
 * once the groups found account for every advert but one (no further active
 * group can still hold an advert of this property), at the end of the ledger, or
 * at the page cap. `list` is injectable for tests. */
export async function findActivePropertyMergeGroups(
  propertyId: number,
  rowCount: number,
  list: (params: { limit: number; offset: number }) => Promise<MergesResponse> =
    listPropertyMerges,
): Promise<MergeGroupScan> {
  const groups: MergeGroup[] = [];
  let scanned = 0;
  for (let page = 0; page < MERGE_LEDGER_MAX_PAGES; page++) {
    const res = await list({ limit: MERGE_LEDGER_PAGE_SIZE, offset: scanned });
    const rows = res.data ?? [];
    scanned += rows.length;
    for (const g of rows) {
      if (g.survivor_property_id === propertyId && !g.fully_undone) groups.push(g);
    }
    if (rows.length === 0) return { groups, scanned, exhaustive: true };
    const moved = groups.reduce((sum, g) => sum + g.listings_moved, 0);
    if (moved >= rowCount - 1) return { groups, scanned, exhaustive: false };
    if (rows.length < MERGE_LEDGER_PAGE_SIZE) return { groups, scanned, exhaustive: true };
  }
  return { groups, scanned, exhaustive: false };
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
  | { kind: 'not-found'; scanned: number; exhaustive: boolean };

export function planRowUnmerge(scan: MergeGroupScan, rowCount: number): UnmergePlan {
  const { groups } = scan;
  if (groups.length === 0) {
    return { kind: 'not-found', scanned: scan.scanned, exhaustive: scan.exhaustive };
  }
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
