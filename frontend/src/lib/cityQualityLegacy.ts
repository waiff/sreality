/* Restores the PRE-W5 city-quality CODE PATH for one release — not a semantic mode.
 *
 * Two live definitions of "matches" would violate rule 16 outright, which is the rule W5
 * exists to make structural. This exists so a production regression can be bisected without
 * a revert, and W7 deletes it together with `listings_with_city_quality`.
 *
 * Evaluated ONCE at module load: the URL param arrives with a page load anyway, and this
 * keeps it out of every react-query cache key. It is NOT part of `ListingFilters` and never
 * enters preset identity or the serialized filter blob.
 *
 *   ?cityQualityLegacy=1  -> on, and remembered
 *   ?cityQualityLegacy=0  -> off, and forgotten
 */
const KEY = 'cityQualityLegacy';

const detect = (): boolean => {
  try {
    const q = new URLSearchParams(window.location.search).get(KEY);
    if (q === '1') {
      localStorage.setItem(KEY, '1');
      return true;
    }
    if (q === '0') {
      localStorage.removeItem(KEY);
      return false;
    }
    return localStorage.getItem(KEY) === '1';
  } catch {
    /* private window, or site data blocked — the accessor itself can throw */
    return false;
  }
};

export const CITY_QUALITY_LEGACY = detect();
