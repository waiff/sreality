import type { Disposition } from './types';
import {
  DEFAULT_WATCHDOG_FILTER_SPEC,
  type WatchdogFilterSpec,
} from './types';
import { fmtArea } from './format';

export type TriState = 'any' | 'yes' | 'no';
export type ListingStatus = 'active' | 'inactive' | 'any';

/* All five category_main values. Browse + Watchdog MULTISELECT them
 * (`ListingFilters.categoryMain` is an array, wired to the registry's
 * `category_main_in` filter); the analytical surfaces still anchor a cohort
 * to ONE category via the scalar `category_main` filter. */
export type CategoryMain = 'byt' | 'dum' | 'komercni' | 'pozemek' | 'ostatni';

/* CHECK constraint on listings.category_type allows all four. Deal-type stays
 * SINGLE-select in Browse — rent (monthly Kč) and sale (total Kč) are different
 * price scales, so mixing them would make the price tooling cross-scale. */
export type CategoryType = 'pronajem' | 'prodej' | 'drazba' | 'podil';

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

/* Operator-set point + radius for the "dot + circle on the map" mode.
 * When `locationMode === 'center_radius'` the cohort is filtered by an
 * approximate bbox around (lat, lng) within `radius_m` metres; the
 * sidebar's <LocationControl> owns the point + radius UI and the main
 * map renders a dashed circle for visual context. Viewport bounds are
 * ignored in this mode. */
export interface CenterRadius {
  lat: number;
  lng: number;
  radius_m: number;
}

export type LocationMode = 'viewport' | 'center_radius';

/* A WGS84 bounding box roughly `km` kilometres across (half-extent each
 * side), centred on (lat, lng). Longitude degrees shrink with latitude so
 * the east/west extent scales by cos(lat). Used to frame the map AND seed
 * the viewport cohort when opening Browse focused on a single point (the
 * "Explore area" modal) — the same `bounds` value both fits the camera and
 * narrows the cohort, so no separate flyTo is needed. */
export const bboxAround = (lat: number, lng: number, km: number): MapBounds => {
  const half = km / 2;
  const dLat = half / 111.32;
  const dLng = half / (111.32 * Math.max(Math.cos((lat * Math.PI) / 180), 0.01));
  return { west: lng - dLng, south: lat - dLat, east: lng + dLng, north: lat + dLat };
};

/* Phase QUAL — one entry in `cityIndexRules`. Snake_case keys mirror
 * the wire shape consumed by the `listings_with_city_quality` RPC
 * and `api/notifications._build_match_clauses`, so the same picker
 * output flows unchanged to Browse and Watchdog. `index_name` is the
 * slug from `city_index_definitions_public` (e.g. `bezpecnost`,
 * `prakticti_lekari`); `value` is the threshold the city must meet
 * under `op` (defaults to `>=`). Multiple rules AND. */
export interface CityIndexRule {
  index_name: string;
  op?: '>=' | '<=' | '==' | '!=' | '>' | '<';
  value: number;
}

/* Phase QUAL — composite "within X km of a city matching Y" filter.
 * Snake_case keys for the same wire-parity reason. `index_rules`
 * is the inner per-city criterion (same shape as `CityIndexRule[]`);
 * `population_min` is an optional minimum for the matching city;
 * `radius_km` is the spatial range to allow listings around any
 * matching city. */
export interface NearCityProximity {
  index_rules: CityIndexRule[];
  population_min: number | null;
  radius_km: number;
}

/* One entry of the district chip list. A resolved chip (level + id)
 * matches by STABLE ADMIN ID at its level; a 'locality' (street/POI)
 * pick additionally matches `name` as an ILIKE substring on
 * `place_search_text` (street + locality combined, migration 182);
 * a legacy chip (no level/id) falls back to the `name` ILIKE across
 * `district` / `place_search_text` / `okres` / `region`. `context` is
 * the parent municipality from Mapy.cz's `regionalStructure` that
 * narrows the legacy match when set, so picking the Plzeň entry for
 * "Edvarda Beneše" doesn't drag in the Olomouc + Hradec Králové
 * streets of the same name. Picks at the municipality / okres / kraj
 * level (or coarser) leave context null and behave exactly like the
 * pre-context chips. `excluded` flips the chip from an INCLUDE to an
 * EXCLUDE filter: an excluded chip removes its matches from the cohort
 * instead of requiring them (NOT-ed in the query, red in the UI).
 * Absent / false = the legacy include behaviour. The same shape is
 * sent to the watchdog matcher
 * (`api/notifications.WatchdogFilterSpec.districts`) so Browse and
 * Watchdog stay aligned via the shared filter registry. */
export type LocationLevel = 'obec' | 'okres' | 'kraj' | 'locality';

export interface DistrictChip {
  name: string;
  context: string | null;
  excluded?: boolean;
  /* Resolved admin level of the pick (from `/maps/resolve`). Absent = a legacy
   * or unresolved chip, matched by name ILIKE (the pre-resolution behaviour). */
  level?: LocationLevel;
  /* admin_boundaries.id for an admin level, or the containing obec_id for a
   * 'locality' chip. Null/absent = unresolved → legacy name match. */
  id?: number | null;
}

/* One active price-stats growth filter: keep listings whose obec meets/exceeds
 * the entered rent/sale growth p.a. (CAGR ≥), computed for the [fromYm, toYm]
 * window of a chosen dataset by the price_stat_growth RPC. Multiple rules AND.
 * BROWSE-only (window-dependent, live-computed). */
export interface PriceGrowthRule {
  datasetId: number;
  fromYm: string | null;   // 'YYYY-MM' or null = dataset's full window
  toYm: string | null;
  rentMinPct: number | null;
  saleMinPct: number | null;
}

export interface ListingFilters {
  /* Multi-select (Browse + Watchdog): a property matches if its category_main
   * is in the list. Default ['byt']; empty [] = no category constraint. */
  categoryMain: CategoryMain[];
  /* Single-select deal type (rent / sale / auction / fractional). */
  categoryType: CategoryType;
  districts: DistrictChip[];
  dispositions: Disposition[];
  priceMin: number | null;
  priceMax: number | null;
  /* When true AND a price bound is set, KEEP no-price listings
   * (price_czk IS NULL) instead of dropping them via SQL three-valued
   * logic. A no-op when no bound is set. Browse + Watchdog only. */
  includeNoPrice: boolean;
  /* Price per m² bounds (price_czk / area_m2). Computed on
   * listings_public; toolkit / matcher re-derive from the raw columns.
   * NULL area_m2 rows fall out when either bound is set. */
  pricePerM2Min: number | null;
  pricePerM2Max: number | null;
  /* MF gross rental yield % bounds (migration 133). Sale apartments only;
   * NULL on everything else, so non-matching listings fall out when set. */
  mfGrossYieldPctMin: number | null;
  mfGrossYieldPctMax: number | null;
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
  /* Status-section recency presets (one of 1/3/7/14/30, or null = any).
   * `recentlyAddedDays` → first_seen_at >= now() - N days (the preset twin of
   * firstSeenMaxDays). `recentlyChangedDays` → last_change_at >= now() - N days
   * (properties.last_change_at = newest content snapshot). Browse-only. */
  recentlyAddedDays: number | null;
  recentlyChangedDays: number | null;
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
  /* Multi-select enums. The special '__unknown__' value matches listings whose
   * value is NULL or stored under a non-canonical label (see FURNISHED_VALUES /
   * UNKNOWN_FILTER_VALUE and queries.ts:applyFilters). Empty = no constraint. */
  furnished: string[];
  ownership: string[];
  portals: string[];
  conditionMatch: string[];
  categorySubCb: number | null;
  /* Portal-agnostic property sub-type (multi-select). Only surfaced for
   * categoryMain in (dum, komercni); see SUBTYPE_LABELS_BY_MAIN. */
  subtype: string[];
  buildingMaterial: BuildingMaterial[];
  estateAreaMin: number | null;
  estateAreaMax: number | null;
  usableAreaMin: number | null;
  usableAreaMax: number | null;
  parkingLotsMin: number | null;
  /* Migration 022 — house listings carry a separate garden area
   * (`garden_area`) distinct from the lot area (`estate_area`). Wired
   * to the registry; not yet exposed in the Browse sidebar UI
   * (the Filters.tsx `includeOnly` for the Size group would need to
   * include them), but settable via URL params and honoured by
   * Watchdog. */
  gardenAreaMin: number | null;
  gardenAreaMax: number | null;
  /* Derived condition scores (migrations 072 / 073). 1..5 each; rows
   * with NULL (not yet scored) are excluded from the result when a
   * bound is set. Set by toolkit.condition_scoring.score_listing_condition. */
  buildingConditionLevelMin: number | null;
  buildingConditionLevelMax: number | null;
  apartmentConditionLevelMin: number | null;
  apartmentConditionLevelMax: number | null;
  /* Price-history signals (migration 173). Derived columns on `properties`,
   * maintained by the recompute job; property grain, applied against
   * properties_public. `priceChangeCountMin` counts cuts AND raises across
   * the union of a property's snapshot history, inside the
   * `priceChangeWindowDays` window (30/90/365, null = all time).
   * `totalPriceChangePct` is signed: -10 = "dropped 10%+ overall from the
   * first observed price", +10 = "rose 10%+ overall". */
  priceChangeCountMin: number | null;
  priceChangeWindowDays: number | null;
  totalPriceChangePct: number | null;
  /* Browse-only: restrict to properties with at least one successful
   * estimation run (property_estimates_public, migration 173). */
  withEstimates: boolean;
  /* Migration 025 — operator tags. AND-semantics: a listing must carry
   * every selected tag id. Stored as ids (not names) so renames /
   * recolour-by-delete-recreate stay queryable. */
  tags: number[];
  bounds: MapBounds | null;
  /* `viewport` (default) = map pan/zoom emits bounds, those filter
   * the cohort. `center_radius` = a sidebar-set point + radius drives
   * the spatial predicate; bounds is ignored on the SQL side. */
  locationMode: LocationMode;
  centerRadius: CenterRadius | null;
  /* Phase QUAL — curated-city quality filters. Browse + Watchdog only;
   * intentionally not surfaced to the estimation agent / comparables
   * tool (the registry's agenda gating enforces that). The Browse
   * map renders matching cities as a separate pin layer on top of
   * the listing dots, and the listing query is restricted to the
   * cities' footprints via the `listings_with_city_quality` RPC. */
  cityIndexRules: CityIndexRule[];
  minCityPopulation: number | null;
  maxCityPopulation: number | null;
  nearCityProximity: NearCityProximity | null;
  /* Fast polygon-edge proximity (migration 142). Each is a precomputed
   * `properties` column filtered as `>= value`: the MAX metric found within
   * a fixed 5 / 15 km of the listing (population of obce >= 10k, or one of
   * the three curated-city indexes). No per-request spatial RPC. */
  nearPop5kmMin: number | null;
  nearPop15kmMin: number | null;
  nearJobs5kmMin: number | null;
  nearJobs15kmMin: number | null;
  nearYouth5kmMin: number | null;
  nearYouth15kmMin: number | null;
  nearOverall5kmMin: number | null;
  nearOverall15kmMin: number | null;
  priceGrowthRules: PriceGrowthRule[];
}

export const DEFAULT_FILTERS: ListingFilters = {
  categoryMain: ['byt'],
  categoryType: 'pronajem',
  districts: [],
  dispositions: [],
  priceMin: null,
  priceMax: null,
  includeNoPrice: false,
  pricePerM2Min: null,
  pricePerM2Max: null,
  mfGrossYieldPctMin: null,
  mfGrossYieldPctMax: null,
  areaMin: null,
  areaMax: null,
  status: 'any',
  lastSeenMinDays: null,
  lastSeenMaxDays: null,
  firstSeenMinDays: null,
  firstSeenMaxDays: null,
  recentlyAddedDays: null,
  recentlyChangedDays: null,
  tomDaysMin: null,
  tomDaysMax: null,
  hasBalcony: 'any',
  hasLift: 'any',
  hasParking: 'any',
  terrace: 'any',
  cellar: 'any',
  garage: 'any',
  furnished: [],
  ownership: [],
  portals: [],
  conditionMatch: [],
  categorySubCb: null,
  subtype: [],
  buildingMaterial: [],
  estateAreaMin: null,
  estateAreaMax: null,
  usableAreaMin: null,
  usableAreaMax: null,
  parkingLotsMin: null,
  gardenAreaMin: null,
  gardenAreaMax: null,
  buildingConditionLevelMin: null,
  buildingConditionLevelMax: null,
  apartmentConditionLevelMin: null,
  apartmentConditionLevelMax: null,
  priceChangeCountMin: null,
  priceChangeWindowDays: null,
  totalPriceChangePct: null,
  withEstimates: false,
  tags: [],
  bounds: null,
  locationMode: 'viewport',
  centerRadius: null,
  cityIndexRules: [],
  minCityPopulation: null,
  maxCityPopulation: null,
  nearCityProximity: null,
  nearPop5kmMin: null,
  nearPop15kmMin: null,
  nearJobs5kmMin: null,
  nearJobs15kmMin: null,
  nearYouth5kmMin: null,
  nearYouth15kmMin: null,
  nearOverall5kmMin: null,
  nearOverall15kmMin: null,
  priceGrowthRules: [],
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

const buildingMaterialBucketToValues = (
  m: BuildingMaterial,
): readonly string[] => {
  if (m === 'cihla')   return ['cihla'];
  if (m === 'panel')   return ['panel'];
  if (m === 'smisena') return ['smisena'];
  return BUILDING_MATERIAL_OTHER_VALUES;
};

/* Expand a multi-select of operator-friendly buckets into the union of
 * granular `building_type` values to match against (deduped). Empty in,
 * empty out — the caller skips the predicate entirely in that case. */
export const buildingMaterialToValues = (
  materials: readonly BuildingMaterial[],
): readonly string[] => [
  ...new Set(materials.flatMap((m) => buildingMaterialBucketToValues(m))),
];

const ALL_DISPOSITIONS: ReadonlyArray<Disposition> = [
  '1+kk', '1+1', '2+kk', '2+1',
  '3+kk', '3+1', '4+kk', '4+1',
  '5+kk', '5+1',
];

const TRI_VALUES: ReadonlyArray<TriState> = ['any', 'yes', 'no'];
const STATUS_VALUES: ReadonlyArray<ListingStatus> = ['active', 'inactive', 'any'];
/* Multi-select wire values incl. the '__unknown__' sentinel (NULL / non-canonical).
 * The canonical sets mirror toolkit.filter_registry.{FURNISHED,OWNERSHIP}_CANONICAL
 * and are what queries.ts:applyFilters uses for the `not.in.(…)` unknown predicate. */
export const UNKNOWN_FILTER_VALUE = '__unknown__';
export const FURNISHED_CANONICAL = ['ano', 'ne', 'castecne'] as const;
export const OWNERSHIP_CANONICAL = ['osobni', 'druzstevni', 'statni'] as const;
const FURNISHED_VALUES: ReadonlyArray<string> = [...FURNISHED_CANONICAL, UNKNOWN_FILTER_VALUE];
const OWNERSHIP_VALUES: ReadonlyArray<string> = [...OWNERSHIP_CANONICAL, UNKNOWN_FILTER_VALUE];
const CONDITION_VALUES: ReadonlyArray<string> = [
  'novostavba', 'po_rekonstrukci', 'velmi_dobry',
  'dobry', 'pred_rekonstrukci', 'k_demolici',
];
const CATEGORY_MAIN_VALUES: ReadonlyArray<CategoryMain> = [
  'byt', 'dum', 'komercni', 'pozemek', 'ostatni',
];
/* The single-element default cohort. `cat` is omitted from the URL when the
 * selection equals this, and emitted as an empty `cat=` token for [] (all). */
const DEFAULT_CATEGORY_MAIN: ReadonlyArray<CategoryMain> = ['byt'];
const CATEGORY_TYPE_VALUES: ReadonlyArray<CategoryType> = [
  'pronajem', 'prodej', 'drazba', 'podil',
];
const BUILDING_MATERIAL_VALUES: ReadonlyArray<BuildingMaterial> = [
  'cihla', 'panel', 'smisena', 'ostatni',
];

const splitCsv = (s: string | null): string[] =>
  s == null || s === '' ? [] : s.split(',').map(decodeURIComponent);

const joinCsv = (xs: string[]): string => xs.map(encodeURIComponent).join(',');

const _LOCATION_LEVELS: ReadonlyArray<LocationLevel> = [
  'obec', 'okres', 'kraj', 'locality',
];

/* Parse the parallel `districts` (names) + `districts_ctx` (contexts) +
 * `districts_excl` (exclude flags) + `districts_lvl` (admin level) +
 * `districts_id` (admin id) query params into a `DistrictChip[]`.
 * Empty-string entries in the contexts CSV stand for "no context for
 * this chip" — that's how we keep the URL clean when only some chips
 * carry a parent. Missing `districts_ctx` entirely means every chip has
 * `context: null`, matching the legacy URL shape (`?districts=Praha`).
 * `districts_excl` is a parallel CSV of `1`/`0`; absent means every chip
 * is an include (legacy). `districts_lvl` / `districts_id` are absent for
 * legacy / unresolved chips, which then fall back to name matching — so the
 * schema only ever widens. */
const parseDistrictChips = (
  namesRaw: string | null,
  ctxRaw: string | null,
  exclRaw: string | null,
  lvlRaw: string | null,
  idRaw: string | null,
): DistrictChip[] => {
  const names = splitCsv(namesRaw);
  if (names.length === 0) return [];
  const ctxs = splitCsv(ctxRaw);
  const excls = splitCsv(exclRaw);
  const lvls = splitCsv(lvlRaw);
  const ids = splitCsv(idRaw);
  return names.map((name, i) => {
    const ctx = ctxs[i];
    const chip: DistrictChip = {
      name,
      context: ctx == null || ctx === '' ? null : ctx,
    };
    if (excls[i] === '1') chip.excluded = true;
    const lvl = lvls[i];
    if (lvl != null && (_LOCATION_LEVELS as ReadonlyArray<string>).includes(lvl)) {
      chip.level = lvl as LocationLevel;
      const rawId = ids[i];
      const n = rawId == null || rawId === '' ? null : Number(rawId);
      chip.id = n != null && Number.isFinite(n) ? n : null;
    }
    return chip;
  });
};

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

const parseIntOrNull = (s: string | null): number | null => {
  if (s == null || s === '') return null;
  const n = Number(s);
  return Number.isFinite(n) ? Math.trunc(n) : null;
};

const parseFloatOrNull = (s: string | null): number | null => {
  if (s == null || s === '') return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
};

/* Legal `priceChangeWindowDays` values — must mirror the registry's
 * `price_change_window_days` enum and the precomputed properties columns
 * (price_change_count_30d / _90d / _365d, migration 173). Null = all time. */
export const PRICE_CHANGE_WINDOWS = [30, 90, 365] as const;

const parsePriceChangeWindow = (s: string | null): number | null => {
  const n = parseIntOrNull(s);
  return n != null && (PRICE_CHANGE_WINDOWS as ReadonlyArray<number>).includes(n)
    ? n
    : null;
};

/* Which precomputed properties_public count column `priceChangeCountMin`
 * reads for a given window. Mirrors the backend's canonical
 * `toolkit.filter_registry.PRICE_CHANGE_COUNT_COLUMNS` (the watchdog
 * matcher) and the CASE inside browse_stats_properties (migration 173). */
export const priceChangeCountColumn = (windowDays: number | null): string => {
  switch (windowDays) {
    case 30: return 'price_change_count_30d';
    case 90: return 'price_change_count_90d';
    case 365: return 'price_change_count_365d';
    default: return 'price_change_count';
  }
};

export const fromSearchParams = (sp: URLSearchParams): ListingFilters => {
  const dispRaw = splitCsv(sp.get('disposition'));
  const dispositions = dispRaw.filter((d): d is Disposition =>
    (ALL_DISPOSITIONS as ReadonlyArray<string>).includes(d),
  );
  const [priceMin, priceMax] = parseRange(sp.get('price'));
  const [ppm2Min, ppm2Max] = parseRange(sp.get('ppm2'));
  const [yieldMin, yieldMax] = parseRange(sp.get('yield'));
  const [areaMin, areaMax] = parseRange(sp.get('area'));
  const [estateMin, estateMax] = parseRange(sp.get('estate'));
  const [usableMin, usableMax] = parseRange(sp.get('usable'));
  const [lastMin, lastMax] = parseRange(sp.get('seen'));
  const [firstMin, firstMax] = parseRange(sp.get('first'));
  const [tomMin, tomMax] = parseRange(sp.get('tom'));
  /* Legacy ?active=0 from pre-status-enum URLs. The newer ?status= wins. */
  const legacyStatus: ListingStatus = sp.get('active') === '0' ? 'any' : 'any';
  return {
    /* Absent `cat` = the default cohort (['byt']); an empty `cat=` token =
     * the deliberate "all categories" ([]). Otherwise CSV, filtered to the
     * known set (so an old ?cat=dum still parses, and junk is dropped). */
    categoryMain: sp.get('cat') == null
      ? [...DEFAULT_CATEGORY_MAIN]
      : splitCsv(sp.get('cat')).filter((c): c is CategoryMain =>
          (CATEGORY_MAIN_VALUES as ReadonlyArray<string>).includes(c)),
    categoryType: enumOr(sp.get('deal'), CATEGORY_TYPE_VALUES, 'pronajem'),
    districts: parseDistrictChips(
      sp.get('districts'),
      sp.get('districts_ctx'),
      sp.get('districts_excl'),
      sp.get('districts_lvl'),
      sp.get('districts_id'),
    ),
    dispositions,
    priceMin,
    priceMax,
    includeNoPrice: sp.get('no_price') === '1',
    pricePerM2Min: ppm2Min,
    pricePerM2Max: ppm2Max,
    mfGrossYieldPctMin: yieldMin,
    mfGrossYieldPctMax: yieldMax,
    areaMin,
    areaMax,
    status: enumOr(sp.get('status'), STATUS_VALUES, legacyStatus),
    lastSeenMinDays: lastMin,
    lastSeenMaxDays: lastMax,
    firstSeenMinDays: firstMin,
    firstSeenMaxDays: firstMax,
    recentlyAddedDays: parseIntOrNull(sp.get('added')),
    recentlyChangedDays: parseIntOrNull(sp.get('changed')),
    tomDaysMin: tomMin,
    tomDaysMax: tomMax,
    hasBalcony: enumOr(sp.get('balcony'), TRI_VALUES, 'any'),
    hasLift: enumOr(sp.get('lift'), TRI_VALUES, 'any'),
    hasParking: enumOr(sp.get('parking'), TRI_VALUES, 'any'),
    terrace: enumOr(sp.get('terrace'), TRI_VALUES, 'any'),
    cellar: enumOr(sp.get('cellar'), TRI_VALUES, 'any'),
    garage: enumOr(sp.get('garage'), TRI_VALUES, 'any'),
    furnished: splitCsv(sp.get('furnished')).filter((v) => FURNISHED_VALUES.includes(v)),
    ownership: splitCsv(sp.get('ownership')).filter((v) => OWNERSHIP_VALUES.includes(v)),
    portals: splitCsv(sp.get('portal')),
    conditionMatch: splitCsv(sp.get('condition')).filter(
      (c) => CONDITION_VALUES.includes(c),
    ),
    categorySubCb: parseIntOrNull(sp.get('subcat')),
    subtype: splitCsv(sp.get('subtype')),
    buildingMaterial: splitCsv(sp.get('build')).filter((m): m is BuildingMaterial =>
      (BUILDING_MATERIAL_VALUES as ReadonlyArray<string>).includes(m),
    ),
    estateAreaMin: estateMin,
    estateAreaMax: estateMax,
    usableAreaMin: usableMin,
    usableAreaMax: usableMax,
    parkingLotsMin: parseIntOrNull(sp.get('parking_min')),
    gardenAreaMin: parseIntOrNull(sp.get('garden_min')),
    gardenAreaMax: parseIntOrNull(sp.get('garden_max')),
    buildingConditionLevelMin: parseIntOrNull(sp.get('bld_cond_min')),
    buildingConditionLevelMax: parseIntOrNull(sp.get('bld_cond_max')),
    apartmentConditionLevelMin: parseIntOrNull(sp.get('apt_cond_min')),
    apartmentConditionLevelMax: parseIntOrNull(sp.get('apt_cond_max')),
    priceChangeCountMin: parseIntOrNull(sp.get('changes_min')),
    priceChangeWindowDays: parsePriceChangeWindow(sp.get('changes_window')),
    totalPriceChangePct: parseFloatOrNull(sp.get('total_change_pct')),
    withEstimates: sp.get('with_est') === '1',
    tags: parseIntList(sp.get('tags')),
    bounds: parseBounds(sp.get('bbox')),
    locationMode: sp.get('locmode') === 'center_radius'
      ? 'center_radius'
      : 'viewport',
    centerRadius: parseCenterRadius(sp.get('center')),
    cityIndexRules: parseCityIndexRules(sp.get('cq_rules')),
    minCityPopulation: parseIntOrNull(sp.get('cq_pop_min')),
    maxCityPopulation: parseIntOrNull(sp.get('cq_pop_max')),
    nearCityProximity: parseNearCityProximity(sp.get('cq_prox')),
    nearPop5kmMin: parseIntOrNull(sp.get('np5')),
    nearPop15kmMin: parseIntOrNull(sp.get('np15')),
    nearJobs5kmMin: parseFloatOrNull(sp.get('nj5')),
    nearJobs15kmMin: parseFloatOrNull(sp.get('nj15')),
    nearYouth5kmMin: parseFloatOrNull(sp.get('ny5')),
    nearYouth15kmMin: parseFloatOrNull(sp.get('ny15')),
    nearOverall5kmMin: parseFloatOrNull(sp.get('no5')),
    nearOverall15kmMin: parseFloatOrNull(sp.get('no15')),
    priceGrowthRules: parsePriceGrowthRules(sp.get('pg')),
  };
};

/* `cq_rules` URL shape: `indexName:value[:op],indexName:value[:op]`.
 * `op` defaults to `>=` when omitted, which is the only operator the
 * Browse UI exposes (matches Watchdog parity). */
const _VALID_OPS = new Set(['>=', '<=', '==', '!=', '>', '<']);
const parseCityIndexRules = (s: string | null): CityIndexRule[] => {
  if (!s) return [];
  const out: CityIndexRule[] = [];
  for (const raw of s.split(',')) {
    const parts = raw.split(':');
    if (parts.length < 2) continue;
    const [name, valStr, opStr] = parts;
    if (!name) continue;
    const value = Number(valStr);
    if (!Number.isFinite(value)) continue;
    const op = opStr && _VALID_OPS.has(opStr)
      ? (opStr as CityIndexRule['op'])
      : '>=';
    out.push({ index_name: name, op, value });
  }
  return out;
};

const fmtCityIndexRules = (rules: CityIndexRule[]): string =>
  rules
    .map((r) => {
      const op = r.op && r.op !== '>=' ? `:${r.op}` : '';
      return `${r.index_name}:${r.value}${op}`;
    })
    .join(',');

/* `pg` URL shape: rules joined by `;`, each `datasetId:fromYm:toYm:rentMin:saleMin`.
 * fromYm/toYm are 'YYYY-MM' (no colons) or empty; rent/sale min empty = no bound. */
const _YM_RE = /^\d{4}-\d{2}$/;
const parsePriceGrowthRules = (s: string | null): PriceGrowthRule[] => {
  if (!s) return [];
  const out: PriceGrowthRule[] = [];
  for (const raw of s.split(';')) {
    const parts = raw.split(':');
    const datasetId = Number(parts[0]);
    if (!Number.isInteger(datasetId)) continue;
    const ym = (v: string | undefined): string | null => (v && _YM_RE.test(v) ? v : null);
    const num = (v: string | undefined): number | null => {
      if (v == null || v === '') return null;
      const n = Number(v);
      return Number.isFinite(n) ? n : null;
    };
    out.push({
      datasetId,
      fromYm: ym(parts[1]),
      toYm: ym(parts[2]),
      rentMinPct: num(parts[3]),
      saleMinPct: num(parts[4]),
    });
  }
  return out;
};

const fmtPriceGrowthRules = (rules: PriceGrowthRule[]): string =>
  rules
    .map((r) =>
      [r.datasetId, r.fromYm ?? '', r.toYm ?? '', r.rentMinPct ?? '', r.saleMinPct ?? ''].join(':'),
    )
    .join(';');

/* `cq_prox` URL shape: `radius_km|index_name:value,...|pop_min`.
 * `pop_min` is empty when null. */
const parseNearCityProximity = (s: string | null): NearCityProximity | null => {
  if (!s) return null;
  const parts = s.split('|');
  if (parts.length < 2) return null;
  const radiusKm = Number(parts[0]);
  if (!Number.isFinite(radiusKm) || radiusKm <= 0) return null;
  const indexRules = parseCityIndexRules(parts[1] ?? '');
  const popMin = parts[2] && parts[2] !== ''
    ? parseIntOrNull(parts[2])
    : null;
  return {
    radius_km: Math.trunc(radiusKm),
    index_rules: indexRules,
    population_min: popMin,
  };
};

const fmtNearCityProximity = (p: NearCityProximity): string => {
  const rules = fmtCityIndexRules(p.index_rules);
  const pop = p.population_min == null ? '' : String(p.population_min);
  return `${p.radius_km}|${rules}|${pop}`;
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

const parseCenterRadius = (s: string | null): CenterRadius | null => {
  if (!s) return null;
  const parts = s.split(',');
  if (parts.length !== 3) return null;
  const [lat, lng, radius] = parts.map(Number);
  if (![lat, lng, radius].every((x) => Number.isFinite(x))) return null;
  if (radius <= 0) return null;
  return { lat, lng, radius_m: Math.trunc(radius) };
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

const fmtRange = (lo: number | null, hi: number | null): string =>
  `${lo ?? ''}-${hi ?? ''}`;

export const toSearchParams = (f: ListingFilters): URLSearchParams => {
  const sp = new URLSearchParams();
  /* Emit `cat` whenever the set differs from the default ['byt'] — including
   * the empty set ("all categories"), serialised as an empty `cat=` so a
   * deliberate "all" round-trips distinctly from the omitted default. */
  if (!(f.categoryMain.length === 1 && f.categoryMain[0] === 'byt')) {
    sp.set('cat', f.categoryMain.join(','));
  }
  if (f.categoryType !== 'pronajem') sp.set('deal', f.categoryType);
  if (f.districts.length) {
    sp.set('districts', joinCsv(f.districts.map((d) => d.name)));
    /* Omit the contexts param when every chip's context is null —
     * keeps URLs for the common okres-only case identical to the
     * pre-context shape. */
    if (f.districts.some((d) => d.context !== null)) {
      sp.set(
        'districts_ctx',
        joinCsv(f.districts.map((d) => d.context ?? '')),
      );
    }
    /* Same discipline for the exclude flags: only emit when at least one
     * chip is excluded, so an all-include filter's URL is unchanged. */
    if (f.districts.some((d) => d.excluded)) {
      sp.set(
        'districts_excl',
        joinCsv(f.districts.map((d) => (d.excluded ? '1' : '0'))),
      );
    }
    /* Resolved admin level + id (the precise match path). Emit both as
     * full-length parallel CSVs only when at least one chip is resolved, so a
     * pre-resolution / legacy filter's URL stays byte-identical to before. */
    if (f.districts.some((d) => d.level != null)) {
      sp.set('districts_lvl', joinCsv(f.districts.map((d) => d.level ?? '')));
      sp.set(
        'districts_id',
        joinCsv(f.districts.map((d) => (d.id == null ? '' : String(d.id)))),
      );
    }
  }
  if (f.dispositions.length) sp.set('disposition', f.dispositions.join(','));
  if (f.priceMin != null || f.priceMax != null) {
    sp.set('price', fmtRange(f.priceMin, f.priceMax));
  }
  if (f.includeNoPrice) sp.set('no_price', '1');
  if (f.pricePerM2Min != null || f.pricePerM2Max != null) {
    sp.set('ppm2', fmtRange(f.pricePerM2Min, f.pricePerM2Max));
  }
  if (f.mfGrossYieldPctMin != null || f.mfGrossYieldPctMax != null) {
    sp.set('yield', fmtRange(f.mfGrossYieldPctMin, f.mfGrossYieldPctMax));
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
  if (f.recentlyAddedDays != null) sp.set('added', String(f.recentlyAddedDays));
  if (f.recentlyChangedDays != null) sp.set('changed', String(f.recentlyChangedDays));
  if (f.tomDaysMin != null || f.tomDaysMax != null) {
    sp.set('tom', fmtRange(f.tomDaysMin, f.tomDaysMax));
  }
  if (f.hasBalcony !== 'any') sp.set('balcony', f.hasBalcony);
  if (f.hasLift !== 'any') sp.set('lift', f.hasLift);
  if (f.hasParking !== 'any') sp.set('parking', f.hasParking);
  if (f.terrace !== 'any') sp.set('terrace', f.terrace);
  if (f.cellar !== 'any') sp.set('cellar', f.cellar);
  if (f.garage !== 'any') sp.set('garage', f.garage);
  if (f.furnished.length) sp.set('furnished', f.furnished.join(','));
  if (f.ownership.length) sp.set('ownership', f.ownership.join(','));
  if (f.portals.length) sp.set('portal', f.portals.join(','));
  if (f.conditionMatch.length) sp.set('condition', f.conditionMatch.join(','));
  if (f.categorySubCb != null) sp.set('subcat', String(f.categorySubCb));
  if (f.subtype.length) sp.set('subtype', f.subtype.join(','));
  if (f.buildingMaterial.length) sp.set('build', f.buildingMaterial.join(','));
  if (f.estateAreaMin != null || f.estateAreaMax != null) {
    sp.set('estate', fmtRange(f.estateAreaMin, f.estateAreaMax));
  }
  if (f.usableAreaMin != null || f.usableAreaMax != null) {
    sp.set('usable', fmtRange(f.usableAreaMin, f.usableAreaMax));
  }
  if (f.parkingLotsMin != null) sp.set('parking_min', String(f.parkingLotsMin));
  if (f.gardenAreaMin != null) sp.set('garden_min', String(f.gardenAreaMin));
  if (f.gardenAreaMax != null) sp.set('garden_max', String(f.gardenAreaMax));
  if (f.buildingConditionLevelMin != null) sp.set('bld_cond_min', String(f.buildingConditionLevelMin));
  if (f.buildingConditionLevelMax != null) sp.set('bld_cond_max', String(f.buildingConditionLevelMax));
  if (f.apartmentConditionLevelMin != null) sp.set('apt_cond_min', String(f.apartmentConditionLevelMin));
  if (f.apartmentConditionLevelMax != null) sp.set('apt_cond_max', String(f.apartmentConditionLevelMax));
  if (f.priceChangeCountMin != null) sp.set('changes_min', String(f.priceChangeCountMin));
  if (f.priceChangeWindowDays != null) sp.set('changes_window', String(f.priceChangeWindowDays));
  if (f.totalPriceChangePct != null) sp.set('total_change_pct', String(f.totalPriceChangePct));
  if (f.withEstimates) sp.set('with_est', '1');
  if (f.tags.length) sp.set('tags', f.tags.join(','));
  if (f.bounds) {
    const { west, south, east, north } = f.bounds;
    sp.set(
      'bbox',
      `${fmtBoundsCoord(west)},${fmtBoundsCoord(south)},${fmtBoundsCoord(east)},${fmtBoundsCoord(north)}`,
    );
  }
  if (f.locationMode === 'center_radius') sp.set('locmode', 'center_radius');
  if (f.centerRadius) {
    const { lat, lng, radius_m } = f.centerRadius;
    sp.set(
      'center',
      `${fmtBoundsCoord(lat)},${fmtBoundsCoord(lng)},${radius_m}`,
    );
  }
  if (f.cityIndexRules.length) {
    sp.set('cq_rules', fmtCityIndexRules(f.cityIndexRules));
  }
  if (f.minCityPopulation != null) sp.set('cq_pop_min', String(f.minCityPopulation));
  if (f.maxCityPopulation != null) sp.set('cq_pop_max', String(f.maxCityPopulation));
  if (f.nearCityProximity) {
    sp.set('cq_prox', fmtNearCityProximity(f.nearCityProximity));
  }
  if (f.nearPop5kmMin != null) sp.set('np5', String(f.nearPop5kmMin));
  if (f.nearPop15kmMin != null) sp.set('np15', String(f.nearPop15kmMin));
  if (f.nearJobs5kmMin != null) sp.set('nj5', String(f.nearJobs5kmMin));
  if (f.nearJobs15kmMin != null) sp.set('nj15', String(f.nearJobs15kmMin));
  if (f.nearYouth5kmMin != null) sp.set('ny5', String(f.nearYouth5kmMin));
  if (f.nearYouth15kmMin != null) sp.set('ny15', String(f.nearYouth15kmMin));
  if (f.nearOverall5kmMin != null) sp.set('no5', String(f.nearOverall5kmMin));
  if (f.nearOverall15kmMin != null) sp.set('no15', String(f.nearOverall15kmMin));
  if (f.priceGrowthRules.length) sp.set('pg', fmtPriceGrowthRules(f.priceGrowthRules));
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
  pozemek: 'land',
  ostatni: 'other',
};

/* Join the multi-select category_main into a human label, e.g.
 * ['byt','dum'] -> "apartments / houses". Empty = "listings" (no constraint). */
const categoryMainLabel = (cats: ReadonlyArray<CategoryMain>): string =>
  cats.length === 0
    ? 'listings'
    : cats.map((c) => CATEGORY_MAIN_PLURAL[c]).join(' / ');

const CATEGORY_TYPE_LABEL: Record<CategoryType, string> = {
  pronajem: 'for rent',
  prodej: 'for sale',
  drazba: 'at auction',
  podil: 'fractional',
};

export const categoryHeading = (f: ListingFilters): string =>
  `${categoryMainLabel(f.categoryMain)} ${CATEGORY_TYPE_LABEL[f.categoryType]}`;

/* Stable, order-independent serialization of the cohort-defining filters.
 * Reuses the canonical URL serializer (toSearchParams) and sorts the
 * entries so two equal filter sets always produce the same string —
 * used as the per-region cache key for box-plot annotations. */
export const regionKeyFromFilters = (f: ListingFilters): string => {
  const entries = [...toSearchParams(f).entries()].sort((a, b) =>
    a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0,
  );
  return new URLSearchParams(entries).toString();
};

/* Short human-readable label for the cohort — passed to the annotator
 * as context so it can say "in Praha" rather than "(filtered cohort)". */
export const regionLabelFromFilters = (f: ListingFilters): string => {
  const base = categoryHeading(f);
  /* Only INCLUDE chips belong in an "in X" cohort label — an excluded
   * district is a subtraction, not a place the cohort is "in". */
  const inc = f.districts.filter((d) => !d.excluded);
  if (inc.length) {
    const shown = inc.slice(0, 3).map((d) => d.name).join(', ');
    const extra = inc.length > 3 ? ` +${inc.length - 3}` : '';
    return `${base} in ${shown}${extra}`;
  }
  if (f.locationMode === 'center_radius' && f.centerRadius) {
    return `${base} within ${f.centerRadius.radius_m} m of a point`;
  }
  if (f.bounds) return `${base} in the current map area`;
  return base;
};

const fmtDaysRange = (lo: number | null, hi: number | null): string => {
  if (lo == null && hi == null) return '';
  if (lo != null && hi != null) return `${lo}–${hi} d`;
  if (lo != null)               return `≥ ${lo} d`;
  return `≤ ${hi} d`;
};

export const summarise = (f: ListingFilters, count: number | null): string => {
  const bits: string[] = [];
  bits.push(f.status === 'active' ? 'active' : f.status === 'inactive' ? 'inactive' : 'all');
  bits.push(`${count == null ? '…' : count.toLocaleString('cs-CZ')} ${categoryMainLabel(f.categoryMain)}`);
  bits.push(CATEGORY_TYPE_LABEL[f.categoryType]);
  if (f.districts.length) {
    const shown = f.districts
      .slice(0, 3)
      .map((d) => {
        const base = d.context ? `${d.name} · ${d.context}` : d.name;
        return d.excluded ? `−${base}` : base;
      })
      .join(', ');
    const extra = f.districts.length > 3 ? ` +${f.districts.length - 3}` : '';
    bits.push(`in ${shown}${extra}`);
  }
  if (f.dispositions.length) {
    bits.push(`(${f.dispositions.slice(0, 4).join(', ')}${f.dispositions.length > 4 ? '…' : ''})`);
  }
  const seenLabel = fmtDaysRange(f.lastSeenMinDays, f.lastSeenMaxDays);
  if (seenLabel) bits.push(`last seen ${seenLabel}`);
  if (f.recentlyAddedDays != null) bits.push(`added ≤ ${f.recentlyAddedDays} d`);
  if (f.recentlyChangedDays != null) bits.push(`changed ≤ ${f.recentlyChangedDays} d`);
  const tomLabel = fmtDaysRange(f.tomDaysMin, f.tomDaysMax);
  if (tomLabel) bits.push(`TOM ${tomLabel}`);
  if (f.bounds) bits.push('in this map area');
  return `Showing ${bits.join(' ')}`;
};

/* A short, human default name for a watchdog created from the current Browse
 * filters — `category type · dispositions · districts`, e.g.
 * "byt prodej · 2+kk, 2+1 · Jihlava, HB". Operator can overwrite it in the
 * Create-watchdog dialog. */
export const watchdogNameSuggestion = (f: ListingFilters): string => {
  const parts: string[] = [
    `${f.categoryMain.join('+') || 'vše'} ${f.categoryType}`,
  ];
  if (f.dispositions.length) {
    parts.push(
      f.dispositions.slice(0, 4).join(', ')
      + (f.dispositions.length > 4 ? '…' : ''),
    );
  }
  if (f.districts.length) {
    parts.push(
      f.districts
        .slice(0, 3)
        .map((d) => (d.excluded ? `−${d.name}` : d.name))
        .join(', ')
      + (f.districts.length > 3 ? '…' : ''),
    );
  }
  return parts.join(' · ');
};

/* Deterministic "disposition · area · city" summary of the active Browse
 * filters, for the browser-tab title (the constraints that most identify a
 * Browse tab at a glance). Each segment is omitted when empty; returns null
 * when nothing relevant is set, so the caller falls back to the static
 * "Browse" route title. Reuses fmtArea + the include-only district logic of
 * regionLabelFromFilters; the tab form is tighter (one name + "+N") because
 * tab space is narrow. Dispositions are joined with ", " (they already contain
 * "+", e.g. "2+kk"). */
export const browseTitleSummary = (f: ListingFilters): string | null => {
  const segments: string[] = [];

  if (f.dispositions.length) {
    segments.push(
      f.dispositions.length <= 3
        ? f.dispositions.join(', ')
        : `${f.dispositions.slice(0, 2).join(', ')} +${f.dispositions.length - 2}`,
    );
  }

  const { areaMin, areaMax } = f;
  if (areaMin != null && areaMax != null) {
    segments.push(`${areaMin.toLocaleString('cs-CZ')}–${fmtArea(areaMax)}`);
  } else if (areaMin != null) {
    segments.push(`≥ ${fmtArea(areaMin)}`);
  } else if (areaMax != null) {
    segments.push(`≤ ${fmtArea(areaMax)}`);
  }

  const cities = f.districts.filter((d) => !d.excluded).map((d) => d.name.trim()).filter(Boolean);
  if (cities.length === 1) segments.push(cities[0]);
  else if (cities.length > 1) segments.push(`${cities[0]} +${cities.length - 1}`);

  return segments.length ? segments.join(' · ') : null;
};

/* Generic compare against DEFAULT_FILTERS so new fields can never be
 * forgotten here (the old hand-maintained &&-chain silently missed
 * priceGrowthRules). Arrays count as default when empty; object-valued
 * fields (bounds, centerRadius, nearCityProximity) default to null, so a
 * null-vs-set check is exact. */
export const isDefault = (f: ListingFilters): boolean =>
  (Object.keys(DEFAULT_FILTERS) as Array<keyof ListingFilters>).every((k) => {
    const value = f[k];
    const def = DEFAULT_FILTERS[k];
    /* Array-valued fields are default iff they equal the default array
     * (order-insensitive). Every array default is [] except categoryMain
     * (['byt']) — for [] this reduces to the old "is empty" check. */
    if (Array.isArray(def)) {
      return (
        Array.isArray(value)
        && value.length === def.length
        && def.every((d) => (value as unknown[]).includes(d))
      );
    }
    if (def === null) return value == null;
    return value === def;
  });


/* -------------------------------------------------------------------------- */
/* Saved filter presets                                                        */
/*                                                                            */
/* A preset stores the full ListingFilters. These helpers normalise what we   */
/* persist and detect whether a loaded preset has since been edited. Equality  */
/* is computed on the canonical URL form (`toSearchParams`) — the single       */
/* source of truth for what a filter set "is" — rather than a hand-rolled      */
/* deep-equal that would rot as fields are added.                              */
/* -------------------------------------------------------------------------- */

/** Persisted preset payload. The opaque `filter_spec` blob the API stores
 *  verbatim. `sort` is the serialized sort param (`sortToParam` form, e.g.
 *  "-price_czk") — kept as a plain string so this module needn't depend on
 *  the queries sort types. `null`/absent means the default sort. */
export interface PresetSpec {
  filters: ListingFilters;
  sort?: string | null;
}

/** Build the spec to persist. The transient map viewport (`bounds`) is
 *  dropped unless the operator opts to include the current map area, so a
 *  criteria-only preset doesn't pin a stale bounding box. */
export const filtersForPreset = (
  f: ListingFilters,
  includeMapArea: boolean,
): ListingFilters => (includeMapArea ? f : { ...f, bounds: null });

/* Merge a stored (possibly stale-typed) filter blob onto DEFAULT_FILTERS,
 * dropping any field whose CARDINALITY drifted since it was saved — e.g. a
 * field that used to be a scalar (`furnished: null`) and later became a
 * multi-select array (`furnished: []`). Without this guard, loading an old
 * preset feeds `toSearchParams` a wrong-shaped value (`null.length`) and the
 * whole load throws, so clicking the chip silently does nothing. Unknown /
 * removed keys are dropped too. */
const coerceStoredFilters = (stored: unknown): ListingFilters => {
  const def = DEFAULT_FILTERS as unknown as Record<string, unknown>;
  const out: Record<string, unknown> = { ...def };
  if (stored && typeof stored === 'object') {
    for (const [k, vRaw] of Object.entries(stored as Record<string, unknown>)) {
      if (!(k in def)) continue;
      // Migration shim: categoryMain went scalar -> array. Lift an old stored
      // scalar (e.g. "dum") to ["dum"] so a preset saved before the multiselect
      // change keeps its category instead of silently reverting to ['byt'].
      const v =
        k === 'categoryMain' && typeof vRaw === 'string' ? [vRaw] : vRaw;
      // Array-vs-scalar mismatch ⇒ the field's type evolved; keep the default.
      if (Array.isArray(def[k]) !== Array.isArray(v)) continue;
      out[k] = v;
    }
  }
  return out as unknown as ListingFilters;
};

/** Read a stored preset blob, tolerating the legacy bare-`ListingFilters`
 *  shape (presets saved before sort was captured) and fields whose type drifted
 *  after the preset was saved. Always returns a complete, shape-safe filter set
 *  merged onto DEFAULT_FILTERS, plus the saved sort (or null for the default). */
export const readPresetSpec = (
  spec: PresetSpec | ListingFilters,
): { filters: ListingFilters; sort: string | null } => {
  const wrapped =
    spec != null && (spec as PresetSpec).filters != null;
  const filters = wrapped
    ? (spec as PresetSpec).filters
    : (spec as ListingFilters);
  const sort = wrapped ? (spec as PresetSpec).sort ?? null : null;
  return { filters: coerceStoredFilters(filters), sort };
};

const canonicalParams = (sp: URLSearchParams): string =>
  [...sp.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([k, v]) => `${k}=${v}`)
    .join('&');

/** True when `current` matches the `saved` preset. The map viewport is
 *  ignored unless the preset deliberately stored one, so panning the map
 *  after loading a criteria-only preset doesn't mark it dirty. Both sides
 *  are merged onto DEFAULT_FILTERS first so a preset persisted under an
 *  older schema (missing a newer field) still compares cleanly. */
export const filtersEqualForPreset = (
  current: ListingFilters,
  saved: ListingFilters,
): boolean => {
  const a = toSearchParams({ ...DEFAULT_FILTERS, ...current });
  const b = toSearchParams({ ...DEFAULT_FILTERS, ...saved });
  if (!b.has('bbox')) a.delete('bbox');
  return canonicalParams(a) === canonicalParams(b);
};


/* -------------------------------------------------------------------------- */
/* Registry adapter                                                            */
/*                                                                            */
/* Browse keeps its camelCase `ListingFilters` shape; the unified              */
/* `<FilterForm>` reads snake_case registry ids. These two helpers bridge      */
/* the gap at the boundary so we don't have to rename the type or the 40+     */
/* references inside `queries.ts`. Tri-state amenities pivot here too:        */
/* `'any' | 'yes' | 'no'` ⇄ `null | true | false`.                            */
/* -------------------------------------------------------------------------- */

/** Registry id → ListingFilters key. Keys not present here aren't part
 *  of the Browse filter set (e.g. `radius_m` and `area_band_pct` are
 *  cohort-tuning knobs that don't surface on Browse). */
export const REGISTRY_KEY_MAP = {
  category_main_in: 'categoryMain',
  category_type: 'categoryType',
  category_sub_cb: 'categorySubCb',
  subtype: 'subtype',
  dispositions: 'dispositions',
  districts: 'districts',
  status: 'status',
  min_price_czk: 'priceMin',
  max_price_czk: 'priceMax',
  include_no_price: 'includeNoPrice',
  min_price_per_m2: 'pricePerM2Min',
  max_price_per_m2: 'pricePerM2Max',
  min_mf_gross_yield_pct: 'mfGrossYieldPctMin',
  max_mf_gross_yield_pct: 'mfGrossYieldPctMax',
  min_area_m2: 'areaMin',
  max_area_m2: 'areaMax',
  min_estate_area: 'estateAreaMin',
  max_estate_area: 'estateAreaMax',
  min_usable_area: 'usableAreaMin',
  max_usable_area: 'usableAreaMax',
  has_balcony: 'hasBalcony',
  has_lift: 'hasLift',
  has_parking: 'hasParking',
  terrace: 'terrace',
  cellar: 'cellar',
  garage: 'garage',
  furnished: 'furnished',
  ownership: 'ownership',
  portals: 'portals',
  condition_match: 'conditionMatch',
  building_material: 'buildingMaterial',
  min_parking_lots: 'parkingLotsMin',
  min_garden_area: 'gardenAreaMin',
  max_garden_area: 'gardenAreaMax',
  building_condition_level_min: 'buildingConditionLevelMin',
  building_condition_level_max: 'buildingConditionLevelMax',
  apartment_condition_level_min: 'apartmentConditionLevelMin',
  apartment_condition_level_max: 'apartmentConditionLevelMax',
  price_change_count_min: 'priceChangeCountMin',
  price_change_window_days: 'priceChangeWindowDays',
  total_price_change_pct: 'totalPriceChangePct',
  with_estimates: 'withEstimates',
  tags: 'tags',
  tom_days_min: 'tomDaysMin',
  tom_days_max: 'tomDaysMax',
  last_seen_min_days: 'lastSeenMinDays',
  last_seen_max_days: 'lastSeenMaxDays',
  first_seen_min_days: 'firstSeenMinDays',
  first_seen_max_days: 'firstSeenMaxDays',
  recently_added_days: 'recentlyAddedDays',
  recently_changed_days: 'recentlyChangedDays',
  city_index_rules: 'cityIndexRules',
  min_city_population: 'minCityPopulation',
  max_city_population: 'maxCityPopulation',
  near_city_proximity: 'nearCityProximity',
  near_pop_5km_min: 'nearPop5kmMin',
  near_pop_15km_min: 'nearPop15kmMin',
  near_jobs_5km_min: 'nearJobs5kmMin',
  near_jobs_15km_min: 'nearJobs15kmMin',
  near_youth_5km_min: 'nearYouth5kmMin',
  near_youth_15km_min: 'nearYouth15kmMin',
  near_overall_5km_min: 'nearOverall5kmMin',
  near_overall_15km_min: 'nearOverall15kmMin',
} as const satisfies Record<string, keyof ListingFilters>;

type RegistryKey = keyof typeof REGISTRY_KEY_MAP;

const TRISTATE_KEYS: ReadonlyArray<RegistryKey> = [
  'has_balcony', 'has_lift', 'has_parking', 'terrace', 'cellar', 'garage',
];

const triToBoolNullable = (v: TriState): boolean | null =>
  v === 'any' ? null : v === 'yes';

const boolNullableToTri = (v: unknown): TriState => {
  if (v === null || v === undefined) return 'any';
  return v ? 'yes' : 'no';
};

/** Project a `ListingFilters` onto the snake_case shape that
 *  `<FilterForm>` reads. Tri-state amenities convert to `bool | null`;
 *  empty `tags` array becomes `null` (registry's "no constraint"
 *  sentinel for list filters). */
export function listingFiltersToRegistryView(
  filters: ListingFilters,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [registryId, key] of Object.entries(REGISTRY_KEY_MAP)) {
    const v = filters[key as keyof ListingFilters];
    if ((TRISTATE_KEYS as ReadonlyArray<string>).includes(registryId)) {
      out[registryId] = triToBoolNullable(v as TriState);
    } else if (registryId === 'tags') {
      out[registryId] = (v as number[]).length === 0 ? null : v;
    } else if (
      registryId === 'category_main_in'
      || registryId === 'dispositions'
      || registryId === 'districts'
      || registryId === 'condition_match'
      || registryId === 'furnished'
      || registryId === 'ownership'
      || registryId === 'portals'
      || registryId === 'building_material'
      || registryId === 'subtype'
    ) {
      const arr = v as unknown[];
      out[registryId] = arr.length === 0 ? null : arr;
    } else if (registryId === 'city_index_rules') {
      const arr = v as CityIndexRule[];
      out[registryId] = arr.length === 0 ? null : arr;
    } else {
      out[registryId] = v;
    }
  }
  return out;
}

/** Reverse of `listingFiltersToRegistryView`. Given a `<FilterForm>`
 *  onChange `(id, value)`, returns the next `ListingFilters`. Unknown
 *  ids are no-ops — Browse doesn't track every registry filter. */
export function applyRegistryUpdate(
  filters: ListingFilters,
  id: string,
  value: unknown,
): ListingFilters {
  if (!(id in REGISTRY_KEY_MAP)) return filters;
  const key = REGISTRY_KEY_MAP[id as RegistryKey];
  if ((TRISTATE_KEYS as ReadonlyArray<string>).includes(id)) {
    return { ...filters, [key]: boolNullableToTri(value) };
  }
  if (id === 'tags') {
    const next = value == null ? [] : (value as number[]);
    return { ...filters, tags: next };
  }
  if (id === 'category_main_in') {
    const next = value == null ? [] : (value as CategoryMain[]);
    return { ...filters, categoryMain: next };
  }
  if (id === 'dispositions') {
    const next = value == null ? [] : (value as Disposition[]);
    return { ...filters, dispositions: next };
  }
  if (id === 'districts') {
    if (value == null) return { ...filters, districts: [] };
    /* Lift legacy callers that still emit `string[]` so a registry-
     * scope edit (or a stale test fixture) doesn't quietly drop the
     * context shape. Mixed arrays are tolerated; objects pass through. */
    const next = (value as Array<DistrictChip | string>).map((v) =>
      typeof v === 'string' ? { name: v, context: null } : v,
    );
    return { ...filters, districts: next };
  }
  if (id === 'condition_match') {
    const next = value == null ? [] : (value as string[]);
    return { ...filters, conditionMatch: next };
  }
  if (id === 'furnished') {
    const next = value == null ? [] : (value as string[]);
    return { ...filters, furnished: next };
  }
  if (id === 'ownership') {
    const next = value == null ? [] : (value as string[]);
    return { ...filters, ownership: next };
  }
  if (id === 'subtype') {
    const next = value == null ? [] : (value as string[]);
    return { ...filters, subtype: next };
  }
  if (id === 'portals') {
    const next = value == null ? [] : (value as string[]);
    return { ...filters, portals: next };
  }
  if (id === 'building_material') {
    const next = value == null ? [] : (value as BuildingMaterial[]);
    return { ...filters, buildingMaterial: next };
  }
  if (id === 'city_index_rules') {
    const next = value == null ? [] : (value as CityIndexRule[]);
    return { ...filters, cityIndexRules: next };
  }
  return { ...filters, [key]: value } as ListingFilters;
}

/** Apply a batch of `<FilterForm>` updates atomically. Loops
 *  `applyRegistryUpdate` so paired range edits (min + max from one
 *  slider drag) compose against the same starting state. Without
 *  this, callers that route each update through a non-functional
 *  setter (e.g. Browse's URL writer) would see the second call use
 *  the same stale `filters` and overwrite the first — that's what
 *  made the dual-thumb slider and paired number inputs refuse to
 *  update under PR #112 before this fix. */
export function applyRegistryUpdates(
  filters: ListingFilters,
  updates: ReadonlyArray<{ id: string; value: unknown }>,
): ListingFilters {
  let next = filters;
  for (const u of updates) next = applyRegistryUpdate(next, u.id, u.value);
  return next;
}

/* ------------------------------------------------------------------------- *
 * Browse filters → Watchdog spec (Create-watchdog-from-Browse).
 *
 * The watchdog matcher (`api/notifications._build_match_clauses`) honours a
 * subset of the Browse filter set — the attribute predicates that make sense
 * "the moment a new listing matches". Browse filters that the matcher has no
 * clause for are NOT silently dropped: `filtersToWatchdogSpec` reports them in
 * `unsupported` so the UI can tell the operator what won't be watched.
 *
 * Honoured (mapped): category, disposition, district chips, price / price-per-m²
 * / MF-yield / area / usable / estate bounds, tri-state amenities, furnished,
 * ownership, portals, condition_match, parking-lots min, condition-level mins,
 * the price-history mins (distinct-site / price-drop / price-rise count, max
 * price-drop %), and ALL the city-quality predicates (index rules, population
 * min/max, near-city proximity). center+radius → lat/lng/radius_m.
 *
 * NOT honoured by the matcher (reported as unsupported when set): listing
 * `status`, the last-seen / first-seen / time-on-market day ranges and the
 * `recently added/changed` presets (a watchdog already fires on brand-new /
 * changed listings, so a recency window is redundant there), the map `bounds`
 * viewport (use a district chip or center+radius instead), `buildingMaterial`,
 * `garden_area` bounds, and `tags`. */

const UNSUPPORTED_LABELS: ReadonlyArray<{
  test: (f: ListingFilters) => boolean;
  label: string;
}> = [
  { test: (f) => f.status !== 'any', label: 'listing status' },
  { test: (f) => f.bounds != null, label: 'map area' },
  {
    test: (f) =>
      f.lastSeenMinDays != null || f.lastSeenMaxDays != null
      || f.firstSeenMinDays != null || f.firstSeenMaxDays != null,
    label: 'last/first-seen date range',
  },
  {
    test: (f) => f.recentlyAddedDays != null || f.recentlyChangedDays != null,
    label: 'recently added/changed',
  },
  { test: (f) => f.tomDaysMin != null || f.tomDaysMax != null, label: 'time on market' },
  { test: (f) => f.buildingMaterial.length > 0, label: 'building material' },
  { test: (f) => f.gardenAreaMin != null || f.gardenAreaMax != null, label: 'garden area' },
  { test: (f) => f.tags.length > 0, label: 'tags' },
  { test: (f) => f.priceGrowthRules.length > 0, label: 'market growth (datasets)' },
  { test: (f) => f.withEstimates, label: 'with estimates' },
];

export interface FiltersToWatchdogResult {
  spec: WatchdogFilterSpec;
  /* Human-readable names of set-but-unmonitored Browse filters. */
  unsupported: string[];
}

export function filtersToWatchdogSpec(
  filters: ListingFilters,
): FiltersToWatchdogResult {
  const f = filters;
  const arr = <T>(xs: T[]): T[] | null => (xs.length === 0 ? null : xs);
  /* center+radius is the only spatial mode a watchdog can express (the matcher
   * has no viewport clause). All three of lat/lng/radius are needed or none. */
  const cr = f.locationMode === 'center_radius' ? f.centerRadius : null;

  const spec: WatchdogFilterSpec = {
    ...DEFAULT_WATCHDOG_FILTER_SPEC,
    category_main_in: arr(f.categoryMain),
    category_type: f.categoryType,
    category_sub_cb: f.categorySubCb,
    subtype: arr(f.subtype),
    dispositions: arr(f.dispositions),
    districts: arr(f.districts),
    lat: cr ? cr.lat : null,
    lng: cr ? cr.lng : null,
    radius_m: cr ? cr.radius_m : null,
    min_price_czk: f.priceMin,
    max_price_czk: f.priceMax,
    include_no_price: f.includeNoPrice,
    min_price_per_m2: f.pricePerM2Min,
    max_price_per_m2: f.pricePerM2Max,
    min_mf_gross_yield_pct: f.mfGrossYieldPctMin,
    max_mf_gross_yield_pct: f.mfGrossYieldPctMax,
    min_area_m2: f.areaMin,
    max_area_m2: f.areaMax,
    min_usable_area: f.usableAreaMin,
    max_usable_area: f.usableAreaMax,
    min_estate_area: f.estateAreaMin,
    max_estate_area: f.estateAreaMax,
    has_balcony: triToBoolNullable(f.hasBalcony),
    has_lift: triToBoolNullable(f.hasLift),
    has_parking: triToBoolNullable(f.hasParking),
    terrace: triToBoolNullable(f.terrace),
    cellar: triToBoolNullable(f.cellar),
    garage: triToBoolNullable(f.garage),
    furnished: arr(f.furnished),
    ownership: arr(f.ownership),
    portals: arr(f.portals),
    condition_match: arr(f.conditionMatch),
    min_parking_lots: f.parkingLotsMin,
    building_condition_level_min: f.buildingConditionLevelMin,
    building_condition_level_max: f.buildingConditionLevelMax,
    apartment_condition_level_min: f.apartmentConditionLevelMin,
    apartment_condition_level_max: f.apartmentConditionLevelMax,
    price_change_count_min: f.priceChangeCountMin,
    price_change_window_days:
      f.priceChangeWindowDays != null
      && (PRICE_CHANGE_WINDOWS as ReadonlyArray<number>).includes(f.priceChangeWindowDays)
        ? (f.priceChangeWindowDays as 30 | 90 | 365)
        : null,
    total_price_change_pct: f.totalPriceChangePct,
    city_index_rules: arr(f.cityIndexRules),
    min_city_population: f.minCityPopulation,
    max_city_population: f.maxCityPopulation,
    near_city_proximity: f.nearCityProximity,
    near_pop_5km_min: f.nearPop5kmMin,
    near_pop_15km_min: f.nearPop15kmMin,
    near_jobs_5km_min: f.nearJobs5kmMin,
    near_jobs_15km_min: f.nearJobs15kmMin,
    near_youth_5km_min: f.nearYouth5kmMin,
    near_youth_15km_min: f.nearYouth15kmMin,
    near_overall_5km_min: f.nearOverall5kmMin,
    near_overall_15km_min: f.nearOverall15kmMin,
  };

  const unsupported = UNSUPPORTED_LABELS.filter((u) => u.test(f)).map((u) => u.label);
  return { spec, unsupported };
}
