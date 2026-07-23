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

/* The detail link for a PROPERTY-GRAIN row (Browse Map / Table / Cards, the
 * pipeline board, …). Precedence is CANONICAL → legacy → property:
 *
 *   1. `source` + `source_id_native` present → the self-describing
 *      `/listing/{source}/{native}` URL. This is the preferred form for every
 *      row that carries the natural key: the URL bar is clean from the first
 *      paint, with no post-load legacy→canonical redirect flashing the negative
 *      synthetic id (migration 097). ListingDetail resolves the natural key to
 *      the repr child; in-SPA navs also seed `listing_id` via Link `state` to
 *      skip that resolver round trip entirely.
 *   2. no natural key but a `sreality_id` → the legacy `/listing/{id}` route
 *      (pre-Gate-2 rows whose row payload doesn't carry the natural key, or
 *      callers that only know the id). ListingDetail canonicalizes on land.
 *   3. neither → the property route `/listing?property=<id>`, which ListingDetail
 *      resolves to the representative's canonical URL. Post-Gate-2 a new
 *      non-sreality listing inserts `sreality_id = NULL`, so a row without the
 *      natural key still links here; `property_id` is never null on the property
 *      grain, so this always yields a working link. Never route the surrogate
 *      through the legacy sreality route — the id-spaces overlap.
 *
 * `source`/`source_id_native` are optional so pre-existing callers that pass only
 * `{ sreality_id, property_id }` keep the legacy→property behavior unchanged. */
export function listingRowPath(row: {
  source?: string | null;
  source_id_native?: string | null;
  sreality_id: number | null;
  property_id: number;
}): string {
  if (row.source && row.source_id_native) {
    return listingCanonicalPath(row.source, row.source_id_native);
  }
  return row.sreality_id != null
    ? listingPath(row.sreality_id)
    : propertyListingPath(row.property_id);
}
