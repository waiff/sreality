/* The ONE place that builds an internal listing-detail URL.
 *
 * The route is `/listing/:sreality_id` (see `routes.tsx`); `sreality_id` is the
 * app-wide listing identity — negative synthetic for non-sreality portals
 * (migration 097), which the route accepts. Every surface that links to a
 * listing (Browse cards/table/map, estimations, watchdog, dedup, broker,
 * collections, Health, the Chrome extension's mirror) routes through here so a
 * route change is a single edit. Pairs with `runLinks.runSurfaceUrl`, which
 * builds the run-on-listing variant on top of `listingPath`. */
export function listingPath(srealityId: number): string {
  return `/listing/${srealityId}`;
}

/* Canonical, self-describing listing URL: `/listing/{source}/{native_id}`.
 *
 * The negative synthetic `sreality_id` (migration 097) is an internal artifact
 * that should never appear in a URL. This natural-key form (migration 091's
 * `(source, source_id_native)`) is what external/frozen surfaces emit — the
 * notification-email deep link and the Chrome extension's "Otevřít v aplikaci"
 * link — and what `ListingDetail` redirects the legacy `/listing/{id}` route to
 * on land, so the id-bar shows the clean form regardless of entry point. The
 * legacy numeric route stays forever as a resolver (`listingPath` above):
 * positive → sreality's real id, negative → frozen pre-cutover alias. */
export function listingCanonicalPath(source: string, sourceIdNative: string): string {
  return `/listing/${encodeURIComponent(source)}/${encodeURIComponent(sourceIdNative)}`;
}

/* Property-grain entry: `/listing?property=<id>` lands on `ListingDetail`,
 * which resolves the property's representative listing and redirects to its
 * canonical detail URL. Used where only the property id is known (the dedup
 * merge feed). */
export function propertyListingPath(propertyId: number): string {
  return `/listing?property=${propertyId}`;
}

/* The detail link for a PROPERTY-GRAIN Browse row (Map / Table / Cards). The
 * legacy `/listing/{sreality_id}` route is one round trip, so it stays the fast
 * path whenever the representative child HAS a sreality_id. Post-Gate-2 a new
 * non-sreality listing inserts `sreality_id = NULL`, and `listingPath(null)`
 * would build `/listing/null` (the id-spaces overlap, so we must never route the
 * surrogate through the legacy sreality route either); those rows fall back to
 * the property route, which ListingDetail resolves to the canonical natural-key
 * URL. `property_id` is never null on the property grain, so this always yields
 * a working link. */
export function listingRowPath(row: {
  sreality_id: number | null;
  property_id: number;
}): string {
  return row.sreality_id != null
    ? listingPath(row.sreality_id)
    : propertyListingPath(row.property_id);
}
