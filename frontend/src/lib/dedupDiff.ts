/* Per-attribute comparison for the /dedup review card.
 *
 * Given a candidate pair (two property sides) plus each side's listings_public
 * detail row (for street / house-number / floor, which the candidate payload
 * doesn't carry), produce one verdict per attribute so the card can render a
 * ✓ / ✗ comparison table. Pure + unit-tested.
 *
 * Tolerances are pinned to the matcher's auto-merge constants in
 * scripts/dedup_sweep.py so the table agrees with what the sweep decided:
 *   price  ±2%   (AUTO_PRICE_DRIFT_MAX = 0.02)
 *   area   ±2 m² (AUTO_AREA_DIFF_MAX_M2 = 2.0)
 *   geo    ≤30 m tight (AUTO_RADIUS_M = 30); tier1 pairs are ≤20 m by construction.
 */
import { fmtArea, fmtCzk } from './format';
import type { DedupCandidate, DedupPropertySide } from './types';

export const PRICE_DRIFT_MAX = 0.02; // AUTO_PRICE_DRIFT_MAX
export const AREA_DIFF_MAX_M2 = 2.0; // AUTO_AREA_DIFF_MAX_M2
export const TIGHT_RADIUS_M = 30; // AUTO_RADIUS_M

/* TS port of toolkit/comparables._DISPOSITION_LOOSE — keep in lockstep with the
 * Python source. A disposition matches another when equal, or when the other is
 * in its loose-equivalence group (e.g. 2+kk ≈ 2+1). */
const DISPOSITION_LOOSE: Record<string, readonly string[]> = {
  '1+kk': ['1+kk', '1+1'], '1+1': ['1+kk', '1+1'],
  '2+kk': ['2+kk', '2+1'], '2+1': ['2+kk', '2+1'],
  '3+kk': ['3+kk', '3+1'], '3+1': ['3+kk', '3+1'],
  '4+kk': ['4+kk', '4+1'], '4+1': ['4+kk', '4+1'],
  '5+kk': ['5+kk', '5+1'], '5+1': ['5+kk', '5+1'],
};

export type DiffVerdict = 'match' | 'mismatch' | 'unknown';

export interface DiffRow {
  key: string;
  label: string;
  verdict: DiffVerdict;
  a: string;
  b: string;
  /* Relationship rows (distance) describe the PAIR, not a per-side value;
   * the table renders `a` once, spanning both value columns. */
  single?: boolean;
}

/* The listings_public fields the card fetches per side beyond the candidate
 * payload — street / house number / floor are only on the listing row. */
export interface ListingDetailLite {
  sreality_id: number;
  street: string | null;
  house_number: string | null;
  floor: number | null;
  disposition: string | null;
  district: string | null;
  price_czk: number | null;
  area_m2: number | null;
}

const EM_DASH = '—';

function normalize(s: string | null | undefined): string | null {
  if (s == null) return null;
  const t = s.normalize('NFD').replace(/[̀-ͯ]/g, '').trim().toLowerCase();
  return t || null;
}

function dispositionMatch(a: string | null, b: string | null): DiffVerdict {
  if (a == null || b == null) return 'unknown';
  if (a === b) return 'match';
  return (DISPOSITION_LOOSE[a] ?? []).includes(b) ? 'match' : 'mismatch';
}

function priceVerdict(a: number | null, b: number | null): DiffVerdict {
  if (a == null || b == null) return 'unknown';
  const drift = Math.abs(a - b) / Math.max(a, b);
  return drift <= PRICE_DRIFT_MAX ? 'match' : 'mismatch';
}

function areaVerdict(a: number | null, b: number | null): DiffVerdict {
  if (a == null || b == null) return 'unknown';
  return Math.abs(a - b) <= AREA_DIFF_MAX_M2 ? 'match' : 'mismatch';
}

function eqVerdict(a: string | null, b: string | null): DiffVerdict {
  if (a == null || b == null) return 'unknown';
  return a === b ? 'match' : 'mismatch';
}

function streetLine(d: ListingDetailLite | null | undefined): string | null {
  if (!d) return null;
  const parts = [d.street, d.house_number].filter((p): p is string => !!p && !!p.trim());
  return parts.length ? parts.join(' ') : null;
}

function floorStr(d: ListingDetailLite | null | undefined): string {
  return d && d.floor != null ? String(d.floor) : EM_DASH;
}

function distanceM(c: DedupCandidate): number | null {
  const v = c.markers_matched?.distance_m;
  return typeof v === 'number' ? v : null;
}

/* The card's comparison rows, fixed order. left/right detail are optional —
 * price/area/disposition/district come from the candidate payload (always
 * present) so the table renders before the detail fetch lands; street/floor
 * fill in once detail arrives (null → 'unknown'). */
export function diffCandidate(
  c: DedupCandidate,
  leftDetail?: ListingDetailLite | null,
  rightDetail?: ListingDetailLite | null,
): DiffRow[] {
  const L: DedupPropertySide = c.left_property;
  const R: DedupPropertySide = c.right_property;

  const leftStreet = streetLine(leftDetail);
  const rightStreet = streetLine(rightDetail);

  const dist = distanceM(c);
  const distVerdict: DiffVerdict =
    dist == null ? 'unknown' : dist <= TIGHT_RADIUS_M ? 'match' : 'mismatch';
  const distStr =
    dist != null
      ? `${dist.toFixed(0)} m apart`
      : c.tier === 'tier1'
        ? '≤ 20 m apart'
        : EM_DASH;

  return [
    {
      key: 'price', label: 'Price',
      verdict: priceVerdict(L.price_czk, R.price_czk),
      a: fmtCzk(L.price_czk), b: fmtCzk(R.price_czk),
    },
    {
      key: 'area', label: 'Area',
      verdict: areaVerdict(L.area_m2, R.area_m2),
      a: fmtArea(L.area_m2), b: fmtArea(R.area_m2),
    },
    {
      key: 'disposition', label: 'Disposition',
      verdict: dispositionMatch(L.disposition, R.disposition),
      a: L.disposition ?? EM_DASH, b: R.disposition ?? EM_DASH,
    },
    {
      key: 'street', label: 'Street + No.',
      verdict: eqVerdict(normalize(leftStreet), normalize(rightStreet)),
      a: leftStreet ?? EM_DASH, b: rightStreet ?? EM_DASH,
    },
    {
      key: 'floor', label: 'Floor',
      verdict: eqVerdict(
        leftDetail?.floor != null ? String(leftDetail.floor) : null,
        rightDetail?.floor != null ? String(rightDetail.floor) : null,
      ),
      a: floorStr(leftDetail), b: floorStr(rightDetail),
    },
    {
      key: 'district', label: 'District',
      verdict: eqVerdict(normalize(L.district), normalize(R.district)),
      a: L.district ?? EM_DASH, b: R.district ?? EM_DASH,
    },
    {
      key: 'distance', label: 'Distance',
      verdict: distVerdict, a: distStr, b: distStr, single: true,
    },
  ];
}
