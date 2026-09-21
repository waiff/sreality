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
 * trade for having the links at all. Reas (sold prices, below) opens a box
 * around the point rather than the point, so it tolerates that best.
 *
 * Coordinate ORDER differs per service and a swapped pair fails silently (it
 * just lands somewhere else), so the order is pinned by tests: Mapy.cz takes
 * lon,lat — Google Maps, iKatastr and Reas take lat,lon. */

export type ExternalMapLink = {
  key: 'mapy' | 'google' | 'katastr' | 'reas' | 'cenova-mapa';
  /* Which row the chip sits in: where the place is, or what it sells for. */
  group: 'place' | 'price';
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

/* Metres per degree of latitude — close enough everywhere in Czechia. */
const M_PER_DEG_LAT = 111_320;

/* reas.cz's sold-properties search ("prodané nemovitosti": registered sale
 * prices) for a box around the point. `bounds` is swLat,swLng,neLat,neLng —
 * LAT FIRST, measured, not guessed: reas's own payload stores the longitude in
 * the field it calls `southWestLatitude`, and the lng-first order parses fine
 * but opens an empty map somewhere else (pinned by tests). Without `bounds` the
 * page is the whole country; its path form (`/<obec>-<RÚIAN obec kód>`) would
 * need codes the public view doesn't carry, and a box beats a town anyway —
 * it's the neighbourhood, whichever side of a municipal border it falls on.
 *
 * 1 km each way: a ~2 km square. Measured on production data — ±500 m around a
 * small-town listing (Králův Dvůr) showed no sales at all, ±1 km showed the
 * town; in Prague ±1 km is still the neighbourhood. */
export function reasSoldUrl(lat: number, lng: number, halfSideM = 1000): string {
  const dLat = halfSideM / M_PER_DEG_LAT;
  const dLng = halfSideM / (M_PER_DEG_LAT * Math.cos((lat * Math.PI) / 180));
  const bounds = [lat - dLat, lng - dLng, lat + dLat, lng + dLng].map(coord).join(',');
  return `https://www.reas.cz/prodane/nemovitosti?bounds=${bounds}`;
}

/* The row, in the order the operator reads it: Czech detail → street view →
 * cadastre → what nearby properties actually sold for. */
export function externalMapLinks(lat: number, lng: number): ExternalMapLink[] {
  return [
    {
      key: 'mapy',
      group: 'place',
      label: 'Mapy.cz',
      title: 'Open this point on Mapy.cz — street and aerial detail',
      url: mapyCzPointUrl(lat, lng),
    },
    {
      key: 'google',
      group: 'place',
      label: 'Google',
      title: 'Open this point in Google Maps — Street View and surroundings',
      url: googleMapsPointUrl(lat, lng),
    },
    {
      key: 'katastr',
      group: 'place',
      label: 'Katastr',
      title: 'Open this point on iKatastr.cz — cadastre parcel and building',
      url: iKatastrPointUrl(lat, lng),
    },
    {
      key: 'reas',
      group: 'price',
      label: 'Reas.cz',
      title: 'Sold properties around this point on reas.cz — a ~2 km box, actual sale prices',
      url: reasSoldUrl(lat, lng),
    },
  ];
}

/* sreality's Cenová mapa — registered sale prices of apartments, by kraj → okres
 * → obec → street. It addresses places by Seznam's own locality ids and never
 * by a coordinate, so the precise URL comes from the API
 * (`fetchSrealityPriceMap`); until it answers, or when nothing near the point
 * matched, the chip opens the national map — it always works, it is only
 * sometimes less specific. */
export const SREALITY_PRICE_MAP_URL = 'https://www.sreality.cz/cenova-mapa';

export function srealityPriceMapLink(
  place: { url: string | null; name: string | null } | undefined,
): ExternalMapLink {
  const url = place?.url ?? null;
  return {
    key: 'cenova-mapa',
    group: 'price',
    label: 'Cenová mapa',
    title: url
      ? `Sreality price map — sold apartment prices for ${place?.name ?? 'this place'}`
      : 'Sreality price map — sold apartment prices (opens the national map)',
    url: url ?? SREALITY_PRICE_MAP_URL,
  };
}
