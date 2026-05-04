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

export type LocationResolution =
  | {
      kind: 'admin_polygon';
      level: 'obec' | 'okres' | 'kraj' | 'ku';
      id: number;
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
  kind: 'admin_polygon' | 'point_with_radius' | 'unresolved';
  label: string;
  lat: number | null;
  lng: number | null;
  polygon: { level: 'obec' | 'okres' | 'kraj' | 'ku'; id: number; name: string } | null;
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
    label: pick.label,
    lat: pick.position?.lat ?? null,
    lng: pick.position?.lon ?? null,
    type: pick.type,
    regional_structure: pick.regionalStructure ?? [],
    raw: pick as unknown as Record<string, unknown>,
  };
  const res = await apiPost<ResolveResponse>('/maps/resolve', body);

  if (res.kind === 'unresolved' || res.lat == null || res.lng == null) {
    return { kind: 'unresolved', label: res.label };
  }
  if (res.kind === 'admin_polygon' && res.polygon) {
    return {
      kind: 'admin_polygon',
      level: res.polygon.level,
      id: res.polygon.id,
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
  if (type === 'regional.region') return 'Kraj';
  if (type === 'regional.country') return 'Stát';
  if (type === 'poi') return 'POI';
  return type.replace(/^regional\./, '');
};
