/* How precise a map pin is, drawn rather than described.
 *
 * `listing_location` (migration 501) carries a position AND the radius that says
 * how much to trust it — "one point, one radius: the radius is what says how
 * much to trust the point". Before W3 the Browse map drew every pin identically,
 * so a listing resolved to the middle of a village looked exactly like one
 * resolved to its front door. The rule below is the whole difference: a pin
 * resolved BELOW building level is drawn inside a translucent circle of its own
 * uncertainty radius; at building level or better it is a bare pin, because the
 * pin IS the building.
 *
 * Granularity is compared by RANK and never by enum text or enum ordinality
 * (migration 380's `location_granularity_rank` is the only legal comparator);
 * the projection publishes the rank as an int precisely so this file can hold a
 * number. Clusters and server-side grid cells carry no per-pin identity or
 * radius, so the circle exists only in point mode — above the point budget the
 * question "how precise is this pin" has no per-pin answer to give. */

/* location_granularity_rank: building = 90, address_point = 100. Everything
 * below (street_segment 70, street 60, cast_obce 50, obec 40, okres 30 …) is a
 * position the resolver would not put a door on. */
export const BUILDING_GRANULARITY_RANK = 90;

export interface PinPrecision {
  granularity_rank: number | null;
  uncertainty_radius_m: number | null;
}

/* The rule. Returns the radius to draw, or null for no circle:
 *   - at or above building level -> null (the pin is the answer)
 *   - unresolved (no rank, or no radius) -> null (drawing a circle of unknown
 *     size would be an invented claim, and the pin already stands alone)
 *   - a non-positive radius -> null (nothing to draw) */
export const uncertaintyCircleRadiusM = (p: PinPrecision): number | null => {
  const rank = p.granularity_rank;
  const radius = p.uncertainty_radius_m;
  if (rank == null || radius == null) return null;
  if (rank >= BUILDING_GRANULARITY_RANK) return null;
  const m = Number(radius);
  return Number.isFinite(m) && m > 0 ? m : null;
};

/* The widest circle the map will DRAW, in metres. The data keeps the true
 * radius — `uncertainty_radius_m` rides on every feature and
 * uncertaintyCircleRadiusM() returns it unclamped — but drawing it is a
 * different question from knowing it: the coarse rungs carry radii of a
 * different order (okres ~25 km, kraj ~60 km, unknown ~250 km), and a screen
 * full of 25 km discs is a wash of colour with no pin readable under it, plus a
 * circle wider than the viewport clips into a moving arc on pan. 2 km is about
 * the widest circle that still reads AS a circle at the zoom where a town's
 * pins separate, so past it the mark says "at least this vague" rather than
 * claiming a precise vagueness nobody can see. The pin is still drawn, and the
 * popup still names the place.
 *
 * This is a DISPLAY cap, never a data one: nothing downstream reads it, and the
 * true radius is what any measurement or export should use. */
export const MAX_DRAWN_CIRCLE_RADIUS_M = 2_000;

/* What the map actually draws: the rule above, clamped. */
export const drawnUncertaintyRadiusM = (p: PinPrecision): number | null => {
  const m = uncertaintyCircleRadiusM(p);
  return m == null ? null : Math.min(m, MAX_DRAWN_CIRCLE_RADIUS_M);
};

/* Web-Mercator metres per pixel at zoom 0, at the equator. Halves with every
 * zoom level, which is why the layer can interpolate the radius with an
 * exponential base of exactly 2 and be EXACT at every zoom rather than
 * approximately right between two stops. */
const M_PER_PX_Z0_EQUATOR = 156_543.033_928;

/* The circle is a TRUE metre radius, so its pixel size depends on latitude as
 * well as zoom. Precomputing the zoom-0 pixel radius per feature keeps the
 * trigonometry here — where it is testable — instead of inside a style
 * expression, and keeps the map's geometry at one point per pin: 2,000 pins is
 * 2,000 points, not 2,000 ninety-six-vertex rings. */
export const uncertaintyPixelsAtZoom0 = (radiusM: number, lat: number): number =>
  radiusM / (M_PER_PX_Z0_EQUATOR * Math.cos((lat * Math.PI) / 180));
