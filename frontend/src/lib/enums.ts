/* Czech labels for the enum-typed columns added in migration 022.
 * Source codes live in scraper/parser.py:
 *   FURNISHED   {1: ano, 2: ne, 3: castecne}
 *   OWNERSHIP   {1: osobni, 2: druzstevni, 3: statni}
 * The DB stores the slug; the UI renders the diacritic version here.
 *
 * CATEGORY_SUB_LABELS is a lazy bottom-up table — only the codes the
 * SPA actually surfaces today are mapped. Sreality's full taxonomy has
 * ~30 codes; unmapped ones fall back to "kategorie #${cb}" so the UI
 * never shows a blank cell. Add a row whenever a new code surfaces in
 * a frontend filter dropdown. */

import { filterById } from './filterRegistry.generated';
import type { Furnished, Ownership } from './types';

export const FURNISHED_LABELS: Record<Furnished, string> = {
  ano: 'Vybaveno',
  ne: 'Nevybaveno',
  castecne: 'Částečně',
};

export const OWNERSHIP_LABELS: Record<Ownership, string> = {
  osobni: 'Osobní',
  druzstevni: 'Družstevní',
  statni: 'Státní/obecní',
};

/* Czech singular label for a listing's `category_main` (the property type).
 * The single source for the type word shown on cards + the listing tab title. */
export const CATEGORY_MAIN_LABELS: Record<string, string> = {
  byt: 'Byt',
  dum: 'Dům',
  komercni: 'Komerční prostor',
  pozemek: 'Pozemek',
  ostatni: 'Ostatní',
};

export function categoryMainLabel(cm: string | null | undefined): string {
  return (cm && CATEGORY_MAIN_LABELS[cm]) || 'Nemovitost';
}

/* Plural Czech label for category_main aggregate headings ("Byty", "Domy") and
 * the deal-type word ("prodej", "pronájem") — both derived from the filter
 * registry (the registry's category_main labels are plural, its category_type
 * labels are the canonical Czech words), so Health + region rollups share one
 * source with the Browse filters instead of a private hardcoded copy. Distinct
 * from categoryMainLabel's singular title noun. */
const CATEGORY_MAIN_PLURAL: Record<string, string> = Object.fromEntries(
  (filterById('category_main')?.enum_values ?? []).map((o) => [String(o.value), o.label_cs]),
);
const CATEGORY_TYPE_LABELS: Record<string, string> = Object.fromEntries(
  (filterById('category_type')?.enum_values ?? []).map((o) => [String(o.value), o.label_cs]),
);

export function categoryMainLabelPlural(cm: string | null | undefined): string {
  return (cm && CATEGORY_MAIN_PLURAL[cm]) || (cm ?? '—');
}

export function categoryTypeLabel(ct: string | null | undefined): string {
  return (ct && CATEGORY_TYPE_LABELS[ct]) || (ct ?? '—');
}

/* category_sub_cb meanings differ across category_main. We key by the
 * sreality cb value alone since the codes don't collide across the
 * three category_main values we surface (1=byt, 2=dum, 4=komercni)
 * — they live in disjoint integer ranges. */
export const CATEGORY_SUB_LABELS: Record<number, string> = {
  /* byt (apartments) — disposition-shaped subtypes */
  2:  '1+kk',
  3:  '1+1',
  4:  '2+kk',
  5:  '2+1',
  6:  '3+kk',
  7:  '3+1',
  8:  '4+kk',
  9:  '4+1',
  10: '5+kk',
  11: '5+1',
  12: '6 a více',
  16: 'Atypický',
  47: 'Pokoj',
  /* dum (houses) */
  33: 'Chata',
  35: 'Památka/jiné',
  37: 'Rodinný dům',
  39: 'Vila',
  40: 'Na klíč',
  43: 'Chalupa',
  44: 'Zemědělská usedlost',
  54: 'Vícegenerační dům',
  /* komercni (commercial) */
  25: 'Kanceláře',
  26: 'Sklady',
  27: 'Výroba',
  28: 'Obchodní prostory',
  29: 'Ubytování',
  30: 'Restaurace',
  31: 'Zemědělský objekt',
  32: 'Ostatní',
  38: 'Činžovní dům',
  49: 'Virtuální kancelář',
  56: 'Ordinace',
  57: 'Apartmány',
};

export function categorySubLabel(cb: number | null): string | null {
  if (cb == null) return null;
  return CATEGORY_SUB_LABELS[cb] ?? `kategorie #${cb}`;
}

/* Portal-agnostic `subtype` slugs (migration 152), grouped by the
 * category_main they belong to. Single source: the generated filter registry
 * (toolkit/filter_registry.SUBTYPE_OPTIONS), whose `group` field carries the
 * dum/komercni partition the Browse sidebar renders by selected category_main.
 * Derived here rather than hand-typed so a slug/label added in the Python
 * registry can never drift from the SPA (CI's codegen --check is the guard). */
export interface SubtypeOption {
  slug: string;
  label: string;
}

const SUBTYPE_OPTIONS: SubtypeOption[] = (
  filterById('subtype')?.enum_values ?? []
).map((o) => ({ slug: String(o.value), label: o.label_cs }));

export const SUBTYPE_LABELS_BY_MAIN: Record<'dum' | 'komercni', SubtypeOption[]> = {
  dum: (filterById('subtype')?.enum_values ?? [])
    .filter((o) => o.group === 'dum')
    .map((o) => ({ slug: String(o.value), label: o.label_cs })),
  komercni: (filterById('subtype')?.enum_values ?? [])
    .filter((o) => o.group === 'komercni')
    .map((o) => ({ slug: String(o.value), label: o.label_cs })),
};

const SUBTYPE_LABEL_BY_SLUG: Record<string, string> = Object.fromEntries(
  SUBTYPE_OPTIONS.map((o) => [o.slug, o.label]),
);

export function subtypeLabel(slug: string): string {
  return SUBTYPE_LABEL_BY_SLUG[slug] ?? slug;
}

/* --- listing identity: the "what kind" descriptor --------------------------
 * `disposition` (2+kk, apartments) and `subtype` (Kancelář / Rodinný dům,
 * commercial + houses) are two complementary faces of the same descriptor,
 * partitioned by category_main: apartments carry disposition (subtype NULL),
 * houses/commercial carry subtype (disposition usually NULL), land carries
 * neither. These helpers are THE single place that resolves them so every
 * surface (card title, detail header, table, map, watchdog, …) agrees. */
export interface ListingKindFields {
  subtype?: string | null;
  disposition?: string | null;
}

/* Ordered identity tokens, most-specific first: the subtype label (when the
 * portal resolved one) then disposition. Both appear for the rare house listed
 * by room count (e.g. "Rodinný dům", "5+kk"); empty for land. */
export function listingKindParts(row: ListingKindFields): string[] {
  const parts: string[] = [];
  if (row.subtype) parts.push(subtypeLabel(row.subtype));
  if (row.disposition) parts.push(row.disposition);
  return parts;
}

/* The single most-specific identity token, or null. Apartments → "2+kk",
 * commercial/houses → "Ubytování"/"Rodinný dům", land → null. Replaces the
 * bare `disposition ?? '—'` that rendered a dash for every commercial row. */
export function listingKindLabel(row: ListingKindFields): string | null {
  return listingKindParts(row)[0] ?? null;
}

/* The noun for a listing TITLE ("Ubytování na prodej"): the specific subtype
 * when present, else the generic category_main word ("Byt"/"Komerční prostor").
 * Never null — always yields a usable title noun. */
export function listingTypeLabel(
  row: ListingKindFields & { category_main?: string | null },
): string {
  return row.subtype ? subtypeLabel(row.subtype) : categoryMainLabel(row.category_main);
}
