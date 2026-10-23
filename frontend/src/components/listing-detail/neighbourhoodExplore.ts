/* The property page's neighbourhood scope and its ONE way into the explore window.
 *
 * Two buttons open the explore window over this property's neighbourhood: the tile's
 * "Prozkoumat okolí" and "Explore area" under the header map (operator 2026-10-23: the
 * second is the first's twin). Both go through `useExploreNeighbourhood`, over the same
 * scope, so they can never open different samples.
 *
 * The scope is a module store, not component state, because the two buttons sit in
 * different subtrees and the tile is not always mounted. The radius and the lens are this
 * browser's habit (localStorage, read on every snapshot so the stored value is the truth);
 * the building material and the condition belong to the property you are looking at, held
 * in memory per property for as long as the page lives.
 *
 * Lazy-only: lib/stats is the Stats chunk, and the property page is in the first-visit
 * bundle, so only lazily loaded modules (the tile, the header button) import this one. */
import { useCallback, useSyncExternalStore } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useExploreAreaModal } from '@/components/ExploreAreaModal';
import { isApiConfigured } from '@/lib/api';
import {
  DEFAULT_SCOPE,
  RADII_M,
  isMarketTemplate,
  neighbourhoodFilters,
  type NeighbourhoodScope,
  type RadiusM,
} from '@/lib/neighbourhood';
import type { PropertyPublic } from '@/lib/queries';
import { listStatsDashboards, statsDashboardKeys } from '@/lib/stats';
import type { ImagePublic } from '@/lib/types';

type Prefs = Pick<NeighbourhoodScope, 'radiusM' | 'market'>;
type Picks = Pick<NeighbourhoodScope, 'buildingMaterial' | 'conditionMatch'>;

const PREF_KEY = 'sreality.marketTile';
/* What was last written, for when localStorage refuses both ways (private mode). */
let memoryRaw: string | null = null;

const readRaw = (): string | null => {
  try {
    return localStorage.getItem(PREF_KEY);
  } catch {
    return memoryRaw;
  }
};

const parsePrefs = (raw: string | null): Prefs => {
  try {
    const p = JSON.parse(raw ?? 'null') as Partial<Prefs> | null;
    return {
      radiusM: RADII_M.includes(p?.radiusM as RadiusM) ? (p!.radiusM as RadiusM) : DEFAULT_SCOPE.radiusM,
      market: p?.market === 'all' ? 'all' : DEFAULT_SCOPE.market,
    };
  } catch {
    return { radiusM: DEFAULT_SCOPE.radiusM, market: DEFAULT_SCOPE.market };
  }
};

const picks = new Map<number, Picks>();
const snapshots = new Map<number, { raw: string | null; own: Picks | undefined; scope: NeighbourhoodScope }>();
const listeners = new Set<() => void>();

/* The same object until something it is built from changes (useSyncExternalStore). */
function scopeOf(propertyId: number): NeighbourhoodScope {
  const raw = readRaw();
  const own = picks.get(propertyId);
  const last = snapshots.get(propertyId);
  if (last && last.raw === raw && last.own === own) return last.scope;
  const scope = { ...DEFAULT_SCOPE, ...parsePrefs(raw), ...own };
  snapshots.set(propertyId, { raw, own, scope });
  return scope;
}

function setScope(propertyId: number, patch: Partial<NeighbourhoodScope>): void {
  const next = { ...scopeOf(propertyId), ...patch };
  memoryRaw = JSON.stringify({ radiusM: next.radiusM, market: next.market });
  try {
    localStorage.setItem(PREF_KEY, memoryRaw);
  } catch {
    /* memory only */
  }
  picks.set(propertyId, { buildingMaterial: next.buildingMaterial, conditionMatch: next.conditionMatch });
  listeners.forEach((listener) => listener());
}

const subscribe = (listener: () => void): (() => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};

export function useNeighbourhoodScope(
  propertyId: number,
): [NeighbourhoodScope, (patch: Partial<NeighbourhoodScope>) => void] {
  const read = useCallback(() => scopeOf(propertyId), [propertyId]);
  const scope = useSyncExternalStore(subscribe, read, read);
  const set = useCallback((patch: Partial<NeighbourhoodScope>) => setScope(propertyId, patch), [propertyId]);
  return [scope, set];
}

/* Opens the explore window over this property's neighbourhood under the current scope:
 * its lens, the account's neighbourhood dashboard, the Stats tab. Null when the property
 * has no point. The dashboard list is the Stats bar's own read (same key), fetched on the
 * click when no one has read it yet; unreadable, the window opens without a dashboard. */
export function useExploreNeighbourhood(property: PropertyPublic, images: ImagePublic[]): (() => void) | null {
  const [scope] = useNeighbourhoodScope(property.property_id);
  const { open } = useExploreAreaModal();
  const qc = useQueryClient();
  const hood = neighbourhoodFilters(property, scope);
  if (!hood || property.lat == null || property.lng == null) return null;
  const { lat, lng } = property;
  return () => {
    const list = isApiConfigured()
      ? qc.ensureQueryData({ queryKey: statsDashboardKeys.all, queryFn: listStatsDashboards }).catch(() => null)
      : Promise.resolve(null);
    void list.then((dashboards) =>
      open({
        filters: hood.filters,
        lat,
        lng,
        tab: 'stats',
        market: scope.market,
        dashboard: dashboards?.data.find(isMarketTemplate)?.id ?? null,
        origin: { listing: property, images },
        label: [property.display_label, property.disposition].filter(Boolean).join(' · ') || undefined,
      }),
    );
  };
}
