/* Mapy.cz suggest + resolve via the FastAPI proxy. The frontend never
 * holds the Mapy.cz key — see api/maps.py for the server side.
 *
 * `MapySuggestion` mirrors the subset of Mapy.cz's /v1/suggest item shape
 * we actually use. Unknown fields pass through untouched. */

import { apiGet, apiPost, ApiError } from './api';

export interface MapySuggestionPosition {
  lon: number;
  lat: number;
}

export interface MapyRegionalEntry {
  name: string;
  type: string;
}

export interface MapySuggestion {
  name: string;
  label: string;
  type: string;
  position?: MapySuggestionPosition;
  location?: string;
  regionalStructure?: MapyRegionalEntry[];
  [extra: string]: unknown;
}

/* The resolution of a picked suggestion to a stable admin identity. `admin`
 * (obec/okres/kraj) carries the admin_boundaries id matched by `/maps/resolve`'s
 * point-in-polygon; `locality` (street / POI / address / část obce) carries its
 * containing `obecId` so a street narrows to its municipality + a locality-text
 * match. `point_with_radius` / `unresolved` are the fallbacks for points that
 * resolve to no admin unit (foreign points). */
export type LocationResolution =
  | {
      kind: 'admin';
      level: 'obec' | 'okres' | 'kraj';
      id: number;
      name: string;
      label: string;
      lat: number;
      lng: number;
      default_radius_m: number;
    }
  | {
      kind: 'locality';
      obecId: number | null;
      label: string;
      lat: number;
      lng: number;
      default_radius_m: number;
    }
  | {
      kind: 'point_with_radius';
      lat: number;
      lng: number;
      radius_m: number;
      label: string;
    }
  | { kind: 'unresolved'; label: string };

interface SuggestResponse {
  items: MapySuggestion[];
}

interface ResolveResponse {
  kind: 'admin' | 'locality' | 'point_with_radius' | 'unresolved';
  level: 'obec' | 'okres' | 'kraj' | 'locality' | null;
  id: number | null;
  obec_id: number | null;
  name: string | null;
  label: string;
  lat: number | null;
  lng: number | null;
  default_radius_m: number;
  raw: Record<string, unknown>;
}

export const SUGGEST_NOT_CONFIGURED = 'suggest_not_configured';

export const fetchSuggest = async (
  query: string,
  signal?: AbortSignal,
): Promise<MapySuggestion[]> => {
  try {
    const res = await apiGet<SuggestResponse>(
      '/maps/suggest',
      { query, limit: 10, lang: 'cs' },
      signal,
    );
    return res.items ?? [];
  } catch (err) {
    if (err instanceof ApiError && err.status === 503) {
      throw new Error(SUGGEST_NOT_CONFIGURED);
    }
    throw err;
  }
};

export const resolveSuggestion = async (
  pick: MapySuggestion,
): Promise<LocationResolution> => {
  const body = {
    label: pick.location ?? pick.name,
    lat: pick.position?.lat ?? null,
    lng: pick.position?.lon ?? null,
    type: pick.type,
    regional_structure: pick.regionalStructure ?? [],
    raw: pick as unknown as Record<string, unknown>,
  };
  const res = await apiPost<ResolveResponse>('/maps/resolve', body);

  if (res.lat == null || res.lng == null) {
    return { kind: 'unresolved', label: res.label };
  }
  if (
    res.kind === 'admin'
    && res.id != null
    && (res.level === 'obec' || res.level === 'okres' || res.level === 'kraj')
  ) {
    return {
      kind: 'admin',
      level: res.level,
      id: res.id,
      name: res.name ?? pick.name,
      label: res.label,
      lat: res.lat,
      lng: res.lng,
      default_radius_m: res.default_radius_m,
    };
  }
  if (res.kind === 'locality') {
    return {
      kind: 'locality',
      obecId: res.obec_id,
      label: res.label,
      lat: res.lat,
      lng: res.lng,
      default_radius_m: res.default_radius_m,
    };
  }
  return {
    kind: 'point_with_radius',
    lat: res.lat,
    lng: res.lng,
    radius_m: res.default_radius_m,
    label: res.label,
  };
};

/* Short, human-readable label for the suggestion's type — rendered as a
 * subdued tag next to the suggestion's main label. Czech UI copy. */
export const typeBadge = (type: string): string => {
  if (type.startsWith('regional.address')) return 'Adresa';
  if (type === 'regional.street') return 'Ulice';
  if (type === 'regional.municipality_part') return 'Část obce';
  if (type === 'regional.municipality') return 'Obec';
  if (type === 'regional.region.district') return 'Okres';
  if (type === 'regional.region') return 'Kraj';
  if (type === 'regional.country') return 'Stát';
  if (type === 'poi') return 'POI';
  return type.replace(/^regional\./, '');
};
