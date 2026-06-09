/* The human-readable "place" line for a listing / property.
 *
 * Most sources carry a rich free-text `locality` (sreality's street +
 * city-quarter, e.g. "Edvarda Beneše, Plzeň") that is the best label and is
 * kept verbatim. But Bazoš sellers type the okres / post-town seat into the
 * location field, so a flat that is really in Telč gets locality = "Jihlava" —
 * exactly the geo-derived `okres`. When the free-text locality is merely the
 * okres name we fall back to the geo-derived municipality `obec` ("Telč"),
 * which is accurate to the coordinate, prefixing the parsed `street` when we
 * have one.
 *
 * Source-agnostic and self-correcting: the moment a parser writes a locality
 * richer than the bare okres, that locality wins again — so once the Bazoš
 * parser learns to read the town + street off the listing title, this helper
 * needs no change. */

export interface PlaceFields {
  locality?: string | null;
  district?: string | null;
  obec?: string | null;
  okres?: string | null;
  street?: string | null;
}

const clean = (s: string | null | undefined): string | null => {
  const t = s?.trim();
  return t ? t : null;
};

/** The single most precise reliable place string, or null. */
export function placePrimary(row: PlaceFields): string | null {
  const locality = clean(row.locality);
  const okres = clean(row.okres);
  // A free-text locality that is just the okres name is useless as a place
  // (the Bazoš "Jihlava"-for-Telč case) — defer to the geo-derived town.
  if (locality && locality !== okres) return locality;
  const town = clean(row.obec);
  const street = clean(row.street);
  if (street && town) return `${street}, ${town}`;
  return town ?? clean(row.district);
}
