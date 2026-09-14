/* External map deep links for one listing's resolved point.
 *
 * The operator cross-checks a location in three places that our own map can't
 * be: Mapy.cz (the best Czech street/aerial detail), Google Maps (Street View,
 * the neighbourhood as photographed), and iKatastr (the cadastre — which parcel
 * and building the point falls on, i.e. the ownership check). Each URL below is
 * that service's own documented "show a point" form, so none needs an API key
 * and none can be broken by a key rotation.
 *
 * Precision is inherited, never implied: the coordinate is whatever the location
 * resolver landed on for this listing — an address point for a minority of rows,
 * otherwise a street or municipality centroid. A street-level pin will open the
 * WRONG building on all three services, by construction; that is the accepted
 * trade for having the links at all.
 *
 * Coordinate ORDER differs per service and a swapped pair fails silently (it
 * just lands somewhere else), so the order is pinned by tests: Mapy.cz takes
 * lon,lat — Google Maps and iKatastr take lat,lon. */

export type ExternalMapLink = {
  key: 'mapy' | 'google' | 'katastr';
  /* Chip text — short, because the footer shares a ~300px map column. */
  label: string;
  /* Hover text: the full service name + what it is good for here. */
  title: string;
  url: string;
};

/* Six decimals ≈ 11 cm. Anything beyond that is geocoder noise, not precision,
 * and only makes the URL unreadable when the operator pastes it somewhere. */
function coord(v: number): string {
  return String(Math.round(v * 1e6) / 1e6);
}

/* developer.mapy.com's documented, key-free showmap URL. The host is mapy.com
 * (the .com rebrand) — mapy.cz is the same product and the name the operator
 * uses, so the chip still says Mapy.cz. `center` is lon,lat. */
export function mapyCzPointUrl(lat: number, lng: number, zoom = 17): string {
  return `https://mapy.com/fnc/v1/showmap?center=${coord(lng)},${coord(
    lat,
  )}&zoom=${zoom}&marker=true`;
}

/* Google's Maps URLs API: the `search` action with a lat,lng query drops a pin
 * and opens the coordinate card (Street View one tap away). No zoom parameter —
 * the search form doesn't take one. */
export function googleMapsPointUrl(lat: number, lng: number): string {
  return `https://www.google.com/maps/search/?api=1&query=${coord(lat)},${coord(lng)}`;
}

/* iKatastr keeps its whole view in the hash: `kde` is lat,lon,zoom (the map
 * view), `vrstvy=parcelybudovy` turns on the parcel + building layers, and
 * `info` at the same point opens the cadastre info panel for it — which is the
 * reason to go there at all. Zoom 18: parcel boundaries are the subject. */
export function iKatastrPointUrl(lat: number, lng: number, zoom = 18): string {
  const point = `${coord(lat)},${coord(lng)}`;
  return `https://ikatastr.cz/#kde=${point},${zoom}&mapa=zakladni&vrstvy=parcelybudovy&info=${point}`;
}

/* The row, in the order the operator reads it: Czech detail → street view →
 * cadastre. */
export function externalMapLinks(lat: number, lng: number): ExternalMapLink[] {
  return [
    {
      key: 'mapy',
      label: 'Mapy.cz',
      title: 'Open this point on Mapy.cz — street and aerial detail',
      url: mapyCzPointUrl(lat, lng),
    },
    {
      key: 'google',
      label: 'Google',
      title: 'Open this point in Google Maps — Street View and surroundings',
      url: googleMapsPointUrl(lat, lng),
    },
    {
      key: 'katastr',
      label: 'Katastr',
      title: 'Open this point on iKatastr.cz — cadastre parcel and building',
      url: iKatastrPointUrl(lat, lng),
    },
  ];
}
