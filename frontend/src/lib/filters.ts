import type { Disposition, Furnished, Ownership } from './types';

export type TriState = 'any' | 'yes' | 'no';
export type ListingStatus = 'active' | 'inactive' | 'any';

/* The three category_main values surfaced as filters in the UI. The DB
 * also stores 'pozemek' (land) and 'ostatni' (other), but the scrape /
 * toolkit only target the apartments / houses / commercial trio. */
export type CategoryMain = 'byt' | 'dum' | 'komercni';

/* CHECK constraint on listings.category_type allows pronajem / prodej /
 * drazba / podil; only the first two are user-facing in Browse. */
export type CategoryType = 'pronajem' | 'prodej';

/* Building material buckets surfaced in the filter panel. Maps to
 * one or more sreality building_type values via BUILDING_MATERIAL_VALUES
 * below. */
export type BuildingMaterial = 'cihla' | 'panel' | 'smisena' | 'ostatni';

/* Map-viewport rectangle. west < east, south < north, all WGS84
 * degrees. Acts as an additional filter alongside the sidebar fields:
 * cards / table / stats all narrow to listings whose (lng, lat) falls
 * inside the rectangle. NULL = no map area applied. */
export interface MapBounds {
  west: number;
  south: number;
  east: number;
  north: number;
}

export interface ListingFilters {
  categoryMain: CategoryMain;
  categoryType: CategoryType;
  districts: string[];
  dispositions: Disposition[];
  priceMin: number | null;
  priceMax: number | null;
  areaMin: number | null;
  areaMax: number | null;
  status: ListingStatus;
  /* Days-ago range on last_seen_at. min = most recent allowed (so
   * lastSeenMinDays=3 hides listings seen in the last 2 days);
   * max = oldest allowed. Either end null = unbounded. Replaces the
   * 1d/7d/30d/any preset. */
  lastSeenMinDays: number | null;
  lastSeenMaxDays: number | null;
  /* Days-ago range on first_seen_at — same semantics. */
  firstSeenMinDays: number | null;
  firstSeenMaxDays: number | null;
  /* Days on market (= last_seen_at - first_seen_at, or now() -
   * first_seen_at for active listings). Surfaced as tom_days on
   * listings_public via migration 052. */
  tomDaysMin: number | null;
  tomDaysMax: number | null;
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
  buildingMaterial: BuildingMaterial | null;
  estateAreaMin: number | null;
  estateAreaMax: number | null;
  usableAreaMin: number | null;
  usableAreaMax: number | null;
  parkingLotsMin: number | null;
  /* Migration 025 — operator tags. AND-semantics: a listing must carry
   * every selected tag id. Stored as ids (not names) so renames /
   * recolour-by-delete-recreate stay queryable. */
  tags: number[];
  bounds: MapBounds | null;
}

export const DEFAULT_FILTERS: ListingFilters = {
  categoryMain: 'byt',
  categoryType: 'pronajem',
  districts: [],
  dispositions: [],
  priceMin: null,
  priceMax: null,
  areaMin: null,
  areaMax: null,
  status: 'any',
  lastSeenMinDays: null,
  lastSeenMaxDays: null,
  firstSeenMinDays: null,
  firstSeenMaxDays: null,
  tomDaysMin: null,
  tomDaysMax: null,
  hasBalcony: 'any',
  hasLift: 'any',
  hasParking: 'any',
  terrace: 'any',
  cellar: 'any',
  garage: 'any',
  furnished: null,
  ownership: null,
  categorySubCb: null,
  buildingMaterial: null,
  estateAreaMin: null,
  estateAreaMax: null,
  usableAreaMin: null,
  usableAreaMax: null,
  parkingLotsMin: null,
  tags: [],
  bounds: null,
};

export const ESTATE_AREA_BOUNDS = { min: 0, max: 5000, step: 50 };
export const USABLE_AREA_BOUNDS = { min: 0, max: 500, step: 5 };

export const PRICE_BOUNDS = { min: 0, max: 100_000, step: 500 };
export const AREA_BOUNDS = { min: 0, max: 300, step: 5 };

/* The "Ostatní" bucket expands to every sreality building_type value
 * that isn't in the explicit three. Listings with a NULL building_type
 * fall out of any non-null selection — matching how furnished /
 * ownership filters already behave. */
export const BUILDING_MATERIAL_OTHER_VALUES = [
  'skelet', 'drevo', 'kamen', 'montovana', 'nizkoenergeticka',
] as const;

export const buildingMaterialToValues = (
  m: BuildingMaterial,
): readonly string[] => {
  if (m === 'cihla')   return ['cihla'];
  if (m === 'panel')   return ['panel'];
  if (m === 'smisena') return ['smisena'];
  return BUILDING_MATERIAL_OTHER_VALUES;
};

const ALL_DISPOSITIONS: ReadonlyArray<Disposition> = [
  '1+kk', '1+1', '2+kk', '2+1',
  '3+kk', '3+1', '4+kk', '4+1',
  '5+kk', '5+1',
];

const TRI_VALUES: ReadonlyArray<TriState> = ['any', 'yes', 'no'];
const STATUS_VALUES: ReadonlyArray<ListingStatus> = ['active', 'inactive', 'any'];
const FURNISHED_VALUES: ReadonlyArray<Furnished> = ['ano', 'ne', 'castecne'];
const OWNERSHIP_VALUES: ReadonlyArray<Ownership> = ['osobni', 'druzstevni', 'statni'];
const CATEGORY_MAIN_VALUES: ReadonlyArray<CategoryMain> = ['byt', 'dum', 'komercni'];
const CATEGORY_TYPE_VALUES: ReadonlyArray<CategoryType> = ['pronajem', 'prodej'];
const BUILDING_MATERIAL_VALUES: ReadonlyArray<BuildingMaterial> = [
  'cihla', 'panel', 'smisena', 'ostatni',
];

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
  const [lastMin, lastMax] = parseRange(sp.get('seen'));
  const [firstMin, firstMax] = parseRange(sp.get('first'));
  const [tomMin, tomMax] = parseRange(sp.get('tom'));
  /* Legacy ?active=0 from pre-status-enum URLs. The newer ?status= wins. */
  const legacyStatus: ListingStatus = sp.get('active') === '0' ? 'any' : 'any';
  return {
    categoryMain: enumOr(sp.get('cat'), CATEGORY_MAIN_VALUES, 'byt'),
    categoryType: enumOr(sp.get('deal'), CATEGORY_TYPE_VALUES, 'pronajem'),
    districts: splitCsv(sp.get('districts')),
    dispositions,
    priceMin,
    priceMax,
    areaMin,
    areaMax,
    status: enumOr(sp.get('status'), STATUS_VALUES, legacyStatus),
    lastSeenMinDays: lastMin,
    lastSeenMaxDays: lastMax,
    firstSeenMinDays: firstMin,
    firstSeenMaxDays: firstMax,
    tomDaysMin: tomMin,
    tomDaysMax: tomMax,
    hasBalcony: enumOr(sp.get('balcony'), TRI_VALUES, 'any'),
    hasLift: enumOr(sp.get('lift'), TRI_VALUES, 'any'),
    hasParking: enumOr(sp.get('parking'), TRI_VALUES, 'any'),
    terrace: enumOr(sp.get('terrace'), TRI_VALUES, 'any'),
    cellar: enumOr(sp.get('cellar'), TRI_VALUES, 'any'),
    garage: enumOr(sp.get('garage'), TRI_VALUES, 'any'),
    furnished: enumOrNull(sp.get('furnished'), FURNISHED_VALUES),
    ownership: enumOrNull(sp.get('ownership'), OWNERSHIP_VALUES),
    categorySubCb: parseIntOrNull(sp.get('subcat')),
    buildingMaterial: enumOrNull(sp.get('build'), BUILDING_MATERIAL_VALUES),
    estateAreaMin: estateMin,
    estateAreaMax: estateMax,
    usableAreaMin: usableMin,
    usableAreaMax: usableMax,
    parkingLotsMin: parseIntOrNull(sp.get('parking_min')),
    tags: parseIntList(sp.get('tags')),
    bounds: parseBounds(sp.get('bbox')),
  };
};

const parseBounds = (s: string | null): MapBounds | null => {
  if (!s) return null;
  const parts = s.split(',');
  if (parts.length !== 4) return null;
  const [w, sLat, e, n] = parts.map(Number);
  if (![w, sLat, e, n].every((x) => Number.isFinite(x))) return null;
  if (w >= e || sLat >= n) return null;
  return { west: w, south: sLat, east: e, north: n };
};

const fmtBoundsCoord = (n: number): string => Number(n.toFixed(5)).toString();

const parseIntList = (s: string | null): number[] => {
  if (!s) return [];
  const out: number[] = [];
  for (const part of s.split(',')) {
    const n = Number(part);
    if (Number.isInteger(n) && n > 0) out.push(n);
  }
  return out;
};

const fmtRange = (lo: number | null, hi: number | null): string =>
  `${lo ?? ''}-${hi ?? ''}`;

export const toSearchParams = (f: ListingFilters): URLSearchParams => {
  const sp = new URLSearchParams();
  if (f.categoryMain !== 'byt') sp.set('cat', f.categoryMain);
  if (f.categoryType !== 'pronajem') sp.set('deal', f.categoryType);
  if (f.districts.length) sp.set('districts', joinCsv(f.districts));
  if (f.dispositions.length) sp.set('disposition', f.dispositions.join(','));
  if (f.priceMin != null || f.priceMax != null) {
    sp.set('price', fmtRange(f.priceMin, f.priceMax));
  }
  if (f.areaMin != null || f.areaMax != null) {
    sp.set('area', fmtRange(f.areaMin, f.areaMax));
  }
  if (f.status !== 'any') sp.set('status', f.status);
  if (f.lastSeenMinDays != null || f.lastSeenMaxDays != null) {
    sp.set('seen', fmtRange(f.lastSeenMinDays, f.lastSeenMaxDays));
  }
  if (f.firstSeenMinDays != null || f.firstSeenMaxDays != null) {
    sp.set('first', fmtRange(f.firstSeenMinDays, f.firstSeenMaxDays));
  }
  if (f.tomDaysMin != null || f.tomDaysMax != null) {
    sp.set('tom', fmtRange(f.tomDaysMin, f.tomDaysMax));
  }
  if (f.hasBalcony !== 'any') sp.set('balcony', f.hasBalcony);
  if (f.hasLift !== 'any') sp.set('lift', f.hasLift);
  if (f.hasParking !== 'any') sp.set('parking', f.hasParking);
  if (f.terrace !== 'any') sp.set('terrace', f.terrace);
  if (f.cellar !== 'any') sp.set('cellar', f.cellar);
  if (f.garage !== 'any') sp.set('garage', f.garage);
  if (f.furnished) sp.set('furnished', f.furnished);
  if (f.ownership) sp.set('ownership', f.ownership);
  if (f.categorySubCb != null) sp.set('subcat', String(f.categorySubCb));
  if (f.buildingMaterial) sp.set('build', f.buildingMaterial);
  if (f.estateAreaMin != null || f.estateAreaMax != null) {
    sp.set('estate', fmtRange(f.estateAreaMin, f.estateAreaMax));
  }
  if (f.usableAreaMin != null || f.usableAreaMax != null) {
    sp.set('usable', fmtRange(f.usableAreaMin, f.usableAreaMax));
  }
  if (f.parkingLotsMin != null) sp.set('parking_min', String(f.parkingLotsMin));
  if (f.tags.length) sp.set('tags', f.tags.join(','));
  if (f.bounds) {
    const { west, south, east, north } = f.bounds;
    sp.set(
      'bbox',
      `${fmtBoundsCoord(west)},${fmtBoundsCoord(south)},${fmtBoundsCoord(east)},${fmtBoundsCoord(north)}`,
    );
  }
  return sp;
};

/* Convert a "days ago" integer to an ISO timestamp for PostgREST
 * predicates. n=7 -> seven days ago. */
export const isoNDaysAgo = (days: number): string =>
  new Date(Date.now() - days * 86_400_000).toISOString();

const CATEGORY_MAIN_PLURAL: Record<CategoryMain, string> = {
  byt: 'apartments',
  dum: 'houses',
  komercni: 'commercial',
};

const CATEGORY_TYPE_LABEL: Record<CategoryType, string> = {
  pronajem: 'for rent',
  prodej: 'for sale',
};

export const categoryHeading = (f: ListingFilters): string =>
  `${CATEGORY_MAIN_PLURAL[f.categoryMain]} ${CATEGORY_TYPE_LABEL[f.categoryType]}`;

const fmtDaysRange = (lo: number | null, hi: number | null): string => {
  if (lo == null && hi == null) return '';
  if (lo != null && hi != null) return `${lo}–${hi} d`;
  if (lo != null)               return `≥ ${lo} d`;
  return `≤ ${hi} d`;
};

export const summarise = (f: ListingFilters, count: number | null): string => {
  const bits: string[] = [];
  bits.push(f.status === 'active' ? 'active' : f.status === 'inactive' ? 'inactive' : 'all');
  bits.push(`${count == null ? '…' : count.toLocaleString('cs-CZ')} ${CATEGORY_MAIN_PLURAL[f.categoryMain]}`);
  bits.push(CATEGORY_TYPE_LABEL[f.categoryType]);
  if (f.districts.length) {
    const shown = f.districts.slice(0, 3).join(', ');
    const extra = f.districts.length > 3 ? ` +${f.districts.length - 3}` : '';
    bits.push(`in ${shown}${extra}`);
  }
  if (f.dispositions.length) {
    bits.push(`(${f.dispositions.slice(0, 4).join(', ')}${f.dispositions.length > 4 ? '…' : ''})`);
  }
  const seenLabel = fmtDaysRange(f.lastSeenMinDays, f.lastSeenMaxDays);
  if (seenLabel) bits.push(`last seen ${seenLabel}`);
  const tomLabel = fmtDaysRange(f.tomDaysMin, f.tomDaysMax);
  if (tomLabel) bits.push(`TOM ${tomLabel}`);
  if (f.bounds) bits.push('in this map area');
  return `Showing ${bits.join(' ')}`;
};

export const isDefault = (f: ListingFilters): boolean =>
  f.categoryMain === 'byt' &&
  f.categoryType === 'pronajem' &&
  f.districts.length === 0 &&
  f.dispositions.length === 0 &&
  f.priceMin == null &&
  f.priceMax == null &&
  f.areaMin == null &&
  f.areaMax == null &&
  f.status === 'any' &&
  f.lastSeenMinDays == null &&
  f.lastSeenMaxDays == null &&
  f.firstSeenMinDays == null &&
  f.firstSeenMaxDays == null &&
  f.tomDaysMin == null &&
  f.tomDaysMax == null &&
  f.hasBalcony === 'any' &&
  f.hasLift === 'any' &&
  f.hasParking === 'any' &&
  f.terrace === 'any' &&
  f.cellar === 'any' &&
  f.garage === 'any' &&
  f.furnished == null &&
  f.ownership == null &&
  f.categorySubCb == null &&
  f.buildingMaterial == null &&
  f.estateAreaMin == null &&
  f.estateAreaMax == null &&
  f.usableAreaMin == null &&
  f.usableAreaMax == null &&
  f.parkingLotsMin == null &&
  f.tags.length === 0 &&
  f.bounds == null;
