import { useEffect, useRef } from 'react';
import type maplibregl from 'maplibre-gl';

/* Project a set of hovered feature ids onto a MapLibre source's feature-state so
 * `['feature-state','hovered']` paint expressions light up. Diffs against the
 * previously-styled set so only changed features are touched — no full-source scan.
 * `revision` (pass the source's data array) re-runs the projection after a setData
 * call, which drops feature-state, so a still-hovered id re-applies cleanly.
 *
 * Shared by ListingMap (Browse) and ComparablesMap (estimation) — one hover
 * mechanism, not a copy per map. */
export function useMapFeatureHover(
  map: maplibregl.Map | null,
  ready: boolean,
  source: string,
  hoveredIds: ReadonlySet<number>,
  revision?: unknown,
): void {
  const styledIdsRef = useRef<Set<number>>(new Set());

  useEffect(() => {
    if (!ready || !map) return;
    const prev = styledIdsRef.current;
    for (const id of prev) {
      if (!hoveredIds.has(id)) {
        map.setFeatureState({ source, id }, { hovered: false });
      }
    }
    for (const id of hoveredIds) {
      if (!prev.has(id)) {
        map.setFeatureState({ source, id }, { hovered: true });
      }
    }
    styledIdsRef.current = new Set(hoveredIds);
  }, [map, ready, source, hoveredIds, revision]);
}
