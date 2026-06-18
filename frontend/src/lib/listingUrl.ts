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

/* Property-grain entry: `/listing?property=<id>` lands on `ListingDetail`,
 * which resolves the property's representative listing and redirects to
 * `listingPath(reprId)`. Used where only the property id is known (the dedup
 * merge feed). */
export function propertyListingPath(propertyId: number): string {
  return `/listing?property=${propertyId}`;
}
