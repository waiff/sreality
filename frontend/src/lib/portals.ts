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

/* Short, hand-tuned portal names for compact chrome — e.g. the Health page's
 * per-run "Site" tag. Diacritic
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

/* A listing's URL on its portal is `source_url` on the row — a fact the portal's
 * own parser stored at ingest (sreality included). Nothing here builds one:
 * sreality's sub-category slugs are its own closed vocabulary (37 "Rodinný dům"
 * is `rodinny`, not `rodinny-dum`), auction types are `drazby`/`podily`, and a
 * merged property's siblings have different triples — reconstruction was wrong
 * per code AND structurally impossible per row. Read `source_url`; when it is
 * null, link in-app. docs/design/portal-listing-url.md. */
