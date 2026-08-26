/* Restores the PRE-W6b map READ PATH for one release — not a display mode.
 *
 * W6b replaces the map's unordered `.limit(50000)` (which hid 52% of the default
 * cohort, everything north of ~lat 50.025) with the `browse_map_cells` RPC. This
 * exists so a production regression can be bisected without a revert.
 *
 * Evaluated ONCE at module load, and that is load-bearing rather than tidy: the
 * map query is keyed `['map', filters]`, so a runtime-toggleable flag would let
 * a cluster payload be served out of a cache entry warmed by a points payload —
 * ListingMap would be handed rows for cells, or the reverse, with no refetch. It
 * is NOT part of `ListingFilters` and never enters preset identity, a saved
 * preset, or the serialized filter blob.
 *
 *   ?map=legacy  -> on, and remembered
 *   ?map=cells   -> off, and forgotten
 *
 * Copied from cityQualityLegacy.ts, including the try/catch: in a private window
 * (or with site data blocked) the localStorage accessor itself throws.
 */
const KEY = 'mapLegacy';

const detect = (): boolean => {
  try {
    const q = new URLSearchParams(window.location.search).get('map');
    if (q === 'legacy') {
      localStorage.setItem(KEY, '1');
      return true;
    }
    if (q === 'cells') {
      localStorage.removeItem(KEY);
      return false;
    }
    return localStorage.getItem(KEY) === '1';
  } catch {
    /* private window, or site data blocked — the accessor itself can throw */
    return false;
  }
};

export const MAP_LEGACY = detect();
