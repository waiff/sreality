import type { Disposition, Furnished, Ownership } from './types';

export type TriState = 'any' | 'yes' | 'no';
export type SeenWithin = '1d' | '7d' | '30d' | 'any';
export type ListingStatus = 'active' | 'inactive' | 'any';

export interface ListingFilters {
  districts: string[];
  dispositions: Disposition[];
  priceMin: number | null;
  priceMax: number | null;
  areaMin: number | null;
  areaMax: number | null;
  status: ListingStatus;
  seenWithin: SeenWithin;
  hasBalcony: TriState;
  hasLift: TriState;
  hasParking: TriState;
  /* Migration 022 — granular amenities and category-relevant fields. */
  terrace: TriState;
  cellar: TriState;
  garage: TriState;
  furnished: Furnished | null;
  ownership: Ownership | null;
  categorySubCb: number | null;
  estateAreaMin: number | null;
  estateAreaMax: number | null;
  usableAreaMin: number | null;
  usableAreaMax: number | null;
  parkingLotsMin: number | null;
  /* Migration 025 — operator tags. AND-semantics: a listing must carry
   * every selected tag id. Stored as ids (not names) so renames /
   * recolour-by-delete-recreate stay queryable. */
  tags: number[];
}

export const DEFAULT_FILTERS: ListingFilters = {
  districts: [],
  dispositions: [],
  priceMin: null,
  priceMax: null,
  areaMin: null,
  areaMax: null,
  status: 'active',
  seenWithin: '7d',
  hasBalcony: 'any',
  hasLift: 'any',
  hasParking: 'any',
  terrace: 'any',
  cellar: 'any',
  garage: 'any',
  furnished: null,
  ownership: null,
  categorySubCb: null,
  estateAreaMin: null,
  estateAreaMax: null,
  usableAreaMin: null,
  usableAreaMax: null,
  parkingLotsMin: null,
  tags: [],
};

export const ESTATE_AREA_BOUNDS = { min: 0, max: 5000, step: 50 };
export const USABLE_AREA_BOUNDS = { min: 0, max: 500, step: 5 };

export const PRICE_BOUNDS = { min: 0, max: 100_000, step: 500 };
export const AREA_BOUNDS = { min: 0, max: 300, step: 5 };

const ALL_DISPOSITIONS: ReadonlyArray<Disposition> = [
  '1+kk', '1+1', '2+kk', '2+1',
  '3+kk', '3+1', '4+kk', '4+1',
  '5+kk', '5+1',
];

const SEEN_VALUES: ReadonlyArray<SeenWithin> = ['1d', '7d', '30d', 'any'];
const TRI_VALUES: ReadonlyArray<TriState> = ['any', 'yes', 'no'];
const STATUS_VALUES: ReadonlyArray<ListingStatus> = ['active', 'inactive', 'any'];
const FURNISHED_VALUES: ReadonlyArray<Furnished> = ['ano', 'ne', 'castecne'];
const OWNERSHIP_VALUES: ReadonlyArray<Ownership> = ['osobni', 'druzstevni', 'statni'];

const splitCsv = (s: string | null): string[] =>
  s == null || s === '' ? [] : s.split(',').map(decodeURIComponent);

const joinCsv = (xs: string[]): string => xs.map(encodeURIComponent).join(',');

const parseInt0 = (s: string | null): number | null => {
  if (s == null || s === '') return null;
  const n = Number(s);
  return Number.isFinite(n) && n >= 0 ? n : null;
};

const parseRange = (s: string | null): [number | null, number | null] => {
  if (!s) return [null, null];
  const [a, b] = s.split('-');
  return [parseInt0(a ?? null), parseInt0(b ?? null)];
};

const enumOr = <T extends string>(
  v: string | null,
  values: ReadonlyArray<T>,
  fallback: T,
): T => (v != null && (values as ReadonlyArray<string>).includes(v) ? (v as T) : fallback);

const enumOrNull = <T extends string>(
  v: string | null,
  values: ReadonlyArray<T>,
): T | null =>
  v != null && (values as ReadonlyArray<string>).includes(v) ? (v as T) : null;

const parseIntOrNull = (s: string | null): number | null => {
  if (s == null || s === '') return null;
  const n = Number(s);
  return Number.isFinite(n) ? Math.trunc(n) : null;
};

export const fromSearchParams = (sp: URLSearchParams): ListingFilters => {
  const dispRaw = splitCsv(sp.get('disposition'));
  const dispositions = dispRaw.filter((d): d is Disposition =>
    (ALL_DISPOSITIONS as ReadonlyArray<string>).includes(d),
  );
  const [priceMin, priceMax] = parseRange(sp.get('price'));
  const [areaMin, areaMax] = parseRange(sp.get('area'));
  const [estateMin, estateMax] = parseRange(sp.get('estate'));
  const [usableMin, usableMax] = parseRange(sp.get('usable'));
  const legacyAny: ListingStatus = sp.get('active') === '0' ? 'any' : 'active';
  return {
    districts: splitCsv(sp.get('districts')),
    dispositions,
    priceMin,
    priceMax,
    areaMin,
    areaMax,
    status: enumOr(sp.get('status'), STATUS_VALUES, legacyAny),
    seenWithin: enumOr(sp.get('since'), SEEN_VALUES, '7d'),
    hasBalcony: enumOr(sp.get('balcony'), TRI_VALUES, 'any'),
    hasLift: enumOr(sp.get('lift'), TRI_VALUES, 'any'),
    hasParking: enumOr(sp.get('parking'), TRI_VALUES, 'any'),
    terrace: enumOr(sp.get('terrace'), TRI_VALUES, 'any'),
    cellar: enumOr(sp.get('cellar'), TRI_VALUES, 'any'),
    garage: enumOr(sp.get('garage'), TRI_VALUES, 'any'),
    furnished: enumOrNull(sp.get('furnished'), FURNISHED_VALUES),
    ownership: enumOrNull(sp.get('ownership'), OWNERSHIP_VALUES),
    categorySubCb: parseIntOrNull(sp.get('subcat')),
    estateAreaMin: estateMin,
    estateAreaMax: estateMax,
    usableAreaMin: usableMin,
    usableAreaMax: usableMax,
    parkingLotsMin: parseIntOrNull(sp.get('parking_min')),
    tags: parseIntList(sp.get('tags')),
  };
};

const parseIntList = (s: string | null): number[] => {
  if (!s) return [];
  const out: number[] = [];
  for (const part of s.split(',')) {
    const n = Number(part);
    if (Number.isInteger(n) && n > 0) out.push(n);
  }
  return out;
};

export const toSearchParams = (f: ListingFilters): URLSearchParams => {
  const sp = new URLSearchParams();
  if (f.districts.length) sp.set('districts', joinCsv(f.districts));
  if (f.dispositions.length) sp.set('disposition', f.dispositions.join(','));
  if (f.priceMin != null || f.priceMax != null) {
    sp.set('price', `${f.priceMin ?? ''}-${f.priceMax ?? ''}`);
  }
  if (f.areaMin != null || f.areaMax != null) {
    sp.set('area', `${f.areaMin ?? ''}-${f.areaMax ?? ''}`);
  }
  if (f.status !== 'active') sp.set('status', f.status);
  if (f.seenWithin !== '7d') sp.set('since', f.seenWithin);
  if (f.hasBalcony !== 'any') sp.set('balcony', f.hasBalcony);
  if (f.hasLift !== 'any') sp.set('lift', f.hasLift);
  if (f.hasParking !== 'any') sp.set('parking', f.hasParking);
  if (f.terrace !== 'any') sp.set('terrace', f.terrace);
  if (f.cellar !== 'any') sp.set('cellar', f.cellar);
  if (f.garage !== 'any') sp.set('garage', f.garage);
  if (f.furnished) sp.set('furnished', f.furnished);
  if (f.ownership) sp.set('ownership', f.ownership);
  if (f.categorySubCb != null) sp.set('subcat', String(f.categorySubCb));
  if (f.estateAreaMin != null || f.estateAreaMax != null) {
    sp.set('estate', `${f.estateAreaMin ?? ''}-${f.estateAreaMax ?? ''}`);
  }
  if (f.usableAreaMin != null || f.usableAreaMax != null) {
    sp.set('usable', `${f.usableAreaMin ?? ''}-${f.usableAreaMax ?? ''}`);
  }
  if (f.parkingLotsMin != null) sp.set('parking_min', String(f.parkingLotsMin));
  if (f.tags.length) sp.set('tags', f.tags.join(','));
  return sp;
};

export const seenWithinToIso = (s: SeenWithin): string | null => {
  if (s === 'any') return null;
  const days = s === '1d' ? 1 : s === '7d' ? 7 : 30;
  return new Date(Date.now() - days * 86_400_000).toISOString();
};

export const summarise = (f: ListingFilters, count: number | null): string => {
  const bits: string[] = [];
  bits.push(f.status === 'active' ? 'active' : f.status === 'inactive' ? 'inactive' : 'all');
  bits.push(`${count == null ? '…' : count.toLocaleString('cs-CZ')} listings`);
  if (f.districts.length) {
    const shown = f.districts.slice(0, 3).join(', ');
    const extra = f.districts.length > 3 ? ` +${f.districts.length - 3}` : '';
    bits.push(`in ${shown}${extra}`);
  }
  if (f.dispositions.length) {
    bits.push(`(${f.dispositions.slice(0, 4).join(', ')}${f.dispositions.length > 4 ? '…' : ''})`);
  }
  if (f.seenWithin !== 'any') {
    const human = f.seenWithin === '1d' ? '24 h' : f.seenWithin === '7d' ? '7 days' : '30 days';
    bits.push(`seen within ${human}`);
  }
  return `Showing ${bits.join(' ')}`;
};

export const isDefault = (f: ListingFilters): boolean =>
  f.districts.length === 0 &&
  f.dispositions.length === 0 &&
  f.priceMin == null &&
  f.priceMax == null &&
  f.areaMin == null &&
  f.areaMax == null &&
  f.status === 'active' &&
  f.seenWithin === '7d' &&
  f.hasBalcony === 'any' &&
  f.hasLift === 'any' &&
  f.hasParking === 'any' &&
  f.terrace === 'any' &&
  f.cellar === 'any' &&
  f.garage === 'any' &&
  f.furnished == null &&
  f.ownership == null &&
  f.categorySubCb == null &&
  f.estateAreaMin == null &&
  f.estateAreaMax == null &&
  f.usableAreaMin == null &&
  f.usableAreaMax == null &&
  f.parkingLotsMin == null &&
  f.tags.length === 0;
