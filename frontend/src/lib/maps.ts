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

/* The resolution of a picked suggestion to a RÚIAN CODE. `admin`
 * (kraj/okres/obec/cast_obce) carries the code the chip predicate matches on
 * (`lib/districtCodes`); `locality` (street / POI / address) has no code of its
 * own and carries its containing `obecId`, so it filters at the obec level.
 * `point_with_radius` / `unresolved` are the fallbacks for points that resolve
 * to no admin unit (foreign points).
 *
 * `cast_obce` is the one level resolved BY NAME rather than by point: RÚIAN
 * publishes no part-of-municipality polygon, so the server places the obec from
 * the point and the part by name inside it (api/maps.py). */
export type LocationResolution =
  | {
      kind: 'admin';
      level: 'obec' | 'okres' | 'kraj' | 'cast_obce';
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
  level: 'obec' | 'okres' | 'kraj' | 'cast_obce' | 'locality' | null;
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
    /* The pick's own name, read server-side for the one level that has no
     * polygon to point-in-polygon against: `cast_obce`. */
    name: pick.name,
  };
  const res = await apiPost<ResolveResponse>('/maps/resolve', body);

  if (res.lat == null || res.lng == null) {
    return { kind: 'unresolved', label: res.label };
  }
  if (
    res.kind === 'admin'
    && res.id != null
    && (res.level === 'obec' || res.level === 'okres' || res.level === 'kraj'
        || res.level === 'cast_obce')
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


/* Resolve stored name-only chips to RÚIAN codes, once, at read time.
 *
 * A preset or a URL written before codes existed carries `{name, context}` and
 * nothing else. The stored blob is NEVER rewritten (a preset stores the full
 * blob) — `useLegacyChipUpgrade` calls this on the way into the query and
 * replaces those chips in memory. The Watchdog calls the same server-side
 * resolver in-process, so a saved filter cannot mean one thing in Browse and
 * another in the matcher (rule 16).
 *
 * A name can legitimately answer at several levels ("Jihlava" is an obec AND an
 * okres) — all of them come back, which is the closest code-equality has to the
 * ILIKE-across-four-columns this replaces. */
export interface ChipNameMatch {
  level: 'kraj' | 'okres' | 'obec' | 'cast_obce';
  id: number;
}

interface ResolveNamesResponse {
  chips: Array<{
    name: string;
    context: string | null;
    matches: ChipNameMatch[];
  }>;
}

export const resolveChipNames = async (
  chips: ReadonlyArray<{ name: string; context: string | null }>,
): Promise<ChipNameMatch[][]> => {
  if (!chips.length) return [];
  const res = await apiPost<ResolveNamesResponse>('/maps/resolve-names', {
    chips: chips.map((c) => ({ name: c.name, context: c.context })),
  });
  return chips.map((_c, i) => res.chips?.[i]?.matches ?? []);
};
