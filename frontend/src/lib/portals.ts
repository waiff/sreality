/* Source-portal display labels, sourced from the canonical filter
 * registry's `portals` enum so the Browse card badge, the filter
 * chips, and any future surface stay in lockstep with the backend
 * `listings.source` vocabulary. Falls back to the raw source code
 * (capitalised) for a portal not yet in the registry enum. */
import { filterById } from './filterRegistry.generated';

const PORTAL_LABELS: Record<string, string> = Object.fromEntries(
  (filterById('portals')?.enum_values ?? []).map((o) => [o.value, o.label_cs]),
);

export function portalLabel(source: string | null | undefined): string | null {
  if (!source) return null;
  return PORTAL_LABELS[source] ?? source.charAt(0).toUpperCase() + source.slice(1);
}

/* Short, hand-tuned portal names for compact chrome — the Health page's
 * per-run "Site" tag and the /dedup review card's portal chips. Diacritic
 * spellings (Bazoš) the registry's machine labels don't carry. Falls back to
 * the registry label, then the raw source code. */
export const PORTAL_SHORT_LABEL: Record<string, string> = {
  sreality: 'Sreality',
  bazos: 'Bazoš',
  idnes: 'iDNES',
};

export function portalShort(source: string): string {
  return PORTAL_SHORT_LABEL[source] ?? portalLabel(source) ?? source;
}

/* The best external link to a listing on its origin portal.
 *
 * Prefer the stored `source_url` (set by the bazos / bezrealitky / idnes /
 * mmreality scrapers and the on-demand URL parser). sreality's scraper never
 * stores one, but its public detail page resolves by id regardless of the slug
 * segments, so we reconstruct it from the native id — the same id-only pattern
 * ComparableModal / ListingDetail already use. Returns null when we can't build
 * an external URL (so the caller can fall back to the in-app listing view). */
export function portalListingUrl(
  source: string,
  sourceUrl: string | null | undefined,
  nativeId: string | number | null | undefined,
): string | null {
  if (sourceUrl) return sourceUrl;
  if (source === 'sreality' && nativeId != null && String(nativeId).trim()) {
    return `https://www.sreality.cz/detail/x/x/x/${nativeId}`;
  }
  return null;
}
