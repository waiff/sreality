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
  47: 'Loft',
  /* dum (houses) */
  33: 'Chalupa',
  35: 'Vila',
  37: 'Rodinný dům',
  39: 'Chata',
  40: 'Mobilní bydlení',
  43: 'Zemědělská usedlost',
  44: 'Činžovní dům',
  54: 'Na klíč',
  /* komercni (commercial) */
  25: 'Kanceláře',
  26: 'Sklady',
  27: 'Výroba',
  28: 'Obchodní prostory',
  29: 'Ubytování',
  30: 'Restaurace',
  31: 'Zemědělský objekt',
  32: 'Ostatní komerční',
  38: 'Činžovní dům',
  56: 'Bytový dům',
  57: 'Sportoviště',
};

export function categorySubLabel(cb: number | null): string | null {
  if (cb == null) return null;
  return CATEGORY_SUB_LABELS[cb] ?? `kategorie #${cb}`;
}

/* Portal-agnostic `subtype` slugs (migration 152), grouped by the
 * category_main they belong to and ordered by prevalence. Labels mirror
 * toolkit/filter_registry.SUBTYPE_OPTIONS; the Browse sidebar renders the
 * group matching the selected category_main. Unlike the legacy numeric
 * CATEGORY_SUB_LABELS above, these are the live-verified labels. */
export interface SubtypeOption {
  slug: string;
  label: string;
}

export const SUBTYPE_LABELS_BY_MAIN: Record<'dum' | 'komercni', SubtypeOption[]> = {
  dum: [
    { slug: 'rodinny_dum', label: 'Rodinný dům' },
    { slug: 'chata', label: 'Chata' },
    { slug: 'chalupa', label: 'Chalupa' },
    { slug: 'vicegeneracni_dum', label: 'Vícegenerační dům' },
    { slug: 'vila', label: 'Vila' },
    { slug: 'zemedelska_usedlost', label: 'Zemědělská usedlost' },
    { slug: 'na_klic', label: 'Na klíč' },
    { slug: 'pamatka_jine', label: 'Památka/jiné' },
  ],
  komercni: [
    { slug: 'kancelar', label: 'Kancelář' },
    { slug: 'sklad', label: 'Sklad' },
    { slug: 'obchodni_prostor', label: 'Obchodní prostor' },
    { slug: 'vyroba', label: 'Výroba' },
    { slug: 'ubytovani', label: 'Ubytování' },
    { slug: 'cinzovni_dum', label: 'Činžovní dům' },
    { slug: 'restaurace', label: 'Restaurace' },
    { slug: 'apartmany', label: 'Apartmány' },
    { slug: 'ordinace', label: 'Ordinace' },
    { slug: 'zemedelsky', label: 'Zemědělský objekt' },
    { slug: 'virtualni_kancelar', label: 'Virtuální kancelář' },
    { slug: 'ostatni', label: 'Ostatní' },
  ],
};

const SUBTYPE_LABEL_BY_SLUG: Record<string, string> = Object.fromEntries(
  [...SUBTYPE_LABELS_BY_MAIN.dum, ...SUBTYPE_LABELS_BY_MAIN.komercni].map(
    (o) => [o.slug, o.label],
  ),
);

export function subtypeLabel(slug: string): string {
  return SUBTYPE_LABEL_BY_SLUG[slug] ?? slug;
}
