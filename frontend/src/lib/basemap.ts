/* Single source of truth for map construction across the whole app. Every
 * surface (Browse, listing detail, comparables, datasets choropleth, the
 * location-filter widget) builds its MapLibre map through `createMap` so the
 * basemap style, the standard controls, and the interaction posture are
 * defined once here — change them in one place and all surfaces follow.
 *
 * Swap basemap styles via TILE_STYLE (positron | bright | liberty | dark |
 * fiord). */

import maplibregl, { type MapOptions } from 'maplibre-gl';

export const TILE_STYLE = 'https://tiles.openfreemap.org/styles/liberty';

type CreateMapOptions = Omit<MapOptions, 'container'> & {
  /* Add the metric ScaleControl (bottom-left). On for every map except the
   * Datasets choropleth, which omits it. */
  scaleControl?: boolean;
};

/* Build an app-standard MapLibre map: the shared basemap style, compact
 * attribution, a zoom-only NavigationControl, and (by default) a metric scale.
 *
 * Rotation and tilt are disabled on EVERY surface — the product is a north-up,
 * flat 2D map. Hiding the compass button alone (showCompass:false) only removes
 * the UI affordance; the rotate/tilt GESTURES (right-drag, two-finger twist,
 * multi-finger pitch, Shift+arrows) stay live unless the handlers are switched
 * off. We do that here, once, so no surface can drift. The disable is applied
 * AFTER the caller's overrides so it can never be re-enabled by accident.
 *
 * NOTE: `touchZoomRotate` is left enabled — it also drives pinch-to-ZOOM, which
 * we keep. Only its rotation half is turned off via `disableRotation()`. */
export function createMap(
  container: HTMLElement,
  { scaleControl = true, ...overrides }: CreateMapOptions = {},
): maplibregl.Map {
  const map = new maplibregl.Map({
    container,
    style: TILE_STYLE,
    attributionControl: { compact: true },
    ...overrides,
    dragRotate: false,
    pitchWithRotate: false,
    touchPitch: false,
  });
  map.touchZoomRotate.disableRotation();
  map.keyboard.disableRotation();

  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
  if (scaleControl) {
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: 'metric' }), 'bottom-left');
  }
  return map;
}
