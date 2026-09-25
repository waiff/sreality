import { ROUTES, withQuery, type RoutePath } from './routes';

/* The ONE place that builds an internal detail URL. There is one detail page,
 * the PROPERTY page (decision 11); every property-grain surface (Browse cards,
 * table and map, the pipeline board, collections) links to it by property id,
 * so the address never moves when the property's canonical advert changes. */
export function propertyPath(propertyId: number, advertId?: number | null): RoutePath {
  // `advert` opens that advert's row in the merged-adverts section.
  return withQuery(ROUTES.property.build({ propertyId }), { advert: advertId });
}

/* An ADVERT's own address, for an advert-grain surface that does not know the
 * advert's property (estimation runs, comparables, watchdog dispatches). It is an
 * alias kept forever: the page resolves the advert's property on land and opens
 * the property page with that advert's row expanded. `sreality_id` is negative
 * for non-sreality portals (migration 097), which the route accepts. */
export function listingPath(srealityId: number): RoutePath {
  return ROUTES.listingLegacy.build({ sreality_id: srealityId });
}

/* The same alias from whatever identity the row carries: the self-describing
 * natural key `/listing/{source}/{native}` first, else the legacy numeric id,
 * else null — no destination, so the caller renders inert text rather than a
 * link that 404s. */
export function advertPath(row: {
  source?: string | null;
  source_id_native?: string | null;
  sreality_id?: number | null;
}): RoutePath | null {
  if (row.source && row.source_id_native) {
    return ROUTES.listingCanonical.build({ source: row.source, nativeId: row.source_id_native });
  }
  return row.sreality_id != null ? listingPath(row.sreality_id) : null;
}
