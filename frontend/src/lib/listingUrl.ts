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
 * which resolves the property's representative listing and redirects to
 * `listingPath(reprId)`. Used where only the property id is known (the dedup
 * merge feed). */
export function propertyListingPath(propertyId: number): string {
  return `/listing?property=${propertyId}`;
}
