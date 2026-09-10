/* Location compare (location program W6) — typed wrappers over the admin-gated
 * `/location/compare/*` API. Same posture as locationQuality.ts: every call is
 * `jwt: true` because the serving projection is service-role-only.
 *
 * OLD = `browse_list` (property grain, what Browse reads today). NEW = the
 * serving projection (`property_location_current` joined to the winner's
 * `listing_location_current`). Every counter below is property grain.
 *
 * Verdict vocabulary (design 05 §5.3.3 A): a listing is `certain` /
 * `possible` / `no` in an admin unit by its ASSIGNMENT, never by geometry;
 * `no_row` means the new engine has no projection row for it at all. */

import { apiGet } from './api';

export type Verdict = 'certain' | 'possible' | 'no' | 'no_row';

export type CompareReason =
  | 'no_projection_row'
  | 'assigned_elsewhere'
  | 'unresolved'
  | 'outside_country'
  | 'possible_only'
  | 'moved_in';

export type UnitLevel = 'kraj' | 'okres' | 'obec' | 'cast_obce' | 'street';

export type MethodRow = { admin_assignment_method: string | null; n: number };

export type SourceRow = {
  source: string;
  n_old: number;
  n_new_certain: number;
  n_new_possible: number;
  n_no_row: number;
};

export type ScopeCounters = {
  n_old: number;
  n_new_certain: number;
  n_new_possible: number;
  n_new_no: number;
  n_no_row: number;
  n_only_old: number;
  n_only_new: number;
  agreement_pct: number | null;
};

export type KrajRow = ScopeCounters & { kraj_kod: number; name: string | null };
export type OkresRow = ScopeCounters & {
  okres_kod: number;
  kraj_kod: number;
  name: string | null;
};

/* `kraje` is the echoed scope on EVERY compare response, so the per-kraj
 * counter rows of /compare/scope ride under their own key. */
export type CompareScope = {
  generated_at: string;
  kraje: number[];
  kraje_rows: KrajRow[];
  okresy: OkresRow[];
  by_method: MethodRow[];
  by_source: SourceRow[];
};

export type UnitsRow = {
  code: number;
  name: string | null;
  n_old: number;
  n_new_certain: number;
  n_new_possible: number;
  n_only_old: number;
  n_only_new: number;
};

export type CompareUnits = {
  generated_at: string;
  kraje: number[];
  level: 'obec' | 'cast_obce';
  parent_kod: number;
  rows: UnitsRow[];
};

export type CompareRow = {
  property_id: number;
  listing_id: number | null;
  source: string | null;
  old_label: string | null;
  new_label: string | null;
  granularity: string | null;
  match_confidence: string | null;
  admin_assignment_method: string | null;
  uncertainty_radius_m: number | null;
  distance_to_nearest_boundary_m: number | null;
  verdict: Verdict;
  reason: CompareReason;
};

export type CompareUnit = {
  generated_at: string;
  kraje: number[];
  level: UnitLevel;
  code: number;
  name: string | null;
  counts: {
    n_old: number;
    n_new_certain: number;
    n_new_possible: number;
    n_new_no: number;
    n_no_row: number;
    n_only_old: number;
    n_only_new: number;
    n_claimed: number;
  };
  by_method: MethodRow[];
  by_source: SourceRow[];
  only_old: CompareRow[];
  only_new: CompareRow[];
};

export type StreetRow = { ulice_kod: number; name: string | null; obec_kod: number };

export type CompareStreets = { generated_at: string; rows: StreetRow[] };

export type MapRow = {
  property_id: number;
  listing_id: number | null;
  source: string | null;
  old_lat: number | null;
  old_lng: number | null;
  new_lat: number | null;
  new_lng: number | null;
  render_as: string | null;
  renderable_as_point: boolean | null;
  uncertainty_radius_m: number | null;
  granularity: string | null;
  match_confidence: string | null;
  admin_assignment_method: string | null;
  pin_collision_class: string | null;
  location_disputed: boolean | null;
  disagreement_flags: string[] | null;
  member_spread_m: number | null;
  delta_m: number | null;
};

export type CompareMap = {
  generated_at: string;
  kraje: number[];
  rows: MapRow[];
  truncated: boolean;
  counts: {
    both: number;
    only_old_geom: number;
    only_new_geom: number;
    moved_gt_100m: number;
    demoted_to_circle: number;
    /* Cohort-wide, not box-wide: a row with no position on either side cannot
     * satisfy a bbox test, so counting it inside the box is always zero. */
    no_geom_either: number;
  };
};

export type RadiusOnlyOld = {
  property_id: number;
  listing_id: number | null;
  source: string | null;
  delta_m: number | null;
};

export type RadiusOnlyNew = {
  property_id: number;
  listing_id: number | null;
  source: string | null;
  verdict: Verdict;
  uncertainty_radius_m: number | null;
};

export type CompareRadius = {
  generated_at: string;
  kraje: number[];
  old_bbox_count: number;
  new_certain: number;
  new_possible: number;
  only_old: RadiusOnlyOld[];
  only_new: RadiusOnlyNew[];
};

const kodes = (kraje: readonly number[]): string => kraje.join(',');

export const PRAHA_KOD = 19;
export const STREDOCESKY_KOD = 27;
export const DEFAULT_KRAJE: readonly number[] = [PRAHA_KOD, STREDOCESKY_KOD];

export const KRAJ_LABELS: Readonly<Record<number, string>> = {
  [PRAHA_KOD]: 'Praha',
  [STREDOCESKY_KOD]: 'Středočeský',
};

export const fetchCompareScope = (kraje: readonly number[], signal?: AbortSignal) =>
  apiGet<CompareScope>(
    '/location/compare/scope', { kraje: kodes(kraje) }, signal, true,
  );

export const fetchCompareUnits = (
  level: 'obec' | 'cast_obce',
  parentKod: number,
  kraje: readonly number[],
  signal?: AbortSignal,
) =>
  apiGet<CompareUnits>(
    '/location/compare/units',
    { level, parent_kod: parentKod, kraje: kodes(kraje) },
    signal, true,
  );

export const fetchCompareUnit = (
  level: UnitLevel,
  code: number,
  kraje: readonly number[],
  limit = 200,
  signal?: AbortSignal,
) =>
  apiGet<CompareUnit>(
    '/location/compare/unit',
    { level, code, kraje: kodes(kraje), limit },
    signal, true,
  );

export const fetchCompareStreets = (
  obecKod: number, q: string, limit = 20, signal?: AbortSignal,
) =>
  apiGet<CompareStreets>(
    '/location/compare/streets', { obec_kod: obecKod, q, limit }, signal, true,
  );

export type Bbox = { west: number; south: number; east: number; north: number };

export const fetchCompareMap = (
  bbox: Bbox, kraje: readonly number[], limit = 5000, signal?: AbortSignal,
) =>
  apiGet<CompareMap>(
    '/location/compare/map',
    { ...bbox, kraje: kodes(kraje), limit },
    signal, true,
  );

export const fetchCompareRadius = (
  centre: { lat: number; lng: number },
  radiusM: number,
  kraje: readonly number[],
  limit = 100,
  signal?: AbortSignal,
) =>
  apiGet<CompareRadius>(
    '/location/compare/radius',
    { lat: centre.lat, lng: centre.lng, radius_m: radiusM, kraje: kodes(kraje), limit },
    signal, true,
  );

export const REASON_LABELS: Readonly<Record<CompareReason, string>> = {
  no_projection_row: 'no projection row',
  assigned_elsewhere: 'assigned elsewhere',
  unresolved: 'unresolved',
  outside_country: 'outside CZ',
  possible_only: 'possible',
  moved_in: 'moved in',
};

/* Map-marker colour bucket. Granularity is an ordered enum in the projection;
 * here it only has to answer "how precise does this pin claim to be" in four
 * steps the operator can read off a legend. */
export type GranBucket = 'point' | 'street' | 'area' | 'none';

export const granBucket = (g: string | null | undefined): GranBucket => {
  switch (g) {
    case 'address_point':
    case 'building':
      return 'point';
    case 'parcel':
    case 'street':
    case 'street_segment':
      return 'street';
    case 'cast_obce_or_quarter':
    case 'obec':
    case 'okres':
    case 'kraj':
    case 'country':
      return 'area';
    default:
      return 'none';
  }
};
