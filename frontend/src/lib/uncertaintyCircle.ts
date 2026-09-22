/* How precise a map pin is — carried by the pin itself, drawn on request.
 *
 * `listing_location` (migration 501) carries a position AND the radius that says
 * how much to trust it — "one point, one radius: the radius is what says how
 * much to trust the point". The map answers that in two steps:
 *
 *   - THE PIN SAYS WHICH KIND IT IS. A pin resolved at building level or better
 *     is a solid dot, because the pin IS the building; anything coarser — or not
 *     resolved at all — is an open ring: somewhere around here.
 *   - THE CIRCLE SAYS HOW FAR, FOR THE PIN YOU OPENED. Clicking a pin draws its
 *     true-metre uncertainty circle for as long as its popup is open, and the
 *     popup names the rung and the radius in words.
 *
 * W3-3 drew that circle under EVERY such pin, all the time. ~87 % of active pins
 * sit below building level (street 300 m, část obce 750 m, obec 1 km), so at the
 * zooms where pins separate, the map became stacked discs with the pins and
 * their prices buried underneath — "totally unusable" (operator, 2026-09-22).
 * One circle on request answers the same question without burying the rest.
 *
 * Granularity is compared by RANK and never by enum text or enum ordinality
 * (migration 380's `location_granularity_rank` is the only legal comparator);
 * the projection publishes the rank as an int precisely so this file can hold a
 * number. Clusters and server-side grid cells carry no per-pin identity or
 * radius, so none of this applies above the point budget. */

/* location_granularity_rank: building = 90, address_point = 100. Everything
 * below (street_segment 70, street 60, cast_obce 50, obec 40, okres 30 …) is a
 * position the resolver would not put a door on. */
export const BUILDING_GRANULARITY_RANK = 90;

export interface PinPrecision {
  granularity_rank: number | null;
  uncertainty_radius_m: number | null;
}

/* Whether the pin is drawn SOLID — the claim "exactly here". Only the building
 * rungs make it. An unresolved pin is deliberately NOT solid: a solid dot with no
 * verdict behind it would be the one claim nobody made. */
export const isPinExact = (p: PinPrecision): boolean =>
  p.granularity_rank != null && p.granularity_rank >= BUILDING_GRANULARITY_RANK;

/* The circle rule. Returns the radius to draw, or null for no circle:
 *   - at or above building level -> null (the pin is the answer)
 *   - unresolved (no rank, or no radius) -> null (drawing a circle of unknown
 *     size would be an invented claim)
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
 * different order (okres ~25 km, kraj ~60 km, unknown ~250 km), and a 25 km disc
 * is a wash of colour over the whole viewport that clips into a moving arc on
 * pan. 2 km is about the widest circle that still reads AS a circle at the zoom
 * where a town's pins separate, so past it the mark says "at least this vague";
 * the popup beside it prints the true radius.
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
 * well as zoom. Precomputing the zoom-0 pixel radius keeps the trigonometry
 * here — where it is testable — instead of inside a style expression. */
export const uncertaintyPixelsAtZoom0 = (radiusM: number, lat: number): number =>
  radiusM / (M_PER_PX_Z0_EQUATOR * Math.cos((lat * Math.PI) / 180));

/* The rung names the popup prints, at migration 380's seed ranks. Looked up as
 * the highest rung AT OR BELOW the pin's rank, so a rung inserted into the table
 * later reads as its nearest coarser neighbour rather than as nothing. */
const RUNG_NAMES: ReadonlyArray<readonly [number, string]> = [
  [100, 'adresní bod'],
  [90, 'budova'],
  [80, 'parcela'],
  [70, 'úsek ulice'],
  [60, 'ulice'],
  [50, 'část obce'],
  [40, 'obec'],
  [30, 'okres'],
  [20, 'kraj'],
  [10, 'stát'],
  [0, 'bez upřesnění'],
];

const rungName = (rank: number): string =>
  RUNG_NAMES.find(([r]) => rank >= r)?.[1] ?? 'bez upřesnění';

const czDecimal = new Intl.NumberFormat('cs-CZ', { maximumFractionDigits: 1 });

/* Metres to the popup's figure: tens of metres below a kilometre (the radius is
 * an estimate — "±611,55 m" would claim otherwise), kilometres from there up. */
export const formatUncertaintyRadius = (m: number): string => {
  const tens = Math.max(10, Math.round(m / 10) * 10);
  return tens < 1_000 ? `±${tens} m` : `±${czDecimal.format(m / 1_000)} km`;
};

/* The popup's precision line: the words for what the pin's shape and the circle
 * show. The radius it prints is the TRUE one, never the drawn cap. */
export const pinPrecisionLabel = (p: PinPrecision): string => {
  const rank = p.granularity_rank;
  if (rank == null) return 'Přesnost polohy neznámá';
  if (isPinExact(p)) return `Přesná poloha (${rungName(rank)})`;
  const m = p.uncertainty_radius_m == null ? NaN : Number(p.uncertainty_radius_m);
  const figure = Number.isFinite(m) && m > 0 ? `, ${formatUncertaintyRadius(m)}` : '';
  return `Přibližná poloha: ${rungName(rank)}${figure}`;
};
