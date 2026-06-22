/* The Browse "view state" contract + its two adapters.
 *
 * `<BrowseExperience>` (the filter sidebar + Map/Table/Stats + map overlays)
 * is driven by this contract instead of reading the URL directly, so the SAME
 * experience powers both the Browse page (URL-backed, shareable links) and the
 * "Explore area" modal (in-memory, seeded from a listing). The two adapters are
 * the only place that knows where the state lives:
 *   - useUrlBrowseState()   — searchParams-backed (the Browse page); preserves
 *                             the page's exact replace/push history semantics
 *                             and the overlay-knob "preserveExtras" carry that
 *                             toSearchParams doesn't cover.
 *   - useMemoryBrowseState() — plain useState (the modal). Precedent: WatchdogEdit
 *                             already runs the registry FilterForm URL-free.
 */
import { useCallback, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { RentVk } from '@/components/ListingMap';
import {
  DEFAULT_FILTERS,
  bboxAround,
  fromSearchParams,
  readPresetSpec,
  toSearchParams,
  type ListingFilters,
  type MapBounds,
} from '@/lib/filters';
import {
  DEFAULT_SORT,
  parseSort,
  sortToParam,
  type SortSpec,
} from '@/lib/queries';
import type { Disposition, FilterPreset } from '@/lib/types';

export type TabKey = 'map' | 'table' | 'stats';

/* Map-overlay UI knobs. NOT part of the cohort filter spec — they paint
 * layers (city pins, MF rent / price-growth choropleths). On the Browse page
 * they live in the URL (shareable view); in the modal they're in-memory. */
export interface MapOverlayState {
  showCities: boolean;
  colorByIndexName: string | null;
  showRentMap: boolean;
  rentVk: RentVk;
  showKraje: boolean;
}

export const DEFAULT_OVERLAY: MapOverlayState = {
  showCities: true,
  colorByIndexName: null,
  showRentMap: false,
  rentVk: 1,
  showKraje: false,
};

/* The contract <BrowseExperience> consumes. Setters carry the page's history
 * semantics in the URL adapter (filter/sort/preset = push; bounds/tab/overlay =
 * replace); the memory adapter ignores that (no history) and just sets state. */
export interface BrowseViewState {
  filters: ListingFilters;
  sort: SortSpec;
  tab: TabKey;
  overlay: MapOverlayState;
  activePresetId: string | null;
  setFilters: (next: ListingFilters) => void;
  setBounds: (b: MapBounds | null) => void;
  setSort: (next: SortSpec) => void;
  setTab: (next: TabKey) => void;
  setOverlay: (patch: Partial<MapOverlayState>) => void;
  setActivePresetId: (id: string | null) => void;
  loadPreset: (p: FilterPreset) => void;
}

const OVERLAY_URL_KEYS = ['tab', 'sort', 'cities', 'colorby', 'rentmap', 'rentvk', 'kraje', 'preset'];

/* The Browse page adapter. Lifted verbatim from the page so its behaviour —
 * the dual serialization (filters via toSearchParams + overlay knobs carried by
 * preserveExtras) and the per-setter replace/push choice — is byte-identical. */
export function useUrlBrowseState(): BrowseViewState {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = useMemo(() => fromSearchParams(searchParams), [searchParams]);
  const sort = useMemo(() => parseSort(searchParams.get('sort')), [searchParams]);
  const tab = (searchParams.get('tab') ?? 'map') as TabKey;
  const overlay = useMemo<MapOverlayState>(() => {
    const rentVkParam = parseInt(searchParams.get('rentvk') ?? '1', 10);
    return {
      showCities: searchParams.get('cities') !== '0',
      colorByIndexName: searchParams.get('colorby') ?? null,
      showRentMap: searchParams.get('rentmap') === '1',
      rentVk: ([1, 2, 3, 4].includes(rentVkParam) ? rentVkParam : 1) as RentVk,
      showKraje: searchParams.get('kraje') === '1',
    };
  }, [searchParams]);
  const activePresetId = searchParams.get('preset');

  const preserveExtras = useCallback(
    (sp: URLSearchParams): URLSearchParams => {
      for (const key of OVERLAY_URL_KEYS) {
        const v = searchParams.get(key);
        if (v != null) sp.set(key, v);
      }
      return sp;
    },
    [searchParams],
  );

  const setFilters = useCallback(
    (next: ListingFilters) => {
      setSearchParams(preserveExtras(toSearchParams(next)), { replace: false });
    },
    [preserveExtras, setSearchParams],
  );

  const setBounds = useCallback(
    (b: MapBounds | null) => {
      const next: ListingFilters = { ...filters, bounds: b };
      setSearchParams(preserveExtras(toSearchParams(next)), { replace: true });
    },
    [filters, preserveExtras, setSearchParams],
  );

  const setSort = useCallback(
    (next: SortSpec) => {
      const sp = new URLSearchParams(searchParams);
      if (sortToParam(next) === sortToParam(DEFAULT_SORT)) sp.delete('sort');
      else sp.set('sort', sortToParam(next));
      setSearchParams(sp, { replace: false });
    },
    [searchParams, setSearchParams],
  );

  const setTab = useCallback(
    (next: TabKey) => {
      const sp = new URLSearchParams(searchParams);
      if (next === 'map') sp.delete('tab');
      else sp.set('tab', next);
      setSearchParams(sp, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const setOverlay = useCallback(
    (patch: Partial<MapOverlayState>) => {
      const sp = new URLSearchParams(searchParams);
      if ('showCities' in patch) patch.showCities ? sp.delete('cities') : sp.set('cities', '0');
      if ('colorByIndexName' in patch)
        patch.colorByIndexName ? sp.set('colorby', patch.colorByIndexName) : sp.delete('colorby');
      if ('showRentMap' in patch) patch.showRentMap ? sp.set('rentmap', '1') : sp.delete('rentmap');
      if ('rentVk' in patch) patch.rentVk === 1 ? sp.delete('rentvk') : sp.set('rentvk', String(patch.rentVk));
      if ('showKraje' in patch) patch.showKraje ? sp.set('kraje', '1') : sp.delete('kraje');
      setSearchParams(sp, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const setActivePresetId = useCallback(
    (id: string | null) => {
      const sp = new URLSearchParams(searchParams);
      if (id) sp.set('preset', id);
      else sp.delete('preset');
      setSearchParams(sp, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const loadPreset = useCallback(
    (p: FilterPreset) => {
      const { filters: pf, sort: ps } = readPresetSpec(p.filter_spec);
      const sp = preserveExtras(toSearchParams(pf));
      const presetSort = ps ?? sortToParam(DEFAULT_SORT);
      if (presetSort === sortToParam(DEFAULT_SORT)) sp.delete('sort');
      else sp.set('sort', presetSort);
      sp.set('preset', p.id);
      setSearchParams(sp, { replace: false });
    },
    [preserveExtras, setSearchParams],
  );

  return {
    filters, sort, tab, overlay, activePresetId,
    setFilters, setBounds, setSort, setTab, setOverlay, setActivePresetId, loadPreset,
  };
}

/* The in-memory adapter (the modal). No URL, no history — every setter just
 * updates React state. Preset loading is supported but the modal mounts the
 * preset bar off, so it's effectively unused there. */
export function useMemoryBrowseState(init: {
  filters: ListingFilters;
  sort?: SortSpec;
  tab?: TabKey;
  overlay?: MapOverlayState;
}): BrowseViewState {
  const [filters, setFiltersState] = useState<ListingFilters>(init.filters);
  const [sort, setSortState] = useState<SortSpec>(init.sort ?? DEFAULT_SORT);
  const [tab, setTabState] = useState<TabKey>(init.tab ?? 'map');
  const [overlay, setOverlayState] = useState<MapOverlayState>(init.overlay ?? DEFAULT_OVERLAY);
  const [activePresetId, setActivePresetId] = useState<string | null>(null);

  return useMemo<BrowseViewState>(
    () => ({
      filters, sort, tab, overlay, activePresetId,
      setFilters: (next) => setFiltersState(next),
      setBounds: (b) => setFiltersState((f) => ({ ...f, bounds: b })),
      setSort: (next) => setSortState(next),
      setTab: (next) => setTabState(next),
      setOverlay: (patch) => setOverlayState((o) => ({ ...o, ...patch })),
      setActivePresetId,
      loadPreset: (p) => {
        const { filters: pf, sort: ps } = readPresetSpec(p.filter_spec);
        setFiltersState(pf);
        if (ps) setSortState(parseSort(ps));
      },
    }),
    [filters, sort, tab, overlay, activePresetId],
  );
}

const CATEGORY_MAINS = ['byt', 'dum', 'komercni', 'pozemek', 'ostatni'] as const;
const CATEGORY_TYPES = ['prodej', 'pronajem'] as const;

/* Default viewport span when opening Browse focused on a property: the camera
 * frames ~5 km across and the cohort is "everything in that viewport". */
export const EXPLORE_VIEWPORT_KM = 5;

export interface ExploreAreaSeed {
  lat: number;
  lng: number;
  categoryMain: string | null;
  categoryType: string | null;
  disposition: Disposition | null;
}

/* Build the modal's initial cohort filter from a listing: same category +
 * disposition, scoped to a ~5 km viewport around the property's coordinates.
 * Disposition is dropped when null (komerční / pozemek / unparsed). Category
 * falls back to the Browse default if the listing isn't one of the three the
 * UI supports (e.g. pozemek) — Browse can't render those anyway. */
export const browseFiltersForArea = (
  seed: ExploreAreaSeed,
  km: number = EXPLORE_VIEWPORT_KM,
): ListingFilters => {
  const categoryMain: ListingFilters['categoryMain'] =
    (CATEGORY_MAINS as readonly string[]).includes(seed.categoryMain ?? '')
      ? [seed.categoryMain as ListingFilters['categoryMain'][number]]
      : [...DEFAULT_FILTERS.categoryMain];
  const categoryType = (CATEGORY_TYPES as readonly string[]).includes(seed.categoryType ?? '')
    ? (seed.categoryType as ListingFilters['categoryType'])
    : DEFAULT_FILTERS.categoryType;
  return {
    ...DEFAULT_FILTERS,
    categoryMain,
    categoryType,
    dispositions: seed.disposition ? [seed.disposition] : [],
    bounds: bboxAround(seed.lat, seed.lng, km),
    locationMode: 'viewport',
  };
};

/* Serialize a full Browse view-state to a `/browse?…` URL. The inverse of the
 * URL adapter's reads, so the modal's "Go to Browse" link reproduces exactly
 * what the operator built in the modal (filters + sort + tab + overlay). */
export const browseUrlFromState = (s: {
  filters: ListingFilters;
  sort: SortSpec;
  tab: TabKey;
  overlay: MapOverlayState;
}): string => {
  const sp = toSearchParams(s.filters);
  if (sortToParam(s.sort) !== sortToParam(DEFAULT_SORT)) sp.set('sort', sortToParam(s.sort));
  if (s.tab !== 'map') sp.set('tab', s.tab);
  if (!s.overlay.showCities) sp.set('cities', '0');
  if (s.overlay.colorByIndexName) sp.set('colorby', s.overlay.colorByIndexName);
  if (s.overlay.showRentMap) sp.set('rentmap', '1');
  if (s.overlay.rentVk !== 1) sp.set('rentvk', String(s.overlay.rentVk));
  if (s.overlay.showKraje) sp.set('kraje', '1');
  const qs = sp.toString();
  return qs ? `/browse?${qs}` : '/browse';
};
