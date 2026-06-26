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
import type {
  AuditRung,
  DecisionFeedback,
  DedupCandidate,
  DedupPropertySide,
} from './types';

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
  /* Sreality category triple — needed to build a real portal detail URL for
   * the source chip (a sreality /detail/ path 404s without the slug). NULL for
   * non-sreality sources. */
  category_type: string | null;
  category_main: string | null;
  category_sub_cb: number | null;
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

/* ---- N-way clusters ------------------------------------------------------- */
/* A pairwise candidate (A-B) plus (B-C) plus (A-C) all describe ONE real-world
 * property seen as 3 listings. Rather than show three two-up rows (the same
 * idnes listing appearing in several of them), we union the pairs into one
 * cluster and render it once with a column per member. */

export interface ClusterVisual {
  verdict: string;       // High | Medium | Low (engine's forensic read)
  rationale: string | null;
  room: string | null;
}

export interface DedupCluster {
  key: string;                  // stable key (sorted property ids)
  members: DedupPropertySide[]; // one per distinct property, oldest-id first
  candidateIds: number[];       // every candidate edge inside the cluster
  tier: string;
  /* The engine's room-aware verdict, if any edge recorded one (the new
   * street+disposition engine's queued pairs carry it; old geo pairs don't). */
  visual: ClusterVisual | null;
  /* The representative edge's full `markers_matched` factor dict (reason, stage,
   * phash_pairs, cosine, verdict, room_type, rationale) — fed to the shared
   * <DedupFactors> so a queued card shows the SAME evidence as Decision history. */
  markers: Record<string, unknown> | null;
  /* The representative edge's auditable breakdown + the operator's "wrong" flag for that
   * pair (the flag control is shown only on 2-member clusters, where the representative
   * edge IS the members[0]↔members[1] pair). */
  audit_breakdown?: AuditRung[];
  feedback?: DecisionFeedback | null;
}

/* Union-find over the candidate pairs → connected components. */
export function clusterCandidates(candidates: DedupCandidate[]): DedupCluster[] {
  const parent = new Map<number, number>();
  const find = (x: number): number => {
    let r = x;
    while (parent.get(r) !== r) r = parent.get(r) as number;
    let c = x;
    while (parent.get(c) !== r) {
      const next = parent.get(c) as number;
      parent.set(c, r);
      c = next;
    }
    return r;
  };
  const ensure = (x: number) => {
    if (!parent.has(x)) parent.set(x, x);
  };
  const union = (a: number, b: number) => {
    ensure(a); ensure(b);
    parent.set(find(a), find(b));
  };

  for (const c of candidates) union(c.left_property.property_id, c.right_property.property_id);

  const sideById = new Map<number, DedupPropertySide>();
  const edges = new Map<number, number[]>();   // root → candidate ids
  const tierByRoot = new Map<number, string>();
  const visualByRoot = new Map<number, ClusterVisual>();
  const markersByRoot = new Map<number, Record<string, unknown>>();
  const breakdownByRoot = new Map<number, AuditRung[]>();
  const feedbackByRoot = new Map<number, DecisionFeedback | null>();
  for (const c of candidates) {
    sideById.set(c.left_property.property_id, c.left_property);
    sideById.set(c.right_property.property_id, c.right_property);
    const root = find(c.left_property.property_id);
    (edges.get(root) ?? edges.set(root, []).get(root)!).push(c.id);
    if (!tierByRoot.has(root)) tierByRoot.set(root, c.tier);
    if (!breakdownByRoot.has(root) && c.audit_breakdown) {
      breakdownByRoot.set(root, c.audit_breakdown);
    }
    if (!feedbackByRoot.has(root)) feedbackByRoot.set(root, c.feedback ?? null);
    const m = c.markers_matched ?? {};
    if (!markersByRoot.has(root) && Object.keys(m).length > 0) {
      markersByRoot.set(root, m as Record<string, unknown>);
    }
    if (!visualByRoot.has(root)) {
      if (typeof m.verdict === 'string') {
        visualByRoot.set(root, {
          verdict: m.verdict,
          rationale: typeof m.rationale === 'string' ? m.rationale : null,
          room: typeof m.room_type === 'string' ? m.room_type : null,
        });
      }
    }
  }

  const membersByRoot = new Map<number, number[]>();
  for (const pid of parent.keys()) {
    const root = find(pid);
    (membersByRoot.get(root) ?? membersByRoot.set(root, []).get(root)!).push(pid);
  }

  const clusters: DedupCluster[] = [];
  for (const [root, pids] of membersByRoot) {
    const ids = [...pids].sort((a, b) => a - b);
    const members = ids
      .map((id) => sideById.get(id))
      .filter((s): s is DedupPropertySide => s != null);
    clusters.push({
      key: ids.join('-'),
      members,
      candidateIds: [...new Set(edges.get(root) ?? [])].sort((a, b) => a - b),
      tier: tierByRoot.get(root) ?? 'tier1',
      visual: visualByRoot.get(root) ?? null,
      markers: markersByRoot.get(root) ?? null,
      audit_breakdown: breakdownByRoot.get(root),
      feedback: feedbackByRoot.get(root) ?? null,
    });
  }
  // Biggest clusters first; stable thereafter.
  clusters.sort((a, b) => b.members.length - a.members.length || a.key.localeCompare(b.key));
  return clusters;
}

export interface ClusterDiffRow {
  key: string;
  label: string;
  verdict: DiffVerdict;   // do all KNOWN values agree (pairwise-compatible)?
  values: string[];       // one display string per member, in member order
}

type Compat = (a: string, b: string) => boolean;

/* A row agrees when every pair of present values is compatible; unknown if
 * fewer than two members supply the value. */
function clusterVerdict<T>(
  raw: (T | null)[],
  compatible: (a: T, b: T) => boolean,
): DiffVerdict {
  const present = raw.filter((v): v is T => v != null);
  if (present.length < 2) return 'unknown';
  for (let i = 0; i < present.length; i++) {
    for (let j = i + 1; j < present.length; j++) {
      if (!compatible(present[i], present[j])) return 'mismatch';
    }
  }
  return 'match';
}

const priceCompat: Compat = (a, b) => {
  const na = Number(a); const nb = Number(b);
  return Math.abs(na - nb) / Math.max(na, nb) <= PRICE_DRIFT_MAX;
};

export function diffCluster(
  members: DedupPropertySide[],
  detailById: (id: number | null) => ListingDetailLite | null | undefined,
): ClusterDiffRow[] {
  const detail = members.map((m) => detailById(m.sreality_id));
  const streets = members.map((_, i) => streetLine(detail[i]));
  const floors = members.map((_, i) => (detail[i]?.floor != null ? String(detail[i]!.floor) : null));

  return [
    {
      key: 'price', label: 'Price',
      verdict: clusterVerdict(
        members.map((m) => (m.price_czk != null ? String(m.price_czk) : null)),
        priceCompat,
      ),
      values: members.map((m) => fmtCzk(m.price_czk)),
    },
    {
      key: 'area', label: 'Area',
      verdict: clusterVerdict(
        members.map((m) => m.area_m2),
        (a, b) => Math.abs(a - b) <= AREA_DIFF_MAX_M2,
      ),
      values: members.map((m) => fmtArea(m.area_m2)),
    },
    {
      key: 'disposition', label: 'Disposition',
      verdict: clusterVerdict(
        members.map((m) => m.disposition),
        (a, b) => a === b || (DISPOSITION_LOOSE[a] ?? []).includes(b),
      ),
      values: members.map((m) => m.disposition ?? EM_DASH),
    },
    {
      key: 'street', label: 'Street + No.',
      verdict: clusterVerdict(streets.map(normalize), (a, b) => a === b),
      values: streets.map((s) => s ?? EM_DASH),
    },
    {
      key: 'floor', label: 'Floor',
      verdict: clusterVerdict(floors, (a, b) => a === b),
      values: members.map((_, i) => floors[i] ?? EM_DASH),
    },
    {
      key: 'district', label: 'District',
      verdict: clusterVerdict(members.map((m) => normalize(m.district)), (a, b) => a === b),
      values: members.map((m) => m.district ?? EM_DASH),
    },
  ];
}
