/* sreality.cz detail URL → numeric listing id.
 *
 * The canonical shape is
 *   https://www.sreality.cz/detail/<category>/<type>/<slug>/<id>?...
 * with `id` as the last non-query path segment. Older URLs used a
 * trailing numeric id too. We pull the last path segment that parses
 * as a positive integer.
 *
 * Returns null when the URL doesn't look like a detail page. The
 * caller renders nothing in that case — the content script's
 * matches:[] glob already narrows us to /detail/* so this is a
 * defensive guard. */
export function extractSrealityId(url: string): number | null {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.host !== 'www.sreality.cz') return null;
  const segments = parsed.pathname.split('/').filter(Boolean);
  for (let i = segments.length - 1; i >= 0; i--) {
    const n = Number(segments[i]);
    if (Number.isFinite(n) && Number.isInteger(n) && n > 0) {
      return n;
    }
  }
  return null;
}
