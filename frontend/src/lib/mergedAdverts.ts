/* The merged-adverts section on the property page and the pages that link to
 * its split (the proposed splits, the category review, the rulings): their query
 * keys, their words for a merge's origin, the letters as a link and as the split
 * statement, the one receipt toast after a merge or a split, and the refresh
 * after either. The split is ONE dialog on the property page, by letters (MS18):
 * the server's preview, then `POST /properties/{id}/split`. A merge, from Browse
 * or the Rulings page, answers with the same toast slot (`pushMergeReceipt`,
 * MS15). */

import type { QueryClient } from '@tanstack/react-query';

import type {
  MergeCarried,
  MergeResult,
  SplitChoice,
  SplitItemKind,
  SplitLetters,
  SplitResult,
} from '@/lib/api';
import { UNIT_LETTERS, distinctUnits, unitOf, type UnitMap } from '@/components/autodedup/UnitSplit';

import { invalidateBrowseQueries } from '@/lib/browseInvalidation';
import { ROUTES, withQuery, type RoutePath } from '@/lib/routes';
import { revalidateCollections } from '@/lib/collectionCache';
import { czPlural } from '@/lib/format';
import { revalidatePipeline } from '@/lib/pipelineCache';
import { autodedupKeys } from '@/lib/autodedupKeys';
import { hydrationKeys } from '@/lib/hydration/keys';
import { dismissToast, pushToast } from '@/lib/toast';

export const mergedAdvertsKeys = {
  all: ['merged-adverts'] as const,
  listings: (ids: readonly number[]) => ['merged-adverts', 'listings', ids] as const,
  origins: (propertyId: number) => ['merged-adverts', 'origins', propertyId] as const,
  /* The split preview over one statement of letters (`lettersParam`) and the
   * user's picks (`choicesParam`). */
  plan: (propertyId: number, letters: string, choices: string) =>
    ['merged-adverts', 'plan', propertyId, letters, choices] as const,
  /* MS12's count before a merge, over the ticked ids ascending. */
  mergePreview: (ids: readonly number[]) => ['merged-adverts', 'merge-preview', ids] as const,
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

/* ONE slot for the receipt of a merge or a split: a new one replaces the last,
 * so several in a row never stack; it stays until dismissed or opened. */
function pushReceipt(text: string, propertyId: number, open: (propertyId: number) => void): void {
  if (receiptToast != null) dismissToast(receiptToast);
  const id = pushToast('ok', text, 0, {
    label: `Otevřít #${propertyId}`,
    onClick: () => {
      dismissToast(id);
      open(propertyId);
    },
  });
  receiptToast = id;
}

/* The merge's receipt with "Otevřít #S". */
export function pushMergeReceipt(r: MergeResult, open: (propertyId: number) => void): void {
  pushReceipt(mergeReceiptText(r), r.survivor_id, open);
}

/* An item the way the split's lines and its toast name it. */
export function splitItemWords(kind: SplitItemKind, label: string | null): string {
  const named = label ? `„${label}“` : '';
  switch (kind) {
    case 'note':
      return `poznámka ${named}`.trim();
    case 'pipeline':
      return label ? `zařazení v pipeline (${label})` : 'zařazení v pipeline';
    case 'collection':
      return `kolekce ${named}`.trim();
    case 'tag':
      return `štítek ${named}`.trim();
    default:
      return 'skrytí z vašeho Browse';
  }
}

const dvojic = (n: number) => czPlural(n, 'dvojice', 'dvojic', 'dvojic');

/* The one toast after a split (MS18): where each letter landed, which of the
 * acting account's items went to each leaving letter and where copies went, a
 * fold or a copy that was not made, the "Různé" rulings written, the "Stejné"
 * a letter's join ruled (a merge, MS12) and the "Různé" it took back. */
export function splitReceiptText(r: SplitResult): string {
  const kept = r.letters.find((l) => l.lands === 'kept');
  const words = (c: SplitResult['curation'][number]) => splitItemWords(c.kind, c.label);
  const lands = r.letters
    .map((l) =>
      l.lands === 'kept'
        ? `${l.letter} zůstává v #${l.property_id}`
        : `${l.letter} → ${l.lands === 'new' ? 'nová ' : ''}#${l.property_id}`,
    )
    .join('; ');
  const into = r.letters
    .filter((l) => l !== kept)
    .map((l) => {
      const items = r.curation.filter((c) => c.letter === l.letter && !c.skipped).map(words);
      return items.length > 0 ? `Do ${l.letter}: ${items.join(', ')}.` : null;
    });
  const copies = r.letters.map((l) => {
    const items = r.curation
      .filter((c) => c.copies.some((x) => x.letter === l.letter && !x.skipped))
      .map(words);
    return items.length > 0 ? `Kopie do ${l.letter}: ${items.join(', ')}.` : null;
  });
  const folds = r.curation.filter((c) => c.skipped).map(words);
  const uncopied = r.curation.flatMap((c) =>
    c.copies.filter((x) => x.skipped).map((x) => `${words(c)} (do ${x.letter})`),
  );
  const n = r.rulings.different;
  const same = r.rulings.same;
  const joined = r.letters.filter((l) => l.joined).map((l) => l.letter);
  return [
    `Rozděleno: ${lands}.`,
    ...into,
    ...copies,
    folds.length > 0 ? `Neobnoveno: ${folds.join(', ')}.` : null,
    uncopied.length > 0 ? `Kopie nevytvořena: ${uncopied.join(', ')}.` : null,
    `„Různé“ zapsáno u ${n} ${dvojic(n)}.`,
    same > 0 ? `„Stejné“ zapsáno u ${same} ${dvojic(same)} (sloučení písmene ${joined.join(', ')}).` : null,
    r.rulings.taken_back > 0 ? `Zrušená rozhodnutí „Různé“: ${r.rulings.taken_back}.` : null,
  ]
    .filter((part) => part != null)
    .join(' ');
}

/* The split's receipt with "Otevřít #<the first letter that left>". */
export function pushSplitReceipt(r: SplitResult, open: (propertyId: number) => void): void {
  const first = r.letters.find((l) => l.lands !== 'kept') ?? r.letters[0];
  pushReceipt(splitReceiptText(r), first?.property_id ?? r.property_id, open);
}

/* The letters as the preview's query and a page's link, `94020:A,94492:B`. */
export function lettersParam(letters: SplitLetters): string {
  return Object.entries(letters)
    .map(([id, letter]) => [Number(id), letter] as const)
    .sort((a, b) => a[0] - b[0])
    .map(([id, letter]) => `${id}:${letter}`)
    .join(',');
}

/* The user's picks as the preview's query and the click's own shape, keys in
 * order ('' with none). */
export function choicesParam(choices: Record<string, SplitChoice>): string {
  const keys = Object.keys(choices).sort();
  return keys.length > 0 ? JSON.stringify(Object.fromEntries(keys.map((k) => [k, choices[k]]))) : '';
}

/* The property page's split dialog opened on these letters: the one place the
 * review pages send a split (an advert the link leaves out starts at A). */
export function splitPath(propertyId: number, letters: SplitLetters): RoutePath {
  return withQuery(ROUTES.property.build({ propertyId }), { letters: lettersParam(letters) });
}

/* A link's `?letters=` back as letters; a part that is not `id:A–Z` is dropped. */
export function parseLetters(raw: string | null): SplitLetters {
  const out: SplitLetters = {};
  for (const part of (raw ?? '').split(',')) {
    const m = /^(\d{1,15}):([A-Z])$/.exec(part.trim());
    if (m && UNIT_LETTERS.includes(m[2])) out[Number(m[1])] = m[2];
  }
  return out;
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

export interface PlanGroup {
  letter: string;
  /* Ascending. */
  listingIds: number[];
}

export interface SplitPlan {
  /* Every advert shown with its letter: the statement's `letters`. */
  letters: SplitLetters;
  kept: PlanGroup;
  /* Letter order. */
  leaving: PlanGroup[];
}

/* The letters as the split's statement, every advert named, and the group the
 * server's rule keeps on the property, as the client reads it before the preview
 * answers: the group with the most own adverts (no merge brought them), the
 * earliest letter on a tie, the canonical advert's group when none is own. The
 * category review's riders take that letter; the server decides. */
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
    letters: Object.fromEntries(adverts.map((id) => [id, unitOf(units, id)])),
    kept,
    leaving,
  };
}

/* Read-your-writes after a split or a merge, for the property page, the
 * proposals page, the category review AND every Browse surface. The property
 * page is keyed on the property, so a plain invalidation re-reads the property
 * (a separated canonical advert hands the header to the next one) and its advert
 * list; `curation` re-reads notes, tags and their counts, which both move;
 * `mergedAdvertsKeys.all` drops both previews; `adCountsAll` re-counts the
 * Browse cards' ads, a decoration no Browse sweep reaches. */
export function refreshAfterSplit(qc: QueryClient): void {
  for (const key of [
    ['property'],
    ['property-sources'],
    ['snapshots'],
    ['curation'],
    mergedAdvertsKeys.all,
    hydrationKeys.adCountsAll,
    autodedupKeys.proposedSplits,
    autodedupKeys.categorySplits,
  ]) {
    qc.invalidateQueries({ queryKey: key });
  }
  invalidateBrowseQueries(qc);
  revalidateCollections(qc);
  revalidatePipeline(qc);
}
