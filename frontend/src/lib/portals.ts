/* Source-portal display labels, sourced from the canonical filter
 * registry's `portals` enum so the Browse card badge, the filter
 * chips, and any future surface stay in lockstep with the backend
 * `listings.source` vocabulary. Falls back to the raw source code
 * (capitalised) for a portal not yet in the registry enum. */
import { CATEGORY_SUB_LABELS } from './enums';
import { filterById } from './filterRegistry.generated';

const PORTAL_LABELS: Record<string, string> = Object.fromEntries(
  (filterById('portals')?.enum_values ?? []).map((o) => [o.value, o.label_cs]),
);

export function portalLabel(source: string | null | undefined): string | null {
  if (!source) return null;
  return PORTAL_LABELS[source] ?? source.charAt(0).toUpperCase() + source.slice(1);
}

/* Short, hand-tuned portal names for compact chrome — the Health page's
 * per-run "Site" tag and the /dedup review card's portal chips. Diacritic
 * spellings (Bazoš) the registry's machine labels don't carry. Falls back to
 * the registry label, then the raw source code. */
export const PORTAL_SHORT_LABEL: Record<string, string> = {
  sreality: 'Sreality',
  bazos: 'Bazoš',
  idnes: 'iDNES',
};

export function portalShort(source: string): string {
  return PORTAL_SHORT_LABEL[source] ?? portalLabel(source) ?? source;
}

/* The category triple a sreality detail URL needs. The text fields are the
 * seo slugs sreality itself uses (we store them verbatim: 'prodej', 'komercni',
 * 'byt', …); categorySubCb is the integer cb code we map to its slug. */
export interface SrealityCategory {
  categoryType?: string | null;
  categoryMain?: string | null;
  categorySubCb?: number | null;
}

/* Slug for the sub-category segment of a sreality detail URL, derived from the
 * cb→label table. Lowercase, diacritics stripped (NFKD then drop the combining
 * marks), spaces→hyphens; the '+' in disposition labels is preserved because
 * sreality's slugs keep it ("Činžovní dům" → "cinzovni-dum", "2+1" → "2+1").
 * Returns null for an unmapped code so the caller emits no link rather than a
 * guaranteed-404 one. */
function srealitySubSlug(categorySubCb: number | null | undefined): string | null {
  if (categorySubCb == null) return null;
  const label = CATEGORY_SUB_LABELS[categorySubCb];
  if (!label) return null;
  return label
    .normalize('NFKD')
    .replace(/\p{Mn}/gu, '')
    .toLowerCase()
    .trim()
    .replace(/\s+/g, '-');
}

/* Build a working sreality detail URL.
 *
 * sreality's modern site does NOT resolve a listing by id alone: a path with
 * the wrong (or placeholder "x") category_type / category_main / sub-category
 * 404s. Only `/detail/{type}/{main}/{sub}/{locality}/{id}` reaches the page —
 * and the *locality* segment is the one sreality is lenient about (it 301-
 * redirects any value, including "x", to the canonical slug). So we supply the
 * real type/main/sub and "x" for the locality and let sreality canonicalise.
 * Returns null when we lack the category triple, so callers fall back to the
 * in-app listing view instead of linking to a guaranteed 404. */
export function srealityListingUrl(
  nativeId: string | number | null | undefined,
  category: SrealityCategory,
): string | null {
  const id = nativeId == null ? '' : String(nativeId).trim();
  const sub = srealitySubSlug(category.categorySubCb);
  if (!id || !category.categoryType || !category.categoryMain || !sub) return null;
  return `https://www.sreality.cz/detail/${category.categoryType}/${category.categoryMain}/${sub}/x/${id}`;
}

/* The best external link to a listing on its origin portal.
 *
 * Prefer the stored `source_url` (set by the bazos / bezrealitky / idnes /
 * mmreality scrapers and the on-demand URL parser). sreality's scraper stores
 * none, so we reconstruct it from the category triple via `srealityListingUrl`
 * (which needs `srealityCategory`). Returns null when we can't build a
 * resolvable external URL — so the caller falls back to the in-app listing view
 * rather than linking to a sreality 404. */
export function portalListingUrl(
  source: string,
  sourceUrl: string | null | undefined,
  nativeId: string | number | null | undefined,
  srealityCategory?: SrealityCategory,
): string | null {
  if (sourceUrl) return sourceUrl;
  if (source === 'sreality') {
    return srealityListingUrl(nativeId, srealityCategory ?? {});
  }
  return null;
}
